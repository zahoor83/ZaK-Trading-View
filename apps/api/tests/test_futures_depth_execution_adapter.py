from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
import inspect
import ast

import pytest

from app.market_data.config import MarketDataConfig
from app.market_data.futures_order_book import FuturesOrderBookSynchronizer
from app.market_data.service import MarketDataService, MarketResources
from app.market_data.service_models import PipelineState
from app.market_data.symbol_metadata import (
    SymbolMetadata,
    SymbolMetadataSnapshot,
)
from app.services.futures_depth_execution_adapter import (
    FuturesDepthExecutionAdapter,
)
from app.services.paper_execution_price_gate import PaperExecutionPriceError


BASE_MS = 1_700_000_000_000


class FakeClock:
    is_synchronized = True

    def __init__(self, *, expired=False):
        self.expired = expired

    def now_exchange_ms(self, *, at_monotonic=None):
        if self.expired:
            raise RuntimeError("clock expired")
        point = 100.0 if at_monotonic is None else at_monotonic
        return BASE_MS + round((point - 100.0) * 1000)


def make_metadata(symbols, *, fetched_at=99.5, server_time_ms=None, tick="0.01"):
    records = {}
    for symbol in symbols:
        filters = (
            MappingProxyType({
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "1000000",
                "tickSize": tick,
            }),
        )
        records[symbol] = SymbolMetadata(
            market="usdm_futures",
            symbol=symbol,
            status="TRADING",
            base_asset=symbol[:-4],
            quote_asset="USDT",
            order_types=("LIMIT", "MARKET"),
            filter_types=("PRICE_FILTER",),
            pair=symbol,
            contract_type="PERPETUAL",
            margin_asset="USDT",
            filters=filters,
        )

    return SymbolMetadataSnapshot(
        market="usdm_futures",
        server_time_ms=BASE_MS - 500 if server_time_ms is None else server_time_ms,
        fetched_at_monotonic=fetched_at,
        max_age_seconds=300.0,
        symbols=MappingProxyType(records),
    )


def make_book(symbol, *, received_at=99.8, now=100.0):
    synchronizer = FuturesOrderBookSynchronizer(
        symbol,
        monotonic=lambda: received_at,
    )
    event = {
        "e": "depthUpdate",
        "E": BASE_MS - 100,
        "T": BASE_MS - 100,
        "s": symbol,
        "U": 100,
        "u": 101,
        "pu": 99,
        "b": [["100.00", "2.0"]],
        "a": [["100.01", "3.0"]],
    }
    assert synchronizer.ingest_event(event) is True
    synchronizer.apply_snapshot({
        "lastUpdateId": 100,
        "E": BASE_MS - 200,
        "T": BASE_MS - 200,
        "bids": [["100.00", "1.0"]],
        "asks": [["100.01", "1.0"]],
    })
    return synchronizer


def make_service(
    symbols=("BTCUSDT",),
    *,
    now=100.0,
    received_at=99.8,
    metadata_fetched_at=99.5,
    metadata_server_time_ms=None,
    tick="0.01",
    pipeline_state=PipelineState.SUBSCRIBING,
    active_streams=None,
    book_symbols=None,
    expired_clock=False,
):
    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=tuple(symbols)),
        monotonic=lambda: now,
    )
    metadata = make_metadata(
        symbols,
        fetched_at=metadata_fetched_at,
        server_time_ms=metadata_server_time_ms,
        tick=tick,
    )
    service._resources["usdm_futures"] = MarketResources(
        market="usdm_futures",
        clock=FakeClock(expired=expired_clock),
        metadata=metadata,
        freshness_validator=None,
        server_time_provider=None,
        metadata_refresh_provider=lambda: None,
    )
    service._pipeline_states["futures_depth"] = pipeline_state

    expected = tuple(sorted(f"{s.lower()}@depth" for s in symbols))
    if active_streams is None:
        active_streams = expected
    service._supervisors["futures_depth"] = SimpleNamespace(
        state=SimpleNamespace(value="SUBSCRIPTIONS_READY"),
        active_streams=active_streams,
    )

    if book_symbols is None:
        book_symbols = symbols
    service._futures_order_books = {
        s: make_book(s, received_at=received_at, now=now)
        for s in book_symbols
    }
    return service


def adapter(service):
    return FuturesDepthExecutionAdapter(service)


def test_buy_selects_best_ask_and_sell_selects_best_bid():
    instance = adapter(make_service())
    buy = instance.validate("BTCUSDT", side="buy")
    sell = instance.validate("BTCUSDT", side="sell")

    assert buy.price == Decimal("100.01")
    assert buy.source == "futures_depth_book"
    assert buy.event_time.tzinfo is timezone.utc
    assert sell.price == Decimal("100.00")


def test_context_contains_exchange_clock_converted_receive_time():
    _, context = adapter(make_service()).validate_with_context(
        "BTCUSDT", side="buy"
    )
    assert context.received_at.tzinfo is timezone.utc
    assert context.metadata_received_at.tzinfo is timezone.utc
    assert context.now.tzinfo is timezone.utc
    assert context.symbol_metadata_snapshot["market"] == "usdm_futures"


