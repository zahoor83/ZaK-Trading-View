from decimal import Decimal

import pytest

from app.services.paper_ledger_rules import (
    JournalEntryInput,
    PaperLedgerRulesError,
    validate_decimal,
    validate_journal_entries,
    validate_transaction_journal,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1000.00", Decimal("1000.00")),
        ("0", Decimal("0")),
        ("1e3", Decimal("1e3")),
        (Decimal("0.123456"), Decimal("0.123456")),
        ("0." + ("1" * 36), Decimal("0." + ("1" * 36))),
    ],
)
def test_valid_decimal_inputs(value, expected):
    assert validate_decimal(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.25,
        float("nan"),
        float("inf"),
        "NaN",
        "Infinity",
        "",
        "1_000",
        "abc",
        "1e36",
        "1e-37",
        "1" * 129,
    ],
)
def test_invalid_decimal_inputs_fail_closed(value):
    with pytest.raises(PaperLedgerRulesError):
        validate_decimal(value)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1e36"),
        Decimal("1e-37"),
    ],
)
def test_invalid_decimal_objects_fail_closed(value):
    with pytest.raises(PaperLedgerRulesError):
        validate_decimal(value)


def test_negative_and_zero_policies():
    with pytest.raises(PaperLedgerRulesError):
        validate_decimal("-1", allow_negative=False)

    with pytest.raises(PaperLedgerRulesError):
        validate_decimal("0", allow_zero=False)

    assert validate_decimal("-1") == Decimal("-1")


def test_balanced_spot_journal_passes():
    result = validate_journal_entries(
        "spot",
        [
            JournalEntryInput(
                "spot_position_cost", "debit", Decimal("100")
            ),
            JournalEntryInput(
                "cash_available", "credit", Decimal("100")
            ),
        ],
    )
    assert result.debit_total == Decimal("100")
    assert result.credit_total == Decimal("100")


def test_balanced_futures_journal_passes():
    result = validate_journal_entries(
        "futures",
        [
            JournalEntryInput(
                "futures_margin_locked", "debit", Decimal("100")
            ),
            JournalEntryInput(
                "cash_available", "credit", Decimal("100")
            ),
        ],
    )
    assert result.debit_total == result.credit_total


def test_unbalanced_journal_is_rejected():
    with pytest.raises(PaperLedgerRulesError, match="exactly equal"):
        validate_journal_entries(
            "spot",
            [
                JournalEntryInput(
                    "spot_position_cost", "debit", Decimal("100")
                ),
                JournalEntryInput(
                    "cash_available", "credit", Decimal("99")
                ),
            ],
        )


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [JournalEntryInput("cash_available", "debit", Decimal("1"))],
        [
            JournalEntryInput("cash_available", "debit", Decimal("1")),
            JournalEntryInput("fee_expense", "debit", Decimal("1")),
        ],
    ],
)
def test_journal_requires_debit_and_credit(entries):
    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries("spot", entries)


@pytest.mark.parametrize(
    ("market", "account"),
    [
        ("spot", "futures_margin_locked"),
        ("futures", "spot_position_cost"),
    ],
)
def test_cross_market_accounts_are_rejected(market, account):
    with pytest.raises(
        PaperLedgerRulesError,
        match="cross-market",
    ):
        validate_journal_entries(
            market,
            [
                JournalEntryInput(account, "debit", Decimal("1")),
                JournalEntryInput("cash_available", "credit", Decimal("1")),
            ],
        )


def test_non_positive_journal_amount_is_rejected():
    for amount in (Decimal("0"), Decimal("-1")):
        with pytest.raises(PaperLedgerRulesError):
            validate_journal_entries(
                "spot",
                [
                    JournalEntryInput("spot_position_cost", "debit", amount),
                    JournalEntryInput(
                        "cash_available", "credit", Decimal("1")
                    ),
                ],
            )


def test_invalid_market_and_account_are_rejected():
    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries("margin", [])

    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries(
            "spot",
            [
                JournalEntryInput("unknown_account", "debit", Decimal("1")),
                JournalEntryInput("cash_available", "credit", Decimal("1")),
            ],
        )


def test_invalid_direction_is_rejected():
    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries(
            "spot",
            [
                JournalEntryInput("spot_position_cost", "sideways", Decimal("1")),
                JournalEntryInput("cash_available", "credit", Decimal("1")),
            ],
        )

def test_journal_aggregation_does_not_round_high_precision_values():
    entries = (
        JournalEntryInput("cash_available", "debit", Decimal("1e35")),
        JournalEntryInput("spot_position_cost", "debit", Decimal("1e-36")),
        JournalEntryInput("contributed_capital", "credit", Decimal("1e35")),
        JournalEntryInput("realized_pnl", "credit", Decimal("1e-36")),
    )

    result = validate_journal_entries("spot", entries)

    assert result.debit_total == Decimal("100000000000000000000000000000000000.000000000000000000000000000000000001")
    assert result.credit_total == Decimal("100000000000000000000000000000000000.000000000000000000000000000000000001")
    assert result.debit_total == result.credit_total


