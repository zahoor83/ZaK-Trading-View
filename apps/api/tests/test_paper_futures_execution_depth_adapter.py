import ast
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.market_data.futures_order_book import (
    FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS,
)
from app.market_data.service import (
    FuturesDepthExecutionContext,
    MarketDataService,
)
from app.services.futures_depth_execution_adapter import (
    FuturesDepthExecutionAdapter,
)
from app.services.paper_execution_price_gate import (
    PaperExecutionPriceError,
)
from app.services import paper_futures_execution as execution


def make_context(
    *,
    symbol="BTCUSDT",
    event_time=None,
):
    now = datetime.now(timezone.utc)
    received = now - timedelta(milliseconds=100)

    if event_time is None:
        event_time = received

    return FuturesDepthExecutionContext(
        symbol=symbol,
        last_update_id=12345,
        bid=Decimal("99999.00"),
        ask=Decimal("100000.00"),
        bids=((Decimal("99999.00"), Decimal("2")),),
        asks=((Decimal("100000.00"), Decimal("3")),),
        event_time=event_time,
        received_at=received,
        metadata_received_at=now - timedelta(milliseconds=200),
        received_at_monotonic=99.9,
        metadata_received_at_monotonic=99.8,
        tick_size=Decimal("0.01"),
        current_monotonic=100.0,
        now=now,
        metadata_snapshot=None,
        symbol_metadata_snapshot={
            "schema_version": 1,
            "market": "usdm_futures",
            "symbol": symbol,
            "status": "TRADING",
            "filters": [],
            "tick_size": "0.01",
        },
    )


@pytest.fixture
def trusted_market_data(monkeypatch):
    context = make_context()
    market_data = object.__new__(MarketDataService)
    calls = []

    def context_provider(self, symbol):
        calls.append(symbol)
        assert symbol == context.symbol
        return context

    monkeypatch.setattr(
        MarketDataService,
        "futures_depth_execution_context",
        context_provider,
    )

    return market_data, context, calls


def test_buy_execution_uses_trusted_best_ask(trusted_market_data):
    market_data, context, calls = trusted_market_data

    adapter = FuturesDepthExecutionAdapter(market_data)
    validated, returned_context = adapter.validate_with_context(
        "BTCUSDT",
        side="buy",
    )

    assert validated.side == "buy"
    assert validated.price == context.ask
    assert validated.source == "futures_depth_book"
    assert validated.market_type == "futures"
    assert returned_context is context
    assert calls == ["BTCUSDT"]


def test_sell_execution_uses_trusted_best_bid(trusted_market_data):
    market_data, context, calls = trusted_market_data

    adapter = FuturesDepthExecutionAdapter(market_data)
    validated, returned_context = adapter.validate_with_context(
        "BTCUSDT",
        side="sell",
    )

    assert validated.side == "sell"
    assert validated.price == context.bid
    assert validated.source == "futures_depth_book"
    assert validated.market_type == "futures"
    assert returned_context is context
    assert calls == ["BTCUSDT"]


def test_stale_futures_depth_is_rejected(monkeypatch):
    now = datetime.now(timezone.utc)
    stale_event = now - timedelta(
        seconds=FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS + 10
    )
    context = make_context(event_time=stale_event)

    market_data = object.__new__(MarketDataService)

    monkeypatch.setattr(
        MarketDataService,
        "futures_depth_execution_context",
        lambda self, symbol: context,
    )

    adapter = FuturesDepthExecutionAdapter(market_data)

    with pytest.raises(PaperExecutionPriceError):
        adapter.validate("BTCUSDT", side="buy")


def test_execution_service_uses_trusted_depth_adapter():
    source_path = Path(execution.__file__).resolve()
    module = ast.parse(source_path.read_text(encoding="utf-8"))
    
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_futures_trade_in_transaction"
    )

    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
    ]

    adapter_construction = next(
        (
            node
            for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == "FuturesDepthExecutionAdapter"
        ),
        None,
    )

    assert adapter_construction is not None
    assert len(adapter_construction.args) == 1
    assert ast.unparse(adapter_construction.args[0]) == "market_data"

    adapter_call = next(
        (
            node
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and node.func.attr == "validate_with_context"
        ),
        None,
    )

    assert adapter_call is not None
    assert ast.unparse(adapter_call.args[0]) == "symbol"

    keyword_arguments = {
        item.arg: ast.unparse(item.value)
        for item in adapter_call.keywords
    }

    assert keyword_arguments.get("side") == "side"