from dataclasses import dataclass
from decimal import Decimal, localcontext


@dataclass(frozen=True, slots=True)
class FuturesDepthFillLevel:
    price: Decimal
    quantity: Decimal
    notional: Decimal


@dataclass(frozen=True, slots=True)
class FuturesDepthFill:
    side: str
    requested_quantity: Decimal
    executed_quantity: Decimal
    vwap: Decimal
    gross_value: Decimal
    levels: tuple[FuturesDepthFillLevel, ...]

    @property
    def level_count(self) -> int:
        return len(self.levels)


def _positive_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _validate_levels(
    levels: object,
    *,
    name: str,
    descending: bool,
    tick_size: Decimal,
) -> tuple[tuple[Decimal, Decimal], ...]:
    if type(levels) is not tuple or not levels:
        raise ValueError(f"{name} must be a non-empty tuple")

    validated = []
    seen_prices = set()

    for index, level in enumerate(levels):
        if type(level) is not tuple or len(level) != 2:
            raise ValueError(f"{name}[{index}] must be a 2-item tuple")

        price = _positive_decimal(level[0], f"{name}[{index}].price")
        quantity = _positive_decimal(level[1], f"{name}[{index}].quantity")

        if price in seen_prices:
            raise ValueError(f"{name} contains duplicate prices")
        seen_prices.add(price)

        if price % tick_size != 0:
            raise ValueError(f"{name}[{index}].price is not tick aligned")

        validated.append((price, quantity))

    prices = [level[0] for level in validated]
    expected = sorted(prices, reverse=descending)
    if prices != expected:
        raise ValueError(f"{name} is not correctly price-sorted")

    return tuple(validated)


def calculate_futures_depth_fill(
    *,
    side: str,
    quantity: Decimal,
    bids: tuple[tuple[Decimal, Decimal], ...],
    asks: tuple[tuple[Decimal, Decimal], ...],
    tick_size: Decimal,
) -> FuturesDepthFill:
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")

    requested = _positive_decimal(quantity, "quantity")
    tick = _positive_decimal(tick_size, "tick_size")

    validated_bids = _validate_levels(
        bids, name="bids", descending=True, tick_size=tick
    )
    validated_asks = _validate_levels(
        asks, name="asks", descending=False, tick_size=tick
    )

    if validated_bids[0][0] > validated_asks[0][0]:
        raise ValueError("order book is crossed")

    source = validated_asks if side == "buy" else validated_bids
    remaining = requested
    fills = []
    gross_value = Decimal("0")

    with localcontext() as context:
        context.prec = 120

        for price, available in source:
            if remaining <= 0:
                break

            consumed = min(remaining, available)
            notional = price * consumed
            fills.append(
                FuturesDepthFillLevel(
                    price=price,
                    quantity=consumed,
                    notional=notional,
                )
            )
            gross_value += notional
            remaining -= consumed

        if remaining != 0:
            raise ValueError("insufficient depth for full fill")

        executed = sum(
            (fill.quantity for fill in fills), Decimal("0")
        )
        vwap = gross_value / executed

    return FuturesDepthFill(
        side=side,
        requested_quantity=requested,
        executed_quantity=executed,
        vwap=vwap,
        gross_value=gross_value,
        levels=tuple(fills),
    )
