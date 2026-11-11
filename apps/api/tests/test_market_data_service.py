import asyncio

import pytest
from types import SimpleNamespace

from app.market_data.binance_spot_rest import BinanceSpotRestClient
from app.market_data.binance_usdm_futures_rest import BinanceUsdMFuturesRestClient
from app.market_data.config import MarketDataConfig
from app.market_data.freshness import ExchangeClock, FreshnessState
from app.market_data.service import MarketDataService, BootstrapReport
from app.market_data.service_models import PipelineState


class FakeSpotClient(BinanceSpotRestClient):
    def __init__(self, calls):
        self.calls = calls

    def get_server_time(self):
        self.calls.append("spot_server_time")
        return {"serverTime": 1_800_000_000_000}

    def get_exchange_info(self, symbol=None):
        self.calls.append("spot_exchange_info")
        return {"fake": "spot"}


class FakeFuturesClient(BinanceUsdMFuturesRestClient):
    def __init__(self, calls):
        self.calls = calls

    def get_server_time(self):
        self.calls.append("futures_server_time")
        return {"serverTime": 1_800_000_000_000}

    def get_exchange_info(self):
        self.calls.append("futures_exchange_info")
        return {"fake": "futures"}


class FakeMetadata:
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def require_fresh(self, now_monotonic=None):
        return None

    def require_symbol(self, symbol, *, now_monotonic=None):
        if symbol not in self.allowed:
            raise ValueError("Symbol is not eligible.")


def make_clock():
    return ExchangeClock(
        wall_clock_ms=lambda: 1_800_000_000_000,
        monotonic=lambda: 100.0,
        sync_samples=1,
    )


@pytest.mark.asyncio
async def test_empty_config_performs_zero_client_initializations():
    calls = []

    def forbidden_factory():
        calls.append("factory_called")
        raise AssertionError("Disabled configuration must not initialize clients.")

    service = MarketDataService(
        MarketDataConfig(),
        spot_client_factory=forbidden_factory,
        futures_client_factory=forbidden_factory,
    )

    report = await service.bootstrap()

    assert dict(report.resources) == {}
    assert dict(report.errors) == {}
    assert calls == []


