"""Policy-enforced public Binance HTTP transport.

This module sends public market-data GET requests only.
It does not authenticate, place orders, or follow redirects.
"""

from __future__ import annotations

import math
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.security.egress_policy import (
    EgressPolicyViolation,
    validate_public_market_data_request,
)


@dataclass(frozen=True, slots=True)
class PublicHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


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


def fetch_public_market_data(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_response_bytes: int = 2_000_000,
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

    request = urllib.request.Request(
        url,
        headers=headers or {},
        method=approved.method,
    )

    opener = _build_opener()

    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()

            if not isinstance(status, int) or not 200 <= status < 300:
                raise EgressPolicyViolation("Unexpected HTTP response status.")

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
        exc.close()
        raise EgressPolicyViolation(
            f"HTTP request rejected with status {exc.code}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EgressPolicyViolation("Public market-data HTTP request failed.") from exc
