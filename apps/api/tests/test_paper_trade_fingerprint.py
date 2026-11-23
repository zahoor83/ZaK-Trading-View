from decimal import Decimal
from uuid import uuid4

import pytest

from app.services.paper_trade_fingerprint import (
    fingerprint_trade_execution_request,
    fingerprint_trade_intent_request,
)


def spot_intent(**overrides):
    value = {
        "position_id": None,
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": None,
        "quantity": Decimal("0.0100"),
        "order_type": "market",
        "leverage": None,
        "limit_price": None,
        "reduce_only": False,
    }
    value.update(overrides)
    return value


def futures_intent(**overrides):
    value = {
        "position_id": str(uuid4()),
        "symbol": "BTCUSDT",
        "side": "sell",
        "position_side": "short",
        "quantity": Decimal("0.010"),
        "order_type": "market",
        "leverage": Decimal("5"),
        "limit_price": None,
        "reduce_only": False,
    }
    value.update(overrides)
    return value


def journal():
    return [
        {
            "account_code": "spot_position_cost",
            "direction": "debit",
            "amount": Decimal("100.00"),
        },
        {
            "account_code": "cash_available",
            "direction": "credit",
            "amount": Decimal("100.00"),
        },
    ]


def make_fingerprint(intent=None, entries=None, market_type="spot"):
    return fingerprint_trade_execution_request(
        transaction_type="spot_buy" if market_type == "spot" else "futures_open",
        market_type=market_type,
        trade_intent=intent or spot_intent(),
        journal_entries=entries or journal(),
    )


def test_same_semantic_decimal_values_have_same_fingerprint():
    first = make_fingerprint(spot_intent(quantity=Decimal("0.0100")))
    second = make_fingerprint(spot_intent(quantity=Decimal("0.01")))
    assert first == second


def test_quantity_change_changes_fingerprint():
    first = make_fingerprint(spot_intent(quantity=Decimal("0.01")))
    second = make_fingerprint(spot_intent(quantity=Decimal("0.02")))
    assert first != second


def test_symbol_change_changes_fingerprint():
    first = make_fingerprint(spot_intent(symbol="BTCUSDT"))
    second = make_fingerprint(spot_intent(symbol="ETHUSDT"))
    assert first != second


def test_side_change_changes_fingerprint():
    first = make_fingerprint(spot_intent(side="buy"))
    second = make_fingerprint(spot_intent(side="sell"))
    assert first != second


def test_position_id_change_changes_fingerprint():
    first = make_fingerprint(futures_intent(), market_type="futures")
    second = make_fingerprint(
        futures_intent(position_id=str(uuid4())),
        market_type="futures",
    )
    assert first != second


def test_market_quote_is_not_part_of_stable_intent():
    first = make_fingerprint(spot_intent())
    second = make_fingerprint(spot_intent())
    assert first == second


def test_limit_price_change_changes_fingerprint():
    base = spot_intent(order_type="limit", limit_price=Decimal("100"))
    changed = spot_intent(order_type="limit", limit_price=Decimal("101"))
    assert make_fingerprint(base) != make_fingerprint(changed)


def test_market_order_rejects_limit_price():
    with pytest.raises(ValueError, match="must not specify limit_price"):
        make_fingerprint(spot_intent(limit_price=Decimal("100")))


def test_limit_order_requires_limit_price():
    with pytest.raises(ValueError, match="must be Decimal"):
        make_fingerprint(spot_intent(order_type="limit"))


