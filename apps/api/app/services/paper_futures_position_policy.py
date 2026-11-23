from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class FuturesPositionDecision:
    action: str
    position: Any | None


def _request_position_id(value: Any) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError("position_id must be a canonical lowercase UUIDv4 or None")

    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid position_id") from exc

    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("position_id must be a canonical lowercase UUIDv4 or None")

    return value


def _position_identifier(position: Any) -> str:
    value = getattr(position, "id", None)

    if isinstance(value, UUID):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Existing position has an invalid identifier") from exc
    else:
        raise ValueError("Existing position has an invalid identifier")

    if parsed.version != 4:
        raise ValueError("Existing position identifier must be UUIDv4")

    return str(parsed)


def _validate_open_positions(
    symbol: str,
    open_positions: Sequence[Any],
) -> list[Any]:
    if isinstance(open_positions, (str, bytes)) or not isinstance(
        open_positions, Sequence
    ):
        raise ValueError("open_positions must be a sequence")

    result = []

    for position in open_positions:
        if getattr(position, "market_type", None) != "futures":
            raise ValueError("Position market type mismatch")

        if getattr(position, "symbol", None) != symbol:
            raise ValueError("Position symbol mismatch")

        if getattr(position, "status", None) != "open":
            raise ValueError("Only open positions may be supplied")

        if getattr(position, "side", None) not in ("long", "short"):
            raise ValueError("Existing position has invalid side")

        quantity = getattr(position, "remaining_quantity", None)

        if (
            not isinstance(quantity, Decimal)
            or not quantity.is_finite()
            or quantity <= 0
        ):
            raise ValueError("Existing position quantity is invalid")

        _position_identifier(position)
        result.append(position)

    return result


def resolve_futures_position_action(
    *,
    position_mode: str,
    symbol: str,
    side: str,
    position_side: str,
    quantity: Decimal,
    reduce_only: bool,
    position_id: str | None,
    open_positions: Sequence[Any],
) -> FuturesPositionDecision:

    if position_mode not in ("one_way", "hedge"):
        raise ValueError("Unsupported Futures position mode")

    if (
        not isinstance(symbol, str)
        or not symbol
        or symbol != symbol.upper()
    ):
        raise ValueError("symbol must be non-empty uppercase text")

    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")

    if position_side not in ("long", "short"):
        raise ValueError("position_side must be long or short")

    if (
        not isinstance(quantity, Decimal)
        or not quantity.is_finite()
        or quantity <= 0
    ):
        raise ValueError("quantity must be a positive finite Decimal")

    if not isinstance(reduce_only, bool):
        raise ValueError("reduce_only must be boolean")

    requested_id = _request_position_id(position_id)
    positions = _validate_open_positions(symbol, open_positions)

    opening_side = {"long": "buy", "short": "sell"}[position_side]
    closing_side = {"long": "sell", "short": "buy"}[position_side]

    if position_mode == "one_way":
        if len(positions) > 1:
            raise ValueError("One-way mode permits at most one open position per symbol")

        current = positions[0] if positions else None

        if current is None:
            if side != opening_side:
                raise ValueError("New one-way position direction mismatch")
            if reduce_only:
                raise ValueError("Cannot reduce a position that does not exist")
            if requested_id is not None:
                raise ValueError("A new position must not supply position_id")

            return FuturesPositionDecision("open", None)

        current_side = current.side
        current_id = _position_identifier(current)

        if position_side != current_side:
            raise ValueError("One-way position_side must match existing position")

        if side == opening_side:
            if reduce_only:
                raise ValueError("Increase cannot be reduce-only")

            if requested_id is not None and requested_id != current_id:
                raise ValueError("position_id does not match existing position")

            return FuturesPositionDecision("increase", current)

        if side != closing_side:
            raise ValueError("Invalid one-way transition")

        if not reduce_only:
            raise ValueError("Opposite-direction orders must be reduce-only")

        if requested_id != current_id:
            raise ValueError("Closing requires the exact existing position_id")

        if quantity > current.remaining_quantity:
            raise ValueError("Close quantity exceeds remaining quantity; reversal is prohibited")

        action = "close" if quantity == current.remaining_quantity else "reduce"
        return FuturesPositionDecision(action, current)

    matching = [
        position for position in positions
        if position.side == position_side
    ]

    if len(matching) > 1:
        raise ValueError("Hedge mode permits at most one open position per symbol and side")

    current = matching[0] if matching else None

    if side == opening_side:
        if reduce_only:
            raise ValueError("Opening or increasing cannot be reduce-only")

        if current is None:
            if requested_id is not None:
                raise ValueError("New hedge position must not supply position_id")

            return FuturesPositionDecision("open", None)

        current_id = _position_identifier(current)

        if requested_id is not None and requested_id != current_id:
            raise ValueError("position_id does not match selected hedge leg")

        return FuturesPositionDecision("increase", current)

    if side != closing_side:
        raise ValueError("Invalid hedge transition")

    if not reduce_only:
        raise ValueError("Hedge closing orders must be reduce-only")

    if current is None:
        raise ValueError("Selected hedge position does not exist")

    current_id = _position_identifier(current)

    if requested_id != current_id:
        raise ValueError("Hedge closing requires exact position_id")

    if quantity > current.remaining_quantity:
        raise ValueError("Close quantity exceeds remaining quantity; reversal is prohibited")

    action = "close" if quantity == current.remaining_quantity else "reduce"
    return FuturesPositionDecision(action, current)
