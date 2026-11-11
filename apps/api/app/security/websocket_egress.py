"""Fail-closed Binance public market-data WebSocket URL policy."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


class WebSocketEgressViolation(ValueError):
    """Raised when a WebSocket URL is outside the public-data policy."""


@dataclass(frozen=True, slots=True)
class ApprovedWebSocketRequest:
    market: str
    host: str
    path: str
    streams: tuple[str, ...]


_ALLOWED_ENDPOINTS = {
    "data-stream.binance.vision": {
        "market": "spot",
        "raw_prefixes": ("/ws/",),
        "combined_paths": frozenset({"/stream"}),
    },
    "fstream.binance.com": {
        "market": "usdm_futures",
        "raw_prefixes": ("/public/ws/", "/market/ws/"),
        "combined_paths": frozenset(
            {
                "/public/stream",
                "/market/stream",
            }
        ),
    },
}

_STREAM_PATTERN = re.compile(
    r"^(?:[a-z0-9]+@[A-Za-z][A-Za-z0-9_@.-]*|![A-Za-z][A-Za-z0-9_@.-]*)$"
)

_MAX_STREAMS = 1024


def _contains_control_or_whitespace(value: str) -> bool:
    """Return True for Unicode control characters or whitespace."""
    return any(
        unicodedata.category(char) == "Cc" or char.isspace()
        for char in value
    )


def _validate_streams(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise WebSocketEgressViolation(
            "At least one stream is required."
        )

    streams = tuple(value.split("/"))

    if not streams or len(streams) > _MAX_STREAMS:
        raise WebSocketEgressViolation(
            "Invalid number of streams."
        )

    if len(set(streams)) != len(streams):
        raise WebSocketEgressViolation(
            "Duplicate streams are prohibited."
        )

    for stream in streams:
        if (
            not _STREAM_PATTERN.fullmatch(stream)
            or ("@" not in stream and not stream.startswith("!"))
        ):
            raise WebSocketEgressViolation(
                "Invalid public market-data stream name."
            )

    return streams


def validate_public_market_data_websocket_url(
    url: str,
) -> ApprovedWebSocketRequest:
    """Validate a public Binance Spot or USD-M Futures market-data WSS URL.

    Only documented public market-data endpoint categories are accepted.
    Private streams, authenticated WebSocket APIs, arbitrary hosts and
    ambiguous URLs, control characters and whitespace are rejected.
    """
    if not isinstance(url, str) or not url:
        raise WebSocketEgressViolation(
            "A non-empty WSS URL is required."
        )

    # Validate the original input before urlsplit() can normalize it.
    if _contains_control_or_whitespace(url):
        raise WebSocketEgressViolation(
            "WebSocket URL contains prohibited control or whitespace characters."
        )

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise WebSocketEgressViolation(
            "Malformed WebSocket URL."
        ) from exc

    if parsed.scheme.lower() != "wss":
        raise WebSocketEgressViolation("Secure WSS is required.")

    if not parsed.hostname:
        raise WebSocketEgressViolation("A hostname is required.")

    if parsed.username is not None or parsed.password is not None:
        raise WebSocketEgressViolation(
            "URL user information is prohibited."
        )

    if parsed.fragment:
        raise WebSocketEgressViolation(
            "URL fragments are prohibited."
        )

    host = parsed.hostname.lower()

    if host not in _ALLOWED_ENDPOINTS:
        raise WebSocketEgressViolation(
            "WebSocket host is not allowlisted."
        )

    if port not in (None, 443):
        raise WebSocketEgressViolation(
            "Only standard WSS port 443 is permitted."
        )

    path = parsed.path

    if (
        not path
        or "\\" in path
        or "//" in path
        or "%" in path
        or not path.isascii()
    ):
        raise WebSocketEgressViolation(
            "Ambiguous or encoded paths are prohibited."
        )

    endpoint = _ALLOWED_ENDPOINTS[host]
    market = endpoint["market"]

    raw_stream = None

    for prefix in endpoint["raw_prefixes"]:
        if path.startswith(prefix):
            raw_stream = path[len(prefix):]
            break

    if raw_stream is not None:
        if not raw_stream or "/" in raw_stream or parsed.query:
            raise WebSocketEgressViolation(
                "Invalid raw stream URL."
            )

        streams = _validate_streams(raw_stream)

    elif path in endpoint["combined_paths"]:
        try:
            query_items = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError as exc:
            raise WebSocketEgressViolation(
                "Malformed WebSocket query."
            ) from exc

        if len(query_items) != 1 or query_items[0][0] != "streams":
            raise WebSocketEgressViolation(
                "Only the public streams query parameter is permitted."
            )

        streams = _validate_streams(query_items[0][1])

    else:
        raise WebSocketEgressViolation(
            "WebSocket path is not an approved public market-data endpoint."
        )

    if market == "usdm_futures":
        if path.startswith(("/public/ws/", "/public/stream")):
            expected_stream = re.compile(r"^[a-z0-9]+@depth$")
        elif path.startswith(("/market/ws/", "/market/stream")):
            expected_stream = re.compile(r"^[a-z0-9]+@markPrice$")
        else:
            raise WebSocketEgressViolation(
                "Futures endpoint category is not recognized."
            )

        if any(expected_stream.fullmatch(stream) is None for stream in streams):
            raise WebSocketEgressViolation(
                "Futures stream does not match its endpoint category."
            )

    return ApprovedWebSocketRequest(
        market=market,
        host=host,
        path=path,
        streams=streams,
    )