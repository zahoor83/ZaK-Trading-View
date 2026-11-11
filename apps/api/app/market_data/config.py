"""Explicit, fail-closed configuration for public market-data streams."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.security.websocket_egress import (
    validate_public_market_data_websocket_url,
)


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,30}$")
_MAX_STREAMS_PER_CONNECTION = 1024


def _validate_symbols(value: tuple[str, ...], field: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple.")

    if len(value) > _MAX_STREAMS_PER_CONNECTION:
        raise ValueError(f"{field} exceeds the stream limit.")

    if len(value) != len(set(value)):
        raise ValueError(f"{field} contains duplicate symbols.")

    for symbol in value:
        if not isinstance(symbol, str) or not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"{field} contains an invalid symbol.")


@dataclass(frozen=True, slots=True)
class MarketDataConfig:
    """Explicit symbol selection; empty groups remain disabled."""

    spot_symbols: tuple[str, ...] = ()
    futures_depth_symbols: tuple[str, ...] = ()
    futures_mark_price_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_symbols(self.spot_symbols, "spot_symbols")
        _validate_symbols(self.futures_depth_symbols, "futures_depth_symbols")
        _validate_symbols(
            self.futures_mark_price_symbols,
            "futures_mark_price_symbols",
        )

    @property
    def is_enabled(self) -> bool:
        return bool(
            self.spot_symbols
            or self.futures_depth_symbols
            or self.futures_mark_price_symbols
        )

    @staticmethod
    def _build_url(host: str, path: str, streams: tuple[str, ...]) -> str | None:
        if not streams:
            return None

        stream_query = "/".join(streams)
        url = f"wss://{host}{path}?streams={stream_query}"

        # Validate every internally generated URL against the existing policy.
        approved = validate_public_market_data_websocket_url(url)

        if tuple(sorted(approved.streams)) != tuple(sorted(streams)):
            raise ValueError("Generated URL does not match configured streams.")

        return url

    @property
    def spot_websocket_url(self) -> str | None:
        streams = tuple(
            f"{symbol.lower()}@trade"
            for symbol in self.spot_symbols
        )
        return self._build_url(
            "data-stream.binance.vision",
            "/stream",
            streams,
        )

    @property
    def futures_depth_websocket_url(self) -> str | None:
        streams = tuple(
            f"{symbol.lower()}@depth"
            for symbol in self.futures_depth_symbols
        )
        return self._build_url(
            "fstream.binance.com",
            "/public/stream",
            streams,
        )

    @property
    def futures_mark_price_websocket_url(self) -> str | None:
        streams = tuple(
            f"{symbol.lower()}@markPrice"
            for symbol in self.futures_mark_price_symbols
        )
        return self._build_url(
            "fstream.binance.com",
            "/market/stream",
            streams,
        )

    @property
    def configured_groups(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        groups = []

        if self.spot_symbols:
            groups.append(("spot", self.spot_symbols))

        if self.futures_depth_symbols:
            groups.append(("futures_depth", self.futures_depth_symbols))

        if self.futures_mark_price_symbols:
            groups.append(("futures_mark_price", self.futures_mark_price_symbols))

        return tuple(groups)