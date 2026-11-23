"""Pure Decimal and double-entry journal validation rules."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
from typing import Iterable


MAX_DECIMAL_TEXT_LENGTH = 128
MAX_DECIMAL_SCALE = 36
MAX_DECIMAL_MAGNITUDE = Decimal("1e36")

ACCOUNT_NORMAL_SIDES = {
    "cash_available": "debit",
    "spot_position_cost": "debit",
    "futures_margin_locked": "debit",
    "realized_pnl": "credit",
    "fee_expense": "debit",
    "contributed_capital": "credit",
}

_DECIMAL_TEXT = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
)


class PaperLedgerRulesError(ValueError):
    """A financial input or journal violates ledger rules."""


@dataclass(frozen=True, slots=True)
class JournalEntryInput:
    account_code: str
    direction: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class ValidatedJournal:
    market_type: str
    entries: tuple[JournalEntryInput, ...]
    debit_total: Decimal
    credit_total: Decimal


def validate_decimal(
    value: Decimal | str,
    *,
    allow_negative: bool = True,
    allow_zero: bool = True,
) -> Decimal:
    """Parse and validate an exact Decimal without rounding."""

    if isinstance(value, bool) or isinstance(value, float):
        raise PaperLedgerRulesError(
            "Decimal input must not be a boolean or float."
        )

    if isinstance(value, str):
        if len(value) > MAX_DECIMAL_TEXT_LENGTH:
            raise PaperLedgerRulesError(
                "Decimal text exceeds the 128-character limit."
            )
        if not value or not _DECIMAL_TEXT.fullmatch(value):
            raise PaperLedgerRulesError("Invalid decimal text.")
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise PaperLedgerRulesError("Invalid decimal text.") from exc
    elif isinstance(value, Decimal):
        result = value
    else:
        raise PaperLedgerRulesError(
            "Decimal input must be Decimal or decimal text."
        )

    if not result.is_finite():
        raise PaperLedgerRulesError(
            "Decimal value must be finite."
        )

    if abs(result) >= MAX_DECIMAL_MAGNITUDE:
        raise PaperLedgerRulesError(
            "Decimal magnitude must be strictly below 1e36."
        )

    scale = max(0, -result.as_tuple().exponent)
    if scale > MAX_DECIMAL_SCALE:
        raise PaperLedgerRulesError(
            "Decimal scale must not exceed 36."
        )

    if not allow_negative and result < 0:
        raise PaperLedgerRulesError(
            "Negative decimal values are not allowed."
        )

    if not allow_zero and result == 0:
        raise PaperLedgerRulesError(
            "Zero decimal values are not allowed."
        )

    return result


def validate_journal_entries(
    market_type: str,
    entries: Iterable[JournalEntryInput],
) -> ValidatedJournal:
    """Validate an exact, balanced, market-isolated journal."""

    if not isinstance(market_type, str) or market_type not in {"spot", "futures"}:
        raise PaperLedgerRulesError(
            "market_type must be 'spot' or 'futures'."
        )

    try:
        materialized = tuple(entries)
    except (TypeError, ValueError) as exc:
        raise PaperLedgerRulesError(
            "Journal entries must be iterable."
        ) from exc

    if len(materialized) < 2:
        raise PaperLedgerRulesError(
            "A journal requires at least two entries."
        )

    normalized_entries: list[JournalEntryInput] = []
    debit_amounts: list[Decimal] = []
    credit_amounts: list[Decimal] = []

    for entry in materialized:
        if not isinstance(entry, JournalEntryInput):
            raise PaperLedgerRulesError(
                "Every journal item must be a JournalEntryInput."
            )

        if (
            not isinstance(entry.account_code, str)
            or entry.account_code not in ACCOUNT_NORMAL_SIDES
        ):
            raise PaperLedgerRulesError(
                "Unsupported ledger account code."
            )

        if (
            market_type == "spot"
            and entry.account_code == "futures_margin_locked"
        ) or (
            market_type == "futures"
            and entry.account_code == "spot_position_cost"
        ):
            raise PaperLedgerRulesError(
                "Prohibited cross-market account posting."
            )

        if (
            not isinstance(entry.direction, str)
            or entry.direction not in {"debit", "credit"}
        ):
            raise PaperLedgerRulesError(
                "Journal direction must be debit or credit."
            )

        amount = validate_decimal(
            entry.amount,
            allow_negative=False,
            allow_zero=False,
        )

        normalized_entries.append(
            replace(entry, amount=amount)
        )

        if entry.direction == "debit":
            debit_amounts.append(amount)
        else:
            credit_amounts.append(amount)

    if not debit_amounts or not credit_amounts:
        raise PaperLedgerRulesError(
            "Journal requires at least one debit and one credit."
        )

    # Each amount has at most 72 significant digits under the approved
    # magnitude and scale rules. Add enough precision for the number
    # of entries so summation cannot silently round.
    precision = 72 + len(str(len(materialized))) + 2

    try:
        with localcontext() as context:
            context.prec = precision

            debit_total = sum(debit_amounts, Decimal("0"))
            credit_total = sum(credit_amounts, Decimal("0"))

            # Validate aggregate totals as well as individual entries.
            debit_total = validate_decimal(
                debit_total,
                allow_negative=False,
                allow_zero=False,
            )
            credit_total = validate_decimal(
                credit_total,
                allow_negative=False,
                allow_zero=False,
            )

            if debit_total != credit_total:
                raise PaperLedgerRulesError(
                    "Journal debit and credit totals must be exactly equal."
                )

    except PaperLedgerRulesError:
        raise

    return ValidatedJournal(
        market_type=market_type,
        entries=tuple(normalized_entries),
        debit_total=debit_total,
        credit_total=credit_total,
    )


def validate_transaction_journal(transaction_type: str, journal: ValidatedJournal) -> ValidatedJournal:
    """Enforce transaction-specific account and debit/credit topology.

    This validates journal structure only. It does not calculate position
    quantity, cost basis, margin, fees, execution price, or realized P&L.
    """
    if not isinstance(journal, ValidatedJournal):
        raise PaperLedgerRulesError("A validated journal is required.")

    # Portfolio-scoped Manual Top-up is valid for either isolated market.
    # It must contain exactly one cash debit and one capital credit.
    if transaction_type == "manual_top_up":
        if journal.market_type not in {"spot", "futures"}:
            raise PaperLedgerRulesError(
                "Manual Top-up requires a Spot or Futures journal."
            )

        expected_pairs = {
            ("cash_available", "debit"),
            ("contributed_capital", "credit"),
        }
        actual_pairs = {
            (entry.account_code, entry.direction)
            for entry in journal.entries
        }

        if len(journal.entries) != 2 or actual_pairs != expected_pairs:
            raise PaperLedgerRulesError(
                "Manual Top-up requires exactly one cash debit "
                "and one contributed-capital credit."
            )

        cash_debit = sum(
            (
                entry.amount
                for entry in journal.entries
                if entry.account_code == "cash_available"
                and entry.direction == "debit"
            ),
            Decimal("0"),
        )
        capital_credit = sum(
            (
                entry.amount
                for entry in journal.entries
                if entry.account_code == "contributed_capital"
                and entry.direction == "credit"
            ),
            Decimal("0"),
        )

        if cash_debit != capital_credit:
            raise PaperLedgerRulesError(
                "Manual Top-up cash and contributed-capital amounts must match."
            )

        return journal

    rules = {
        "spot_buy": (
            "spot",
            {
                ("spot_position_cost", "debit"),
                ("fee_expense", "debit"),
                ("cash_available", "credit"),
            },
            {
                ("spot_position_cost", "debit"),
                ("cash_available", "credit"),
            },
        ),
        "spot_sell": (
            "spot",
            {
                ("cash_available", "debit"),
                ("cash_available", "credit"),
                ("spot_position_cost", "credit"),
                ("realized_pnl", "debit"),
                ("realized_pnl", "credit"),
                ("fee_expense", "debit"),
            },
            {
                ("spot_position_cost", "credit"),
            },
        ),
        "futures_open": (
            "futures",
            {
                ("futures_margin_locked", "debit"),
                ("fee_expense", "debit"),
                ("cash_available", "credit"),
            },
            {
                ("futures_margin_locked", "debit"),
                ("cash_available", "credit"),
            },
        ),
        "futures_close": (
            "futures",
            {
                ("cash_available", "debit"),
                ("cash_available", "credit"),
                ("futures_margin_locked", "credit"),
                ("realized_pnl", "debit"),
                ("realized_pnl", "credit"),
                ("fee_expense", "debit"),
            },
            {
                ("futures_margin_locked", "credit"),
            },
        ),
    }

    if type(transaction_type) is not str or transaction_type not in rules:
        raise PaperLedgerRulesError("Unsupported paper transaction type.")

    expected_market, allowed_pairs, required_pairs = rules[transaction_type]

    if journal.market_type != expected_market:
        raise PaperLedgerRulesError(
            "Transaction type does not match the journal market."
        )

    actual_pairs = {
        (entry.account_code, entry.direction)
        for entry in journal.entries
    }

    if not actual_pairs.issubset(allowed_pairs):
        raise PaperLedgerRulesError(
            "Journal contains an account or direction prohibited for this transaction."
        )

    if not required_pairs.issubset(actual_pairs):
        raise PaperLedgerRulesError(
            "Journal is missing a required transaction accounting entry."
        )

    pnl_directions = {
        entry.direction
        for entry in journal.entries
        if entry.account_code == "realized_pnl"
    }
    if len(pnl_directions) > 1:
        raise PaperLedgerRulesError(
            "Realized P&L cannot be debited and credited in the same transaction."
        )

    # Validate the financial relationships between account totals.
    # This is a second-line integrity check after exact double-entry balancing.
    precision = 80 + len(str(len(journal.entries))) + 4

    with localcontext() as context:
        context.prec = precision

        def amount(account_code: str, direction: str) -> Decimal:
            return sum(
                (
                    entry.amount
                    for entry in journal.entries
                    if entry.account_code == account_code
                    and entry.direction == direction
                ),
                Decimal("0"),
            )

        fee = amount("fee_expense", "debit")
        pnl_credit = amount("realized_pnl", "credit")
        pnl_debit = amount("realized_pnl", "debit")

        if transaction_type == "spot_buy":
            position_cost = amount("spot_position_cost", "debit")
            cash_credit = amount("cash_available", "credit")

            if cash_credit != position_cost + fee:
                raise PaperLedgerRulesError(
                    "Spot Buy cash debit/fee relationship is invalid."
                )

        elif transaction_type == "spot_sell":
            proceeds = amount("cash_available", "debit")
            cash_credit = amount("cash_available", "credit")
            released_basis = amount("spot_position_cost", "credit")

            expected_pnl = proceeds + fee - cash_credit - released_basis

            if expected_pnl > 0:
                if pnl_credit != expected_pnl or pnl_debit != 0:
                    raise PaperLedgerRulesError(
                        "Spot Sell realized gain does not match proceeds, "
                        "fees and released cost basis."
                    )
            elif expected_pnl < 0:
                if pnl_debit != -expected_pnl or pnl_credit != 0:
                    raise PaperLedgerRulesError(
                        "Spot Sell realized loss does not match proceeds, "
                        "fees and released cost basis."
                    )
            elif pnl_credit != 0 or pnl_debit != 0:
                raise PaperLedgerRulesError(
                    "Spot Sell must not post realized P&L when break-even."
                )

        elif transaction_type == "futures_open":
            margin = amount("futures_margin_locked", "debit")
            cash_credit = amount("cash_available", "credit")

            if cash_credit != margin + fee:
                raise PaperLedgerRulesError(
                    "Futures Open cash credit does not match margin and fee."
                )

        elif transaction_type == "futures_close":
            cash_debit = amount("cash_available", "debit")
            cash_credit = amount("cash_available", "credit")
            released_margin = amount("futures_margin_locked", "credit")

            expected_pnl = (
                cash_debit + fee - cash_credit - released_margin
            )

            if expected_pnl > 0:
                if pnl_credit != expected_pnl or pnl_debit != 0:
                    raise PaperLedgerRulesError(
                        "Futures Close realized gain does not match cash, "
                        "released margin and fees."
                    )
            elif expected_pnl < 0:
                if pnl_debit != -expected_pnl or pnl_credit != 0:
                    raise PaperLedgerRulesError(
                        "Futures Close realized loss does not match cash, "
                        "released margin and fees."
                    )
            elif pnl_credit != 0 or pnl_debit != 0:
                raise PaperLedgerRulesError(
                    "Futures Close must not post realized P&L when break-even."
                )

    return journal
