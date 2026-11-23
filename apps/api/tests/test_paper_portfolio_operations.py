from decimal import Decimal

import pytest

from app.services.paper_portfolio_operations import (
    MANUAL_TOP_UP_THRESHOLD,
    INITIAL_BALANCE,
    PaperPortfolioOperationError,
    calculate_manual_top_up,
)


@pytest.mark.parametrize(
    ("balance", "expected_amount"),
    [
        ("100.00", "900.00"),
        ("99.99", "900.01"),
        ("0.00", "1000.00"),
        ("-100.00", "1100.00"),
    ],
)
def test_manual_top_up_calculates_exact_target(balance, expected_amount):
    previous, amount, resulting = calculate_manual_top_up(balance)

    assert previous == Decimal(balance)
    assert amount == Decimal(expected_amount)
    assert resulting == INITIAL_BALANCE


def test_manual_top_up_rejects_balance_above_threshold():
    with pytest.raises(PaperPortfolioOperationError):
        calculate_manual_top_up("100.000001")


def test_manual_top_up_preserves_36_digit_decimal_precision():
    balance = Decimal("0." + ("0" * 35) + "1")

    previous, amount, resulting = calculate_manual_top_up(balance)

    assert previous == balance
    assert amount == Decimal(
        "999." + ("9" * 36)
    )
    assert resulting == INITIAL_BALANCE


@pytest.mark.parametrize(
    "balance",
    [
        True,
        1.25,
        "NaN",
        "Infinity",
        "1e36",
        "-1e36",
        "",
        "invalid",
    ],
)
def test_manual_top_up_rejects_invalid_balances(balance):
    with pytest.raises(PaperPortfolioOperationError):
        calculate_manual_top_up(balance)


def test_manual_top_up_threshold_is_exactly_100_usdt():
    assert MANUAL_TOP_UP_THRESHOLD == Decimal("100.00")
    assert calculate_manual_top_up("100.00")[2] == Decimal("1000.00")

