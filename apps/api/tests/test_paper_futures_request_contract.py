from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.v1.paper_trading import FuturesPaperTradeRequest


def valid_payload(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": "buy",
        "position_side": "long",
        "quantity": "0.01",
        "leverage": "10",
        "order_type": "market",
        "reduce_only": False,
    }
    payload.update(overrides)
    return payload


def test_valid_long_open_request():
    request = FuturesPaperTradeRequest(**valid_payload())
    assert request.position_side == "long"
    assert request.side == "buy"


def test_valid_short_open_request():
    request = FuturesPaperTradeRequest(**valid_payload(
        side="sell", position_side="short"
    ))
    assert request.position_side == "short"


@pytest.mark.parametrize("overrides", [
    {"side": "sell", "position_side": "long"},
    {"side": "buy", "position_side": "short"},
])
def test_open_direction_must_match_position_side(overrides):
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(**overrides))


def test_valid_long_reduce_only():
    request = FuturesPaperTradeRequest(**valid_payload(
        side="sell",
        position_side="long",
        reduce_only=True,
        position_id=uuid4(),
    ))
    assert request.reduce_only is True


def test_valid_short_reduce_only():
    request = FuturesPaperTradeRequest(**valid_payload(
        side="buy",
        position_side="short",
        reduce_only=True,
        position_id=uuid4(),
    ))
    assert request.reduce_only is True


def test_reduce_only_requires_position_id():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(
            side="sell", position_side="long", reduce_only=True
        ))


def test_reduce_only_rejects_wrong_side():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(
            side="buy",
            position_side="long",
            reduce_only=True,
            position_id=uuid4(),
        ))


@pytest.mark.parametrize("value", [
    "0", "0.0", "-1", "NaN", "Infinity", "1e2", "01"
])
def test_invalid_quantity_rejected(value):
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(quantity=value))


@pytest.mark.parametrize("value", [
    "0", "0.0", "-2", "NaN", "Infinity", "1e2"
])
def test_invalid_leverage_rejected(value):
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(leverage=value))


def test_float_quantity_rejected():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(quantity=0.01))


def test_float_leverage_rejected():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(leverage=10.0))


def test_boolean_is_strict():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(reduce_only=1))


def test_limit_order_rejected():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(order_type="limit"))


def test_non_null_limit_price_rejected():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(limit_price="100.00"))


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(unexpected_field="value"))


def test_position_id_can_identify_increase_target():
    position_id = uuid4()
    request = FuturesPaperTradeRequest(**valid_payload(
        position_id=position_id
    ))
    assert request.position_id == position_id


def test_market_order_defaults():
    request = FuturesPaperTradeRequest(**valid_payload())
    assert request.order_type == "market"
    assert request.limit_price is None


def test_symbol_is_strict():
    with pytest.raises(ValidationError):
        FuturesPaperTradeRequest(**valid_payload(symbol="btcusdt"))