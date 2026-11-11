from dataclasses import FrozenInstanceError

import pytest

from app.market_data.binance_klines import BinanceCandle
from app.market_data.candle_sequence import (
    CandleGap,
    CandleSequenceError,
    validate_candle_sequence,
)


def make_candle(open_time: int, interval: str = "1m", **overrides):
    duration = {
        "1s": 1_000,
        "1m": 60_000,
        "3m": 180_000,
        "1h": 3_600_000,
        "1d": 86_400_000,
        "3d": 259_200_000,
        "1w": 604_800_000,
    }.get(interval)

    if interval == "1M":
        from datetime import datetime, timezone

        current = datetime.fromtimestamp(open_time // 1000, timezone.utc)

        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)

        close_time = int(next_month.timestamp() * 1000) - 1
    else:
        assert duration is not None
        close_time = open_time + duration - 1

    values = {
        "exchange": "BINANCE",
        "market": "spot",
        "symbol": "BTCUSDT",
        "interval": interval,
        "open_time": open_time,
        "close_time": close_time,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": "10",
        "quote_volume": "1000",
        "trade_count": 5,
        "taker_buy_base_volume": "4",
        "taker_buy_quote_volume": "400",
    }

    values.update(overrides)
    return BinanceCandle(**values)


def test_valid_contiguous_sequence():
    candles = (
        make_candle(0),
        make_candle(60_000),
        make_candle(120_000),
    )

    report = validate_candle_sequence(candles)

    assert report.candle_count == 3
    assert report.gaps == ()
    assert report.is_complete is True


def test_empty_sequence_is_rejected():
    with pytest.raises(CandleSequenceError, match="empty_candle_sequence"):
        validate_candle_sequence(())


def test_duplicate_non_adjacent_open_time_is_rejected():
    candles = (
        make_candle(0),
        make_candle(60_000),
        make_candle(0),
    )

    with pytest.raises(CandleSequenceError, match="duplicate_candle_open_time"):
        validate_candle_sequence(candles)


def test_out_of_order_sequence_is_rejected():
    candles = (
        make_candle(60_000),
        make_candle(0),
    )

    with pytest.raises(CandleSequenceError, match="out_of_order_candle_sequence"):
        validate_candle_sequence(candles)


def test_gap_is_reported_without_filling():
    candles = (
        make_candle(0),
        make_candle(180_000),
    )

    report = validate_candle_sequence(candles)

    assert report.is_complete is False
    assert report.gaps == (
        CandleGap(
            previous_open_time=0,
            next_open_time=180_000,
            first_missing_open_time=60_000,
            missing_count=2,
        ),
    )
    assert report.candle_count == 2


def test_multiple_gaps_are_reported():
    candles = (
        make_candle(0),
        make_candle(180_000),
        make_candle(360_000),
    )

    report = validate_candle_sequence(candles)

    assert len(report.gaps) == 2
    assert [gap.missing_count for gap in report.gaps] == [2, 2]


def test_mixed_symbol_is_rejected():
    candles = (
        make_candle(0),
        make_candle(60_000, symbol="ETHUSDT"),
    )

    with pytest.raises(CandleSequenceError, match="mixed_candle_metadata"):
        validate_candle_sequence(candles)


def test_mixed_market_is_rejected():
    candles = (
        make_candle(0),
        make_candle(60_000, market="usdm_futures"),
    )

    with pytest.raises(CandleSequenceError, match="mixed_candle_metadata"):
        validate_candle_sequence(candles)


def test_wrong_close_time_is_rejected():
    candles = (
        make_candle(0, close_time=59_998),
    )

    with pytest.raises(
        CandleSequenceError,
        match="candle_close_time_duration_mismatch",
    ):
        validate_candle_sequence(candles)


def test_misaligned_minute_open_is_rejected():
    candles = (make_candle(1),)

    with pytest.raises(
        CandleSequenceError,
        match="open_timestamp_not_interval_aligned",
    ):
        validate_candle_sequence(candles)


def test_three_day_candles_require_utc_midnight():
    candles = (make_candle(1, interval="3d"),)

    with pytest.raises(
        CandleSequenceError,
        match="three_day_open_not_utc_midnight",
    ):
        validate_candle_sequence(candles)


def test_three_day_gap_uses_three_day_cadence():
    duration = 259_200_000
    candles = (
        make_candle(0, interval="3d"),
        make_candle(duration * 3, interval="3d"),
    )

    report = validate_candle_sequence(candles)

    assert report.is_complete is False
    assert report.gaps[0].missing_count == 2
    assert report.gaps[0].first_missing_open_time == duration


def test_three_day_non_multiple_spacing_is_rejected():
    duration = 259_200_000
    candles = (
        make_candle(0, interval="3d"),
        make_candle(duration + 86_400_000, interval="3d"),
    )

    with pytest.raises(
        CandleSequenceError,
        match="timestamp_spacing_not_interval_multiple",
    ):
        validate_candle_sequence(candles)


def test_weekly_open_must_be_monday_utc():
    # 2024-01-02 00:00 UTC is Tuesday.
    from datetime import datetime, timezone

    timestamp = int(
        datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp() * 1000
    )

    candles = (make_candle(timestamp, interval="1w"),)

    with pytest.raises(
        CandleSequenceError,
        match="weekly_open_not_monday_utc",
    ):
        validate_candle_sequence(candles)


def test_monthly_gap_is_calendar_aware():
    from datetime import datetime, timezone

    january = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    april = int(datetime(2024, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)

    report = validate_candle_sequence(
        (
            make_candle(january, interval="1M"),
            make_candle(april, interval="1M"),
        )
    )

    assert report.is_complete is False
    assert report.gaps[0].missing_count == 2

    march = int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert report.gaps[0].first_missing_open_time == (
        int(datetime(2024, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
    )
    assert march > report.gaps[0].first_missing_open_time


def test_monthly_open_must_be_first_day_utc():
    from datetime import datetime, timezone

    timestamp = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)

    candles = (make_candle(timestamp, interval="1M"),)

    with pytest.raises(
        CandleSequenceError,
        match="monthly_open_not_utc_month_boundary",
    ):
        validate_candle_sequence(candles)


def test_report_and_gap_are_immutable():
    report = validate_candle_sequence(
        (
            make_candle(0),
            make_candle(120_000),
        )
    )

    with pytest.raises(FrozenInstanceError):
        report.candle_count = 99

    with pytest.raises(FrozenInstanceError):
        report.gaps[0].missing_count = 99


def test_input_order_is_not_modified():
    candles = [
        make_candle(0),
        make_candle(60_000),
        make_candle(120_000),
    ]
    original = tuple(candles)

    validate_candle_sequence(candles)

    assert tuple(candles) == original


def test_non_candle_record_is_rejected():
    with pytest.raises(CandleSequenceError, match="invalid_candle_record_type"):
        validate_candle_sequence((object(),))


def test_unsupported_futures_interval_is_rejected():
    candles = (
        make_candle(0, interval="1s", market="usdm_futures"),
    )

    with pytest.raises(CandleSequenceError, match="unsupported_market_interval"):
        validate_candle_sequence(candles)