from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.services.paper_execution_price_gate as price_gate
from app.services.paper_execution_price_gate import (
    ExecutableQuote,
    PaperExecutionPriceError,
    validate_executable_price,
)


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
MONO_NOW = 1001.0


@pytest.fixture(autouse=True)
def fixed_monotonic_clock(monkeypatch):
    monkeypatch.setattr(
        price_gate.time,
        "monotonic",
        lambda: MONO_NOW,
    )


def quote(**overrides):
    values = {
        "market_type": "spot",
        "symbol": "BTCUSDT",
        "source": "spot_book_ticker",
        "bid": Decimal("99999.90"),
        "ask": Decimal("100000.00"),
        "event_time": None,
        "received_at": None,
        "metadata_received_at": None,
        "book_synchronized": None,
        "sequence_contiguous": None,
        "update_id": 123456,
        "received_at_monotonic": 1000.5,
        "metadata_received_at_monotonic": 900.0,
    }
    values.update(overrides)
    return ExecutableQuote(**values)


def futures_quote(**overrides):
    values = {
        "market_type": "futures",
        "symbol": "BTCUSDT",
        "source": "futures_depth_book",
        "bid": Decimal("99999.90"),
        "ask": Decimal("100000.00"),
        "event_time": NOW - timedelta(milliseconds=100),
        "received_at": NOW - timedelta(milliseconds=50),
        "metadata_received_at": NOW - timedelta(minutes=1),
        "book_synchronized": True,
        "sequence_contiguous": True,
        "update_id": None,
        "received_at_monotonic": None,
        "metadata_received_at_monotonic": None,
    }
    values.update(overrides)
    return ExecutableQuote(**values)


def validate(item, side="buy"):
    if isinstance(item, ExecutableQuote) and item.market_type == "spot":
        return validate_executable_price(
            item,
            side=side,
            tick_size=Decimal("0.10"),
        )

    return validate_executable_price(
        item,
        side=side,
        tick_size=Decimal("0.10"),
        now=NOW,
        max_quote_age=timedelta(seconds=2),
        max_metadata_age=timedelta(minutes=10),
    )


# ---------------- BUY / SELL ----------------

def test_buy_uses_ask():
    result = validate(quote(), "buy")
    assert result.price == Decimal("100000.00")


def test_sell_uses_bid():
    result = validate(quote(), "sell")
    assert result.price == Decimal("99999.90")


def test_spot_result_does_not_invent_event_time():
    result = validate(quote())

    assert result.event_time is None
    assert result.update_id == 123456
    assert result.received_at_monotonic == 1000.5


@pytest.mark.parametrize("side", ["BUY", "short", "", None])
def test_invalid_side_rejected(side):
    with pytest.raises(PaperExecutionPriceError):
        validate(quote(), side)


# ---------------- SOURCE AND MARKET CONTRACT ----------------

def test_last_trade_source_is_rejected_for_spot():
    with pytest.raises(PaperExecutionPriceError, match="source"):
        validate(quote(source="spot_trade"))


def test_mark_price_is_rejected_for_futures():
    with pytest.raises(PaperExecutionPriceError, match="source"):
        validate(futures_quote(source="futures_mark_price"))


def test_futures_requires_synchronized_depth_source():
    item = futures_quote(book_synchronized=False)

    with pytest.raises(PaperExecutionPriceError, match="synchronized"):
        validate(item)


def test_futures_requires_contiguous_depth_sequence():
    item = futures_quote(sequence_contiguous=False)

    with pytest.raises(PaperExecutionPriceError, match="continuous"):
        validate(item)


def test_spot_rejects_provider_event_timestamp():
    item = quote(event_time=NOW)

    with pytest.raises(PaperExecutionPriceError, match="timestamps"):
        validate(item)


def test_spot_rejects_wall_clock_receive_timestamp():
    item = quote(received_at=NOW)

    with pytest.raises(PaperExecutionPriceError, match="timestamps"):
        validate(item)


def test_spot_rejects_wall_clock_metadata_timestamp():
    item = quote(metadata_received_at=NOW)

    with pytest.raises(PaperExecutionPriceError, match="timestamps"):
        validate(item)


def test_spot_rejects_depth_synchronization_claim():
    item = quote(book_synchronized=True)

    with pytest.raises(PaperExecutionPriceError, match="synchronization"):
        validate(item)


def test_spot_rejects_sequence_continuity_claim():
    item = quote(sequence_contiguous=True)

    with pytest.raises(PaperExecutionPriceError, match="synchronization"):
        validate(item)


def test_spot_rejects_caller_supplied_wall_clock_policy():
    with pytest.raises(PaperExecutionPriceError, match="monotonic"):
        validate_executable_price(
            quote(),
            side="buy",
            tick_size=Decimal("0.10"),
            now=NOW,
            max_quote_age=timedelta(seconds=2),
            max_metadata_age=timedelta(minutes=10),
        )


# ---------------- SPOT UPDATE ID ----------------

@pytest.mark.parametrize("bad_update_id", [
    None,
    0,
    -1,
    True,
    False,
    1.0,
    "123",
])
def test_spot_update_id_must_be_positive_exact_integer(bad_update_id):
    with pytest.raises(PaperExecutionPriceError, match="update ID"):
        validate(quote(update_id=bad_update_id))


# ---------------- SPOT MONOTONIC RECEIVE TTL ----------------

