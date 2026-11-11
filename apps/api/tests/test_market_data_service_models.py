import pytest

from app.market_data.freshness import FreshnessState
from app.market_data.service_models import (
    MarketDataGroup,
    MarketDataPipelineStatus,
    MarketDataServiceHealth,
    MarketDataServiceState,
    PipelineState,
)


def make_ready_spot(**overrides):
    values = {
        "group": MarketDataGroup.SPOT,
        "state": PipelineState.READY,
        "symbols": ("BTCUSDT",),
        "active_streams": ("btcusdt@trade",),
        "clock_synchronized": True,
        "metadata_fresh": True,
        "last_event_type": "trade",
        "last_event_timestamp_ms": 1_000_000,
        "last_event_received_at_monotonic": 10.0,
        "last_freshness_state": FreshnessState.FRESH,
        "updated_at_monotonic": 10.0,
    }
    values.update(overrides)
    return MarketDataPipelineStatus(**values)


def test_valid_ready_spot_pipeline():
    status = make_ready_spot()
    assert status.state is PipelineState.READY


def test_disabled_pipeline_requires_no_symbols():
    status = MarketDataPipelineStatus(
        group=MarketDataGroup.SPOT,
        state=PipelineState.DISABLED,
        symbols=(),
    )
    assert status.state is PipelineState.DISABLED


def test_disabled_pipeline_rejects_symbols():
    with pytest.raises(ValueError, match="Disabled pipelines"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT,
            state=PipelineState.DISABLED,
            symbols=("BTCUSDT",),
        )


def test_enabled_pipeline_requires_symbols():
    with pytest.raises(ValueError, match="require configured symbols"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.SPOT,
            state=PipelineState.BOOTSTRAPPING,
            symbols=(),
        )


def test_ready_requires_synchronized_clock():
    with pytest.raises(ValueError, match="synchronized exchange clock"):
        make_ready_spot(clock_synchronized=False)


def test_ready_requires_fresh_metadata():
    with pytest.raises(ValueError, match="fresh symbol metadata"):
        make_ready_spot(metadata_fresh=False)


def test_ready_requires_event():
    with pytest.raises(ValueError, match="verified market-data event"):
        make_ready_spot(last_event_type=None,
                        last_event_timestamp_ms=None,
                        last_event_received_at_monotonic=None)


def test_ready_requires_fresh_event():
    with pytest.raises(ValueError, match="FRESH market-data event"):
        make_ready_spot(last_freshness_state=FreshnessState.STALE)


def test_spot_cannot_claim_book_synchronization():
    with pytest.raises(ValueError, match="Only Futures Depth"):
        make_ready_spot(book_synchronized=True)


def test_active_streams_must_match_configured_symbols():
    with pytest.raises(ValueError, match="do not match"):
        make_ready_spot(active_streams=("ethusdt@trade",))


def test_depth_ready_requires_synchronized_book():
    with pytest.raises(ValueError, match="synchronized book"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.FUTURES_DEPTH,
            state=PipelineState.READY,
            symbols=("BTCUSDT",),
            active_streams=("btcusdt@depth",),
            clock_synchronized=True,
            metadata_fresh=True,
            last_event_type="depthUpdate",
            last_event_timestamp_ms=1_000_000,
            last_event_received_at_monotonic=10.0,
            updated_at_monotonic=10.0,
        )


def test_depth_does_not_accept_trade_freshness_state():
    with pytest.raises(ValueError, match="do not use trade freshness"):
        MarketDataPipelineStatus(
            group=MarketDataGroup.FUTURES_DEPTH,
            state=PipelineState.UNSYNCED,
            symbols=("BTCUSDT",),
            last_freshness_state=FreshnessState.FRESH,
        )


def test_depth_ready_with_explicit_book_sync():
    status = MarketDataPipelineStatus(
        group=MarketDataGroup.FUTURES_DEPTH,
        state=PipelineState.READY,
        symbols=("BTCUSDT",),
        active_streams=("btcusdt@depth",),
        clock_synchronized=True,
        metadata_fresh=True,
        book_synchronized=True,
        last_event_type="depthUpdate",
        last_event_timestamp_ms=1_000_000,
        last_event_received_at_monotonic=10.0,
        updated_at_monotonic=10.0,
    )
    assert status.book_synchronized is True


def test_event_fields_must_be_supplied_together():
    with pytest.raises(ValueError, match="supplied together"):
        make_ready_spot(last_event_timestamp_ms=None)


def test_event_receive_time_cannot_exceed_update_time():
    with pytest.raises(ValueError, match="cannot exceed"):
        make_ready_spot(
            last_event_received_at_monotonic=11.0,
            updated_at_monotonic=10.0,
        )


def test_invalid_symbol_is_rejected():
    with pytest.raises(ValueError, match="Invalid configured symbol"):
        make_ready_spot(symbols=("btc/usdt",))


def test_duplicate_symbols_are_rejected():
    with pytest.raises(ValueError, match="Duplicate configured symbols"):
        make_ready_spot(symbols=("BTCUSDT", "BTCUSDT"))


def test_duplicate_active_streams_are_rejected():
    with pytest.raises(ValueError, match="Duplicate active streams"):
        make_ready_spot(
            active_streams=("btcusdt@trade", "btcusdt@trade")
        )


def test_invalid_monotonic_timestamp_is_rejected():
    with pytest.raises(ValueError, match="finite and non-negative"):
        make_ready_spot(updated_at_monotonic=float("nan"))


def test_health_snapshot_accepts_independent_pipelines():
    spot = make_ready_spot()
    futures = MarketDataPipelineStatus(
        group=MarketDataGroup.FUTURES_MARK_PRICE,
        state=PipelineState.BOOTSTRAPPING,
        symbols=("BTCUSDT",),
    )

    health = MarketDataServiceHealth(
        state=MarketDataServiceState.DEGRADED,
        generated_at_monotonic=12.0,
        pipelines=(spot, futures),
    )

    assert len(health.pipelines) == 2


def test_health_snapshot_rejects_duplicate_groups():
    with pytest.raises(ValueError, match="Duplicate pipeline groups"):
        MarketDataServiceHealth(
            state=MarketDataServiceState.RUNNING,
            generated_at_monotonic=12.0,
            pipelines=(make_ready_spot(), make_ready_spot()),
        )


def test_health_snapshot_rejects_invalid_timestamp():
    with pytest.raises(ValueError, match="finite and non-negative"):
        MarketDataServiceHealth(
            state=MarketDataServiceState.STARTING,
            generated_at_monotonic=-1,
            pipelines=(),
        )


def test_models_are_immutable():
    status = make_ready_spot()
    with pytest.raises(AttributeError):
        status.state = PipelineState.FAILED

def test_ready_requires_active_subscriptions():
    with pytest.raises(ValueError, match="verified active subscriptions"):
        make_ready_spot(active_streams=())