@pytest.mark.asyncio
async def test_spot_bootstrap_uses_clock_and_metadata(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient(calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.resources
    assert not report.errors
    assert calls == ["spot_server_time", "spot_exchange_info"]
    assert report.resources["spot"].market == "spot"


@pytest.mark.asyncio
async def test_futures_depth_and_mark_share_one_bootstrap(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.market_data.service.parse_usdm_futures_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata(
            {"BTCUSDT", "ETHUSDT"}
        ),
    )

    factory_calls = []

    def factory():
        factory_calls.append("created")
        return FakeFuturesClient(calls)

    service = MarketDataService(
        MarketDataConfig(
            futures_depth_symbols=("BTCUSDT",),
            futures_mark_price_symbols=("ETHUSDT",),
        ),
        futures_client_factory=factory,
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "usdm_futures" in report.resources
    assert not report.errors
    assert factory_calls == ["created"]
    assert calls == ["futures_server_time", "futures_exchange_info"]


@pytest.mark.asyncio
async def test_spot_failure_does_not_prevent_futures_bootstrap(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.market_data.service.parse_usdm_futures_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    def broken_spot_factory():
        raise RuntimeError("spot unavailable")

    service = MarketDataService(
        MarketDataConfig(
            spot_symbols=("ETHUSDT",),
            futures_mark_price_symbols=("BTCUSDT",),
        ),
        spot_client_factory=broken_spot_factory,
        futures_client_factory=lambda: FakeFuturesClient(calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert report.errors["spot"] == "RuntimeError"
    assert "usdm_futures" in report.resources


@pytest.mark.asyncio
async def test_bootstrap_result_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient(calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    first = await service.bootstrap()
    second = await service.bootstrap()

    assert first is second
    assert calls == ["spot_server_time", "spot_exchange_info"]


@pytest.mark.asyncio
async def test_bootstrap_rejects_unavailable_configured_symbol(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"ETHUSDT"}),
    )

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient([]),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert "spot" not in report.resources


@pytest.mark.asyncio
async def test_bootstrap_report_mappings_are_immutable():
    from app.market_data.service import BootstrapReport

    report = BootstrapReport({}, {})

    with pytest.raises(TypeError):
        report.resources["spot"] = object()

    with pytest.raises(TypeError):
        report.errors["spot"] = "error"


@pytest.mark.asyncio
async def test_bootstrap_retries_failed_market_without_rebootstrapping_successful_market(monkeypatch):
    spot_factory_calls = []
    futures_factory_calls = []
    rest_calls = []

    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )
    monkeypatch.setattr(
        "app.market_data.service.parse_usdm_futures_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    def spot_factory():
        spot_factory_calls.append("called")
        if len(spot_factory_calls) == 1:
            raise RuntimeError("temporary Spot bootstrap failure")
        return FakeSpotClient(rest_calls)

    def futures_factory():
        futures_factory_calls.append("called")
        return FakeFuturesClient(rest_calls)

    service = MarketDataService(
        MarketDataConfig(
            spot_symbols=("BTCUSDT",),
            futures_mark_price_symbols=("BTCUSDT",),
        ),
        spot_client_factory=spot_factory,
        futures_client_factory=futures_factory,
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    first_report = await service.bootstrap()

    assert "spot" in first_report.errors
    assert "usdm_futures" in first_report.resources
    futures_resources = first_report.resources["usdm_futures"]

    second_report = await service.bootstrap()

    assert "spot" in second_report.resources
    assert "spot" not in second_report.errors
    assert second_report.resources["usdm_futures"] is futures_resources

    assert len(spot_factory_calls) == 2
    assert len(futures_factory_calls) == 1
    assert rest_calls.count("futures_server_time") == 1
    assert rest_calls.count("futures_exchange_info") == 1
    assert rest_calls.count("spot_server_time") == 1
    assert rest_calls.count("spot_exchange_info") == 1

@pytest.mark.asyncio
async def test_clock_sync_failure_blocks_metadata_request(monkeypatch):
    calls = []

    def fail_sync(self, provider):
        provider()
        raise RuntimeError("clock synchronization failed")

    monkeypatch.setattr(
        "app.market_data.service.ExchangeClock.synchronize",
        fail_sync,
    )

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient(calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert report.errors["spot"] == "RuntimeError"
    assert "spot" not in report.resources
    assert calls == ["spot_server_time"]


@pytest.mark.asyncio
async def test_metadata_parser_failure_does_not_publish_resources(monkeypatch):
    calls = []

    def fail_parser(payload, *, fetched_at_monotonic):
        raise ValueError("invalid exchange metadata")

    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        fail_parser,
    )

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient(calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert report.errors["spot"] == "ValueError"
    assert "spot" not in report.resources
    assert calls == ["spot_server_time", "spot_exchange_info"]


@pytest.mark.asyncio
async def test_bootstrap_cancellation_does_not_cache_report_and_can_retry(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient(calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    original_bootstrap_spot = service._bootstrap_spot

    async def cancel_spot():
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "_bootstrap_spot", cancel_spot)

    with pytest.raises(asyncio.CancelledError):
        await service.bootstrap()

    assert service.report is None

    monkeypatch.setattr(
        service,
        "_bootstrap_spot",
        original_bootstrap_spot,
    )

    report = await service.bootstrap()

    assert "spot" in report.resources
    assert not report.errors
    assert calls == ["spot_server_time", "spot_exchange_info"]

class FakeLifecycleSupervisor:
    def __init__(self, config, group, **kwargs):
        self.config = config
        self.group = group
        self.kwargs = kwargs
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = False

    async def run(self):
        self.started.set()
        await self.release.wait()

    async def stop(self):
        self.stopped = True
        self.release.set()


@pytest.mark.asyncio
async def test_empty_config_starts_no_supervisors():
    factory_calls = []

    def forbidden_factory(*args, **kwargs):
        factory_calls.append(args)
        raise AssertionError("No supervisor should be created.")

    service = MarketDataService(
        MarketDataConfig(),
        supervisor_factory=forbidden_factory,
    )

    report = await service.start()

    assert not report.resources
    assert not report.errors
    assert service.state.value == "DISABLED"
    assert not service.supervisors
    assert factory_calls == []


@pytest.mark.asyncio
async def test_service_starts_all_enabled_supervisor_groups_independently(monkeypatch):
    rest_calls = []
    created = {}

    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )
    monkeypatch.setattr(
        "app.market_data.service.parse_usdm_futures_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata(
            {"BTCUSDT", "ETHUSDT"}
        ),
    )

    def factory(config, group, **kwargs):
        supervisor = FakeLifecycleSupervisor(config, group, **kwargs)
        created[group] = supervisor
        return supervisor

    service = MarketDataService(
        MarketDataConfig(
            spot_symbols=("BTCUSDT",),
            futures_depth_symbols=("BTCUSDT",),
            futures_mark_price_symbols=("ETHUSDT",),
        ),
        spot_client_factory=lambda: FakeSpotClient(rest_calls),
        futures_client_factory=lambda: FakeFuturesClient(rest_calls),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
        supervisor_factory=factory,
    )

    await service.start()

    clock_tasks = dict(service._clock_tasks)
    assert set(clock_tasks) == {"spot", "usdm_futures"}
    assert len(clock_tasks) == 2
    assert all(not task.done() for task in clock_tasks.values())

    await asyncio.gather(
        *(supervisor.started.wait() for supervisor in created.values())
    )

    assert set(created) == {"spot", "futures_depth", "futures_mark_price"}
    assert service.pipeline_states == {
        "spot": PipelineState.SUBSCRIBING,
        "futures_depth": PipelineState.SUBSCRIBING,
        "futures_mark_price": PipelineState.SUBSCRIBING,
    }

    assert created["spot"].kwargs["freshness_validator"] is not None
    assert created["futures_depth"].kwargs["freshness_validator"] is None
    assert created["futures_mark_price"].kwargs["freshness_validator"] is not None

    assert created["spot"].kwargs["on_event"] is not None
    assert created["futures_depth"].kwargs["on_depth_event"] is not None
    assert created["futures_mark_price"].kwargs["on_event"] is not None

    assert service.state.value == "RUNNING"

    await service.stop()

    assert all(item.stopped for item in created.values())
    assert all(
        state is PipelineState.STOPPED
        for state in service.pipeline_states.values()
    )
    assert all(task.done() for task in clock_tasks.values())
    assert service._clock_tasks == {}
    assert service.state.value == "STOPPED"


@pytest.mark.asyncio
async def test_only_fresh_trade_event_establishes_spot_readiness(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    created = {}

    def factory(config, group, **kwargs):
        created[group] = FakeLifecycleSupervisor(config, group, **kwargs)
        return created[group]

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient([]),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
        supervisor_factory=factory,
    )

    await service.start()

    callback = created["spot"].kwargs["on_event"]

    event = type("FakeEvent", (), {
        "message": type("FakeMessage", (), {
            "kind": "event",
            "market": "spot",
            "stream": "btcusdt@trade",
            "event_type": "trade",
            "symbol": "BTCUSDT",
            "payload": {"E": 1_800_000_000_000, "s": "BTCUSDT"},
        })(),
        "received_at_monotonic": 100.0,
        "freshness": type("FakeFreshness", (), {
            "state": "FRESH",
            "event_type": "trade",
            "event_timestamp_ms": 1_800_000_000_000,
        })(),
        "book_synchronized": False,
    })()

    await callback(event)

    assert service.pipeline_states["spot"] is PipelineState.READY

    await service.stop()


@pytest.mark.asyncio
async def test_depth_event_remains_unsynced_and_shutdown_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.service.parse_usdm_futures_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    created = {}

    def factory(config, group, **kwargs):
        created[group] = FakeLifecycleSupervisor(config, group, **kwargs)
        return created[group]

    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
        futures_client_factory=lambda: FakeFuturesClient([]),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
        supervisor_factory=factory,
    )

    await service.start()

    callback = created["futures_depth"].kwargs["on_depth_event"]

    event = type("FakeDepthEvent", (), {
        "message": type("FakeMessage", (), {
            "kind": "event",
            "market": "usdm_futures",
            "stream": "btcusdt@depth",
            "event_type": "depthUpdate",
            "symbol": "BTCUSDT",
            "payload": {"E": 1_800_000_000_000, "s": "BTCUSDT"},
        })(),
        "received_at_monotonic": 100.0,
        "freshness": None,
        "book_synchronized": False,
    })()

    await callback(event)

    assert service.pipeline_states["futures_depth"] is PipelineState.UNSYNCED
    assert service.state.value == "DEGRADED"

    await service.stop()
    await service.stop()

    assert created["futures_depth"].stopped
    assert service.state.value == "STOPPED"

@pytest.mark.asyncio
async def test_start_cancellation_resets_state_and_allows_retry(monkeypatch):
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    bootstrap_started = asyncio.Event()

    async def blocked_bootstrap():
        bootstrap_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "bootstrap", blocked_bootstrap)

    start_task = asyncio.create_task(service.start())
    await bootstrap_started.wait()

    start_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert service.state.value == "STOPPED"
    assert not service.supervisors
    assert not service._tasks

    async def retry_bootstrap():
        return BootstrapReport({}, {"spot": "RuntimeError"})

    monkeypatch.setattr(service, "bootstrap", retry_bootstrap)

    report = await service.start()

    assert "spot" in report.errors
    assert service.state.value == "FAILED"


@pytest.mark.asyncio
async def test_self_terminated_supervisor_is_marked_failed(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    created = {}

    class SelfTerminatingSupervisor:
        def __init__(self, config, group, **kwargs):
            self.group = group
            self.stopped = False
            self.started = asyncio.Event()

        async def run(self):
            self.started.set()
            self.stopped = True
            return

        async def stop(self):
            self.stopped = True

    def factory(config, group, **kwargs):
        item = SelfTerminatingSupervisor(config, group, **kwargs)
        created[group] = item
        return item

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient([]),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
        supervisor_factory=factory,
    )

    await service.start()

    await created["spot"].started.wait()
    await service._tasks["spot"]

    assert service.pipeline_states["spot"] is PipelineState.FAILED
    assert service.pipeline_errors["spot"] == "UnexpectedSupervisorExit"
    assert service.state.value == "FAILED"

    await service.stop()


@pytest.mark.asyncio
async def test_one_supervisor_failure_does_not_stop_other_pipeline(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )
    monkeypatch.setattr(
        "app.market_data.service.parse_usdm_futures_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"ETHUSDT"}),
    )

    created = {}

    class FailingSupervisor:
        def __init__(self, config, group, **kwargs):
            self.group = group
            self.started = asyncio.Event()
            self.stopped = False

        async def run(self):
            self.started.set()
            raise RuntimeError("simulated supervisor failure")

        async def stop(self):
            self.stopped = True

    def factory(config, group, **kwargs):
        if group == "spot":
            item = FailingSupervisor(config, group, **kwargs)
        else:
            item = FakeLifecycleSupervisor(config, group, **kwargs)

        created[group] = item
        return item

    service = MarketDataService(
        MarketDataConfig(
            spot_symbols=("BTCUSDT",),
            futures_mark_price_symbols=("ETHUSDT",),
        ),
        spot_client_factory=lambda: FakeSpotClient([]),
        futures_client_factory=lambda: FakeFuturesClient([]),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
        supervisor_factory=factory,
    )

    await service.start()

    await created["spot"].started.wait()
    await service._tasks["spot"]
    await created["futures_mark_price"].started.wait()

    assert service.pipeline_states["spot"] is PipelineState.FAILED
    assert service.pipeline_states["futures_mark_price"] is PipelineState.SUBSCRIBING

    assert not service._tasks["futures_mark_price"].done()
    assert service.state.value == "DEGRADED"

    await service.stop()

    assert service.state.value == "STOPPED"
    assert created["futures_mark_price"].stopped



def install_freshness_validator(service, market="spot", *, state="FRESH", reason="within_freshness_policy"):
    calls = []

    class FakeFreshnessValidator:
        def validate_event(self, event_type, event_timestamp_ms, *, received_at_monotonic=None):
            calls.append((event_type, event_timestamp_ms, received_at_monotonic))
            return SimpleNamespace(
                state=state,
                event_type=event_type,
                event_timestamp_ms=event_timestamp_ms,
                reason=reason,
            )

    service._resources[market] = SimpleNamespace(
        freshness_validator=FakeFreshnessValidator(),
    )
    return calls


def make_cache_event(
    group,
    symbol,
    timestamp,
    received_at,
    *,
    freshness_state="FRESH",
    payload_extra=None,
):
    event_types = {
        "spot": ("spot", "trade", f"{symbol.lower()}@trade"),
        "futures_depth": ("usdm_futures", "depthUpdate", f"{symbol.lower()}@depth"),
        "futures_mark_price": (
            "usdm_futures",
            "markPriceUpdate",
            f"{symbol.lower()}@markPrice",
        ),
    }
    market, event_type, stream = event_types[group]

    payload = {
        "E": timestamp,
        "s": symbol,
        "nested": {"levels": [1, 2, 3]},
    }
    if payload_extra:
        payload.update(payload_extra)

    freshness = None
    if group != "futures_depth":
        freshness = SimpleNamespace(
            state=freshness_state,
            event_type=event_type,
            event_timestamp_ms=timestamp,
        )

    return SimpleNamespace(
        message=SimpleNamespace(
            kind="event",
            market=market,
            stream=stream,
            event_type=event_type,
            symbol=symbol,
            payload=payload,
        ),
        received_at_monotonic=received_at,
        freshness=freshness,
        book_synchronized=False,
    ), payload


@pytest.mark.asyncio
async def test_event_cache_deep_freezes_payload_and_detaches_source():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    install_freshness_validator(service)
    event, source_payload = make_cache_event(
        "spot", "BTCUSDT", 1000, 10.0
    )

    accepted = await service._record_supervised_event("spot", event)

    assert accepted is True
    cached = service.latest_event("spot", "BTCUSDT")

    assert cached is not None
    assert cached.event_timestamp_ms == 1000
    assert cached.book_synchronized is False
    assert cached.payload["nested"]["levels"] == (1, 2, 3)

    source_payload["nested"]["levels"].append(4)

    assert cached.payload["nested"]["levels"] == (1, 2, 3)

    with pytest.raises(TypeError):
        cached.payload["new"] = "mutation"

    with pytest.raises(TypeError):
        cached.payload["nested"]["new"] = "mutation"


@pytest.mark.asyncio
async def test_older_event_cannot_overwrite_newer_cached_event():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    install_freshness_validator(service)
    newer, _ = make_cache_event("spot", "BTCUSDT", 2000, 20.0)
    older, _ = make_cache_event("spot", "BTCUSDT", 1999, 21.0)

    assert await service._record_supervised_event("spot", newer) is True
    assert await service._record_supervised_event("spot", older) is False

    cached = service.latest_event("spot", "BTCUSDT")
    assert cached.event_timestamp_ms == 2000
    assert service.event_cache_size == 1


@pytest.mark.asyncio
async def test_depth_cache_is_always_explicitly_unsynchronized():
    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
    )

    service._resources["usdm_futures"] = SimpleNamespace(
        clock=FakeHealthClock(synchronized=True),
    )

    event, _ = make_cache_event(
        "futures_depth", "BTCUSDT", 3000, 30.0
    )

    assert await service._record_supervised_event(
        "futures_depth", event
    ) is True

    cached = service.latest_event("futures_depth", "BTCUSDT")

    assert cached is not None
    assert cached.freshness is None
    assert cached.book_synchronized is False
    assert service.pipeline_states["futures_depth"] is PipelineState.UNSYNCED


@pytest.mark.asyncio
async def test_wrong_symbol_stream_and_market_are_rejected():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    event, _ = make_cache_event("spot", "BTCUSDT", 4000, 40.0)

    wrong_stream = SimpleNamespace(
        message=SimpleNamespace(
            kind="event",
            market="spot",
            stream="ethusdt@trade",
            event_type="trade",
            symbol="BTCUSDT",
            payload={"E": 4000, "s": "BTCUSDT"},
        ),
        received_at_monotonic=40.0,
        freshness=event.freshness,
        book_synchronized=False,
    )

    with pytest.raises(RuntimeError):
        await service._record_supervised_event("spot", wrong_stream)

    wrong_market = SimpleNamespace(
        message=SimpleNamespace(
            kind="event",
            market="usdm_futures",
            stream="btcusdt@trade",
            event_type="trade",
            symbol="BTCUSDT",
            payload={"E": 4000, "s": "BTCUSDT"},
        ),
        received_at_monotonic=40.0,
        freshness=event.freshness,
        book_synchronized=False,
    )

    with pytest.raises(RuntimeError):
        await service._record_supervised_event("spot", wrong_market)

    assert service.event_cache_size == 0


@pytest.mark.asyncio
async def test_non_fresh_event_is_rejected_from_cache():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    event, _ = make_cache_event(
        "spot",
        "BTCUSDT",
        5000,
        50.0,
        freshness_state="STALE",
    )

    with pytest.raises(RuntimeError):
        await service._record_supervised_event("spot", event)

    assert service.event_cache_size == 0


@pytest.mark.asyncio
async def test_unknown_symbol_is_rejected_and_cache_is_bounded():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    event, _ = make_cache_event("spot", "ETHUSDT", 6000, 60.0)

    with pytest.raises(RuntimeError):
        await service._record_supervised_event("spot", event)

    assert service.event_cache_size == 0

    with pytest.raises(ValueError):
        service.latest_event("spot", "ETHUSDT")


def test_event_cache_clear_removes_all_cached_entries():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    service._latest_events[("spot", "BTCUSDT")] = object()
    assert service.event_cache_size == 1

    service.clear_event_cache()

    assert service.event_cache_size == 0
    assert ("spot", "BTCUSDT") not in service._latest_events


@pytest.mark.asyncio
async def test_mark_price_event_is_cached_under_its_own_pipeline():
    service = MarketDataService(
        MarketDataConfig(futures_mark_price_symbols=("BTCUSDT",)),
    )

    install_freshness_validator(service, "usdm_futures")
    event, _ = make_cache_event(
        "futures_mark_price",
        "BTCUSDT",
        7000,
        70.0,
    )

    accepted = await service._record_supervised_event(
        "futures_mark_price",
        event,
    )

    assert accepted is True

    cached = service.latest_event("futures_mark_price", "BTCUSDT")
    assert cached is not None
    assert cached.event_type == "markPriceUpdate"
    assert cached.freshness.state == "FRESH"
    assert cached.book_synchronized is False
    assert service.pipeline_states["futures_mark_price"] is PipelineState.READY

    assert ("spot", "BTCUSDT") not in service._latest_events


@pytest.mark.asyncio
async def test_equal_timestamp_events_use_receive_time_as_tie_breaker():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
    )

    install_freshness_validator(service)
    first, _ = make_cache_event("spot", "BTCUSDT", 8000, 80.0)
    older_arrival, _ = make_cache_event("spot", "BTCUSDT", 8000, 79.0)
    later_arrival, _ = make_cache_event("spot", "BTCUSDT", 8000, 81.0)

    assert await service._record_supervised_event("spot", first) is True

    assert await service._record_supervised_event(
        "spot",
        older_arrival,
    ) is False

    cached = service.latest_event("spot", "BTCUSDT")
    assert cached.received_at_monotonic == 80.0

    assert await service._record_supervised_event(
        "spot",
        later_arrival,
    ) is True

    cached = service.latest_event("spot", "BTCUSDT")
    assert cached.event_timestamp_ms == 8000
    assert cached.received_at_monotonic == 81.0


@pytest.mark.asyncio
async def test_shutdown_clears_cached_market_data(monkeypatch):
    monkeypatch.setattr(
        "app.market_data.service.parse_spot_exchange_info",
        lambda payload, *, fetched_at_monotonic: FakeMetadata({"BTCUSDT"}),
    )

    created = {}

    def factory(config, group, **kwargs):
        supervisor = FakeLifecycleSupervisor(config, group, **kwargs)
        created[group] = supervisor
        return supervisor

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: FakeSpotClient([]),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
        supervisor_factory=factory,
    )

    await service.start()
    await created["spot"].started.wait()

    event, _ = make_cache_event(
        "spot", "BTCUSDT", 1_800_000_000_000, 100.0
    )

    await service._record_supervised_event("spot", event)

    assert service.event_cache_size == 1
    assert service.latest_event("spot", "BTCUSDT") is not None

    await service.stop()

    assert service.event_cache_size == 0
    assert ("spot", "BTCUSDT") not in service._latest_events
    assert service.state.value == "STOPPED"

    health = service.health_snapshot()
    assert health.state.value == "STOPPED"
    assert health.pipelines[0].state is PipelineState.STOPPED

@pytest.mark.asyncio
async def test_latest_event_revalidates_using_original_receive_monotonic_time():
    service = MarketDataService(MarketDataConfig(spot_symbols=("BTCUSDT",)))
    calls = install_freshness_validator(service)
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)

    assert await service._record_supervised_event("spot", event) is True
    cached = service.latest_event("spot", "BTCUSDT")

    assert cached is not None
    assert calls == [("trade", 10_000, 12.5)]
    assert cached.freshness.reason == "within_freshness_policy"
    assert service.pipeline_states["spot"] is PipelineState.READY


