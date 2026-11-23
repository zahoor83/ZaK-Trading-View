import pytest

from app.market_data.freshness import FreshnessState
from app.market_data.service_models import (
    MarketDataGroup,
    MarketDataPipelineStatus,
    PipelineState,
)


def test_book_ticker_pipeline_accepts_timestamp_free_receive_metadata():
    status = MarketDataPipelineStatus(
        group=MarketDataGroup.SPOT_BOOK_TICKER,
        state=PipelineState.SUBSCRIBING,
        symbols=("BTCUSDT",),
        active_streams=("btcusdt@bookTicker",),
        clock_synchronized=False,
        metadata_fresh=True,
        last_update_id=123,
        last_event_received_at_monotonic=50.0,
        updated_at_monotonic=50.0,
    )
    assert status.last_update_id == 123
    assert status.last_event_type is None
    assert status.last_event_timestamp_ms is None
    assert status.last_freshness_state is None
    assert status.clock_synchronized is False


def test_book_ticker_can_exist_without_first_event():
    status = MarketDataPipelineStatus(
        group=MarketDataGroup.SPOT_BOOK_TICKER,
        state=PipelineState.SUBSCRIBING,
        symbols=("BTCUSDT",),
        active_streams=("btcusdt@bookTicker",),
        updated_at_monotonic=10.0,
    )
    assert status.last_update_id is None
    assert status.last_event_received_at_monotonic is None


@pytest.mark.parametrize("bad_id", [0, -1, True, 1.5, "12"])
def test_book_ticker_rejects_invalid_update_id(bad_id):
    with pytest.raises(ValueError, match="update ID"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT_BOOK_TICKER,
            state=PipelineState.SUBSCRIBING,
            symbols=("BTCUSDT",),
            last_update_id=bad_id,
            last_event_received_at_monotonic=10.0,
            updated_at_monotonic=10.0,
        )


def test_book_ticker_requires_update_id_and_receive_time_together():
    with pytest.raises(ValueError, match="supplied together"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT_BOOK_TICKER,
            state=PipelineState.SUBSCRIBING,
            symbols=("BTCUSDT",),
            last_update_id=123,
            updated_at_monotonic=10.0,
        )


def test_book_ticker_rejects_provider_timestamp_or_event_type_claims():
    with pytest.raises(ValueError, match="cannot claim"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT_BOOK_TICKER,
            state=PipelineState.SUBSCRIBING,
            symbols=("BTCUSDT",),
            last_event_type="bookTicker",
            last_event_timestamp_ms=123,
            last_update_id=123,
            last_event_received_at_monotonic=10.0,
            updated_at_monotonic=10.0,
        )


def test_book_ticker_rejects_trade_freshness_claim():
    with pytest.raises(ValueError, match="trade freshness result"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT_BOOK_TICKER,
            state=PipelineState.SUBSCRIBING,
            symbols=("BTCUSDT",),
            last_freshness_state=FreshnessState.FRESH,
            updated_at_monotonic=10.0,
        )


def test_book_ticker_ready_requires_fresh_quote_evidence():
    with pytest.raises(ValueError, match="fresh quote evidence"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT_BOOK_TICKER,
            state=PipelineState.READY,
            symbols=("BTCUSDT",),
            active_streams=("btcusdt@bookTicker",),
            metadata_fresh=True,
            last_update_id=123,
            last_event_received_at_monotonic=10.0,
            updated_at_monotonic=10.0,
        )


def test_other_pipeline_rejects_book_ticker_update_id():
    with pytest.raises(ValueError, match="only valid for Spot BookTicker"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT,
            state=PipelineState.SUBSCRIBING,
            symbols=("BTCUSDT",),
            last_update_id=123,
            updated_at_monotonic=10.0,
        )


def test_existing_spot_event_contract_remains_timestamp_based():
    status = MarketDataPipelineStatus(
        group=MarketDataGroup.SPOT,
        state=PipelineState.READY,
        symbols=("BTCUSDT",),
        active_streams=("btcusdt@trade",),
        clock_synchronized=True,
        metadata_fresh=True,
        last_event_type="trade",
        last_event_timestamp_ms=1000,
        last_event_received_at_monotonic=10.0,
        last_freshness_state=FreshnessState.FRESH,
        updated_at_monotonic=10.0,
    )
    assert status.last_event_timestamp_ms == 1000
