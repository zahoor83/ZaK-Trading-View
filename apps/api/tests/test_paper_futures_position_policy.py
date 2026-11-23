from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.paper_futures_position_policy import (
    resolve_futures_position_action,
)


def position(side, quantity="2"):
    return SimpleNamespace(
        id=uuid4(),
        market_type="futures",
        symbol="BTCUSDT",
        side=side,
        remaining_quantity=Decimal(quantity),
        status="open",
    )


def resolve(
    *,
    mode="one_way",
    side="buy",
    position_side="long",
    quantity="1",
    reduce_only=False,
    position_id=None,
    positions=(),
):
    return resolve_futures_position_action(
        position_mode=mode,
        symbol="BTCUSDT",
        side=side,
        position_side=position_side,
        quantity=Decimal(quantity),
        reduce_only=reduce_only,
        position_id=position_id,
        open_positions=list(positions),
    )


def test_one_way_opens_long():
    assert resolve().action == "open"


def test_one_way_opens_short():
    assert resolve(side="sell", position_side="short").action == "open"


def test_one_way_increases_long():
    current = position("long")
    result = resolve(positions=[current])
    assert result.action == "increase"
    assert result.position is current


def test_one_way_increases_short():
    current = position("short")
    result = resolve(
        side="sell",
        position_side="short",
        positions=[current],
    )
    assert result.action == "increase"


def test_one_way_reduces_long():
    current = position("long")
    result = resolve(
        side="sell",
        quantity="1",
        reduce_only=True,
        position_id=str(current.id),
        positions=[current],
    )
    assert result.action == "reduce"


def test_one_way_closes_long():
    current = position("long")
    result = resolve(
        side="sell",
        quantity="2",
        reduce_only=True,
        position_id=str(current.id),
        positions=[current],
    )
    assert result.action == "close"


def test_one_way_rejects_reversal():
    current = position("long")
    with pytest.raises(ValueError, match="reversal is prohibited"):
        resolve(
            side="sell",
            quantity="3",
            reduce_only=True,
            position_id=str(current.id),
            positions=[current],
        )


def test_one_way_rejects_opposite_order_without_reduce_only():
    current = position("long")
    with pytest.raises(ValueError, match="reduce-only"):
        resolve(side="sell", positions=[current])


def test_one_way_rejects_wrong_position_side():
    current = position("long")
    with pytest.raises(ValueError, match="match existing"):
        resolve(
            side="sell",
            position_side="short",
            reduce_only=True,
            position_id=str(current.id),
            positions=[current],
        )


def test_one_way_rejects_multiple_positions():
    with pytest.raises(ValueError, match="at most one"):
        resolve(positions=[position("long"), position("short")])


def test_hedge_opens_long_while_short_exists():
    short = position("short")
    result = resolve(mode="hedge", positions=[short])
    assert result.action == "open"


def test_hedge_opens_short_while_long_exists():
    long = position("long")
    result = resolve(
        mode="hedge",
        side="sell",
        position_side="short",
        positions=[long],
    )
    assert result.action == "open"


def test_hedge_increases_long():
    current = position("long")
    result = resolve(mode="hedge", positions=[current])
    assert result.action == "increase"


def test_hedge_increases_short():
    current = position("short")
    result = resolve(
        mode="hedge",
        side="sell",
        position_side="short",
        positions=[current],
    )
    assert result.action == "increase"


def test_hedge_reduces_long():
    current = position("long")
    result = resolve(
        mode="hedge",
        side="sell",
        quantity="1",
        reduce_only=True,
        position_id=str(current.id),
        positions=[current],
    )
    assert result.action == "reduce"


def test_hedge_closes_short():
    current = position("short")
    result = resolve(
        mode="hedge",
        side="buy",
        position_side="short",
        quantity="2",
        reduce_only=True,
        position_id=str(current.id),
        positions=[current],
    )
    assert result.action == "close"


def test_hedge_close_requires_position_id():
    current = position("long")
    with pytest.raises(ValueError, match="exact position_id"):
        resolve(
            mode="hedge",
            side="sell",
            reduce_only=True,
            positions=[current],
        )


def test_hedge_rejects_wrong_position_id():
    current = position("long")
    with pytest.raises(ValueError, match="exact position_id"):
        resolve(
            mode="hedge",
            side="sell",
            reduce_only=True,
            position_id=str(uuid4()),
            positions=[current],
        )


def test_hedge_rejects_missing_leg():
    with pytest.raises(ValueError, match="does not exist"):
        resolve(
            mode="hedge",
            side="sell",
            reduce_only=True,
            position_id=str(uuid4()),
        )


def test_hedge_rejects_duplicate_same_side():
    with pytest.raises(ValueError, match="at most one"):
        resolve(
            mode="hedge",
            positions=[position("long"), position("long")],
        )


@pytest.mark.parametrize("mode", ["invalid", None, "spot"])
def test_rejects_invalid_mode(mode):
    with pytest.raises(ValueError, match="position mode"):
        resolve(mode=mode)


def test_rejects_float_quantity():
    with pytest.raises(ValueError, match="Decimal"):
        resolve_futures_position_action(
            position_mode="one_way",
            symbol="BTCUSDT",
            side="buy",
            position_side="long",
            quantity=1.0,
            reduce_only=False,
            position_id=None,
            open_positions=[],
        )


def test_new_position_rejects_supplied_id():
    with pytest.raises(ValueError, match="new position"):
        resolve(position_id=str(uuid4()))


def test_increase_rejects_mismatched_id():
    current = position("long")
    with pytest.raises(ValueError, match="does not match"):
        resolve(position_id=str(uuid4()), positions=[current])