def test_refresh_freshness_removes_stale_event_and_degrades_only_its_pipeline():
    service = MarketDataService(
        MarketDataConfig(
            spot_symbols=("BTCUSDT",),
            futures_mark_price_symbols=("ETHUSDT",),
        )
    )
    install_freshness_validator(service, "spot", state="STALE", reason="event_exceeds_stale_threshold")
    install_freshness_validator(service, "usdm_futures")
    service._pipeline_states.update({
        "spot": PipelineState.READY,
        "futures_mark_price": PipelineState.READY,
    })
    stale, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)
    fresh, _ = make_cache_event("futures_mark_price", "ETHUSDT", 10_001, 12.6)
    # Cache directly to isolate refresh behavior from callback-time validation.
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("spot", "BTCUSDT")] = CachedMarketDataEvent(
        "spot", "BTCUSDT", "trade", 10_000, 12.5, stale.freshness, False, stale.message.payload
    )
    service._latest_events[("futures_mark_price", "ETHUSDT")] = CachedMarketDataEvent(
        "futures_mark_price", "ETHUSDT", "markPriceUpdate", 10_001, 12.6, fresh.freshness, False, fresh.message.payload
    )

    assert service.refresh_freshness() == 1
    assert ("spot", "BTCUSDT") not in service._latest_events
    assert ("futures_mark_price", "ETHUSDT") in service._latest_events
    assert service.pipeline_states["spot"] is PipelineState.STALE
    assert service.pipeline_states["futures_mark_price"] is PipelineState.READY
    assert service.pipeline_errors["spot"] == "event_exceeds_stale_threshold"
    assert service.state.value == "DEGRADED"
    assert service.refresh_freshness() == 0


