from decimal import Decimal

import pytest

from app.services.paper_ledger_rules import (
    JournalEntryInput,
    PaperLedgerRulesError,
    validate_journal_entries,
    validate_transaction_journal,
)


def journal(market, *entries):
    return validate_journal_entries(market, tuple(
        JournalEntryInput(code, direction, Decimal(amount))
        for code, direction, amount in entries
    ))


@pytest.mark.parametrize(
    "transaction_type,market,entries",
    [
        (
            "spot_buy", "spot",
            [
                ("spot_position_cost", "debit", "100"),
                ("cash_available", "credit", "100"),
            ],
        ),
        (
            "spot_buy", "spot",
            [
                ("spot_position_cost", "debit", "100"),
                ("fee_expense", "debit", "1"),
                ("cash_available", "credit", "101"),
            ],
        ),
        (
            "spot_sell", "spot",
            [
                ("cash_available", "debit", "120"),
                ("spot_position_cost", "credit", "100"),
                ("realized_pnl", "credit", "20"),
            ],
        ),
        (
            "spot_sell", "spot",
            [
                ("cash_available", "debit", "119"),
                ("spot_position_cost", "credit", "100"),
                ("realized_pnl", "credit", "20"),
                ("fee_expense", "debit", "1"),
            ],
        ),
        (
            "futures_open", "futures",
            [
                ("futures_margin_locked", "debit", "100"),
                ("cash_available", "credit", "100"),
            ],
        ),
        (
            "futures_open", "futures",
            [
                ("futures_margin_locked", "debit", "100"),
                ("fee_expense", "debit", "1"),
                ("cash_available", "credit", "101"),
            ],
        ),
        (
            "futures_close", "futures",
            [
                ("cash_available", "debit", "120"),
                ("futures_margin_locked", "credit", "100"),
                ("realized_pnl", "credit", "20"),
            ],
        ),
        (
            "futures_close", "futures",
            [
                ("cash_available", "debit", "100"),
                ("cash_available", "credit", "3"),
                ("futures_margin_locked", "credit", "100"),
                ("realized_pnl", "debit", "2"),
                ("fee_expense", "debit", "1"),
            ],
        ),
    ],
)
def test_valid_transaction_specific_journal(transaction_type, market, entries):
    validated = journal(market, *entries)
    assert validate_transaction_journal(transaction_type, validated) is validated


@pytest.mark.parametrize(
    "transaction_type,market,entries",
    [
        (
            "spot_buy", "spot",
            [
                ("cash_available", "debit", "100"),
                ("realized_pnl", "credit", "100"),
            ],
        ),
        (
            "spot_sell", "spot",
            [
                ("spot_position_cost", "debit", "100"),
                ("cash_available", "credit", "100"),
            ],
        ),
        (
            "futures_open", "futures",
            [
                ("cash_available", "debit", "100"),
                ("realized_pnl", "credit", "100"),
            ],
        ),
        (
            "futures_close", "futures",
            [
                ("futures_margin_locked", "debit", "100"),
                ("cash_available", "credit", "100"),
            ],
        ),
    ],
)
def test_invalid_transaction_topology_rejected(transaction_type, market, entries):
    validated = journal(market, *entries)
    with pytest.raises(PaperLedgerRulesError):
        validate_transaction_journal(transaction_type, validated)


def test_transaction_market_mismatch_rejected():
    validated = journal(
        "spot",
        ("spot_position_cost", "debit", "100"),
        ("cash_available", "credit", "100"),
    )
    with pytest.raises(PaperLedgerRulesError, match="market"):
        validate_transaction_journal("futures_open", validated)


def test_realized_pnl_cannot_be_both_debit_and_credit():
    validated = journal(
        "spot",
        ("cash_available", "debit", "100"),
        ("spot_position_cost", "credit", "100"),
        ("realized_pnl", "debit", "10"),
        ("realized_pnl", "credit", "10"),
    )
    with pytest.raises(PaperLedgerRulesError, match="Realized P&L"):
        validate_transaction_journal("spot_sell", validated)


def test_unknown_transaction_type_rejected():
    validated = journal(
        "spot",
        ("spot_position_cost", "debit", "100"),
        ("cash_available", "credit", "100"),
    )
    with pytest.raises(PaperLedgerRulesError, match="Unsupported"):
        validate_transaction_journal("unknown_transaction_type", validated)
