"""Sequence-level integrity validation for Binance candles.

This module does not sort, mutate, deduplicate, or repair candle data.
Missing candles are reported as compact gap records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from app.market_data.binance_klines import BinanceCandle
from app.market_data.binance_spot_rest import (
    SUPPORTED_INTERVALS as SPOT_SUPPORTED_INTERVALS,
)
from app.market_data.binance_usdm_futures_rest import (
    SUPPORTED_INTERVALS as FUTURES_SUPPORTED_INTERVALS,
)


class CandleSequenceError(ValueError):
    """Raised when a candle sequence violates structural integrity."""


@dataclass(frozen=True, slots=True)
class CandleGap:
    previous_open_time: int
    next_open_time: int
    first_missing_open_time: int
    missing_count: int


@dataclass(frozen=True, slots=True)
class CandleSequenceReport:
    candle_count: int
    gaps: tuple[CandleGap, ...]
    is_complete: bool


_FIXED_INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}

_DAY_MS = 86_400_000


def _utc_datetime(timestamp_ms: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp_ms // 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise CandleSequenceError("timestamp_out_of_datetime_range") from exc


def _month_start(timestamp_ms: int) -> datetime:
    value = _utc_datetime(timestamp_ms)

    if timestamp_ms % 1000 != 0:
        raise CandleSequenceError("monthly_open_has_subsecond_component")

    if value.day != 1 or value.hour != 0 or value.minute != 0 or value.second != 0:
        raise CandleSequenceError("monthly_open_not_utc_month_boundary")

    return value


def _next_month_open(timestamp_ms: int) -> int:
    current = _month_start(timestamp_ms)

    if current.month == 12:
        next_month = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

    return int(next_month.timestamp() * 1000)


def _validate_open_alignment(open_time: int, interval: str) -> None:
    if isinstance(open_time, bool) or not isinstance(open_time, int) or open_time < 0:
        raise CandleSequenceError("invalid_open_timestamp")

    if interval == "1M":
        _month_start(open_time)
        return

    if interval == "1w":
        value = _utc_datetime(open_time)

        if (
            open_time % 1000 != 0
            or value.weekday() != 0
            or value.hour != 0
            or value.minute != 0
            or value.second != 0
        ):
            raise CandleSequenceError("weekly_open_not_monday_utc")

        return

    if interval == "3d":
        value = _utc_datetime(open_time)

        if (
            open_time % 1000 != 0
            or value.hour != 0
            or value.minute != 0
            or value.second != 0
        ):
            raise CandleSequenceError("three_day_open_not_utc_midnight")

        return

    duration = _FIXED_INTERVAL_MS.get(interval)

    if duration is None:
        raise CandleSequenceError("unsupported_interval")

    if open_time % duration != 0:
        raise CandleSequenceError("open_timestamp_not_interval_aligned")


def _expected_close_time(open_time: int, interval: str) -> int:
    if interval == "1M":
        return _next_month_open(open_time) - 1

    duration = _FIXED_INTERVAL_MS.get(interval)

    if duration is None:
        raise CandleSequenceError("unsupported_interval")

    return open_time + duration - 1


def _next_open_time(open_time: int, interval: str) -> int:
    if interval == "1M":
        return _next_month_open(open_time)

    duration = _FIXED_INTERVAL_MS.get(interval)

    if duration is None:
        raise CandleSequenceError("unsupported_interval")

    return open_time + duration


def _missing_count(previous: int, current: int, interval: str) -> int:
    if interval == "1M":
        previous_dt = _month_start(previous)
        current_dt = _month_start(current)

        month_delta = (
            (current_dt.year - previous_dt.year) * 12
            + current_dt.month
            - previous_dt.month
        )

        if month_delta <= 0:
            raise CandleSequenceError("non_increasing_monthly_timestamp")

        return month_delta - 1

    duration = _FIXED_INTERVAL_MS.get(interval)

    if duration is None:
        raise CandleSequenceError("unsupported_interval")

    delta = current - previous

    if delta <= 0 or delta % duration != 0:
        raise CandleSequenceError("timestamp_spacing_not_interval_multiple")

    return (delta // duration) - 1


def validate_candle_sequence(
    candles: Sequence[BinanceCandle],
) -> CandleSequenceReport:
    """Validate candle ordering, identity, alignment, close times and gaps.

    Empty input is rejected. Gaps are reported, not repaired.
    Duplicate timestamps and out-of-order records are hard errors.
    """

    try:
        records = tuple(candles)
    except TypeError as exc:
        raise CandleSequenceError("invalid_candle_sequence_container") from exc

    if not records:
        raise CandleSequenceError("empty_candle_sequence")

    first = records[0]

    if not isinstance(first, BinanceCandle):
        raise CandleSequenceError("invalid_candle_record_type")

    identity = (
        first.exchange,
        first.market,
        first.symbol,
        first.interval,
    )

    if first.market == "spot":
        supported = SPOT_SUPPORTED_INTERVALS
    elif first.market == "usdm_futures":
        supported = FUTURES_SUPPORTED_INTERVALS
    else:
        raise CandleSequenceError("unsupported_market")

    if first.interval not in supported:
        raise CandleSequenceError("unsupported_market_interval")

    if first.interval not in _FIXED_INTERVAL_MS and first.interval != "1M":
        raise CandleSequenceError("interval_duration_not_supported")

    seen_open_times: set[int] = set()

    for candle in records:
        if not isinstance(candle, BinanceCandle):
            raise CandleSequenceError("invalid_candle_record_type")

        current_identity = (
            candle.exchange,
            candle.market,
            candle.symbol,
            candle.interval,
        )

        if current_identity != identity:
            raise CandleSequenceError("mixed_candle_metadata")

        open_time = candle.open_time
        close_time = candle.close_time

        if isinstance(open_time, bool) or not isinstance(open_time, int) or open_time < 0:
            raise CandleSequenceError("invalid_open_timestamp")

        if isinstance(close_time, bool) or not isinstance(close_time, int) or close_time < 0:
            raise CandleSequenceError("invalid_close_timestamp")

        if open_time in seen_open_times:
            raise CandleSequenceError("duplicate_candle_open_time")

        seen_open_times.add(open_time)

    gaps: list[CandleGap] = []

    previous = None

    for candle in records:
        _validate_open_alignment(candle.open_time, candle.interval)

        expected_close = _expected_close_time(
            candle.open_time,
            candle.interval,
        )

        if candle.close_time != expected_close:
            raise CandleSequenceError("candle_close_time_duration_mismatch")

        if previous is not None:
            if candle.open_time < previous.open_time:
                raise CandleSequenceError("out_of_order_candle_sequence")

            expected_next = _next_open_time(
                previous.open_time,
                candle.interval,
            )

            if candle.open_time < expected_next:
                raise CandleSequenceError("overlapping_candle_sequence")

            if candle.open_time > expected_next:
                count = _missing_count(
                    previous.open_time,
                    candle.open_time,
                    candle.interval,
                )

                if count <= 0:
                    raise CandleSequenceError("invalid_gap_calculation")

                gaps.append(
                    CandleGap(
                        previous_open_time=previous.open_time,
                        next_open_time=candle.open_time,
                        first_missing_open_time=expected_next,
                        missing_count=count,
                    )
                )

        previous = candle

    immutable_gaps = tuple(gaps)

    return CandleSequenceReport(
        candle_count=len(records),
        gaps=immutable_gaps,
        is_complete=not immutable_gaps,
    )