def test_latest_event_fails_closed_when_exchange_clock_is_unsynchronized():
    service = MarketDataService(MarketDataConfig(spot_symbols=("BTCUSDT",)))
    install_freshness_validator(service, state="CLOCK_UNSYNCED", reason="exchange_clock_unavailable")
    service._pipeline_states["spot"] = PipelineState.READY
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("spot", "BTCUSDT")] = CachedMarketDataEvent(
        "spot", "BTCUSDT", "trade", 10_000, 12.5, event.freshness, False, event.message.payload
    )

    assert service.latest_event("spot", "BTCUSDT") is None
    assert service.pipeline_states["spot"] is PipelineState.UNSYNCED
    assert service.pipeline_errors["spot"] == "exchange_clock_unavailable"


def test_invalid_freshness_result_fails_pipeline_and_removes_cache():
    service = MarketDataService(MarketDataConfig(spot_symbols=("BTCUSDT",)))
    install_freshness_validator(service, state="INVALID", reason="invalid_event_timestamp")
    service._pipeline_states["spot"] = PipelineState.READY
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("spot", "BTCUSDT")] = CachedMarketDataEvent(
        "spot", "BTCUSDT", "trade", 10_000, 12.5, event.freshness, False, event.message.payload
    )

    assert service.refresh_freshness() == 1
    assert service.latest_event("spot", "BTCUSDT") is None
    assert service.pipeline_states["spot"] is PipelineState.FAILED


