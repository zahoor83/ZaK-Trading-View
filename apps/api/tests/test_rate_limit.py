from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from app.security.egress_policy import ApprovedRequest, EgressPolicyViolation
from app.security.rate_limit import (
    RateLimitBlockedError,
    RateLimitCoordinator,
    RateLimitCoordinatorError,
    parse_retry_after,
    provider_for_approved_request,
)
from app.security.rate_limit_store import RateLimitStoreError, SQLiteRateLimitStore


UTC = timezone.utc
BASE = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, wall: datetime = BASE, monotonic: float = 100.0):
        self.wall = wall
        self.monotonic = monotonic

    def now(self):
        return self.wall

    def tick(self, seconds: float):
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)

    def monotonic_now(self):
        return self.monotonic


def approved(host: str = "api.binance.com") -> ApprovedRequest:
    return ApprovedRequest(method="GET", host=host, path="/api/v3/time")


def make_coordinator(tmp_path, clock: FakeClock | None = None):
    active_clock = clock or FakeClock()
    store = SQLiteRateLimitStore(tmp_path / "rate-limit.sqlite3")
    coordinator = RateLimitCoordinator(
        store,
        wall_clock=active_clock.now,
        monotonic_clock=active_clock.monotonic_now,
    )
    return coordinator, store, active_clock


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.binance.com", "spot"),
        ("data-api.binance.vision", "spot"),
        ("fapi.binance.com", "usdm_futures"),
    ],
)
def test_exact_approved_host_provider_mapping(host, expected):
    assert provider_for_approved_request(approved(host)) == expected


@pytest.mark.parametrize("host", ["evil.example", "binance.com", "api.binance.com.evil"])
def test_unknown_host_is_rejected(host):
    with pytest.raises(EgressPolicyViolation):
        provider_for_approved_request(approved(host))


def test_requires_approved_request_instance():
    with pytest.raises(EgressPolicyViolation):
        provider_for_approved_request("https://api.binance.com/api/v3/time")


def test_delta_seconds_retry_after_returns_utc_deadline():
    assert parse_retry_after(
        [("Retry-After", "30")], now_utc=BASE
    ) == BASE + timedelta(seconds=30)


def test_http_date_retry_after_returns_utc_deadline():
    deadline = BASE + timedelta(minutes=2)
    value = format_datetime(deadline, usegmt=True)

    assert parse_retry_after(
        [("retry-after", value)], now_utc=BASE
    ) == deadline


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Retry-After", "")],
        [("Retry-After", "0")],
        [("Retry-After", "-5")],
        [("Retry-After", "+5")],
        [("Retry-After", "1.5")],
        [("Retry-After", "tomorrow")],
        [("Retry-After", "Wed, 04 Nov 2026 11:59:00 GMT")],
        [
            ("Retry-After", "10"),
            ("retry-after", "20"),
        ],
        [("Retry-After", 10)],
        ["Retry-After: 10"],
    ],
)
def test_invalid_missing_or_duplicate_retry_after_requires_indefinite(headers):
    assert parse_retry_after(headers, now_utc=BASE) is None


def test_retry_after_http_date_without_timezone_is_rejected():
    assert parse_retry_after(
        [("Retry-After", "Wed, 04 Nov 2026 12:05:00")],
        now_utc=BASE,
    ) is None


def test_guard_allows_provider_without_saved_state(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)
    assert coordinator.guard(approved()) is None


