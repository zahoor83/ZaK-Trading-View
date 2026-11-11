"""Fail-closed validation of Binance Spot and USD-M Futures symbol metadata.

This module parses exchangeInfo payloads into immutable, time-bounded snapshots.
It does not perform network requests or refresh expired metadata.
Callers must fetch fresh metadata and replace expired snapshots explicitly.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal


Market = Literal["spot", "usdm_futures"]

METADATA_MAX_AGE_SECONDS = 300.0

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,30}$")
_ASSET_RE = re.compile(r"^[A-Z0-9]{1,30}$")
_DELIVERY_SUFFIX_RE = re.compile(r"^[0-9]{6}$")

def _provider_identifier(value: object, field: str, *, allow_delivery_suffix: bool = False) -> str:
    result = _string(value, field)
    if len(result) > 30:
        raise MetadataValidationError(f"{field} has an invalid identifier format")
    if allow_delivery_suffix and "_" in result:
        if result.count("_") != 1:
            raise MetadataValidationError(f"{field} has an invalid identifier format")
        prefix, suffix = result.rsplit("_", 1)
        valid = bool(prefix) and prefix.isalnum() and bool(_DELIVERY_SUFFIX_RE.fullmatch(suffix))
    else:
        valid = result.isalnum()
    if not valid:
        raise MetadataValidationError(f"{field} has an invalid identifier format")
    return result


class MetadataValidationError(ValueError):
    """Exchange metadata is structurally or semantically invalid."""


class MetadataUnavailableError(RuntimeError):
    """Metadata is missing or cannot safely be used."""


class MetadataStaleError(MetadataUnavailableError):
    """Metadata has expired or its clock reading is invalid."""


class SymbolNotFoundError(MetadataUnavailableError):
    """The requested symbol is absent from the current snapshot."""


class SymbolNotTradingError(MetadataUnavailableError):
    """The symbol is not explicitly marked TRADING."""


def _mapping(value: object, field: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise MetadataValidationError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetadataValidationError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise MetadataValidationError(f"{field} must not contain surrounding whitespace")
    return value


def _identifier(value: object, field: str, pattern: re.Pattern[str]) -> str:
    result = _string(value, field)
    if not pattern.fullmatch(result):
        raise MetadataValidationError(f"{field} has an invalid identifier format")
    return result


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MetadataValidationError(f"{field} must be a non-negative integer")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MetadataValidationError(f"{field} must be a list")

    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{field}[{index}]"))

    if len(set(result)) != len(result):
        raise MetadataValidationError(f"{field} contains duplicate values")

    return tuple(result)


def _filter_types(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MetadataValidationError(f"{field} must be a list")

    result: list[str] = []
    for index, item in enumerate(value):
        record = _mapping(item, f"{field}[{index}]")
        filter_type = _string(
            record.get("filterType"),
            f"{field}[{index}].filterType",
        )
        result.append(filter_type)

    if len(set(result)) != len(result):
        raise MetadataValidationError(f"{field} contains duplicate filterType values")

    # Unknown filter types are preserved. This module does not interpret
    # trading-filter arithmetic such as tickSize or stepSize.
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SymbolMetadata:
    market: Market
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    order_types: tuple[str, ...]
    filter_types: tuple[str, ...]
    pair: str | None = None
    contract_type: str | None = None
    margin_asset: str | None = None
    delivery_date: int | None = None
    onboard_date: int | None = None
    price_precision: int | None = None
    quantity_precision: int | None = None

    @property
    def is_active(self) -> bool:
        """Only Binance's exact TRADING status is active."""
        return self.status == "TRADING"


