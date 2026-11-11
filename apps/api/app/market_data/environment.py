"""Strict environment loading for public market-data configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping

from app.market_data.config import MarketDataConfig


_ENV_FIELDS = {
    "ZAK_MARKET_DATA_SPOT_SYMBOLS": "spot_symbols",
    "ZAK_MARKET_DATA_FUTURES_DEPTH_SYMBOLS": "futures_depth_symbols",
    "ZAK_MARKET_DATA_FUTURES_MARK_PRICE_SYMBOLS": "futures_mark_price_symbols",
}


def _parse_symbols(value: str | None, variable: str) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if not isinstance(value, str):
        raise TypeError(f"{variable} must be a string.")

    symbols: list[str] = []
    for part in value.split(","):
        symbol = part.strip()
        if not symbol:
            raise ValueError(f"{variable} contains an empty symbol.")
        if symbol != symbol.upper():
            raise ValueError(f"{variable} symbols must use uppercase.")
        symbols.append(symbol)
    return tuple(symbols)


def market_data_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> MarketDataConfig:
    """Build a fail-closed config; absent or empty variables disable groups."""
    source = os.environ if environ is None else environ
    values = {
        field: _parse_symbols(source.get(variable), variable)
        for variable, field in _ENV_FIELDS.items()
    }
    # MarketDataConfig enforces symbol syntax, per-group limits and duplicates.
    return MarketDataConfig(**values)