def test_spot_receive_age_exactly_one_second_is_accepted():
    item = quote(received_at_monotonic=1000.0)

    result = validate(item)

    assert result.update_id == 123456


def test_spot_receive_age_over_one_second_is_rejected():
    item = quote(received_at_monotonic=999.999)

    with pytest.raises(PaperExecutionPriceError, match="TTL"):
        validate(item)


def test_spot_receive_time_in_monotonic_future_is_rejected():
    item = quote(received_at_monotonic=1001.001)

    with pytest.raises(PaperExecutionPriceError, match="future"):
        validate(item)


@pytest.mark.parametrize("bad_time", [
    True,
    False,
    float("nan"),
    float("inf"),
    float("-inf"),
    -1,
    "1000.5",
])
def test_spot_receive_monotonic_timestamp_rejects_invalid_values(bad_time):
    with pytest.raises(PaperExecutionPriceError):
        validate(quote(received_at_monotonic=bad_time))


# ---------------- SPOT METADATA MONOTONIC AGE ----------------

def test_spot_metadata_age_exactly_300_seconds_is_accepted():
    item = quote(metadata_received_at_monotonic=701.0)

    result = validate(item)

    assert result.price == Decimal("100000.00")


def test_spot_metadata_age_over_300_seconds_is_rejected():
    item = quote(metadata_received_at_monotonic=700.999)

    with pytest.raises(PaperExecutionPriceError, match="metadata"):
        validate(item)


def test_spot_metadata_timestamp_in_monotonic_future_is_rejected():
    item = quote(metadata_received_at_monotonic=1001.001)

    with pytest.raises(PaperExecutionPriceError, match="future"):
        validate(item)


@pytest.mark.parametrize("bad_time", [
    True,
    float("nan"),
    float("inf"),
    -1,
    "900",
])
def test_spot_metadata_monotonic_timestamp_rejects_invalid_values(bad_time):
    with pytest.raises(PaperExecutionPriceError):
        validate(quote(metadata_received_at_monotonic=bad_time))


def test_internal_spot_gate_uses_injected_monotonic_clock():
    item = quote(received_at_monotonic=1001.5)

    result = price_gate._validate_executable_price(
        item,
        side="buy",
        tick_size=Decimal("0.10"),
        current_monotonic=1002.0,
    )

    assert result.price == Decimal("100000.00")


def test_internal_spot_gate_rejects_quote_using_injected_stale_clock():
    item = quote(received_at_monotonic=1000.5)

    with pytest.raises(PaperExecutionPriceError, match="TTL"):
        price_gate._validate_executable_price(
            item,
            side="buy",
            tick_size=Decimal("0.10"),
            current_monotonic=1002.0,
        )

# ---------------- FUTURES WALL-CLOCK FRESHNESS ----------------

def test_futures_stale_quote_rejected():
    item = futures_quote(
        event_time=NOW - timedelta(seconds=3),
    )

    with pytest.raises(PaperExecutionPriceError, match="stale"):
        validate(item)


def test_futures_stale_metadata_rejected():
    item = futures_quote(
        metadata_received_at=NOW - timedelta(minutes=11),
    )

    with pytest.raises(PaperExecutionPriceError, match="metadata"):
        validate(item)


def test_futures_future_dated_quote_rejected():
    item = futures_quote(
        event_time=NOW + timedelta(seconds=3),
    )

    with pytest.raises(PaperExecutionPriceError, match="future"):
        validate(item)


def test_futures_naive_timestamp_rejected():
    item = futures_quote(
        event_time=datetime(2026, 11, 17, 12, 0),
    )

    with pytest.raises(PaperExecutionPriceError, match="timezone-aware"):
        validate(item)


def test_futures_result_preserves_event_time():
    item = futures_quote()

    result = validate(item)

    assert result.event_time == item.event_time
    assert result.update_id is None
    assert result.received_at_monotonic is None


def test_futures_rejects_spot_only_monotonic_evidence():
    item = futures_quote(received_at_monotonic=1000.5)

    with pytest.raises(PaperExecutionPriceError, match="Spot-only"):
        validate(item)


# ---------------- PRICE AND DECIMAL VALIDATION ----------------

def test_inverted_bid_ask_rejected():
    item = quote(
        bid=Decimal("100000.10"),
        ask=Decimal("100000.00"),
    )

    with pytest.raises(PaperExecutionPriceError, match="exceed"):
        validate(item)


@pytest.mark.parametrize(
    "field,value",
    [
        ("bid", Decimal("0")),
        ("ask", Decimal("-1")),
        ("bid", Decimal("NaN")),
        ("ask", Decimal("Infinity")),
        ("bid", 100000.0),
        ("ask", True),
    ],
)
def test_invalid_price_values_rejected(field, value):
    with pytest.raises(PaperExecutionPriceError):
        validate(quote(**{field: value}))


def test_tick_size_violation_rejected():
    item = quote(bid=Decimal("99999.91"))

    with pytest.raises(PaperExecutionPriceError, match="tick size"):
        validate(item)


def test_invalid_tick_size_rejected():
    with pytest.raises(PaperExecutionPriceError):
        validate_executable_price(
            quote(),
            side="buy",
            tick_size=Decimal("0"),
        )


def test_unknown_market_rejected():
    item = quote(market_type="margin")

    with pytest.raises(PaperExecutionPriceError, match="market"):
        validate(item)


def test_quote_must_be_exact_contract_type():
    with pytest.raises(PaperExecutionPriceError):
        validate({"market_type": "spot"})