@pytest.mark.parametrize("bad", [1.0, True, "1", Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")])
def test_quantity_rejects_invalid_numeric_values(bad):
    with pytest.raises(ValueError):
        make_fingerprint(spot_intent(quantity=bad))


def test_float_journal_amount_is_rejected():
    entries = journal()
    entries[0]["amount"] = 1.0
    with pytest.raises(ValueError, match="must be Decimal"):
        make_fingerprint(entries=entries)


def test_unknown_or_missing_intent_fields_are_rejected():
    intent = spot_intent()
    del intent["position_id"]
    with pytest.raises(ValueError, match="exactly the documented keys"):
        make_fingerprint(intent)


def test_spot_rejects_leverage():
    with pytest.raises(ValueError, match="must not specify leverage"):
        make_fingerprint(spot_intent(leverage=Decimal("2")))


def test_futures_requires_position_side_and_leverage():
    with pytest.raises(ValueError):
        make_fingerprint(
            futures_intent(position_side=None),
            market_type="futures",
        )


def test_futures_leverage_is_fingerprinted():
    first = make_fingerprint(futures_intent(leverage=Decimal("5")), market_type="futures")
    second = make_fingerprint(futures_intent(leverage=Decimal("10")), market_type="futures")
    assert first != second


def test_futures_open_rejects_reduce_only():
    with pytest.raises(ValueError, match="must not be reduce_only"):
        make_fingerprint(futures_intent(reduce_only=True), market_type="futures")


def test_journal_change_changes_fingerprint():
    entries = journal()
    entries[0]["amount"] = Decimal("101")
    assert make_fingerprint() != make_fingerprint(entries=entries)


def test_invalid_market_type_is_rejected():
    with pytest.raises(ValueError, match="market_type"):
        make_fingerprint(market_type="margin")


def test_journal_requires_two_entries():
    with pytest.raises(ValueError, match="at least two"):
        make_fingerprint(entries=journal()[:1])


def test_invalid_position_id_is_rejected():
    with pytest.raises(ValueError, match="canonical lowercase UUIDv4"):
        make_fingerprint(futures_intent(position_id="not-a-uuid"), market_type="futures")


def stable_spot_fingerprint(intent=None, transaction_type="spot_buy"):
    return fingerprint_trade_intent_request(
        transaction_type=transaction_type,
        market_type="spot",
        trade_intent=intent or spot_intent(),
    )


def test_stable_intent_fingerprint_ignores_journal_values():
    first = stable_spot_fingerprint(spot_intent())
    second = stable_spot_fingerprint(spot_intent())
    assert first == second


def test_stable_intent_fingerprint_changes_when_quantity_changes():
    first = stable_spot_fingerprint(spot_intent(quantity=Decimal("0.01")))
    second = stable_spot_fingerprint(spot_intent(quantity=Decimal("0.02")))
    assert first != second


def test_stable_intent_fingerprint_changes_when_transaction_type_changes():
    first = stable_spot_fingerprint(
        spot_intent(side="buy"),
        transaction_type="spot_buy",
    )
    second = stable_spot_fingerprint(
        spot_intent(side="buy"),
        transaction_type="spot_sell",
    )
    assert first != second


def test_stable_intent_fingerprint_rejects_invalid_transaction_type():
    with pytest.raises(ValueError, match="not permitted"):
        stable_spot_fingerprint(
            spot_intent(),
            transaction_type="futures_open",
        )


def test_stable_intent_fingerprint_supports_futures():
    first = fingerprint_trade_intent_request(
        transaction_type="futures_open",
        market_type="futures",
        trade_intent=futures_intent(),
    )
    second = fingerprint_trade_intent_request(
        transaction_type="futures_open",
        market_type="futures",
        trade_intent=futures_intent(leverage=Decimal("10")),
    )
    assert first != second

def _futures_semantic_fingerprint(transaction_type, intent):
    return fingerprint_trade_intent_request(
        transaction_type=transaction_type,
        market_type="futures",
        trade_intent=intent,
    )


def test_futures_open_allows_new_position_without_position_id():
    intent = futures_intent(position_id=None)
    assert _futures_semantic_fingerprint("futures_open", intent)


def test_futures_open_allows_same_side_increase_with_position_id():
    intent = futures_intent(position_id=str(uuid4()))
    assert _futures_semantic_fingerprint("futures_open", intent)


@pytest.mark.parametrize(
    "position_side,wrong_side",
    [
        ("long", "sell"),
        ("short", "buy"),
    ],
)
def test_futures_open_rejects_opposite_direction(position_side, wrong_side):
    with pytest.raises(ValueError, match="must open or increase"):
        _futures_semantic_fingerprint(
            "futures_open",
            futures_intent(position_side=position_side, side=wrong_side),
        )


def test_futures_open_rejects_reduce_only():
    with pytest.raises(ValueError, match="must not be reduce_only"):
        _futures_semantic_fingerprint(
            "futures_open",
            futures_intent(reduce_only=True),
        )


@pytest.mark.parametrize(
    "position_side,closing_side",
    [
        ("long", "sell"),
        ("short", "buy"),
    ],
)
def test_futures_close_accepts_correct_direction(position_side, closing_side):
    intent = futures_intent(
        position_id=str(uuid4()),
        position_side=position_side,
        side=closing_side,
        reduce_only=True,
    )
    assert _futures_semantic_fingerprint("futures_close", intent)


def test_futures_close_requires_reduce_only():
    with pytest.raises(ValueError, match="must be reduce_only"):
        _futures_semantic_fingerprint(
            "futures_close",
            futures_intent(reduce_only=False),
        )


def test_futures_close_requires_exact_position_id():
    with pytest.raises(ValueError, match="requires position_id"):
        _futures_semantic_fingerprint(
            "futures_close",
            futures_intent(position_id=None, reduce_only=True),
        )


@pytest.mark.parametrize(
    "position_side,wrong_side",
    [
        ("long", "buy"),
        ("short", "sell"),
    ],
)
def test_futures_close_rejects_wrong_direction(position_side, wrong_side):
    with pytest.raises(ValueError, match="must close"):
        _futures_semantic_fingerprint(
            "futures_close",
            futures_intent(
                position_side=position_side,
                side=wrong_side,
                reduce_only=True,
            ),
        )


def test_futures_close_rejects_limit_orders():
    with pytest.raises(ValueError, match="market orders only"):
        _futures_semantic_fingerprint(
            "futures_close",
            futures_intent(
                order_type="limit",
                limit_price=Decimal("100"),
                reduce_only=True,
            ),
        )


def test_futures_open_rejects_limit_orders():
    with pytest.raises(ValueError, match="market orders only"):
        _futures_semantic_fingerprint(
            "futures_open",
            futures_intent(
                order_type="limit",
                limit_price=Decimal("100"),
            ),
        )


def test_execution_fingerprint_enforces_futures_close_semantics():
    intent = futures_intent(
        position_side="long",
        side="sell",
        reduce_only=True,
    )
    result = fingerprint_trade_execution_request(
        transaction_type="futures_close",
        market_type="futures",
        trade_intent=intent,
        journal_entries=journal(),
    )
    assert result


def test_execution_fingerprint_rejects_invalid_futures_open_semantics():
    with pytest.raises(ValueError, match="must open or increase"):
        fingerprint_trade_execution_request(
            transaction_type="futures_open",
            market_type="futures",
            trade_intent=futures_intent(
                position_side="long",
                side="sell",
            ),
            journal_entries=journal(),
        )


def test_execution_fingerprint_rejects_wrong_market_transaction_type():
    with pytest.raises(ValueError, match="not permitted"):
        fingerprint_trade_execution_request(
            transaction_type="futures_close",
            market_type="spot",
            trade_intent=spot_intent(),
            journal_entries=journal(),
        )
