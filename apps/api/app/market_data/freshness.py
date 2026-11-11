"""Clock synchronization and fail-closed Binance market-data freshness."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ClockSynchronizationError(RuntimeError):
    """Raised when a trustworthy exchange clock cannot be established."""


class FreshnessState(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    INVALID = "INVALID"
    CLOCK_UNSYNCED = "CLOCK_UNSYNCED"


@dataclass(frozen=True, slots=True)
class ClockSnapshot:
    server_time_ms: int
    offset_ms: float
    uncertainty_ms: float
    synchronized_at_monotonic: float


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    state: FreshnessState
    event_type: str
    event_timestamp_ms: int | None
    age_ms: float | None
    stale_after_ms: int | None
    processing_delay_ms: float | None
    reason: str


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class ExchangeClock:
    """Estimate Binance time without modifying the operating-system clock.

    The server timestamp is anchored to the midpoint of the request's
    monotonic duration. Half the round-trip duration is treated as
    synchronization uncertainty.
    """

    def __init__(
        self,
        *,
        max_offset_ms: int = 1000,
        sync_interval_seconds: float = 60.0,
        sync_samples: int = 3,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if type(max_offset_ms) is not int or max_offset_ms <= 0:
            raise ValueError("max_offset_ms must be a positive integer.")

        if not _finite_number(sync_interval_seconds) or sync_interval_seconds <= 0:
            raise ValueError("sync_interval_seconds must be finite and positive.")

        if type(sync_samples) is not int or not 1 <= sync_samples <= 5:
            raise ValueError("sync_samples must be an integer between 1 and 5.")

        self._max_offset_ms = max_offset_ms
        self._sync_interval_seconds = float(sync_interval_seconds)
        self._sync_samples = sync_samples
        self._wall_clock_ms = wall_clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic or time.monotonic
        self._snapshot: ClockSnapshot | None = None
        self._last_error: str | None = None

    @property
    def snapshot(self) -> ClockSnapshot | None:
        return self._snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def is_synchronized(self) -> bool:
        if self._snapshot is None:
            return False
        try:
            self.now_exchange_ms()
        except ClockSynchronizationError:
            return False
        return True

    def synchronize(
        self,
        server_time_provider: Callable[[], Mapping[str, Any]],
    ) -> ClockSnapshot:
        """Synchronize using a REST get_server_time-compatible callback."""
        # Keep the previous valid snapshot available while refreshing.
        # Every failure path below still invalidates it immediately.
        self._last_error = None

        if not callable(server_time_provider):
            error = ValueError("Server-time provider must be callable.")
            self._snapshot = None
            self._last_error = type(error).__name__
            raise ClockSynchronizationError(
                "Binance clock synchronization failed; market data is untrusted."
            ) from error

        # Use a bounded sample set and select the lowest-RTT valid measurement.
        # The existing offset + uncertainty safety threshold is unchanged.
        candidates: list[tuple[float, ClockSnapshot]] = []
        last_error: Exception | None = None

        for _ in range(self._sync_samples):
            try:
                mono_before = self._monotonic()
                wall_before = self._wall_clock_ms()

                if not _finite_number(mono_before):
                    raise ValueError("Invalid monotonic clock reading.")
                if type(wall_before) is not int or wall_before < 0:
                    raise ValueError("Invalid local wall-clock reading.")

                payload = server_time_provider()
                mono_after = self._monotonic()

                if not _finite_number(mono_after) or mono_after < mono_before:
                    raise ValueError("Monotonic clock moved backwards.")
                if not isinstance(payload, Mapping):
                    raise ValueError("Server-time response must be an object.")

                server_time = payload.get("serverTime")
                if type(server_time) is not int or server_time < 0:
                    raise ValueError("Invalid Binance server timestamp.")

                round_trip_ms = (mono_after - mono_before) * 1000.0
                uncertainty_ms = round_trip_ms / 2.0
                midpoint_monotonic = (mono_before + mono_after) / 2.0
                midpoint_wall_ms = wall_before + uncertainty_ms
                offset_ms = float(server_time) - midpoint_wall_ms

                snapshot = ClockSnapshot(
                    server_time_ms=server_time,
                    offset_ms=offset_ms,
                    uncertainty_ms=uncertainty_ms,
                    synchronized_at_monotonic=midpoint_monotonic,
                )
                candidates.append((round_trip_ms, snapshot))
            except Exception as exc:
                last_error = exc

        if not candidates:
            error = last_error or ValueError("No valid clock samples were received.")
            self._snapshot = None
            self._last_error = type(error).__name__
            raise ClockSynchronizationError(
                "Binance clock synchronization failed; market data is untrusted."
            ) from error

        _, best = min(candidates, key=lambda item: item[0])
        if abs(best.offset_ms) + best.uncertainty_ms > self._max_offset_ms:
            error = ValueError("Clock offset or uncertainty exceeds policy.")
            self._snapshot = None
            self._last_error = type(error).__name__
            raise ClockSynchronizationError(
                "Binance clock synchronization failed; market data is untrusted."
            ) from error

        self._snapshot = best
        self._last_error = None
        return best

    def now_exchange_ms(self, *, at_monotonic: float | None = None) -> int:
        snapshot = self._snapshot

        if snapshot is None:
            raise ClockSynchronizationError("Exchange clock is not synchronized.")

        current = self._monotonic() if at_monotonic is None else at_monotonic

        if not _finite_number(current):
            raise ClockSynchronizationError("Invalid monotonic clock reading.")

        elapsed = current - snapshot.synchronized_at_monotonic

        if elapsed < 0:
            raise ClockSynchronizationError("Monotonic time precedes synchronization.")

        if elapsed > self._sync_interval_seconds:
            raise ClockSynchronizationError("Exchange clock synchronization expired.")

        return int(round(snapshot.server_time_ms + elapsed * 1000.0))


class MarketDataFreshnessValidator:
    """Validate Binance trade and mark-price event freshness."""

    _STALE_THRESHOLDS_MS = {
        "trade": 10_000,
        "markPriceUpdate": 10_000,
    }

    def __init__(
        self,
        exchange_clock: ExchangeClock,
        *,
        max_future_skew_ms: int = 2000,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(exchange_clock, ExchangeClock):
            raise TypeError("exchange_clock must be an ExchangeClock.")

        if type(max_future_skew_ms) is not int or max_future_skew_ms < 0:
            raise ValueError("max_future_skew_ms must be a non-negative integer.")

        self._clock = exchange_clock
        self._max_future_skew_ms = max_future_skew_ms
        self._monotonic = monotonic or time.monotonic

    def validate_event(
        self,
        event_type: str,
        event_timestamp_ms: Any,
        *,
        received_at_monotonic: float | None = None,
    ) -> FreshnessResult:
        threshold = self._STALE_THRESHOLDS_MS.get(event_type)

        if threshold is None:
            return FreshnessResult(
                FreshnessState.INVALID, str(event_type), None, None, None,
                None, "unsupported_event_type",
            )

        if type(event_timestamp_ms) is not int or event_timestamp_ms <= 0:
            return FreshnessResult(
                FreshnessState.INVALID, event_type, None, None, threshold,
                None, "invalid_event_timestamp",
            )

        now_mono = self._monotonic()

        if not _finite_number(now_mono):
            return FreshnessResult(
                FreshnessState.INVALID, event_type, event_timestamp_ms, None,
                threshold, None, "invalid_local_monotonic_time",
            )

        received = now_mono if received_at_monotonic is None else received_at_monotonic

        if (
            not _finite_number(received)
            or received < 0
            or received > now_mono
        ):
            return FreshnessResult(
                FreshnessState.INVALID, event_type, event_timestamp_ms, None,
                threshold, None, "invalid_receive_monotonic_time",
            )

        processing_delay_ms = (now_mono - received) * 1000.0

        try:
            exchange_now_ms = self._clock.now_exchange_ms(
                at_monotonic=now_mono
            )
        except ClockSynchronizationError:
            return FreshnessResult(
                FreshnessState.CLOCK_UNSYNCED,
                event_type,
                event_timestamp_ms,
                None,
                threshold,
                processing_delay_ms,
                "exchange_clock_unavailable",
            )

        snapshot = self._clock.snapshot

        if snapshot is None:
            return FreshnessResult(
                FreshnessState.CLOCK_UNSYNCED,
                event_type,
                event_timestamp_ms,
                None,
                threshold,
                processing_delay_ms,
                "exchange_clock_unavailable",
            )

        age_ms = float(exchange_now_ms - event_timestamp_ms)

        # Conservatively account for exchange-clock synchronization uncertainty.
        minimum_age_ms = age_ms - snapshot.uncertainty_ms
        maximum_age_ms = age_ms + snapshot.uncertainty_ms

        if minimum_age_ms < -self._max_future_skew_ms:
            reason = (
                "event_exceeds_future_skew"
                if age_ms < -self._max_future_skew_ms
                else "event_exceeds_future_skew_with_uncertainty"
            )
            return FreshnessResult(
                FreshnessState.INVALID,
                event_type,
                event_timestamp_ms,
                age_ms,
                threshold,
                processing_delay_ms,
                reason,
            )

        if maximum_age_ms > threshold:
            reason = (
                "event_exceeds_stale_threshold"
                if age_ms > threshold
                else "event_exceeds_stale_threshold_with_uncertainty"
            )
            return FreshnessResult(
                FreshnessState.STALE,
                event_type,
                event_timestamp_ms,
                age_ms,
                threshold,
                processing_delay_ms,
                reason,
            )

        return FreshnessResult(
            FreshnessState.FRESH,
            event_type,
            event_timestamp_ms,
            age_ms,
            threshold,
            processing_delay_ms,
            "within_freshness_policy",
        )