"""Fail-closed executable bid/ask validation for Paper Trading.

Spot BookTicker and USD-M Futures use separate freshness contracts.

This module validates quotes supplied by a trusted market-data adapter.
It does not fetch market data, infer prices, or declare a provider trusted.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.market_data.service_models import (
    SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS,
)
from app.market_data.symbol_metadata import METADATA_MAX_AGE_SECONDS


class PaperExecutionPriceError(ValueError):
    """An executable price cannot be safely established."""


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    market_type: Literal["spot", "futures"]
    symbol: str
    source: str
    bid: Decimal
    ask: Decimal

    # Futures-only wall-clock evidence.
    event_time: datetime | None = None
    received_at: datetime | None = None
    metadata_received_at: datetime | None = None

    # Futures-only depth-book evidence.
    book_synchronized: bool | None = None
    sequence_contiguous: bool | None = None

    # Spot-only BookTicker evidence.
    update_id: int | None = None
    received_at_monotonic: float | None = None
    metadata_received_at_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class ValidatedExecutionPrice:
    market_type: str
    symbol: str
    side: Literal["buy", "sell"]
    price: Decimal
    bid: Decimal
    ask: Decimal
    source: str

    # None for Spot: Binance BookTicker does not provide event time.
    event_time: datetime | None

    # Present only for Spot BookTicker.
    update_id: int | None
    received_at_monotonic: float | None


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal:
        raise PaperExecutionPriceError(
            f"{field} must be supplied as an exact Decimal."
        )

    if not value.is_finite() or value <= 0 or value >= Decimal("1e36"):
        raise PaperExecutionPriceError(
            f"{field} is outside the permitted range."
        )

    if max(0, -value.as_tuple().exponent) > 36:
        raise PaperExecutionPriceError(
            f"{field} scale exceeds 36."
        )

    return value


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise PaperExecutionPriceError(
            f"{field} must be a datetime."
        )

    if value.tzinfo is None or value.utcoffset() is None:
        raise PaperExecutionPriceError(
            f"{field} must be timezone-aware."
        )

    return value


def _monotonic(value: object, field: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise PaperExecutionPriceError(
            f"{field} must be a monotonic numeric timestamp."
        )

    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise PaperExecutionPriceError(
            f"{field} is outside the permitted range."
        ) from None

    if not math.isfinite(result) or result < 0:
        raise PaperExecutionPriceError(
            f"{field} is invalid or non-finite."
        )

    return result


def _validate_spot_quote(
    quote: ExecutableQuote,
    *,
    current_monotonic: float,
    now: datetime | None,
    max_quote_age: timedelta | None,
    max_metadata_age: timedelta | None,
) -> tuple[datetime | None, int, float]:
    """Validate Spot BookTicker using local monotonic receive age only."""

    if quote.source != "spot_book_ticker":
        raise PaperExecutionPriceError(
            "Quote source is not approved for Spot."
        )

    # Spot BookTicker has no provider event timestamp.
    # Reject wall-clock timestamp fields rather than fabricating or using them.
    if (
        quote.event_time is not None
        or quote.received_at is not None
        or quote.metadata_received_at is not None
    ):
        raise PaperExecutionPriceError(
            "Spot BookTicker must not contain provider or wall-clock timestamps."
        )

    if (
        quote.book_synchronized is not None
        or quote.sequence_contiguous is not None
    ):
        raise PaperExecutionPriceError(
            "Spot BookTicker does not provide depth synchronization evidence."
        )

    # Spot TTL is fixed by the approved system contract.
    # Caller-supplied wall-clock freshness controls are prohibited.
    if (
        now is not None
        or max_quote_age is not None
        or max_metadata_age is not None
    ):
        raise PaperExecutionPriceError(
            "Spot freshness must use the fixed monotonic policy."
        )

    if type(quote.update_id) is not int or quote.update_id <= 0:
        raise PaperExecutionPriceError(
            "Spot BookTicker update ID must be a positive exact integer."
        )

    received = _monotonic(
        quote.received_at_monotonic,
        "received_at_monotonic",
    )

    metadata_received = _monotonic(
        quote.metadata_received_at_monotonic,
        "metadata_received_at_monotonic",
    )

    current = _monotonic(
        current_monotonic,
        "current monotonic time",
    )

    receive_age = current - received
    metadata_age = current - metadata_received

    if receive_age < 0:
        raise PaperExecutionPriceError(
            "Spot quote receive time is in the monotonic future."
        )

    if receive_age > SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS:
        raise PaperExecutionPriceError(
            "Spot BookTicker receive age exceeds the fixed TTL."
        )

    if metadata_age < 0:
        raise PaperExecutionPriceError(
            "Spot metadata time is in the monotonic future."
        )

    if metadata_age > METADATA_MAX_AGE_SECONDS:
        raise PaperExecutionPriceError(
            "Spot symbol metadata is stale."
        )

    return None, quote.update_id, received


def _validate_futures_quote(
    quote: ExecutableQuote,
    *,
    now: datetime | None,
    max_quote_age: timedelta | None,
    max_metadata_age: timedelta | None,
    max_future_skew: timedelta,
) -> tuple[datetime, None, None]:
    """Validate Futures using event time, receive time and depth sequence."""

    if quote.source != "futures_depth_book":
        raise PaperExecutionPriceError(
            "Quote source is not approved for Futures."
        )

    if (
        quote.update_id is not None
        or quote.received_at_monotonic is not None
        or quote.metadata_received_at_monotonic is not None
    ):
        raise PaperExecutionPriceError(
            "Futures quote contains Spot-only BookTicker evidence."
        )

    if type(quote.book_synchronized) is not bool:
        raise PaperExecutionPriceError(
            "Invalid order-book synchronization status."
        )

    if quote.book_synchronized is not True:
        raise PaperExecutionPriceError(
            "Order book is not synchronized."
        )

    if type(quote.sequence_contiguous) is not bool:
        raise PaperExecutionPriceError(
            "Invalid sequence-continuity status."
        )

    if quote.sequence_contiguous is not True:
        raise PaperExecutionPriceError(
            "Futures depth sequence is not continuous."
        )

    if not isinstance(now, datetime):
        raise PaperExecutionPriceError(
            "Futures wall-clock reference time is required."
        )

    if not isinstance(max_quote_age, timedelta) or max_quote_age <= timedelta(0):
        raise PaperExecutionPriceError(
            "Futures quote freshness limit must be positive."
        )

    if (
        not isinstance(max_metadata_age, timedelta)
        or max_metadata_age <= timedelta(0)
    ):
        raise PaperExecutionPriceError(
            "Futures metadata freshness limit must be positive."
        )

    if not isinstance(max_future_skew, timedelta):
        raise PaperExecutionPriceError(
            "Invalid Futures future-skew limit."
        )

    if max_future_skew < timedelta(0):
        raise PaperExecutionPriceError(
            "Future-skew limit cannot be negative."
        )

    current = _aware(now, "now")
    event_time = _aware(quote.event_time, "event_time")
    received_at = _aware(quote.received_at, "received_at")
    metadata_time = _aware(
        quote.metadata_received_at,
        "metadata_received_at",
    )

    if event_time > current + max_future_skew:
        raise PaperExecutionPriceError(
            "Quote event time is in the future."
        )

    if received_at > current + max_future_skew:
        raise PaperExecutionPriceError(
            "Quote receive time is in the future."
        )

    if metadata_time > current + max_future_skew:
        raise PaperExecutionPriceError(
            "Symbol metadata time is in the future."
        )

    if current - event_time > max_quote_age:
        raise PaperExecutionPriceError(
            "Executable quote is stale."
        )

    if current - received_at > max_quote_age:
        raise PaperExecutionPriceError(
            "Locally received quote is stale."
        )

    if current - metadata_time > max_metadata_age:
        raise PaperExecutionPriceError(
            "Exchange symbol metadata is stale."
        )

    if event_time > received_at + max_future_skew:
        raise PaperExecutionPriceError(
            "Quote event time exceeds receive-time skew."
        )

    return event_time, None, None


def _validate_executable_price(
    quote: ExecutableQuote,
    *,
    side: Literal["buy", "sell"],
    tick_size: Decimal,
    current_monotonic: float,
    now: datetime | None = None,
    max_quote_age: timedelta | None = None,
    max_metadata_age: timedelta | None = None,
    max_future_skew: timedelta = timedelta(seconds=1),
) -> ValidatedExecutionPrice:
    """Validate a trusted quote and select ask for BUY / bid for SELL.

    Spot:
      - Requires Spot BookTicker source and positive update ID.
      - Uses a fixed one-second monotonic receive-age TTL.
      - Uses monotonic metadata age, capped by the metadata contract.
      - Does not accept or invent provider event timestamps.

    Futures:
      - Requires synchronized depth and contiguous sequence.
      - Retains event-time, receive-time and metadata wall-clock checks.
    """

    if not isinstance(quote, ExecutableQuote):
        raise PaperExecutionPriceError(
            "A validated quote object is required."
        )

    if not isinstance(side, str) or side not in {"buy", "sell"}:
        raise PaperExecutionPriceError(
            "Unsupported execution side."
        )

    if (
        not isinstance(quote.market_type, str)
        or quote.market_type not in {"spot", "futures"}
    ):
        raise PaperExecutionPriceError(
            "Unsupported market type."
        )

    if not isinstance(quote.symbol, str) or not quote.symbol.strip():
        raise PaperExecutionPriceError(
            "A valid symbol is required."
        )

    bid = _decimal(quote.bid, "bid")
    ask = _decimal(quote.ask, "ask")
    tick = _decimal(tick_size, "tick_size")

    if bid > ask:
        raise PaperExecutionPriceError(
            "Bid cannot exceed ask."
        )

    if bid % tick != 0 or ask % tick != 0:
        raise PaperExecutionPriceError(
            "Bid or ask violates the exchange tick size."
        )

    if quote.market_type == "spot":
        event_time, update_id, received_monotonic = _validate_spot_quote(
            quote,
            current_monotonic=current_monotonic,
            now=now,
            max_quote_age=max_quote_age,
            max_metadata_age=max_metadata_age,
        )
    else:
        event_time, update_id, received_monotonic = _validate_futures_quote(
            quote,
            now=now,
            max_quote_age=max_quote_age,
            max_metadata_age=max_metadata_age,
            max_future_skew=max_future_skew,
        )

    selected = ask if side == "buy" else bid

    return ValidatedExecutionPrice(
        market_type=quote.market_type,
        symbol=quote.symbol,
        side=side,
        price=selected,
        bid=bid,
        ask=ask,
        source=quote.source,
        event_time=event_time,
        update_id=update_id,
        received_at_monotonic=received_monotonic,
    )
def validate_executable_price(
    quote: ExecutableQuote,
    *,
    side: Literal["buy", "sell"],
    tick_size: Decimal,
    now: datetime | None = None,
    max_quote_age: timedelta | None = None,
    max_metadata_age: timedelta | None = None,
    max_future_skew: timedelta = timedelta(seconds=1),
) -> ValidatedExecutionPrice:
    """Validate using the process monotonic clock.

    Trusted market-data adapters should use the private helper with
    their service-owned monotonic reference.
    """
    return _validate_executable_price(
        quote,
        side=side,
        tick_size=tick_size,
        current_monotonic=time.monotonic(),
        now=now,
        max_quote_age=max_quote_age,
        max_metadata_age=max_metadata_age,
        max_future_skew=max_future_skew,
    )