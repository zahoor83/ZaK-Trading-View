"""Fail-closed allowlist for public Binance market-data HTTP requests.

This module validates request intent only. It does not create an HTTP client,
send network traffic, authenticate with Binance, or execute exchange orders.
Call validate_public_market_data_request immediately before every HTTP send.
Disable automatic redirects in the eventual HTTP transport and validate each
redirect target independently.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


class EgressPolicyViolation(ValueError):
    """Raised when an outbound request is outside the public-data allowlist."""


@dataclass(frozen=True, slots=True)
class ApprovedRequest:
    """Normalized request details accepted by the policy."""

    method: str
    host: str
    path: str


# Exact hosts only. No wildcard or suffix matching.
_ALLOWED_HOST_PATHS: dict[str, frozenset[str]] = {
    "api.binance.com": frozenset(
        {
            "/api/v3/time",
            "/api/v3/exchangeInfo",
            "/api/v3/klines",
            "/api/v3/depth",
            "/api/v3/avgPrice",
            "/api/v3/ticker/24hr",
            "/api/v3/ticker/price",
            "/api/v3/ticker/bookTicker",
        }
    ),
    "data-api.binance.vision": frozenset(
        {
            "/api/v3/time",
            "/api/v3/exchangeInfo",
            "/api/v3/klines",
            "/api/v3/depth",
            "/api/v3/avgPrice",
            "/api/v3/ticker/24hr",
            "/api/v3/ticker/price",
            "/api/v3/ticker/bookTicker",
        }
    ),
    "fapi.binance.com": frozenset(
        {
            "/fapi/v1/time",
            "/fapi/v1/exchangeInfo",
            "/fapi/v1/klines",
            "/fapi/v1/depth",
            "/fapi/v1/premiumIndex",
            "/fapi/v1/fundingRate",
            "/fapi/v1/ticker/24hr",
            "/fapi/v1/ticker/price",
            "/fapi/v1/ticker/bookTicker",
        }
    ),
}

# Only explicitly approved, non-authentication headers are accepted.
_ALLOWED_HEADERS = frozenset({"accept", "user-agent"})

_FORBIDDEN_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "signature",
        "recvwindow",
    }
)

_ENCODED_PATH_SEPARATORS = ("%2f", "%5c", "%00")


def _contains_control_or_whitespace(value: str) -> bool:
    """Return True for Unicode control characters or whitespace."""
    return any(
        unicodedata.category(char) == "Cc" or char.isspace()
        for char in value
    )


def validate_public_market_data_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
) -> ApprovedRequest:
    """Validate a public, read-only Binance Spot/Futures REST request.

    Only HTTPS GET requests to exact approved hosts and exact public market-data
    paths are accepted. Unknown hosts, paths, methods, credentials, redirects
    to unapproved targets, malformed URLs, control characters and whitespace
    fail closed.
    """
    if not isinstance(method, str) or method.upper() != "GET":
        raise EgressPolicyViolation("Only HTTP GET requests are permitted.")

    if not isinstance(url, str) or not url:
        raise EgressPolicyViolation("A non-empty absolute URL is required.")

    # Validate the original input before urlsplit() can normalize it.
    if _contains_control_or_whitespace(url):
        raise EgressPolicyViolation(
            "URL contains prohibited control or whitespace characters."
        )

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise EgressPolicyViolation("Malformed outbound URL.") from exc

    if parsed.scheme.lower() != "https":
        raise EgressPolicyViolation("HTTPS is required.")

    if not parsed.hostname:
        raise EgressPolicyViolation(
            "An absolute URL with a hostname is required."
        )

    if parsed.username is not None or parsed.password is not None:
        raise EgressPolicyViolation("URL user information is prohibited.")

    if parsed.fragment:
        raise EgressPolicyViolation("URL fragments are prohibited.")

    host = parsed.hostname.lower()

    if host not in _ALLOWED_HOST_PATHS:
        raise EgressPolicyViolation("Outbound host is not allowlisted.")

    if port not in (None, 443):
        raise EgressPolicyViolation(
            "Only the standard HTTPS port is permitted."
        )

    path = parsed.path

    if (
        not path
        or "\\" in path
        or "//" in path
        or any(
            token in path.lower()
            for token in _ENCODED_PATH_SEPARATORS
        )
        or "%" in path
    ):
        raise EgressPolicyViolation(
            "Ambiguous or encoded URL paths are prohibited."
        )

    if path not in _ALLOWED_HOST_PATHS[host]:
        raise EgressPolicyViolation(
            "Endpoint is not an approved public market-data path."
        )

    if headers is not None:
        if not isinstance(headers, dict):
            raise EgressPolicyViolation(
                "Headers must be supplied as a dictionary."
            )

        seen_headers: set[str] = set()

        for name, value in headers.items():
            if (
                not isinstance(name, str)
                or not name
                or name != name.strip()
            ):
                raise EgressPolicyViolation("Invalid header name.")

            normalized_name = name.lower()

            if normalized_name not in _ALLOWED_HEADERS:
                raise EgressPolicyViolation(
                    "Header is not explicitly allowlisted."
                )

            if normalized_name in seen_headers:
                raise EgressPolicyViolation(
                    "Duplicate header names are prohibited."
                )

            seen_headers.add(normalized_name)

            if not isinstance(value, str):
                raise EgressPolicyViolation(
                    "Header values must be strings."
                )

            if "\r" in value or "\n" in value:
                raise EgressPolicyViolation(
                    "Header values must not contain CR/LF."
                )

    try:
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
        )
    except ValueError as exc:
        raise EgressPolicyViolation(
            "Malformed query string."
        ) from exc

    for key, _value in query_items:
        if key.strip().lower() in _FORBIDDEN_QUERY_KEYS:
            raise EgressPolicyViolation(
                "Credential or signed-request query parameters are prohibited."
            )

    return ApprovedRequest(
        method="GET",
        host=host,
        path=path,
    )