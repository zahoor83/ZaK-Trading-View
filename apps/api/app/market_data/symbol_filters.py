"""Exact, fail-closed Binance Spot and USD-M filter validation.

This module performs local validation only. It never sends orders or
performs network requests.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from app.market_data.symbol_metadata import (
    SymbolMetadataSnapshot,
)

MAX_DECIMAL_TEXT_LENGTH = 128
MAX_DECIMAL_SCALE = 36
MAX_DECIMAL_ABSOLUTE_VALUE = Decimal("1e36")

_DECIMAL_RE = re.compile(
    r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"
)

SUPPORTED_FILTERS = frozenset({
    "PRICE_FILTER",
    "LOT_SIZE",
    "MARKET_LOT_SIZE",
    "MIN_NOTIONAL",
    "NOTIONAL",
    "PERCENT_PRICE",
    "PERCENT_PRICE_BY_SIDE",
})


class FilterValidationError(ValueError):
    """An order or filter cannot be validated safely."""


def parse_decimal(value: object, field: str = "value") -> Decimal:
    """Parse strict decimal text without float conversion."""

    if isinstance(value, bool) or isinstance(value, float):
        raise FilterValidationError(f"{field}: bool and float are prohibited")

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FilterValidationError(f"{field}: non-finite Decimal")
        text = format(value, "f")
    elif isinstance(value, str):
        text = value
    else:
        raise FilterValidationError(f"{field}: expected decimal text")

    if len(text) > MAX_DECIMAL_TEXT_LENGTH:
        raise FilterValidationError(f"{field}: text is too long")

    if not _DECIMAL_RE.fullmatch(text):
        raise FilterValidationError(
            f"{field}: whitespace, exponent or malformed decimal"
        )

    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise FilterValidationError(f"{field}: invalid decimal") from exc

    if not result.is_finite():
        raise FilterValidationError(f"{field}: non-finite value")

    if max(0, -result.as_tuple().exponent) > MAX_DECIMAL_SCALE:
        raise FilterValidationError(f"{field}: excessive fractional scale")

    if result.copy_abs() >= MAX_DECIMAL_ABSOLUTE_VALUE:
        raise FilterValidationError(f"{field}: value exceeds safe bound")

    return result


@dataclass(frozen=True, slots=True)
class ReferencePrice:
    """A caller-supplied verified market reference with a monotonic TTL."""

    price: str | Decimal
    fetched_at_monotonic: float
    max_age_seconds: float

    def __post_init__(self):
        parsed = parse_decimal(self.price, "reference_price")

        if parsed <= 0:
            raise FilterValidationError("Reference price must be positive")

        for name, value in (
            ("fetched_at_monotonic", self.fetched_at_monotonic),
            ("max_age_seconds", self.max_age_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not __import__("math").isfinite(value)
            ):
                raise FilterValidationError(f"Invalid {name}")

        if self.fetched_at_monotonic < 0 or self.max_age_seconds <= 0:
            raise FilterValidationError("Invalid reference price time bounds")

        object.__setattr__(self, "price", parsed)

    def require_fresh(self, now_monotonic: float | None = None) -> Decimal:
        now = time.monotonic() if now_monotonic is None else now_monotonic

        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not __import__("math").isfinite(now)
        ):
            raise FilterValidationError("Invalid monotonic clock")

        age = now - self.fetched_at_monotonic

        if age < 0 or age > self.max_age_seconds:
            raise FilterValidationError("Reference price is stale or invalid")

        return self.price


def _field(record, name: str, filter_type: str) -> Decimal:
    if name not in record:
        raise FilterValidationError(
            f"{filter_type}: missing required field {name}"
        )

    value = parse_decimal(record[name], f"{filter_type}.{name}")

    if value < 0:
        raise FilterValidationError(
            f"{filter_type}.{name}: negative value"
        )

    return value


def _required_bool(record, name: str, filter_type: str) -> bool:
    value = record.get(name)

    if not isinstance(value, bool):
        raise FilterValidationError(
            f"{filter_type}.{name}: expected boolean"
        )

    return value


def _required_int(record, name: str, filter_type: str) -> int:
    value = record.get(name)

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FilterValidationError(
            f"{filter_type}.{name}: expected non-negative integer"
        )

    return value


def _check_grid(value: Decimal, origin: Decimal, step: Decimal, name: str):
    if step <= 0:
        raise FilterValidationError(f"{name}: step size must be positive")

    with localcontext() as ctx:
        ctx.prec = 100

        if (value - origin) % step != 0:
            raise FilterValidationError(f"{name}: value is off-grid")


def _check_range(value, minimum, maximum, name):
    if minimum != 0 and value < minimum:
        raise FilterValidationError(f"{name}: below minimum")

    if maximum != 0 and value > maximum:
        raise FilterValidationError(f"{name}: above maximum")

    if minimum != 0 and maximum != 0 and minimum > maximum:
        raise FilterValidationError(f"{name}: malformed minimum/maximum")


def _check_lot(record, quantity, market, filter_type):
    minimum = _field(record, "minQty", filter_type)
    maximum = _field(record, "maxQty", filter_type)
    step = _field(record, "stepSize", filter_type)

    if step == 0:
        raise FilterValidationError(
            f"{filter_type}: zero stepSize is invalid"
        )

    _check_range(quantity, minimum, maximum, filter_type)

    # Spot grids are zero-based; USD-M Futures grids are minQty-offset.
    origin = minimum if market == "usdm_futures" else Decimal(0)
    _check_grid(quantity, origin, step, filter_type)


def _check_price(record, price, market):
    filter_type = "PRICE_FILTER"

    minimum = _field(record, "minPrice", filter_type)
    maximum = _field(record, "maxPrice", filter_type)
    tick = _field(record, "tickSize", filter_type)

    _check_range(price, minimum, maximum, filter_type)

    if tick > 0:
        origin = minimum if market == "usdm_futures" else Decimal(0)
        _check_grid(price, origin, tick, filter_type)


def _check_percent(record, price, reference, side, filter_type):
    if reference is None:
        raise FilterValidationError(
            f"{filter_type}: verified reference price required"
        )

    if filter_type == "PERCENT_PRICE_BY_SIDE":
        if side == "BUY":
            up_name, down_name = "bidMultiplierUp", "bidMultiplierDown"
        else:
            up_name, down_name = "askMultiplierUp", "askMultiplierDown"
    else:
        up_name, down_name = "multiplierUp", "multiplierDown"

    upper = _field(record, up_name, filter_type)
    lower = _field(record, down_name, filter_type)

    if lower > upper:
        raise FilterValidationError(f"{filter_type}: invalid multiplier range")

    if price > reference * upper or price < reference * lower:
        raise FilterValidationError(f"{filter_type}: price outside reference range")


def _check_notional(
    record,
    market,
    order_type,
    price,
    quantity,
    reference,
    filter_type,
):
    if market == "usdm_futures":
        if filter_type != "MIN_NOTIONAL":
            raise FilterValidationError(
                f"{filter_type}: unsupported for USD-M Futures"
            )

        minimum = _field(record, "notional", filter_type)
        maximum = Decimal(0)
        apply_min = True
        apply_max = False
    elif filter_type == "MIN_NOTIONAL":
        minimum = _field(record, "minNotional", filter_type)
        maximum = Decimal(0)
        apply_min = (
            True if order_type == "LIMIT"
            else _required_bool(record, "applyToMarket", filter_type)
        )
        apply_max = False

        if "avgPriceMins" in record:
            _required_int(record, "avgPriceMins", filter_type)
    else:
        minimum = _field(record, "minNotional", filter_type)
        maximum = _field(record, "maxNotional", filter_type)

        apply_min = (
            True if order_type == "LIMIT"
            else _required_bool(record, "applyMinToMarket", filter_type)
        )
        apply_max = (
            True if order_type == "LIMIT"
            else _required_bool(record, "applyMaxToMarket", filter_type)
        )

        if "avgPriceMins" in record:
            _required_int(record, "avgPriceMins", filter_type)

    if not apply_min and not apply_max:
        return

    basis_price = price if order_type == "LIMIT" else reference

    if basis_price is None:
        raise FilterValidationError(
            f"{filter_type}: verified reference price required"
        )

    notional = basis_price * quantity

    if apply_min and minimum != 0 and notional < minimum:
        raise FilterValidationError(f"{filter_type}: below minimum notional")

    if apply_max and maximum != 0 and notional > maximum:
        raise FilterValidationError(f"{filter_type}: above maximum notional")



def get_price_tick_size(
    snapshot: SymbolMetadataSnapshot,
    symbol: str,
    *,
    now_monotonic: float | None = None,
) -> Decimal:
    """Return a strictly validated tick size from fresh symbol metadata."""

    if not isinstance(snapshot, SymbolMetadataSnapshot):
        raise FilterValidationError(
            "A verified symbol metadata snapshot is required."
        )

    metadata = snapshot.require_symbol(
        symbol,
        now_monotonic=now_monotonic,
    )

    records = metadata.filters

    if not isinstance(records, tuple):
        raise FilterValidationError("Symbol filter metadata is invalid.")

    price_filters = []

    for record in records:
        if not hasattr(record, "get"):
            raise FilterValidationError("Symbol filter record is invalid.")

        if record.get("filterType") == "PRICE_FILTER":
            price_filters.append(record)

    if not price_filters:
        raise FilterValidationError(
            "Mandatory PRICE_FILTER metadata missing."
        )

    if len(price_filters) != 1:
        raise FilterValidationError(
            "Duplicate PRICE_FILTER metadata."
        )

    tick_size = _field(
        price_filters[0],
        "tickSize",
        "PRICE_FILTER",
    )

    if tick_size <= 0:
        raise FilterValidationError(
            "PRICE_FILTER.tickSize must be positive."
        )

    return tick_size

def validate_order(
    snapshot: SymbolMetadataSnapshot,
    symbol: str,
    *,
    order_type: Literal["LIMIT", "MARKET"],
    side: Literal["BUY", "SELL"],
    quantity: str | Decimal,
    price: str | Decimal | None = None,
    reference_price: ReferencePrice | None = None,
    now_monotonic: float | None = None,
) -> None:
    """Validate a simulated order against a fresh symbol snapshot.

    This function only validates. It never submits or executes an order.
    """

    if not isinstance(snapshot, SymbolMetadataSnapshot):
        raise FilterValidationError(
            "A verified symbol metadata snapshot is required."
        )

    with localcontext() as ctx:
        ctx.prec = 100

        metadata = snapshot.require_symbol(
            symbol,
            now_monotonic=now_monotonic,
        )

        if order_type not in ("LIMIT", "MARKET"):
            raise FilterValidationError("Unsupported order type")

        if side not in ("BUY", "SELL"):
            raise FilterValidationError("Unsupported order side")

        if order_type not in metadata.order_types:
            raise FilterValidationError("Order type is not supported by symbol")

        qty = parse_decimal(quantity, "quantity")

        if qty <= 0:
            raise FilterValidationError("Quantity must be positive")

        if order_type == "LIMIT":
            if price is None:
                raise FilterValidationError("LIMIT order requires price")
            order_price = parse_decimal(price, "price")
            if order_price <= 0:
                raise FilterValidationError("Price must be positive")
        else:
            if price is not None:
                raise FilterValidationError(
                    "MARKET order must not provide a limit price"
                )
            order_price = None

        records = metadata.filters

        if not records:
            raise FilterValidationError("Symbol filter metadata is missing")

        filters = {}
        for record in records:
            filter_type = record.get("filterType")

            if not isinstance(filter_type, str) or filter_type in filters:
                raise FilterValidationError("Invalid or duplicate filter metadata")

            filters[filter_type] = record

        unsupported = set(filters) - SUPPORTED_FILTERS

        if unsupported:
            raise FilterValidationError(
                "Unsupported filters: " + ", ".join(sorted(unsupported))
            )

        if "LOT_SIZE" not in filters:
            raise FilterValidationError("Mandatory LOT_SIZE filter missing")

        if order_type == "LIMIT":
            if "PRICE_FILTER" not in filters:
                raise FilterValidationError("Mandatory PRICE_FILTER missing")
            _check_price(filters["PRICE_FILTER"], order_price, metadata.market)

        _check_lot(filters["LOT_SIZE"], qty, metadata.market, "LOT_SIZE")

        if order_type == "MARKET" and "MARKET_LOT_SIZE" in filters:
            _check_lot(
                filters["MARKET_LOT_SIZE"],
                qty,
                metadata.market,
                "MARKET_LOT_SIZE",
            )

        dynamic_needed = order_type == "LIMIT" and any(
            key in filters
            for key in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE")
        )

        notional_reference_needed = (
            order_type == "MARKET"
            and (
                metadata.market == "usdm_futures"
                and "MIN_NOTIONAL" in filters
                or "MIN_NOTIONAL" in filters
                and filters["MIN_NOTIONAL"].get("applyToMarket") is True
                or "NOTIONAL" in filters
                and (
                    filters["NOTIONAL"].get("applyMinToMarket") is True
                    or filters["NOTIONAL"].get("applyMaxToMarket") is True
                )
            )
        )

        if dynamic_needed or notional_reference_needed:
            if reference_price is None:
                raise FilterValidationError(
                    "Verified reference price required"
                )
            reference = reference_price.require_fresh(now_monotonic)
        else:
            reference = (
                reference_price.require_fresh(now_monotonic)
                if reference_price is not None
                else None
            )

        if order_type == "LIMIT":
            for key in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
                if key in filters:
                    if key == "PERCENT_PRICE_BY_SIDE" and metadata.market != "spot":
                        raise FilterValidationError(
                            "PERCENT_PRICE_BY_SIDE is unsupported for this market"
                        )
                    _check_percent(
                        filters[key],
                        order_price,
                        reference,
                        side,
                        key,
                    )

        for key in ("MIN_NOTIONAL", "NOTIONAL"):
            if key in filters:
                _check_notional(
                    filters[key],
                    metadata.market,
                    order_type,
                    order_price,
                    qty,
                    reference,
                    key,
                )

        return None