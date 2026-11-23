from decimal import Decimal
from types import SimpleNamespace
import inspect

import pytest

from app.market_data.config import MarketDataConfig
from app.market_data.service import (
    CachedSpotBookTicker,
    MarketDataService,
    MarketResources,
)
from app.market_data.service_models import PipelineState
from app.market_data.symbol_metadata import parse_spot_exchange_info
from app.services.paper_execution_price_gate import PaperExecutionPriceError
from app.services.spot_book_ticker_execution_adapter import (
    SpotBookTickerExecutionAdapter,
)


def make_symbol(symbol: str, tick: str = "0.01") -> dict:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "baseAsset": symbol[:-4],
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": tick,
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.00001",
                "maxQty": "100000",
                "stepSize": "0.00001",
            },
        ],
    }


def make_metadata(symbols, *, fetched_at=95.0, tick="0.01"):
    return parse_spot_exchange_info(
        {
            "serverTime": 1700000000000,
            "symbols": [
                make_symbol(symbol, tick=tick)
                for symbol in symbols
            ],
        },
        fetched_at_monotonic=fetched_at,
    )


def make_payload(symbol, *, update_id=123456, bid="100.00",
                 ask="100.01", bid_qty="2.0", ask_qty="3.0"):
    return {
        "u": update_id,
        "s": symbol,
        "b": bid,
        "B": bid_qty,
        "a": ask,
        "A": ask_qty,
    }


def make_service(
    symbols=("BTCUSDT",),
    *,
    now=100.0,
    received_at=99.5,
    metadata_fetched_at=95.0,
    tick="0.01",
    pipeline_state=PipelineState.SUBSCRIBING,
    active_streams=None,
    quote_symbols=None,
    payload_overrides=None,
):
    service = MarketDataService(
        MarketDataConfig(spot_book_ticker_symbols=tuple(symbols)),
        monotonic=lambda: now,
    )

    metadata = make_metadata(
        symbols,
        fetched_at=metadata_fetched_at,
        tick=tick,
    )

    service._resources["spot"] = MarketResources(
        market="spot",
        clock=None,
        metadata=metadata,
        freshness_validator=None,
        server_time_provider=None,
        metadata_refresh_provider=lambda: None,
    )

    service._pipeline_states["spot_book_ticker"] = pipeline_state

    expected_streams = tuple(sorted(
        f"{symbol.lower()}@bookTicker" for symbol in symbols
    ))
    if active_streams is None:
        active_streams = expected_streams

    service._supervisors["spot_book_ticker"] = SimpleNamespace(
        state=SimpleNamespace(value="SUBSCRIPTIONS_READY"),
        active_streams=active_streams,
    )

    if quote_symbols is None:
        quote_symbols = symbols

    payload_overrides = payload_overrides or {}

    for index, symbol in enumerate(quote_symbols):
        payload = make_payload(symbol)
        payload.update(payload_overrides.get(symbol, {}))
        service._spot_book_ticker_quotes[
            ("spot_book_ticker", symbol)
        ] = CachedSpotBookTicker(
            symbol=symbol,
            update_id=payload["u"],
            received_at_monotonic=received_at,
            payload=payload,
        )

    return service


def make_adapter(service):
    return SpotBookTickerExecutionAdapter(service)


def test_buy_selects_ask_and_sell_selects_bid():
    service = make_service()
    adapter = make_adapter(service)

    buy = adapter.validate("BTCUSDT", side="buy")
    sell = adapter.validate("BTCUSDT", side="sell")

    assert buy.price == Decimal("100.01")
    assert buy.ask == Decimal("100.01")
    assert buy.bid == Decimal("100.00")
    assert buy.source == "spot_book_ticker"
    assert buy.update_id == 123456
    assert buy.event_time is None

    assert sell.price == Decimal("100.00")
    assert sell.ask == Decimal("100.01")
    assert sell.bid == Decimal("100.00")


@pytest.mark.parametrize(
    "received_at",
    [98.999, 100.001],
)
def test_stale_or_future_quote_fails_closed(received_at):
    service = make_service(received_at=received_at)
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_unverified_subscription_fails_closed():
    service = make_service(active_streams=())
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_wrong_subscription_stream_set_fails_closed():
    service = make_service(
        active_streams=("ethusdt@bookTicker",)
    )
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_unsynced_pipeline_fails_closed():
    service = make_service(
        pipeline_state=PipelineState.UNSYNCED
    )
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_multi_symbol_pipeline_requires_every_configured_quote():
    service = make_service(
        symbols=("BTCUSDT", "ETHUSDT"),
        quote_symbols=("BTCUSDT",),
    )
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_stale_metadata_fails_closed():
    service = make_service(
        now=400.0,
        received_at=399.5,
        metadata_fetched_at=99.0,
    )
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_tick_size_is_enforced_by_execution_gate():
    service = make_service(tick="0.03")
    adapter = make_adapter(service)

    with pytest.raises(PaperExecutionPriceError):
        adapter.validate("BTCUSDT", side="buy")


def test_malformed_or_inverted_bbo_fails_closed():
    service = make_service(
        payload_overrides={
            "BTCUSDT": {
                "b": "100.02",
                "a": "100.01",
            }
        }
    )
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_boolean_update_id_fails_closed():
    service = make_service(
        payload_overrides={
            "BTCUSDT": {"u": True}
        }
    )
    adapter = make_adapter(service)

    with pytest.raises(RuntimeError):
        adapter.validate("BTCUSDT", side="buy")


def test_execution_path_does_not_call_health_snapshot(monkeypatch):
    service = make_service()

    def forbidden_health_snapshot():
        raise AssertionError(
            "Execution adapter must not call health_snapshot()."
        )

    monkeypatch.setattr(
        service,
        "health_snapshot",
        forbidden_health_snapshot,
    )

    result = make_adapter(service).validate(
        "BTCUSDT",
        side="buy",
    )

    assert result.price == Decimal("100.01")


def test_unrelated_aggregate_degraded_state_does_not_reject_valid_bbo():
    service = make_service()
    service._service_state = "DEGRADED"

    result = make_adapter(service).validate(
        "BTCUSDT",
        side="buy",
    )

    assert result.price == Decimal("100.01")


def test_adapter_does_not_access_market_data_private_internals():
    import ast

    module = __import__(
        "app.services.spot_book_ticker_execution_adapter",
        fromlist=["SpotBookTickerExecutionAdapter"],
    )
    source = inspect.getsource(module)
    tree = ast.parse(source)

    forbidden_attributes = {
        "_resources",
        "_monotonic",
        "_supervisors",
        "_spot_book_ticker_quotes",
        "_verified_active_streams",
    }

    accessed_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }

    assert accessed_attributes.isdisjoint(forbidden_attributes), (
        "Adapter accesses private MarketDataService internals: "
        f"{accessed_attributes.intersection(forbidden_attributes)}"
    )


def test_invalid_side_fails_closed():
    service = make_service()
    adapter = make_adapter(service)

    with pytest.raises(PaperExecutionPriceError):
        adapter.validate("BTCUSDT", side="hold")