@dataclass(frozen=True, slots=True)
class SymbolMetadataSnapshot:
    market: Market
    server_time_ms: int
    fetched_at_monotonic: float
    max_age_seconds: float
    symbols: Mapping[str, SymbolMetadata]
    assets: frozenset[str] = frozenset()

    def age_seconds(self, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else now_monotonic

        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise MetadataStaleError("Monotonic clock returned an invalid value")

        if not math.isfinite(now):
            raise MetadataStaleError("Monotonic clock returned a non-finite value")

        age = float(now) - self.fetched_at_monotonic

        if not math.isfinite(age) or age < 0:
            raise MetadataStaleError("Monotonic clock regressed or metadata time is invalid")

        return age

    def require_fresh(self, now_monotonic: float | None = None) -> None:
        age = self.age_seconds(now_monotonic)

        if age > self.max_age_seconds:
            raise MetadataStaleError(
                f"{self.market} metadata expired: age={age:.3f}s, "
                f"maximum={self.max_age_seconds:.3f}s"
            )

    def require_symbol(
        self,
        symbol: str,
        *,
        now_monotonic: float | None = None,
    ) -> SymbolMetadata:
        self.require_fresh(now_monotonic)

        if not isinstance(symbol, str) or not symbol:
            raise MetadataUnavailableError("A non-empty symbol is required")

        record = self.symbols.get(symbol)
        if record is None:
            raise SymbolNotFoundError(
                f"Symbol {symbol!r} is not present in the {self.market} metadata snapshot"
            )

        if not record.is_active:
            raise SymbolNotTradingError(
                f"Symbol {symbol!r} is not eligible: status={record.status!r}"
            )

        return record


def _snapshot_time(
    fetched_at_monotonic: float | None,
    max_age_seconds: float,
) -> float:
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(max_age_seconds)
        or max_age_seconds <= 0
        or max_age_seconds > METADATA_MAX_AGE_SECONDS
    ):
        raise MetadataValidationError(
            f"max_age_seconds must be greater than zero and no more than "
            f"{METADATA_MAX_AGE_SECONDS:g}"
        )

    fetched = time.monotonic() if fetched_at_monotonic is None else fetched_at_monotonic

    if (
        isinstance(fetched, bool)
        or not isinstance(fetched, (int, float))
        or not math.isfinite(fetched)
        or fetched < 0
    ):
        raise MetadataValidationError("fetched_at_monotonic must be finite and non-negative")

    return float(fetched)


def _common_payload(payload: object) -> tuple[Mapping, int, list]:
    root = _mapping(payload, "exchangeInfo")

    server_time = _nonnegative_int(
        root.get("serverTime"),
        "exchangeInfo.serverTime",
    )

    symbols = root.get("symbols")
    if not isinstance(symbols, list):
        raise MetadataValidationError("exchangeInfo.symbols must be a list")

    return root, server_time, symbols


def _parse_symbol_common(
    raw: object,
    *,
    market: Market,
    index: int,
) -> SymbolMetadata:
    field = f"exchangeInfo.symbols[{index}]"
    record = _mapping(raw, field)

    if market == "spot":
        symbol = _identifier(record.get("symbol"), f"{field}.symbol", _SYMBOL_RE)
        base_asset = _identifier(record.get("baseAsset"), f"{field}.baseAsset", _ASSET_RE)
    else:
        symbol = _provider_identifier(
            record.get("symbol"), f"{field}.symbol", allow_delivery_suffix=True
        )
        base_asset = _provider_identifier(record.get("baseAsset"), f"{field}.baseAsset")
    status = _string(record.get("status"), f"{field}.status")
    quote_asset = _identifier(record.get("quoteAsset"), f"{field}.quoteAsset", _ASSET_RE)

    if base_asset == quote_asset:
        raise MetadataValidationError(f"{field} baseAsset and quoteAsset must differ")

    order_types = _string_list(record.get("orderTypes"), f"{field}.orderTypes")
    filter_types = _filter_types(record.get("filters"), f"{field}.filters")

    if market == "spot":
        return SymbolMetadata(
            market="spot",
            symbol=symbol,
            status=status,
            base_asset=base_asset,
            quote_asset=quote_asset,
            order_types=order_types,
            filter_types=filter_types,
        )

    pair = _provider_identifier(record.get("pair"), f"{field}.pair")
    contract_type = _string(record.get("contractType"), f"{field}.contractType")
    margin_asset = _identifier(
        record.get("marginAsset"),
        f"{field}.marginAsset",
        _ASSET_RE,
    )

    delivery_date = _nonnegative_int(
        record.get("deliveryDate"),
        f"{field}.deliveryDate",
    )
    onboard_date = _nonnegative_int(
        record.get("onboardDate"),
        f"{field}.onboardDate",
    )
    price_precision = _nonnegative_int(
        record.get("pricePrecision"),
        f"{field}.pricePrecision",
    )
    quantity_precision = _nonnegative_int(
        record.get("quantityPrecision"),
        f"{field}.quantityPrecision",
    )

    return SymbolMetadata(
        market="usdm_futures",
        symbol=symbol,
        status=status,
        base_asset=base_asset,
        quote_asset=quote_asset,
        order_types=order_types,
        filter_types=filter_types,
        pair=pair,
        contract_type=contract_type,
        margin_asset=margin_asset,
        delivery_date=delivery_date,
        onboard_date=onboard_date,
        price_precision=price_precision,
        quantity_precision=quantity_precision,
    )