def test_missing_freshness_resources_fail_closed():
    service = MarketDataService(MarketDataConfig(spot_symbols=("BTCUSDT",)))
    service._pipeline_states["spot"] = PipelineState.READY
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("spot", "BTCUSDT")] = CachedMarketDataEvent(
        "spot", "BTCUSDT", "trade", 10_000, 12.5, event.freshness, False, event.message.payload
    )

    assert service.latest_event("spot", "BTCUSDT") is None
    assert service.pipeline_states["spot"] is PipelineState.FAILED
    assert service.pipeline_errors["spot"] == "FreshnessResourcesUnavailable"


def test_fresh_revalidation_does_not_recover_stale_pipeline_state():
    service = MarketDataService(MarketDataConfig(spot_symbols=("BTCUSDT",)))
    install_freshness_validator(service)
    service._pipeline_states["spot"] = PipelineState.STALE
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("spot", "BTCUSDT")] = CachedMarketDataEvent(
        "spot", "BTCUSDT", "trade", 10_000, 12.5, event.freshness, False, event.message.payload
    )

    assert service.latest_event("spot", "BTCUSDT") is not None
    assert service.pipeline_states["spot"] is PipelineState.STALE


def test_depth_event_is_not_sent_to_freshness_validator():
    service = MarketDataService(MarketDataConfig(futures_depth_symbols=("BTCUSDT",)))
    calls = install_freshness_validator(service, "usdm_futures")
    service._resources["usdm_futures"].clock = FakeHealthClock(True)
    event, _ = make_cache_event("futures_depth", "BTCUSDT", 10_000, 12.5)
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("futures_depth", "BTCUSDT")] = CachedMarketDataEvent(
        "futures_depth", "BTCUSDT", "depthUpdate", 10_000, 12.5, None, False, event.message.payload
    )

    assert service.latest_event("futures_depth", "BTCUSDT") is not None
    assert calls == []


