from decimal import Decimal

import pytest

from app.services.paper_ledger_rules import (
    PaperLedgerRulesError,
    validate_journal_entries,
    validate_transaction_journal,
)
from app.services.paper_trade_calculations import (
    calculate_futures_close,
    calculate_futures_open,
    calculate_spot_buy,
    calculate_spot_sell,
)


D = Decimal


def assert_balanced(result, market):
    journal = validate_journal_entries(market, result.journal_entries)
    assert journal.debit_total == journal.credit_total
    assert validate_transaction_journal(result.transaction_type, journal) is journal


def test_spot_buy_calculates_gross_fee_and_cash():
    result = calculate_spot_buy(
        quantity=D("2"), price=D("100"), fee_rate=D("0.001")
    )
    assert result.gross_value == D("200")
    assert result.fee_original == D("0.200")
    assert result.fee_posted == D("0.20")
    assert result.remaining_cost_basis == D("200")
    assert_balanced(result, "spot")


def test_spot_buy_zero_rounded_fee_has_no_zero_entry():
    result = calculate_spot_buy(
        quantity=D("0.001"), price=D("1"), fee_rate=D("0.0001")
    )
    assert result.fee_posted == D("0.00")
    assert all(item.account_code != "fee_expense" for item in result.journal_entries)
    assert_balanced(result, "spot")

def test_spot_buy_large_decimal_fee_uses_high_precision_context():
    result = calculate_spot_buy(
        quantity=D("12345678901234567890123456789012345"),
        price=D("1"),
        fee_rate=D("0.123456789012345678901234567890123456"),
    )

    assert result.gross_value == D("12345678901234567890123456789012345")
    assert result.fee_original == D(
        "1524157875323883675049535156256668.100944978742206980727431946358064320"
    )
    assert result.fee_posted == D(
        "1524157875323883675049535156256668.10"
    )
    assert_balanced(result, "spot")


def test_spot_sell_partial_realized_gain_and_remaining_basis():
    result = calculate_spot_sell(
        quantity=D("1"),
        price=D("120"),
        remaining_quantity=D("2"),
        remaining_cost_basis=D("200"),
        fee_rate=D("0.01"),
    )
    assert result.released_cost_basis == D("100")
    assert result.fee_posted == D("1.20")
    assert result.realized_pnl == D("20")
    assert result.remaining_quantity == D("1")
    assert result.remaining_cost_basis == D("100")
    assert_balanced(result, "spot")

def test_spot_sell_zero_net_proceeds_omits_zero_cash_entry():
    result = calculate_spot_sell(
        quantity=D("1"),
        price=D("0.01"),
        remaining_quantity=D("1"),
        remaining_cost_basis=D("0.01"),
        fee_rate=D("0.99"),
    )

    assert result.gross_value == D("0.01")
    assert result.fee_original == D("0.0099")
    assert result.fee_posted == D("0.01")
    assert result.realized_pnl == D("0")
    assert not any(
        item.account_code == "cash_available"
        for item in result.journal_entries
    )
    assert_balanced(result, "spot")


def test_spot_sell_positive_net_proceeds_posts_cash_debit():
    result = calculate_spot_sell(
        quantity=D("1"),
        price=D("120"),
        remaining_quantity=D("2"),
        remaining_cost_basis=D("200"),
        fee_rate=D("0.01"),
    )

    assert any(
        item.account_code == "cash_available"
        and item.direction == "debit"
        and item.amount == D("118.80")
        for item in result.journal_entries
    )
    assert_balanced(result, "spot")


def test_spot_sell_full_close_releases_exact_residual_basis():
    result = calculate_spot_sell(
        quantity=D("3"), price=D("9"),
        remaining_quantity=D("3"),
        remaining_cost_basis=D("25"),
        fee_rate=D("0"),
    )
    assert result.released_cost_basis == D("25")
    assert result.remaining_cost_basis == D("0")
    assert result.remaining_quantity == D("0")
    assert_balanced(result, "spot")


def test_spot_sell_rejects_overclose():
    with pytest.raises(PaperLedgerRulesError):
        calculate_spot_sell(
            quantity=D("2"), price=D("10"),
            remaining_quantity=D("1"),
            remaining_cost_basis=D("10"),
            fee_rate=D("0"),
        )


def test_futures_open_calculates_margin_and_fee():
    result = calculate_futures_open(
        quantity=D("2"), price=D("100"),
        leverage=D("10"), fee_rate=D("0.001"),
    )
    assert result.gross_value == D("200")
    assert result.remaining_margin == D("20")
    assert result.fee_posted == D("0.20")
    assert_balanced(result, "futures")


@pytest.mark.parametrize(
    ("side", "exit_price", "expected_pnl"),
    [
        ("long", "120", D("20")),
        ("long", "80", D("-20")),
        ("short", "80", D("20")),
        ("short", "120", D("-20")),
    ],
)
def test_futures_close_long_short_profit_and_loss(side, exit_price, expected_pnl):
    result = calculate_futures_close(
        side=side,
        quantity=D("1"),
        entry_price=D("100"),
        exit_price=D(exit_price),
        remaining_quantity=D("1"),
        remaining_margin=D("10"),
        leverage=D("10"),
        fee_rate=D("0.001"),
    )
    assert result.realized_pnl == expected_pnl
    assert result.remaining_quantity == D("0")
    assert result.remaining_margin == D("0")
    assert_balanced(result, "futures")


def test_futures_close_rejects_overclose():
    with pytest.raises(PaperLedgerRulesError):
        calculate_futures_close(
            side="long",
            quantity=D("2"),
            entry_price=D("100"),
            exit_price=D("110"),
            remaining_quantity=D("1"),
            remaining_margin=D("10"),
            leverage=D("10"),
            fee_rate=D("0"),
        )


def test_invalid_futures_side_rejected():
    with pytest.raises(PaperLedgerRulesError):
        calculate_futures_close(
            side="hedge",
            quantity=D("1"),
            entry_price=D("100"),
            exit_price=D("110"),
            remaining_quantity=D("1"),
            remaining_margin=D("10"),
            leverage=D("10"),
            fee_rate=D("0"),
        )
