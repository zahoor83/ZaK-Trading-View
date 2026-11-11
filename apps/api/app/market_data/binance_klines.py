"""Strict normalized parser for Binance Spot and USD-M Futures klines."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.market_data.binance_spot_rest import (
    SUPPORTED_INTERVALS as SPOT_INTERVALS,
)
from app.market_data.binance_usdm_futures_rest import (
    SUPPORTED_INTERVALS as FUTURES_INTERVALS,
)


Market = Literal["spot", "usdm_futures"]

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+$")
_DECIMAL_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_MAX_DECIMAL_TEXT_LENGTH = 128


class KlineParseError(ValueError):
    """Raised when a Binance Kline row cannot be safely normalized."""


@dataclass(frozen=True, slots=True)
class BinanceCandle:
    """Immutable normalized Binance OHLCV candle."""

    exchange: Literal["BINANCE"]
    market: Market
    symbol: str
    interval: str

    open_time: int
    close_time: int

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    volume: Decimal
    quote_volume: Decimal
    trade_count: int

    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal


def _validate_metadata(
    market: str,
    symbol: str,
    interval: str,
) -> None:
    if market not in ("spot", "usdm_futures"):
        raise KlineParseError("Unsupported Binance market.")

    if not isinstance(symbol, str) or not _SYMBOL_PATTERN.fullmatch(symbol):
        raise KlineParseError("Invalid Binance symbol.")

    if not isinstance(interval, str):
        raise KlineParseError("Invalid Binance Kline interval.")

    supported = (
        SPOT_INTERVALS if market == "spot" else FUTURES_INTERVALS
    )

    if interval not in supported:
        raise KlineParseError("Unsupported interval for selected market.")


def _parse_timestamp(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise KlineParseError(f"Invalid {field} timestamp.")

    return value


def _parse_trade_count(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise KlineParseError("Invalid Kline trade count.")

    return value


def _parse_decimal(value: object, field: str) -> Decimal:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_DECIMAL_TEXT_LENGTH
        or not _DECIMAL_PATTERN.fullmatch(value)
    ):
        raise KlineParseError(f"Invalid decimal field: {field}.")

    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise KlineParseError(
            f"Invalid decimal field: {field}."
        ) from exc

    if not parsed.is_finite():
        raise KlineParseError(f"Non-finite decimal field: {field}.")

    return parsed


def parse_binance_klines(
    rows: list | tuple,
    *,
    market: Market,
    symbol: str,
    interval: str,
) -> tuple[BinanceCandle, ...]:
    """
    Convert raw Binance 12-field Kline rows into immutable candles.

    This parser validates individual candle records only.
    It deliberately does not sort, deduplicate, detect gaps,
    or decide whether the latest candle is fresh or complete.
    """

    _validate_metadata(market, symbol, interval)

    if not isinstance(rows, (list, tuple)):
        raise KlineParseError("Kline response must be a list or tuple.")

    candles: list[BinanceCandle] = []

    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 12:
            raise KlineParseError(
                f"Kline row {row_index} must contain exactly 12 fields."
            )

        open_time = _parse_timestamp(row[0], "open_time")
        close_time = _parse_timestamp(row[6], "close_time")

        if close_time <= open_time:
            raise KlineParseError(
                f"Kline row {row_index} has invalid time bounds."
            )

        open_price = _parse_decimal(row[1], "open")
        high_price = _parse_decimal(row[2], "high")
        low_price = _parse_decimal(row[3], "low")
        close_price = _parse_decimal(row[4], "close")

        volume = _parse_decimal(row[5], "volume")
        quote_volume = _parse_decimal(row[7], "quote_volume")

        trade_count = _parse_trade_count(row[8])

        taker_buy_base = _parse_decimal(
            row[9], "taker_buy_base_volume"
        )
        taker_buy_quote = _parse_decimal(
            row[10], "taker_buy_quote_volume"
        )

        if not isinstance(row[11], str) or not row[11]:
            raise KlineParseError(
                f"Kline row {row_index} has an invalid final field."
            )

        prices = (
            open_price,
            high_price,
            low_price,
            close_price,
        )

        if any(price <= 0 for price in prices):
            raise KlineParseError(
                f"Kline row {row_index} contains a non-positive price."
            )

        if (
            low_price > high_price
            or low_price > open_price
            or low_price > close_price
            or high_price < open_price
            or high_price < close_price
        ):
            raise KlineParseError(
                f"Kline row {row_index} violates OHLC relationships."
            )

        volumes = (
            volume,
            quote_volume,
            taker_buy_base,
            taker_buy_quote,
        )

        if any(value < 0 for value in volumes):
            raise KlineParseError(
                f"Kline row {row_index} contains negative volume."
            )

        if taker_buy_base > volume:
            raise KlineParseError(
                f"Kline row {row_index} taker-buy base volume exceeds total."
            )

        if taker_buy_quote > quote_volume:
            raise KlineParseError(
                f"Kline row {row_index} taker-buy quote volume exceeds total."
            )

        candles.append(
            BinanceCandle(
                exchange="BINANCE",
                market=market,
                symbol=symbol,
                interval=interval,
                open_time=open_time,
                close_time=close_time,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                quote_volume=quote_volume,
                trade_count=trade_count,
                taker_buy_base_volume=taker_buy_base,
                taker_buy_quote_volume=taker_buy_quote,
            )
        )

    return tuple(candles)