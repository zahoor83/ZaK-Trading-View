"""Provider-specific Binance rate-limit coordination.

This module contains no HTTP transport, retry loop, sleep, or network I/O.
Call guard() before dispatch and record_response() when a 418/429 response
has been received. Provider identity is derived only from ApprovedRequest.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable

from app.security.egress_policy import ApprovedRequest, EgressPolicyViolation
from app.security.rate_limit_store import (
    PROVIDERS,
    ProviderRateLimitState,
    RateLimitStoreError,
    SQLiteRateLimitStore,
)


_PROVIDER_BY_HOST = {
    "api.binance.com": "spot",
    "data-api.binance.vision": "spot",
    "fapi.binance.com": "usdm_futures",
}


class RateLimitCoordinatorError(RuntimeError):
    """Raised when rate-limit state cannot be safely evaluated or persisted."""


class RateLimitBlockedError(RateLimitCoordinatorError):
    """Raised when a provider is currently blocked by rate-limit policy."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"Binance {provider} requests are blocked: {reason}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must return a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


def provider_for_approved_request(approved: ApprovedRequest) -> str:
    """Map an egress-validated request to its exact provider."""
    if not isinstance(approved, ApprovedRequest):
        raise EgressPolicyViolation("A validated ApprovedRequest is required.")
    if approved.method != "GET":
        raise EgressPolicyViolation("Only validated GET requests are supported.")
    provider = _PROVIDER_BY_HOST.get(approved.host)
    if provider not in PROVIDERS:
        raise EgressPolicyViolation("Request host has no approved rate-limit provider.")
    return provider


def parse_retry_after(
    headers: Iterable[tuple[str, str]],
    *,
    now_utc: datetime,
) -> datetime | None:
    """Return a UTC deadline, or None to require an indefinite block.

    Exactly one Retry-After header is required. Delta-seconds must be a
    positive ASCII integer. HTTP-date values must parse as timezone-aware
    dates and must be strictly later than now_utc.
    """
    now = _aware_utc(now_utc, "now_utc")
    values: list[str] = []

    try:
        for item in headers:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                return None
            if item[0].lower() == "retry-after":
                values.append(item[1].strip())
    except (TypeError, ValueError):
        return None

    if len(values) != 1 or not values[0]:
        return None

    value = values[0]
    if re.fullmatch(r"[0-9]+", value, flags=re.ASCII):
        try:
            seconds = int(value)
            if seconds <= 0:
                return None
            return now + timedelta(seconds=seconds)
        except (OverflowError, ValueError):
            return None

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None

    deadline = parsed.astimezone(timezone.utc)
    if deadline <= now:
        return None
    return deadline


