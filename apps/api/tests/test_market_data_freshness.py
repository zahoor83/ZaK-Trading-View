import pytest

from app.market_data.freshness import (
    ClockSynchronizationError,
    ExchangeClock,
    FreshnessState,
    MarketDataFreshnessValidator,
)


class FakeTime:
    def __init__(self):
        self.wall = 1_000_000
        self.mono = 10.0

    def wall_ms(self):
        return self.wall

    def monotonic(self):
        return self.mono


def make_clock(*, offset_ms=0, rtt_seconds=0.0):
    fake = FakeTime()
    clock = ExchangeClock(
        wall_clock_ms=fake.wall_ms,
        monotonic=fake.monotonic,
    )

    def provider():
        fake.mono += rtt_seconds
        server_time = fake.wall + int(rtt_seconds * 500) + offset_ms
        return {"serverTime": server_time}

    clock.synchronize(provider)
    return fake, clock


def test_clock_synchronizes_and_estimates_offset():
    _, clock = make_clock(offset_ms=250)
    assert clock.snapshot is not None
    assert clock.snapshot.offset_ms == 250
    assert clock.is_synchronized


def test_clock_uses_round_trip_uncertainty():
    _, clock = make_clock(offset_ms=100, rtt_seconds=0.2)
    assert clock.snapshot.uncertainty_ms == pytest.approx(100.0)


def test_clock_rejects_excessive_offset():
    fake = FakeTime()
    clock = ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic)

    with pytest.raises(ClockSynchronizationError):
        clock.synchronize(lambda: {"serverTime": fake.wall + 1001})

    assert clock.snapshot is None
    assert not clock.is_synchronized


def test_clock_rejects_offset_when_uncertainty_exceeds_budget():
    fake = FakeTime()
    clock = ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic)

    def provider():
        fake.mono += 0.2
        return {"serverTime": fake.wall + 950 + 100}

    with pytest.raises(ClockSynchronizationError):
        clock.synchronize(provider)


@pytest.mark.parametrize("payload", [
    None,
    [],
    {},
    {"serverTime": True},
    {"serverTime": -1},
    {"serverTime": "1000000"},
])
def test_clock_rejects_invalid_server_time_payload(payload):
    fake = FakeTime()
    clock = ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic)

    with pytest.raises(ClockSynchronizationError):
        clock.synchronize(lambda: payload)

    assert clock.snapshot is None


def test_resynchronization_keeps_previous_snapshot_until_success():
    fake, clock = make_clock()
    previous_snapshot = clock.snapshot
    observed_snapshots = []

    def provider():
        observed_snapshots.append(clock.snapshot)
        return {"serverTime": fake.wall}

    refreshed_snapshot = clock.synchronize(provider)

    assert observed_snapshots
    assert all(
        snapshot is previous_snapshot
        for snapshot in observed_snapshots
    )
    assert refreshed_snapshot is clock.snapshot
    assert refreshed_snapshot is not previous_snapshot
    assert clock.is_synchronized


def test_failed_resynchronization_invalidates_previous_snapshot():
    _, clock = make_clock()
    assert clock.snapshot is not None

    with pytest.raises(ClockSynchronizationError):
        clock.synchronize(lambda: (_ for _ in ()).throw(RuntimeError("network")))

    assert clock.snapshot is None
    assert not clock.is_synchronized


def test_clock_expires_after_sixty_seconds():
    fake, clock = make_clock()
    fake.mono += 60.001

    with pytest.raises(ClockSynchronizationError):
        clock.now_exchange_ms()


def test_clock_does_not_allow_time_before_sync_anchor():
    fake, clock = make_clock()

    with pytest.raises(ClockSynchronizationError):
        clock.now_exchange_ms(at_monotonic=9.0)


def test_clock_rejects_invalid_monotonic_value():
    _, clock = make_clock()

    with pytest.raises(ClockSynchronizationError):
        clock.now_exchange_ms(at_monotonic=float("nan"))


def test_trade_event_is_fresh_within_threshold():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    timestamp = clock.now_exchange_ms() - 5000

    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.FRESH
    assert result.age_ms == 5000


def test_mark_price_event_is_fresh_within_threshold():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    timestamp = clock.now_exchange_ms() - 5000

    result = validator.validate_event("markPriceUpdate", timestamp)

    assert result.state is FreshnessState.FRESH


def test_exact_stale_threshold_is_accepted():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    timestamp = clock.now_exchange_ms() - 10_000

    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.FRESH
    assert result.age_ms == 10_000


def test_event_just_beyond_stale_threshold_is_rejected():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    timestamp = clock.now_exchange_ms() - 10_001

    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.STALE
    assert result.reason == "event_exceeds_stale_threshold"


def test_exact_future_skew_boundary_is_accepted():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    timestamp = clock.now_exchange_ms() + 2000

    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.FRESH


