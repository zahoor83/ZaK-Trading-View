"""Policy-enforced public Binance HTTP transport.

This module sends public market-data GET requests only.
It does not authenticate, place orders, or follow redirects.
"""

from __future__ import annotations

import math
import os
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.security.rate_limit import (
    RateLimitCoordinator,
    RateLimitCoordinatorError,
)
from app.security.rate_limit_store import SQLiteRateLimitStore
from app.security.egress_policy import (
    EgressPolicyViolation,
    validate_public_market_data_request,
)


@dataclass(frozen=True, slots=True)
class PublicHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class PublicHttpError(EgressPolicyViolation):
    """HTTP provider error with bounded, preserved response metadata."""

    def __init__(
        self,
        status_code: int,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
    ) -> None:
        self.status_code = status_code
        self.headers = tuple(headers)
        self.body = body
        self.retry_after = next(
            (
                value
                for name, value in self.headers
                if name.lower() == "retry-after"
            ),
            None,
        )
        super().__init__(f"HTTP request rejected with status {status_code}.")


class _DenyRedirects(urllib.request.HTTPRedirectHandler):
    """Reject every redirect instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EgressPolicyViolation("HTTP redirects are prohibited.")


def _build_opener():
    """Build a transport with no environment proxy inheritance."""
    tls_context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _DenyRedirects(),
        urllib.request.HTTPSHandler(context=tls_context),
    )


_rate_limit_coordinator_lock = threading.Lock()
_rate_limit_coordinator: RateLimitCoordinator | None = None


def _get_default_rate_limit_coordinator() -> RateLimitCoordinator:
    """Lazily create the durable local coordinator; never initialize SQLite on import."""
    global _rate_limit_coordinator
    with _rate_limit_coordinator_lock:
        if _rate_limit_coordinator is None:
            configured_path = os.environ.get("ZAK_TV_RATE_LIMIT_DB_PATH")
            if configured_path is None:
                database_path = (
                    Path(__file__).resolve().parents[2]
                    / "data"
                    / "private"
                    / "rate_limit_state.sqlite3"
                )
            else:
                if not configured_path.strip():
                    raise EgressPolicyViolation(
                        "Configured rate-limit database path is empty."
                    )
                database_path = Path(configured_path).expanduser()
                if not database_path.is_absolute():
                    raise EgressPolicyViolation(
                        "ZAK_TV_RATE_LIMIT_DB_PATH must be an absolute path."
                    )
            try:
                store = SQLiteRateLimitStore(database_path)
                _rate_limit_coordinator = RateLimitCoordinator(store)
            except Exception as exc:
                raise EgressPolicyViolation(
                    "Rate-limit storage configuration is unavailable."
                ) from exc
        return _rate_limit_coordinator


def _guard_provider_request(
    coordinator: RateLimitCoordinator,
    approved,
) -> None:
    try:
        coordinator.guard(approved)
    except RateLimitCoordinatorError as exc:
        raise EgressPolicyViolation(
            "Provider rate-limit policy blocks outbound market-data requests."
        ) from exc


def _record_provider_rate_limit(
    coordinator: RateLimitCoordinator,
    approved,
    status_code: int,
    response_headers: tuple[tuple[str, str], ...],
) -> None:
    if status_code not in (418, 429):
        return
    try:
        coordinator.record_response(
            approved,
            status_code=status_code,
            headers=response_headers,
        )
    except RateLimitCoordinatorError as exc:
        raise EgressPolicyViolation(
            "Provider rate-limit state could not be persisted; outbound requests are blocked."
        ) from exc


def fetch_public_market_data(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_response_bytes: int = 2_000_000,
    rate_limit_coordinator: RateLimitCoordinator | None = None,
) -> PublicHttpResponse:
    """Validate and fetch one approved public market-data endpoint."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise EgressPolicyViolation("A finite positive timeout is required.")

    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes <= 0
    ):
        raise EgressPolicyViolation("A positive response-size limit is required.")

    approved = validate_public_market_data_request(
        "GET",
        url,
        headers=headers,
    )

    coordinator = (
        rate_limit_coordinator
        if rate_limit_coordinator is not None
        else _get_default_rate_limit_coordinator()
    )
    if not isinstance(coordinator, RateLimitCoordinator):
        raise EgressPolicyViolation("A valid rate-limit coordinator is required.")
    _guard_provider_request(coordinator, approved)

    request = urllib.request.Request(
        url,
        headers=headers or {},
        method=approved.method,
    )

    opener = _build_opener()

    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()

            if not isinstance(status, int) or isinstance(status, bool):
                raise EgressPolicyViolation("Unexpected HTTP response status.")

            if not 200 <= status < 300:
                error_headers = tuple(response.headers.items())
                # Persist provider throttling before reading a possibly oversized body.
                _record_provider_rate_limit(
                    coordinator, approved, status, error_headers
                )
                error_body = response.read(max_response_bytes + 1)

                if len(error_body) > max_response_bytes:
                    raise EgressPolicyViolation(
                        "HTTP error response exceeds the configured size limit."
                    )

                raise PublicHttpError(
                    status,
                    error_headers,
                    error_body,
                )

            body = response.read(max_response_bytes + 1)

            if len(body) > max_response_bytes:
                raise EgressPolicyViolation("HTTP response exceeds the configured size limit.")

            response_headers = tuple(response.headers.items())

            return PublicHttpResponse(
                status_code=status,
                headers=response_headers,
                body=body,
            )

    except EgressPolicyViolation:
        raise
    except urllib.error.HTTPError as exc:
        try:
            error_headers = (
                tuple(exc.headers.items())
                if exc.headers is not None
                else ()
            )
            # Persist provider throttling before reading a possibly oversized body.
            _record_provider_rate_limit(
                coordinator, approved, exc.code, error_headers
            )
            error_body = exc.read(max_response_bytes + 1)

            if len(error_body) > max_response_bytes:
                raise EgressPolicyViolation(
                    "HTTP error response exceeds the configured size limit."
                ) from exc

            raise PublicHttpError(
                exc.code,
                error_headers,
                error_body,
            ) from exc
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EgressPolicyViolation("Public market-data HTTP request failed.") from exc