def test_validator_exception_fails_closed_with_sanitized_error_type():
    service = MarketDataService(MarketDataConfig(spot_symbols=("BTCUSDT",)))

    class RaisingValidator:
        def validate_event(self, *args, **kwargs):
            raise RuntimeError("sensitive provider detail must not be exposed")

    service._resources["spot"] = SimpleNamespace(
        freshness_validator=RaisingValidator(),
    )
    service._pipeline_states["spot"] = PipelineState.READY
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 12.5)
    from app.market_data.service import CachedMarketDataEvent
    service._latest_events[("spot", "BTCUSDT")] = CachedMarketDataEvent(
        "spot", "BTCUSDT", "trade", 10_000, 12.5, event.freshness, False, event.message.payload
    )

    assert service.latest_event("spot", "BTCUSDT") is None
    assert service.pipeline_states["spot"] is PipelineState.FAILED
    assert service.pipeline_errors["spot"] == "RuntimeError"
    assert "sensitive" not in str(service.pipeline_errors)


class FakeHealthMetadata:
    def __init__(self, fresh=True):
        self.fresh = fresh
        self.calls = []

    def require_fresh(self, now_monotonic=None):
        self.calls.append(now_monotonic)
        if not self.fresh:
            raise ValueError("metadata freshness unavailable")
        return None


class FakeHealthClock:
    def __init__(self, synchronized=True):
        self.is_synchronized = synchronized


class FakeHealthValidator:
    def __init__(self, state=FreshnessState.FRESH):
        self.state = state
        self.calls = []

    def validate_event(self, event_type, event_timestamp_ms, *, received_at_monotonic=None):
        from types import SimpleNamespace
        self.calls.append((event_type, event_timestamp_ms, received_at_monotonic))
        return SimpleNamespace(
            state=self.state,
            event_type=event_type,
            event_timestamp_ms=event_timestamp_ms,
            reason="within_freshness_policy",
        )


def install_health_resources(service, market="spot", *, clock_sync=True, metadata_fresh=True):
    validator = FakeHealthValidator()
    service._resources[market] = SimpleNamespace(
        clock=FakeHealthClock(clock_sync),
        metadata=FakeHealthMetadata(metadata_fresh),
        freshness_validator=validator,
    )
    return validator


def install_ready_health_supervisor(service, group="spot", *, streams=None, state="SUBSCRIPTIONS_READY"):
    suffix = {
        "spot": "@trade",
        "futures_depth": "@depth",
        "futures_mark_price": "@markPrice",
    }[group]
    configured = service._configured_symbols_for_group(group)
    expected = tuple(sorted(
        f"{symbol.lower()}{suffix}" for symbol in configured
    ))
    service._supervisors[group] = SimpleNamespace(
        state=SimpleNamespace(value=state),
        active_streams=expected if streams is None else streams,
    )


def test_health_snapshot_for_empty_configuration_is_disabled_and_immutable():
    from dataclasses import FrozenInstanceError

    service = MarketDataService(MarketDataConfig(), monotonic=lambda: 100.0)

    snapshot = service.health_snapshot()

    assert snapshot.state.value == "DISABLED"
    assert snapshot.pipelines == ()
    assert snapshot.generated_at_monotonic == 100.0

    with pytest.raises(FrozenInstanceError):
        snapshot.state = snapshot.state


def test_health_snapshot_reports_bootstrapping_without_claiming_readiness():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )

    snapshot = service.health_snapshot()

    status = snapshot.pipelines[0]
    assert snapshot.state.value == "STARTING"
    assert status.group.value == "spot"
    assert status.state is PipelineState.BOOTSTRAPPING
    assert status.active_streams == ()
    assert status.clock_synchronized is False
    assert status.metadata_fresh is False
    assert status.last_event_timestamp_ms is None


def test_health_snapshot_reports_ready_only_with_verified_prerequisites():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )
    validator = install_health_resources(service)
    install_ready_health_supervisor(service)

    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 90.0)
    assert asyncio.run(service._record_supervised_event("spot", event)) is True
    service._pipeline_states["spot"] = PipelineState.READY

    snapshot = service.health_snapshot()
    status = snapshot.pipelines[0]

    assert snapshot.state.value == "RUNNING"
    assert status.state is PipelineState.READY
    assert status.active_streams == ("btcusdt@trade",)
    assert status.clock_synchronized is True
    assert status.metadata_fresh is True
    assert status.last_event_type == "trade"
    assert status.last_event_timestamp_ms == 10_000
    assert status.last_event_received_at_monotonic == 90.0
    assert status.last_freshness_state is FreshnessState.FRESH
    assert validator.calls == [("trade", 10_000, 90.0)]