def test_journal_rejects_high_precision_imbalance_hidden_by_default_context():
    entries = (
        JournalEntryInput("cash_available", "debit", Decimal("1e35")),
        JournalEntryInput("spot_position_cost", "debit", Decimal("1e-36")),
        JournalEntryInput("contributed_capital", "credit", Decimal("1e35")),
        JournalEntryInput("realized_pnl", "credit", Decimal("2e-36")),
    )

    with pytest.raises(
        PaperLedgerRulesError,
        match="exactly equal",
    ):
        validate_journal_entries("spot", entries)


def test_journal_rejects_aggregate_total_at_or_above_magnitude_limit():
    entries = (
        JournalEntryInput("cash_available", "debit", Decimal("6e35")),
        JournalEntryInput("spot_position_cost", "debit", Decimal("6e35")),
        JournalEntryInput("contributed_capital", "credit", Decimal("6e35")),
        JournalEntryInput("realized_pnl", "credit", Decimal("6e35")),
    )

    with pytest.raises(
        PaperLedgerRulesError,
        match="magnitude",
    ):
        validate_journal_entries("spot", entries)


def test_journal_returns_normalized_decimal_entries():
    entries = (
        JournalEntryInput("cash_available", "debit", "12.50"),
        JournalEntryInput("contributed_capital", "credit", Decimal("12.50")),
    )

    result = validate_journal_entries("spot", entries)

    assert isinstance(result.entries[0].amount, Decimal)
    assert result.entries[0].amount == Decimal("12.50")


@pytest.mark.parametrize("market_type", [None, 1, [], {}])
def test_journal_rejects_non_string_market_type(market_type):
    entries = (
        JournalEntryInput("cash_available", "debit", Decimal("1")),
        JournalEntryInput("contributed_capital", "credit", Decimal("1")),
    )

    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries(market_type, entries)


@pytest.mark.parametrize("account_code", [None, 1, [], {}])
def test_journal_rejects_non_string_account_code(account_code):
    entries = (
        JournalEntryInput(account_code, "debit", Decimal("1")),
        JournalEntryInput("contributed_capital", "credit", Decimal("1")),
    )

    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries("spot", entries)


@pytest.mark.parametrize("direction", [None, 1, [], {}])
def test_journal_rejects_non_string_direction(direction):
    entries = (
        JournalEntryInput("cash_available", direction, Decimal("1")),
        JournalEntryInput("contributed_capital", "credit", Decimal("1")),
    )

    with pytest.raises(PaperLedgerRulesError):
        validate_journal_entries("spot", entries)

@pytest.mark.parametrize("market_type", ["spot", "futures"])
def test_manual_top_up_requires_exact_cash_debit_and_capital_credit(market_type):
    journal = validate_journal_entries(
        market_type,
        (
            JournalEntryInput(
                "cash_available", "debit", Decimal("900.00")
            ),
            JournalEntryInput(
                "contributed_capital", "credit", Decimal("900.00")
            ),
        ),
    )

    result = validate_transaction_journal("manual_top_up", journal)

    assert result.market_type == market_type
    assert result.debit_total == Decimal("900.00")
    assert result.credit_total == Decimal("900.00")


@pytest.mark.parametrize(
    "entries",
    [
        (
            JournalEntryInput("cash_available", "credit", Decimal("10.00")),
            JournalEntryInput("contributed_capital", "debit", Decimal("10.00")),
        ),
        (
            JournalEntryInput("cash_available", "debit", Decimal("10.00")),
            JournalEntryInput("realized_pnl", "credit", Decimal("10.00")),
        ),
        (
            JournalEntryInput("cash_available", "debit", Decimal("10.00")),
            JournalEntryInput("contributed_capital", "credit", Decimal("9.00")),
            JournalEntryInput("realized_pnl", "credit", Decimal("1.00")),
        ),
        (
            JournalEntryInput("cash_available", "debit", Decimal("5.00")),
            JournalEntryInput("cash_available", "debit", Decimal("5.00")),
            JournalEntryInput("contributed_capital", "credit", Decimal("10.00")),
        ),
        (
            JournalEntryInput("cash_available", "debit", Decimal("5.00")),
            JournalEntryInput("contributed_capital", "credit", Decimal("5.00")),
            JournalEntryInput("fee_expense", "debit", Decimal("1.00")),
            JournalEntryInput("realized_pnl", "credit", Decimal("1.00")),
        ),
    ],
)
def test_manual_top_up_rejects_invalid_accounting_topology(entries):
    journal = validate_journal_entries("spot", entries)

    with pytest.raises(PaperLedgerRulesError):
        validate_transaction_journal("manual_top_up", journal)


def test_manual_top_up_rejects_unsupported_transaction_market():
    journal = validate_journal_entries(
        "spot",
        (
            JournalEntryInput("cash_available", "debit", Decimal("10.00")),
            JournalEntryInput(
                "contributed_capital", "credit", Decimal("10.00")
            ),
        ),
    )

    with pytest.raises(PaperLedgerRulesError):
        validate_transaction_journal("unknown_operation", journal)