def test_unconfigured_symbol_fails_closed():
    with pytest.raises(RuntimeError):
        adapter(make_service()).validate("ETHUSDT", side="buy")


def test_unsynced_pipeline_fails_closed():
    service = make_service(pipeline_state=PipelineState.UNSYNCED)
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_unverified_subscription_fails_closed():
    service = make_service(active_streams=())
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_missing_configured_symbol_book_fails_closed():
    service = make_service(
        symbols=("BTCUSDT", "ETHUSDT"),
        book_symbols=("BTCUSDT",),
    )
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_stale_book_fails_closed():
    service = make_service(now=106.0)
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_future_dated_book_receive_time_fails_closed():
    service = make_service(now=100.0, received_at=100.1)
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_stale_metadata_fails_closed():
    service = make_service(
        now=500.0,
        received_at=499.8,
        metadata_fetched_at=100.0,
        metadata_server_time_ms=BASE_MS - 400_000,
    )
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_expired_exchange_clock_fails_closed():
    service = make_service(expired_clock=True)
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_tick_size_is_enforced():
    service = make_service(tick="0.03")
    with pytest.raises(PaperExecutionPriceError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_all_configured_books_must_be_fresh():
    service = make_service(
        symbols=("BTCUSDT", "ETHUSDT"),
        received_at=99.8,
    )
    service._futures_order_books["ETHUSDT"] = make_book(
        "ETHUSDT",
        received_at=94.0,
    )
    with pytest.raises(RuntimeError):
        adapter(service).validate("BTCUSDT", side="buy")


def test_adapter_does_not_access_market_data_private_internals():
    module = __import__(
        "app.services.futures_depth_execution_adapter",
        fromlist=["FuturesDepthExecutionAdapter"],
    )
    tree = ast.parse(inspect.getsource(module))
    forbidden = {
        "_resources",
        "_monotonic",
        "_supervisors",
        "_futures_order_books",
        "_verified_active_streams",
    }
    accessed = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    assert accessed.isdisjoint(forbidden)


def test_execution_path_does_not_call_health_snapshot(monkeypatch):
    service = make_service()

    def forbidden():
        raise AssertionError("health_snapshot must not be called")

    monkeypatch.setattr(service, "health_snapshot", forbidden)
    result = adapter(service).validate("BTCUSDT", side="buy")
    assert result.price == Decimal("100.01")


def test_context_exposes_complete_immutable_depth():
    _, context = adapter(make_service()).validate_with_context(
        "BTCUSDT", side="buy"
    )
    assert context.bids == ((Decimal("100.00"), Decimal("2.0")),)
    assert context.asks == ((Decimal("100.01"), Decimal("3.0")),)
    assert isinstance(context.bids, tuple)
    assert isinstance(context.asks, tuple)


def test_deeper_off_tick_price_fails_closed():
    service = make_service()
    sync = service._futures_order_books["BTCUSDT"]
    sync._book = replace(sync._book, asks=(
        (Decimal("100.01"), Decimal("3")),
        (Decimal("100.025"), Decimal("1")),
    ))
    with pytest.raises(RuntimeError, match="tick aligned"):
        adapter(service).validate("BTCUSDT", side="buy")


def test_deeper_zero_quantity_fails_closed():
    service = make_service()
    sync = service._futures_order_books["BTCUSDT"]
    sync._book = replace(sync._book, asks=(
        (Decimal("100.01"), Decimal("3")),
        (Decimal("100.02"), Decimal("0")),
    ))
    with pytest.raises(RuntimeError, match="level values"):
        adapter(service).validate("BTCUSDT", side="buy")


def test_deeper_non_decimal_price_fails_closed():
    service = make_service()
    sync = service._futures_order_books["BTCUSDT"]
    sync._book = replace(sync._book, asks=(
        (Decimal("100.01"), Decimal("3")),
        ("100.02", Decimal("1")),
    ))
    with pytest.raises(RuntimeError, match="level values"):
        adapter(service).validate("BTCUSDT", side="buy")


def test_deeper_duplicate_price_fails_closed():
    service = make_service()
    sync = service._futures_order_books["BTCUSDT"]
    sync._book = replace(sync._book, asks=(
        (Decimal("100.01"), Decimal("3")),
        (Decimal("100.01"), Decimal("1")),
    ))
    with pytest.raises(RuntimeError, match="duplicate"):
        adapter(service).validate("BTCUSDT", side="buy")


def test_deeper_malformed_level_fails_closed():
    service = make_service()
    sync = service._futures_order_books["BTCUSDT"]
    sync._book = replace(sync._book, asks=(
        (Decimal("100.01"), Decimal("3")),
        (Decimal("100.02"),),
    ))
    with pytest.raises(RuntimeError, match="structure"):
        adapter(service).validate("BTCUSDT", side="buy")


def test_deeper_unsorted_level_fails_closed():
    service = make_service()
    sync = service._futures_order_books["BTCUSDT"]
    sync._book = replace(sync._book, asks=(
        (Decimal("100.02"), Decimal("1")),
        (Decimal("100.01"), Decimal("3")),
    ))
    with pytest.raises(RuntimeError, match="correctly ordered"):
        adapter(service).validate("BTCUSDT", side="buy")