class RateLimitCoordinator:
    """Thread-safe request guard and durable provider cooldown coordinator.

    SQLite is consulted before every request, so independent processes on
    this machine observe one another's persisted state. Timed deadlines are
    translated to monotonic deadlines per process after first observation.
    An unknown/corrupt/unavailable store state fails closed.
    """

    def __init__(
        self,
        store: SQLiteRateLimitStore,
        *,
        wall_clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(store, SQLiteRateLimitStore):
            raise TypeError("store must be a SQLiteRateLimitStore.")
        if not callable(wall_clock) or not callable(monotonic_clock):
            raise TypeError("Clock dependencies must be callable.")
        self._store = store
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._local_indefinite_latches: set[str] = set()
        self._monotonic_deadlines: dict[
            str, tuple[datetime, datetime, float]
        ] = {}

    def _provider(self, approved: ApprovedRequest) -> str:
        return provider_for_approved_request(approved)

    def _latch_indefinite(self, provider: str) -> None:
        with self._lock:
            self._local_indefinite_latches.add(provider)
            self._monotonic_deadlines.pop(provider, None)

    def guard(self, approved: ApprovedRequest) -> None:
        """Raise before network dispatch whenever provider state is blocked."""
        provider = self._provider(approved)

        with self._lock:
            if provider in self._local_indefinite_latches:
                raise RateLimitBlockedError(
                    provider, "local fail-closed latch is active"
                )

        try:
            state = self._store.get_state(provider)
        except Exception as exc:
            self._latch_indefinite(provider)
            raise RateLimitBlockedError(
                provider, "durable rate-limit state is unavailable"
            ) from exc

        if state is None:
            with self._lock:
                self._monotonic_deadlines.pop(provider, None)
            return

        if state.provider != provider:
            self._latch_indefinite(provider)
            raise RateLimitBlockedError(provider, "stored provider identity mismatch")

        if state.state == "indefinite":
            self._latch_indefinite(provider)
            raise RateLimitBlockedError(provider, "indefinite provider block")

        if state.state != "cooldown" or state.blocked_until_utc is None:
            self._latch_indefinite(provider)
            raise RateLimitBlockedError(provider, "stored cooldown state is invalid")

        try:
            monotonic_now = self._monotonic_clock()
            if (
                isinstance(monotonic_now, bool)
                or not isinstance(monotonic_now, (int, float))
                or not float("-inf") < float(monotonic_now) < float("inf")
            ):
                raise ValueError("Monotonic clock returned an invalid value.")
            created = _aware_utc(state.created_at_utc, "created_at_utc")
            deadline = _aware_utc(state.blocked_until_utc, "blocked_until_utc")
        except Exception as exc:
            self._latch_indefinite(provider)
            raise RateLimitBlockedError(
                provider, "clock or persisted timestamp is unsafe"
            ) from exc

        signature = (created, deadline)
        with self._lock:
            cached = self._monotonic_deadlines.get(provider)

            # Once a UTC deadline has been converted to a monotonic deadline,
            # this process must use only the monotonic clock for that record.
            # Keep the expired cache entry so a later wall-clock rollback does
            # not send this process through UTC recovery again.
            if cached is not None and cached[:2] == signature:
                if float(monotonic_now) < cached[2]:
                    raise RateLimitBlockedError(provider, "timed cooldown is active")
                return

            # No matching process-local monotonic deadline exists. This is
            # first observation or restart recovery, so validate wall time.
            try:
                wall_now = _aware_utc(self._wall_clock(), "wall_clock")
                if wall_now < created:
                    raise ValueError(
                        "Wall clock precedes persisted cooldown creation time."
                    )
                remaining = (deadline - wall_now).total_seconds()
                monotonic_deadline = float(monotonic_now) + max(remaining, 0.0)
            except Exception as exc:
                self._latch_indefinite(provider)
                raise RateLimitBlockedError(
                    provider, "wall-clock recovery is unsafe"
                ) from exc

            self._monotonic_deadlines[provider] = (
                created,
                deadline,
                monotonic_deadline,
            )
            if remaining > 0:
                raise RateLimitBlockedError(provider, "timed cooldown is active")

    def record_response(
        self,
        approved: ApprovedRequest,
        *,
        status_code: int,
        headers: Iterable[tuple[str, str]],
    ) -> ProviderRateLimitState | None:
        """Persist a 418/429 response; return None for other status codes."""
        provider = self._provider(approved)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or status_code not in (418, 429)
        ):
            return None

        try:
            now = _aware_utc(self._wall_clock(), "wall_clock")
            deadline = parse_retry_after(headers, now_utc=now)
            state = self._store.apply_block(
                provider,
                status_code=status_code,
                blocked_until_utc=deadline,
                now_utc=now,
            )
        except Exception as exc:
            self._latch_indefinite(provider)
            raise RateLimitCoordinatorError(
                f"Unable to persist {provider} rate-limit response; provider latched blocked."
            ) from exc

        with self._lock:
            self._monotonic_deadlines.pop(provider, None)
            if state.state == "indefinite":
                self._local_indefinite_latches.add(provider)
        return state

    def operator_reset(self, provider: str, *, confirmation: str) -> bool:
        """Explicit local operator reset; never expose this through public HTTP.

        The caller must provide the exact provider-specific confirmation phrase.
        A storage failure leaves the provider's local fail-closed latch active.
        """
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError("Provider must be 'spot' or 'usdm_futures'.")

        expected_confirmation = f"RESET {provider.upper()} RATE LIMIT"
        if confirmation != expected_confirmation:
            raise ValueError(
                f"Explicit confirmation required: {expected_confirmation}"
            )

        with self._lock:
            try:
                deleted = self._store.clear_provider(provider)
            except Exception as exc:
                self._latch_indefinite(provider)
                raise RateLimitCoordinatorError(
                    f"Operator reset failed for {provider}; provider remains latched blocked."
                ) from exc

            self._local_indefinite_latches.discard(provider)
            self._monotonic_deadlines.pop(provider, None)
            return deleted