def test_health_snapshot_does_not_claim_active_streams_without_verified_manager_state():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )
    install_health_resources(service)
    install_ready_health_supervisor(service, state="RECONNECTING")
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 90.0)
    asyncio.run(service._record_supervised_event("spot", event))
    service._pipeline_states["spot"] = PipelineState.READY

    status = service.health_snapshot().pipelines[0]

    assert status.active_streams == ()
    assert status.state is PipelineState.SUBSCRIBING


def test_health_snapshot_rejects_mismatched_supervisor_streams():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )
    install_health_resources(service)
    install_ready_health_supervisor(
        service,
        streams=("ethusdt@trade",),
    )
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 90.0)
    asyncio.run(service._record_supervised_event("spot", event))
    service._pipeline_states["spot"] = PipelineState.READY

    status = service.health_snapshot().pipelines[0]

    assert status.active_streams == ()
    assert status.state is PipelineState.SUBSCRIBING


def test_health_snapshot_marks_unsynchronized_clock_as_unsynced():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )
    install_health_resources(service, clock_sync=False)
    install_ready_health_supervisor(service)
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 90.0)
    asyncio.run(service._record_supervised_event("spot", event))
    service._pipeline_states["spot"] = PipelineState.READY

    snapshot = service.health_snapshot()
    status = snapshot.pipelines[0]

    assert status.clock_synchronized is False
    assert status.state is PipelineState.UNSYNCED
    assert snapshot.state.value == "DEGRADED"


def test_health_snapshot_marks_stale_metadata_without_ready_claim():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )
    install_health_resources(service, metadata_fresh=False)
    install_ready_health_supervisor(service)
    event, _ = make_cache_event("spot", "BTCUSDT", 10_000, 90.0)
    asyncio.run(service._record_supervised_event("spot", event))
    service._pipeline_states["spot"] = PipelineState.READY

    status = service.health_snapshot().pipelines[0]

    assert status.metadata_fresh is False
    assert status.state is PipelineState.STALE
    assert status.last_error == "ValueError"


def test_depth_health_never_claims_ready_without_book_synchronization():
    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )
    install_health_resources(service, market="usdm_futures")
    install_ready_health_supervisor(service, group="futures_depth")
    event, _ = make_cache_event("futures_depth", "BTCUSDT", 10_000, 90.0)
    asyncio.run(service._record_supervised_event("futures_depth", event))

    status = service.health_snapshot().pipelines[0]

    assert status.group.value == "futures_depth"
    assert status.active_streams == ("btcusdt@depth",)
    assert status.book_synchronized is False
    assert status.last_freshness_state is None
    assert status.state is PipelineState.UNSYNCED


def test_health_snapshot_selects_latest_event_by_receive_monotonic_time():
    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT", "ETHUSDT")),
        monotonic=lambda: 100.0,
    )
    validator = install_health_resources(service)
    install_ready_health_supervisor(service)
    btc, _ = make_cache_event("spot", "BTCUSDT", 10_000, 80.0)
    eth, _ = make_cache_event("spot", "ETHUSDT", 9_999, 90.0)
    asyncio.run(service._record_supervised_event("spot", btc))
    asyncio.run(service._record_supervised_event("spot", eth))
    service._pipeline_states["spot"] = PipelineState.READY

    status = service.health_snapshot().pipelines[0]

    assert status.last_event_timestamp_ms == 9_999
    assert status.last_event_received_at_monotonic == 90.0
    assert validator.calls == [
        ("trade", 10_000, 80.0),
        ("trade", 9_999, 90.0),
    ]


def _multi_spot_payload(symbol):
    base = symbol.removesuffix("USDT")
    return {
        "serverTime": 1_800_000_000_000,
        "symbols": [
            {
                "symbol": symbol,
                "status": "TRADING",
                "baseAsset": base,
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET"],
                "filters": [{"filterType": "PRICE_FILTER"}],
            }
        ],
    }


class MultiSymbolSpotClient(BinanceSpotRestClient):
    def __init__(self, calls, payloads, fail_symbol=None):
        self.calls = calls
        self.payloads = payloads
        self.fail_symbol = fail_symbol

    def get_server_time(self):
        self.calls.append("spot_server_time")
        return {"serverTime": 1_800_000_000_000}

    def get_exchange_info(self, symbol=None):
        self.calls.append(("spot_exchange_info", symbol))

        if symbol == self.fail_symbol:
            raise RuntimeError("simulated symbol metadata request failure")

        return self.payloads[symbol]


@pytest.mark.asyncio
async def test_spot_multisymbol_bootstrap_combines_immutable_metadata():
    calls = []
    payloads = {
        "BTCUSDT": _multi_spot_payload("BTCUSDT"),
        "ETHUSDT": _multi_spot_payload("ETHUSDT"),
    }

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT", "ETHUSDT")),
        spot_client_factory=lambda: MultiSymbolSpotClient(calls, payloads),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.resources
    assert not report.errors

    metadata = report.resources["spot"].metadata
    assert set(metadata.symbols) == {"BTCUSDT", "ETHUSDT"}
    assert metadata.require_symbol("BTCUSDT", now_monotonic=101.0).base_asset == "BTC"
    assert metadata.require_symbol("ETHUSDT", now_monotonic=101.0).base_asset == "ETH"

    with pytest.raises(TypeError):
        metadata.symbols["BNBUSDT"] = metadata.symbols["BTCUSDT"]

    assert calls == [
        "spot_server_time",
        ("spot_exchange_info", "BTCUSDT"),
        ("spot_exchange_info", "ETHUSDT"),
    ]


