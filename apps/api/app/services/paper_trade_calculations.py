"""Pure Decimal Spot and linear USD-M Futures paper-trade calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext

from app.services.paper_ledger_rules import (
    JournalEntryInput,
    PaperLedgerRulesError,
    validate_decimal,
)


CENT = Decimal("0.01")
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class TradeCalculation:
    transaction_type: str
    quantity: Decimal
    execution_price: Decimal
    gross_value: Decimal
    fee_original: Decimal
    fee_posted: Decimal
    journal_entries: tuple[JournalEntryInput, ...]
    realized_pnl: Decimal = ZERO
    released_cost_basis: Decimal = ZERO
    released_margin: Decimal = ZERO
    remaining_quantity: Decimal = ZERO
    remaining_cost_basis: Decimal = ZERO
    remaining_margin: Decimal = ZERO


def _positive(value, name: str) -> Decimal:
    try:
        return validate_decimal(value, allow_negative=False, allow_zero=False)
    except PaperLedgerRulesError as exc:
        raise PaperLedgerRulesError(f"{name} must be a positive finite Decimal.") from exc


def _nonnegative(value, name: str) -> Decimal:
    try:
        return validate_decimal(value, allow_negative=False, allow_zero=True)
    except PaperLedgerRulesError as exc:
        raise PaperLedgerRulesError(f"{name} must be a non-negative finite Decimal.") from exc


def _fee(notional: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    with localcontext() as ctx:
        ctx.prec = 120
        original = validate_decimal(notional * rate, allow_negative=False)
        posted = original.quantize(CENT, rounding=ROUND_HALF_UP)

    return original, validate_decimal(posted, allow_negative=False)


def _sum_safe(*values: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 120
        return validate_decimal(sum(values, ZERO), allow_negative=False)


def _proportional(value: Decimal, numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 120
        result = value * numerator / denominator
    return validate_decimal(result, allow_negative=False)


def _entry(code: str, direction: str, amount: Decimal) -> JournalEntryInput:
    amount = validate_decimal(amount, allow_negative=False, allow_zero=False)
    return JournalEntryInput(code, direction, amount)


def calculate_spot_buy(
    *,
    quantity: Decimal,
    price: Decimal,
    fee_rate: Decimal,
) -> TradeCalculation:
    quantity = _positive(quantity, "quantity")
    price = _positive(price, "price")
    fee_rate = _nonnegative(fee_rate, "fee_rate")

    with localcontext() as ctx:
        ctx.prec = 120
        gross = validate_decimal(quantity * price, allow_negative=False, allow_zero=False)

    fee_original, fee_posted = _fee(gross, fee_rate)
    cash_outflow = _sum_safe(gross, fee_posted)

    entries = [
        _entry("spot_position_cost", "debit", gross),
    ]
    if fee_posted:
        entries.append(_entry("fee_expense", "debit", fee_posted))
    entries.append(_entry("cash_available", "credit", cash_outflow))

    return TradeCalculation(
        transaction_type="spot_buy",
        quantity=quantity,
        execution_price=price,
        gross_value=gross,
        fee_original=fee_original,
        fee_posted=fee_posted,
        journal_entries=tuple(entries),
        remaining_quantity=quantity,
        remaining_cost_basis=gross,
    )


def calculate_spot_sell(
    *,
    quantity: Decimal,
    price: Decimal,
    remaining_quantity: Decimal,
    remaining_cost_basis: Decimal,
    fee_rate: Decimal,
) -> TradeCalculation:
    quantity = _positive(quantity, "quantity")
    price = _positive(price, "price")
    remaining_quantity = _positive(remaining_quantity, "remaining_quantity")
    remaining_cost_basis = _nonnegative(remaining_cost_basis, "remaining_cost_basis")
    fee_rate = _nonnegative(fee_rate, "fee_rate")

    if quantity > remaining_quantity:
        raise PaperLedgerRulesError("Sell quantity exceeds the open Spot position.")

    with localcontext() as ctx:
        ctx.prec = 120
        gross = validate_decimal(quantity * price, allow_negative=False, allow_zero=False)

    fee_original, fee_posted = _fee(gross, fee_rate)
    released_basis = (
        remaining_cost_basis
        if quantity == remaining_quantity
        else _proportional(remaining_cost_basis, quantity, remaining_quantity)
    )

    net_proceeds = validate_decimal(
        gross - fee_posted,
        allow_negative=False,
    )
    # Fee is posted separately to fee_expense. Realized trading P&L
    # therefore uses gross proceeds less released cost basis.
    pnl = validate_decimal(gross - released_basis)

    entries = [_entry("cash_available", "debit", net_proceeds)] if net_proceeds else []
    entries.append(_entry("spot_position_cost", "credit", released_basis))

    if pnl > ZERO:
        entries.append(_entry("realized_pnl", "credit", pnl))
    elif pnl < ZERO:
        entries.append(_entry("realized_pnl", "debit", -pnl))

    if fee_posted:
        entries.append(_entry("fee_expense", "debit", fee_posted))

    left_qty = validate_decimal(remaining_quantity - quantity, allow_negative=False)
    left_basis = validate_decimal(remaining_cost_basis - released_basis, allow_negative=False)

    return TradeCalculation(
        transaction_type="spot_sell",
        quantity=quantity,
        execution_price=price,
        gross_value=gross,
        fee_original=fee_original,
        fee_posted=fee_posted,
        journal_entries=tuple(entries),
        realized_pnl=pnl,
        released_cost_basis=released_basis,
        remaining_quantity=left_qty,
        remaining_cost_basis=left_basis,
    )


def calculate_futures_open(
    *,
    quantity: Decimal,
    price: Decimal,
    leverage: Decimal,
    fee_rate: Decimal,
) -> TradeCalculation:
    quantity = _positive(quantity, "quantity")
    price = _positive(price, "price")
    leverage = _positive(leverage, "leverage")
    fee_rate = _nonnegative(fee_rate, "fee_rate")

    with localcontext() as ctx:
        ctx.prec = 120
        notional = validate_decimal(quantity * price, allow_negative=False, allow_zero=False)
        margin = validate_decimal(notional / leverage, allow_negative=False, allow_zero=False)

    fee_original, fee_posted = _fee(notional, fee_rate)
    cash_outflow = _sum_safe(margin, fee_posted)

    entries = [_entry("futures_margin_locked", "debit", margin)]
    if fee_posted:
        entries.append(_entry("fee_expense", "debit", fee_posted))
    entries.append(_entry("cash_available", "credit", cash_outflow))

    return TradeCalculation(
        transaction_type="futures_open",
        quantity=quantity,
        execution_price=price,
        gross_value=notional,
        fee_original=fee_original,
        fee_posted=fee_posted,
        journal_entries=tuple(entries),
        remaining_quantity=quantity,
        remaining_margin=margin,
    )


def calculate_futures_close(
    *,
    side: str,
    quantity: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    remaining_quantity: Decimal,
    remaining_margin: Decimal,
    leverage: Decimal,
    fee_rate: Decimal,
) -> TradeCalculation:
    if type(side) is not str or side not in {"long", "short"}:
        raise PaperLedgerRulesError("Futures side must be long or short.")

    quantity = _positive(quantity, "quantity")
    entry_price = _positive(entry_price, "entry_price")
    exit_price = _positive(exit_price, "exit_price")
    remaining_quantity = _positive(remaining_quantity, "remaining_quantity")
    remaining_margin = _nonnegative(remaining_margin, "remaining_margin")
    leverage = _positive(leverage, "leverage")
    fee_rate = _nonnegative(fee_rate, "fee_rate")

    if quantity > remaining_quantity:
        raise PaperLedgerRulesError("Close quantity exceeds the open Futures position.")

    with localcontext() as ctx:
        ctx.prec = 120
        notional = validate_decimal(quantity * exit_price, allow_negative=False, allow_zero=False)
        raw_pnl = (
            quantity * (exit_price - entry_price)
            if side == "long"
            else quantity * (entry_price - exit_price)
        )
        pnl = validate_decimal(raw_pnl)

    fee_original, fee_posted = _fee(notional, fee_rate)

    released_margin = (
        remaining_margin
        if quantity == remaining_quantity
        else _proportional(remaining_margin, quantity, remaining_quantity)
    )

    # Net cash returned (or additionally paid) after P&L and closing fee.
    net_cash = validate_decimal(
        released_margin + pnl - fee_posted
    )

    entries = []
    if net_cash > ZERO:
        entries.append(_entry("cash_available", "debit", net_cash))
    elif net_cash < ZERO:
        entries.append(_entry("cash_available", "credit", -net_cash))

    entries.append(_entry("futures_margin_locked", "credit", released_margin))

    if pnl > ZERO:
        entries.append(_entry("realized_pnl", "credit", pnl))
    elif pnl < ZERO:
        entries.append(_entry("realized_pnl", "debit", -pnl))

    if fee_posted:
        entries.append(_entry("fee_expense", "debit", fee_posted))

    left_qty = validate_decimal(remaining_quantity - quantity, allow_negative=False)
    left_margin = validate_decimal(remaining_margin - released_margin, allow_negative=False)

    return TradeCalculation(
        transaction_type="futures_close",
        quantity=quantity,
        execution_price=exit_price,
        gross_value=notional,
        fee_original=fee_original,
        fee_posted=fee_posted,
        journal_entries=tuple(entries),
        realized_pnl=pnl,
        released_margin=released_margin,
        remaining_quantity=left_qty,
        remaining_margin=left_margin,
    )