def test_record_response_persists_timed_cooldown_and_guard_blocks(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    state = coordinator.record_response(
        approved(),
        status_code=429,
        headers=[("Retry-After", "45")],
    )

    assert state is not None
    assert state.state == "cooldown"
    assert state.blocked_until_utc == BASE + timedelta(seconds=45)
    assert store.get_state("spot") == state
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_timed_cooldown_expires_using_monotonic_clock(tmp_path):
    coordinator, _, clock = make_coordinator(tmp_path)
    coordinator.record_response(
        approved(), status_code=429, headers=[("Retry-After", "10")]
    )

    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

    clock.tick(9)
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

    clock.tick(1)
    assert coordinator.guard(approved()) is None


def test_monotonic_deadline_prevents_wall_clock_rollback_shortening(tmp_path):
    coordinator, _, clock = make_coordinator(tmp_path)
    coordinator.record_response(
        approved(), status_code=429, headers=[("Retry-After", "20")]
    )
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

    clock.wall -= timedelta(minutes=5)
    clock.monotonic += 19
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

    clock.monotonic += 1
    assert coordinator.guard(approved()) is None


def test_restart_recovery_uses_persisted_utc_deadline(tmp_path):
    store = SQLiteRateLimitStore(tmp_path / "rate-limit.sqlite3")
    store.apply_block(
        "spot",
        status_code=429,
        blocked_until_utc=BASE + timedelta(seconds=30),
        now_utc=BASE,
    )
    restarted_clock = FakeClock(wall=BASE + timedelta(seconds=5), monotonic=500)
    restarted = RateLimitCoordinator(
        SQLiteRateLimitStore(tmp_path / "rate-limit.sqlite3"),
        wall_clock=restarted_clock.now,
        monotonic_clock=restarted_clock.monotonic_now,
    )

    with pytest.raises(RateLimitBlockedError):
        restarted.guard(approved())

    restarted_clock.tick(25)
    assert restarted.guard(approved()) is None


def test_clock_rollback_before_record_creation_fails_closed(tmp_path):
    coordinator, store, clock = make_coordinator(tmp_path)
    store.apply_block(
        "spot",
        status_code=429,
        blocked_until_utc=BASE + timedelta(seconds=30),
        now_utc=BASE,
    )
    clock.wall = BASE - timedelta(seconds=1)

    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_missing_retry_after_creates_durable_indefinite_block(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    state = coordinator.record_response(
        approved(), status_code=418, headers=[]
    )

    assert state is not None
    assert state.state == "indefinite"
    assert store.get_state("spot").state == "indefinite"
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_spot_and_futures_cooldowns_are_independent(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)
    futures_request = approved("fapi.binance.com")
    coordinator.record_response(
        approved(), status_code=429, headers=[("Retry-After", "60")]
    )

    assert coordinator.guard(futures_request) is None
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_other_http_status_does_not_change_rate_limit_state(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    assert coordinator.record_response(
        approved(), status_code=500, headers=[("Retry-After", "60")]
    ) is None
    assert store.get_state("spot") is None


def test_persisted_indefinite_block_cannot_be_downgraded(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    coordinator.record_response(approved(), status_code=418, headers=[])
    saved = coordinator.record_response(
        approved(), status_code=429, headers=[("Retry-After", "90")]
    )

    assert saved is not None and saved.state == "indefinite"
    assert store.get_state("spot").state == "indefinite"


def test_store_read_failure_latches_provider_fail_closed(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)

    def broken_read(_provider):
        raise RateLimitStoreError("simulated storage outage")

    coordinator._store.get_state = broken_read

    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_store_write_failure_latches_provider_fail_closed(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)

    def broken_write(*args, **kwargs):
        raise RateLimitStoreError("simulated storage outage")

    coordinator._store.apply_block = broken_write

    with pytest.raises(RateLimitCoordinatorError):
        coordinator.record_response(
            approved(), status_code=429, headers=[("Retry-After", "10")]
        )

    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_explicit_operator_reset_clears_indefinite_database_and_local_latch(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    coordinator.record_response(approved(), status_code=418, headers=[])
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

    assert coordinator.operator_reset("spot", confirmation="RESET SPOT RATE LIMIT") is True
    assert store.get_state("spot") is None
    assert coordinator.guard(approved()) is None


def test_operator_reset_rejects_unknown_provider(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)
    with pytest.raises(ValueError):
        coordinator.operator_reset("binance", confirmation="RESET BINANCE RATE LIMIT")


def test_wall_clock_failure_fails_closed(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)

    def broken_clock():
        raise RuntimeError("clock unavailable")

    coordinator._wall_clock = broken_clock
    with pytest.raises(RateLimitCoordinatorError):
        coordinator.record_response(
            approved(), status_code=429, headers=[("Retry-After", "10")]
        )
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())

def test_operator_reset_requires_exact_confirmation(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    coordinator.record_response(approved(), status_code=418, headers=[])

    with pytest.raises(ValueError):
        coordinator.operator_reset("spot", confirmation="RESET FUTURES RATE LIMIT")

    assert store.get_state("spot") is not None
    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_operator_reset_storage_failure_keeps_provider_latched(tmp_path):
    coordinator, _, _ = make_coordinator(tmp_path)
    coordinator.record_response(approved(), status_code=418, headers=[])

    def broken_clear(_provider):
        raise RateLimitStoreError("simulated reset storage failure")

    coordinator._store.clear_provider = broken_clear

    with pytest.raises(RateLimitCoordinatorError):
        coordinator.operator_reset(
            "spot", confirmation="RESET SPOT RATE LIMIT"
        )

    with pytest.raises(RateLimitBlockedError):
        coordinator.guard(approved())


def test_operator_reset_clears_timed_cooldown(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    coordinator.record_response(
        approved(), status_code=429, headers=[("Retry-After", "120")]
    )

    assert coordinator.operator_reset(
        "spot", confirmation="RESET SPOT RATE LIMIT"
    ) is True
    assert store.get_state("spot") is None
    assert coordinator.guard(approved()) is None


def test_operator_reset_rejects_missing_confirmation(tmp_path):
    coordinator, store, _ = make_coordinator(tmp_path)
    coordinator.record_response(approved(), status_code=418, headers=[])

    with pytest.raises(TypeError):
        coordinator.operator_reset("spot")

    assert store.get_state("spot") is not None