@pytest.mark.asyncio
async def test_spot_multisymbol_bootstrap_rejects_missing_requested_symbol():
    calls = []
    payloads = {
        "BTCUSDT": _multi_spot_payload("BTCUSDT"),
        # Binance response does not contain the requested ETHUSDT record.
        "ETHUSDT": _multi_spot_payload("BTCUSDT"),
    }

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT", "ETHUSDT")),
        spot_client_factory=lambda: MultiSymbolSpotClient(calls, payloads),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert report.errors["spot"] == "SymbolNotFoundError"
    assert "spot" not in report.resources


@pytest.mark.asyncio
async def test_spot_multisymbol_bootstrap_fails_if_one_metadata_request_fails():
    calls = []
    payloads = {
        "BTCUSDT": _multi_spot_payload("BTCUSDT"),
        "ETHUSDT": _multi_spot_payload("ETHUSDT"),
    }

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT", "ETHUSDT")),
        spot_client_factory=lambda: MultiSymbolSpotClient(
            calls,
            payloads,
            fail_symbol="ETHUSDT",
        ),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert report.errors["spot"] == "RuntimeError"
    assert "spot" not in report.resources
    assert ("spot_exchange_info", "ETHUSDT") in calls


@pytest.mark.asyncio
async def test_spot_symbol_filtered_response_rejects_unexpected_extra_symbol():
    calls = []
    payload = _multi_spot_payload("BTCUSDT")
    payload["symbols"].append({
        "symbol": "ETHUSDT",
        "status": "TRADING",
        "baseAsset": "ETH",
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [{"filterType": "PRICE_FILTER"}],
    })

    service = MarketDataService(
        MarketDataConfig(spot_symbols=("BTCUSDT",)),
        spot_client_factory=lambda: MultiSymbolSpotClient(
            calls,
            {"BTCUSDT": payload},
        ),
        clock_factory=make_clock,
        monotonic=lambda: 100.0,
    )

    report = await service.bootstrap()

    assert "spot" in report.errors
    assert report.errors["spot"] == "RuntimeError"
    assert "spot" not in report.resources

@pytest.mark.asyncio
async def test_clock_maintenance_invalidates_only_failed_market_and_retries():
    from app.market_data.service import CachedMarketDataEvent

    sleep_started = asyncio.Queue()
    sleep_release = asyncio.Queue()
    sync_calls = []

    async def controlled_sleep(delay):
        await sleep_started.put(delay)
        await sleep_release.get()

    class RecoveringClock:
        def __init__(self):
            self.is_synchronized = True

        def synchronize(self, provider):
            sync_calls.append(provider)
            if len(sync_calls) == 1:
                self.is_synchronized = False
                raise RuntimeError("Simulated synchronization failure.")

            self.is_synchronized = True
            return object()

    clock = RecoveringClock()
    provider = lambda: {"serverTime": 1_800_000_000_000}

    service = MarketDataService(
        MarketDataConfig(
            spot_symbols=("BTCUSDT",),
            futures_depth_symbols=("BTCUSDT",),
            futures_mark_price_symbols=("BTCUSDT",),
        ),
        sleep=controlled_sleep,
        clock_sync_interval_seconds=30,
        clock_retry_interval_seconds=5,
    )

    def cached_event(group, event_type, *, book_synchronized=False):
        return CachedMarketDataEvent(
            group=group,
            symbol="BTCUSDT",
            event_type=event_type,
            event_timestamp_ms=1_800_000_000_000,
            received_at_monotonic=100.0,
            freshness=None,
            book_synchronized=book_synchronized,
            payload={"s": "BTCUSDT", "E": 1_800_000_000_000},
        )

    service._latest_events[("spot", "BTCUSDT")] = cached_event(
        "spot", "trade"
    )
    service._latest_events[("futures_depth", "BTCUSDT")] = cached_event(
        "futures_depth", "depthUpdate"
    )
    service._latest_events[("futures_mark_price", "BTCUSDT")] = cached_event(
        "futures_mark_price", "markPriceUpdate"
    )

    service._pipeline_states = {
        "spot": PipelineState.READY,
        "futures_depth": PipelineState.READY,
        "futures_mark_price": PipelineState.READY,
    }

    resources = SimpleNamespace(
        clock=clock,
        server_time_provider=provider,
    )

    task = asyncio.create_task(
        service._maintain_exchange_clock("usdm_futures", resources)
    )

    try:
        first_delay = await asyncio.wait_for(sleep_started.get(), timeout=2)
        assert first_delay == 30

        sleep_release.put_nowait(None)

        retry_delay = await asyncio.wait_for(sleep_started.get(), timeout=2)
        assert retry_delay == 5

        assert len(sync_calls) == 1
        assert sync_calls[0] is provider

        assert ("spot", "BTCUSDT") in service._latest_events
        assert ("futures_depth", "BTCUSDT") not in service._latest_events
        assert ("futures_mark_price", "BTCUSDT") not in service._latest_events

        assert service.pipeline_states["spot"] is PipelineState.READY
        assert service.pipeline_states["futures_depth"] is PipelineState.UNSYNCED
        assert service.pipeline_states["futures_mark_price"] is PipelineState.UNSYNCED

        assert (
            service.pipeline_errors["futures_depth"]
            == "ExchangeClockUnsynchronized"
        )
        assert (
            service.pipeline_errors["futures_mark_price"]
            == "ExchangeClockUnsynchronized"
        )

        sleep_release.put_nowait(None)

        next_delay = await asyncio.wait_for(sleep_started.get(), timeout=2)
        assert next_delay == 30

        assert len(sync_calls) == 2
        assert clock.is_synchronized is True

        # Clock recovery alone must not restore pipeline readiness.
        assert service.pipeline_states["futures_depth"] is PipelineState.UNSYNCED
        assert service.pipeline_states["futures_mark_price"] is PipelineState.UNSYNCED

        assert ("futures_depth", "BTCUSDT") not in service._latest_events
        assert ("futures_mark_price", "BTCUSDT") not in service._latest_events

    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