def _build_snapshot(
    *,
    market: Market,
    server_time_ms: int,
    records: list[SymbolMetadata],
    fetched_at_monotonic: float | None,
    max_age_seconds: float,
    assets: frozenset[str] = frozenset(),
) -> SymbolMetadataSnapshot:
    fetched = _snapshot_time(fetched_at_monotonic, max_age_seconds)

    symbol_map: dict[str, SymbolMetadata] = {}
    for record in records:
        if record.symbol in symbol_map:
            raise MetadataValidationError(
                f"Duplicate {market} symbol in exchangeInfo: {record.symbol}"
            )
        symbol_map[record.symbol] = record

    return SymbolMetadataSnapshot(
        market=market,
        server_time_ms=server_time_ms,
        fetched_at_monotonic=fetched,
        max_age_seconds=float(max_age_seconds),
        symbols=MappingProxyType(symbol_map),
        assets=assets,
    )


def parse_spot_exchange_info(
    payload: object,
    *,
    fetched_at_monotonic: float | None = None,
    max_age_seconds: float = METADATA_MAX_AGE_SECONDS,
) -> SymbolMetadataSnapshot:
    """Parse Spot exchangeInfo into an immutable, independent snapshot."""
    _, server_time_ms, raw_symbols = _common_payload(payload)

    records = [
        _parse_symbol_common(raw, market="spot", index=index)
        for index, raw in enumerate(raw_symbols)
    ]

    return _build_snapshot(
        market="spot",
        server_time_ms=server_time_ms,
        records=records,
        fetched_at_monotonic=fetched_at_monotonic,
        max_age_seconds=max_age_seconds,
    )


def parse_usdm_futures_exchange_info(
    payload: object,
    *,
    fetched_at_monotonic: float | None = None,
    max_age_seconds: float = METADATA_MAX_AGE_SECONDS,
) -> SymbolMetadataSnapshot:
    """Parse USD-M Futures exchangeInfo without a restrictive contract-type allowlist.

    Any structurally valid contract type is retained. A symbol is eligible
    when its status is exactly TRADING, regardless of whether its contract
    type is known to this implementation.
    """
    root, server_time_ms, raw_symbols = _common_payload(payload)

    raw_assets = root.get("assets")
    if not isinstance(raw_assets, list):
        raise MetadataValidationError("exchangeInfo.assets must be a list")

    assets: set[str] = set()

    for index, raw_asset in enumerate(raw_assets):
        field = f"exchangeInfo.assets[{index}]"
        asset_record = _mapping(raw_asset, field)
        asset = _identifier(asset_record.get("asset"), f"{field}.asset", _ASSET_RE)

        margin_available = asset_record.get("marginAvailable")
        if not isinstance(margin_available, bool):
            raise MetadataValidationError(f"{field}.marginAvailable must be a boolean")

        auto_asset_exchange = asset_record.get("autoAssetExchange")
        if (
            not isinstance(auto_asset_exchange, str)
            or auto_asset_exchange != auto_asset_exchange.strip()
        ):
            raise MetadataValidationError(
                f"{field}.autoAssetExchange must be a string without surrounding whitespace"
            )

        if asset in assets:
            raise MetadataValidationError(f"Duplicate Futures asset: {asset}")

        assets.add(asset)

    records = [
        _parse_symbol_common(raw, market="usdm_futures", index=index)
        for index, raw in enumerate(raw_symbols)
    ]

    return _build_snapshot(
        market="usdm_futures",
        server_time_ms=server_time_ms,
        records=records,
        fetched_at_monotonic=fetched_at_monotonic,
        max_age_seconds=max_age_seconds,
        assets=frozenset(assets),
    )