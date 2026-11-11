from __future__ import annotations

from contextlib import asynccontextmanager
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest

from app.asgi import create_app
from app.market_data.environment import market_data_config_from_environment


def test_environment_configuration_is_explicit_and_fail_closed():
    config = market_data_config_from_environment({})
    assert config.spot_symbols == ()
    assert config.futures_depth_symbols == ()
    assert config.futures_mark_price_symbols == ()

    config = market_data_config_from_environment({
        "ZAK_MARKET_DATA_SPOT_SYMBOLS": "BTCUSDT, ETHUSDT",
        "ZAK_MARKET_DATA_FUTURES_MARK_PRICE_SYMBOLS": "BTCUSDT",
    })
    assert config.spot_symbols == ("BTCUSDT", "ETHUSDT")
    assert config.futures_mark_price_symbols == ("BTCUSDT",)


@pytest.mark.parametrize(
    "value",
    ["BTCUSDT,", ",BTCUSDT", "btcusdt", "BTCUSDT,BTCUSDT", "BTC-USDT"],
)
def test_environment_configuration_rejects_ambiguous_or_invalid_symbols(value):
    with pytest.raises((TypeError, ValueError)):
        market_data_config_from_environment({
            "ZAK_MARKET_DATA_SPOT_SYMBOLS": value,
        })


class FakeService:
    def __init__(self, config):
        self.config = config
        self.started = False
        self.stopped = False
        self.event = None
        self.snapshot = SimpleNamespace(
            state=SimpleNamespace(value="RUNNING"),
            generated_at_monotonic=123.0,
            pipelines=(
                SimpleNamespace(
                    group=SimpleNamespace(value="spot"),
                    state=SimpleNamespace(value="SUBSCRIBING"),
                    symbols=("BTCUSDT",),
                    active_streams=(),
                    clock_synchronized=True,
                    metadata_fresh=True,
                    book_synchronized=False,
                    last_event_type=None,
                    last_event_timestamp_ms=None,
                    last_event_received_at_monotonic=None,
                    last_freshness_state=None,
                    last_error=None,
                    updated_at_monotonic=123.0,
                ),
            ),
        )

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def health_snapshot(self):
        return self.snapshot

    def latest_event(self, group, symbol):
        if group != "spot" or symbol != "BTCUSDT":
            raise ValueError("not configured")
        return self.event


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_service_and_health_hides_monotonic_times():
    services = []

    def factory(config):
        service = FakeService(config)
        services.append(service)
        return service

    app = create_app(
        service_factory=factory,
        config_factory=lambda: market_data_config_from_environment({}),
    )
    async with client_for(app) as client:
        response = await client.get("/api/v1/market-data/health")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "RUNNING"
        assert body["ready"] is False
        assert body["pipelines"][0]["state"] == "SUBSCRIBING"
        assert "generated_at_monotonic" not in response.text
        assert "updated_at_monotonic" not in response.text

    assert services[0].started is True
    assert services[0].stopped is True


@pytest.mark.asyncio
async def test_latest_endpoint_serializes_immutable_payload_and_utc_timestamp():
    service_ref = []

    def factory(config):
        service = FakeService(config)
        service.event = SimpleNamespace(
            group="spot",
            symbol="BTCUSDT",
            event_type="trade",
            event_timestamp_ms=1_800_000_000_000,
            freshness=SimpleNamespace(state=SimpleNamespace(value="FRESH")),
            book_synchronized=False,
            payload=MappingProxyType({
                "s": "BTCUSDT",
                "p": "65000.10",
                "nested": (1, True, None),
            }),
        )
        service_ref.append(service)
        return service

    app = create_app(
        service_factory=factory,
        config_factory=lambda: market_data_config_from_environment({}),
    )
    async with client_for(app) as client:
        response = await client.get(
            "/api/v1/market-data/latest/spot/BTCUSDT"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["payload"]["nested"] == [1, True, None]
    assert body["freshness_state"] == "FRESH"
    assert body["event_timestamp_utc"].endswith("Z")


@pytest.mark.asyncio
async def test_latest_endpoint_fails_closed_for_missing_or_unconfigured_events():
    app = create_app(
        service_factory=FakeService,
        config_factory=lambda: market_data_config_from_environment({}),
    )
    async with client_for(app) as client:
        missing = await client.get(
            "/api/v1/market-data/latest/spot/BTCUSDT"
        )
        unknown_group = await client.get(
            "/api/v1/market-data/latest/unknown/BTCUSDT"
        )
    assert missing.status_code == 503
    assert unknown_group.status_code == 404


@pytest.mark.asyncio
async def test_latest_endpoint_rejects_unsynchronized_depth_book():
    def factory(config):
        service = FakeService(config)
        service.latest_event = lambda group, symbol: SimpleNamespace(
            group="futures_depth",
            symbol=symbol,
            event_type="depthUpdate",
            event_timestamp_ms=1_800_000_000_000,
            freshness=None,
            book_synchronized=False,
            payload=MappingProxyType({"e": "depthUpdate"}),
        )
        return service

    app = create_app(
        service_factory=factory,
        config_factory=lambda: market_data_config_from_environment({}),
    )
    async with client_for(app) as client:
        response = await client.get(
            "/api/v1/market-data/latest/futures_depth/BTCUSDT"
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_startup_failure_attempts_service_cleanup():
    services = []

    class FailingService(FakeService):
        async def start(self):
            raise RuntimeError("sensitive internal detail")

    def factory(config):
        service = FailingService(config)
        services.append(service)
        return service

    app = create_app(
        service_factory=factory,
        config_factory=lambda: market_data_config_from_environment({}),
    )
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass
    assert services[0].stopped is True


@pytest.mark.asyncio
async def test_latest_endpoint_rejects_event_identity_mismatch():
    def factory(config):
        service = FakeService(config)
        service.event = SimpleNamespace(
            group="spot",
            symbol="ETHUSDT",
            event_type="trade",
            event_timestamp_ms=1_800_000_000_000,
            freshness=SimpleNamespace(state=SimpleNamespace(value="FRESH")),
            book_synchronized=False,
            payload=MappingProxyType({"s": "ETHUSDT"}),
        )
        return service

    app = create_app(
        service_factory=factory,
        config_factory=lambda: market_data_config_from_environment({}),
    )
    async with client_for(app) as client:
        response = await client.get(
            "/api/v1/market-data/latest/spot/BTCUSDT"
        )
    assert response.status_code == 503