def test_event_beyond_future_skew_is_invalid():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    timestamp = clock.now_exchange_ms() + 2001

    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.INVALID
    assert result.reason == "event_exceeds_future_skew"


@pytest.mark.parametrize("timestamp", [True, False, 0, -1, "1000000", None, 1.5])
def test_invalid_event_timestamps_fail_closed(timestamp):
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)

    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.INVALID


def test_unsupported_event_type_is_invalid():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)

    result = validator.validate_event("depthUpdate", 1_000_000)

    assert result.state is FreshnessState.INVALID
    assert result.reason == "unsupported_event_type"


def test_unsynchronized_clock_fails_closed():
    fake = FakeTime()
    clock = ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic)
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)

    result = validator.validate_event("trade", 1_000_000)

    assert result.state is FreshnessState.CLOCK_UNSYNCED


def test_expired_clock_fails_closed():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    fake.mono += 60.001

    result = validator.validate_event("trade", 1_000_000)

    assert result.state is FreshnessState.CLOCK_UNSYNCED


def test_receive_time_uses_monotonic_clock():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)
    received = fake.mono - 1.25
    timestamp = clock.now_exchange_ms() - 1000

    result = validator.validate_event(
        "trade",
        timestamp,
        received_at_monotonic=received,
    )

    assert result.state is FreshnessState.FRESH
    assert result.processing_delay_ms == pytest.approx(1250)


def test_future_receive_monotonic_time_is_invalid():
    fake, clock = make_clock()
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)

    result = validator.validate_event(
        "trade",
        clock.now_exchange_ms(),
        received_at_monotonic=fake.mono + 1,
    )

    assert result.state is FreshnessState.INVALID
    assert result.reason == "invalid_receive_monotonic_time"


def test_spot_and_futures_clocks_are_independent():
    spot_fake, spot_clock = make_clock(offset_ms=100)
    futures_fake, futures_clock = make_clock(offset_ms=-100)

    spot_validator = MarketDataFreshnessValidator(
        spot_clock, monotonic=spot_fake.monotonic
    )
    futures_validator = MarketDataFreshnessValidator(
        futures_clock, monotonic=futures_fake.monotonic
    )

    spot_result = spot_validator.validate_event(
        "trade", spot_clock.now_exchange_ms()
    )
    futures_result = futures_validator.validate_event(
        "markPriceUpdate", futures_clock.now_exchange_ms()
    )

    assert spot_result.state is FreshnessState.FRESH
    assert futures_result.state is FreshnessState.FRESH
    assert spot_clock.snapshot.offset_ms != futures_clock.snapshot.offset_ms
def test_clock_uncertainty_can_make_near_boundary_event_stale():
    fake, clock = make_clock(rtt_seconds=0.2)
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)

    assert clock.snapshot.uncertainty_ms == pytest.approx(100.0)

    timestamp = clock.now_exchange_ms() - 9950
    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.STALE
    assert result.age_ms == pytest.approx(9950)
    assert result.reason == "event_exceeds_stale_threshold_with_uncertainty"


def test_clock_uncertainty_can_make_near_future_boundary_invalid():
    fake, clock = make_clock(rtt_seconds=0.2)
    validator = MarketDataFreshnessValidator(clock, monotonic=fake.monotonic)

    assert clock.snapshot.uncertainty_ms == pytest.approx(100.0)

    timestamp = clock.now_exchange_ms() + 1950
    result = validator.validate_event("trade", timestamp)

    assert result.state is FreshnessState.INVALID
    assert result.age_ms == pytest.approx(-1950)
    assert result.reason == "event_exceeds_future_skew_with_uncertainty"

def test_clock_uses_lowest_rtt_sample_and_keeps_policy_threshold():
    fake = FakeTime()
    clock = ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic, sync_samples=3)
    calls = 0

    def provider():
        nonlocal calls
        calls += 1
        duration = 0.8 if calls == 1 else 0.1
        fake.mono += duration
        return {"serverTime": fake.wall + int(duration * 500)}

    snapshot = clock.synchronize(provider)
    assert calls == 3
    assert snapshot.uncertainty_ms == pytest.approx(50.0)
    assert abs(snapshot.offset_ms) + snapshot.uncertainty_ms <= 1000


def test_clock_rejects_invalid_sample_count():
    fake = FakeTime()
    for count in (0, -1, 6, True, 1.5):
        with pytest.raises(ValueError):
            ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic, sync_samples=count)


def test_clock_fails_closed_when_all_samples_exceed_policy():
    fake = FakeTime()
    clock = ExchangeClock(wall_clock_ms=fake.wall_ms, monotonic=fake.monotonic, sync_samples=3)
    with pytest.raises(ClockSynchronizationError):
        clock.synchronize(lambda: {"serverTime": fake.wall + 2000})
    assert clock.snapshot is None
    assert not clock.is_synchronized
