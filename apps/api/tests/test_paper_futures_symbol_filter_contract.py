import ast
import asyncio
from pathlib import Path

import pytest

from app.market_data.symbol_filters import (
    FilterValidationError,
    validate_order,
)
from app.services import paper_futures_execution as service


@pytest.mark.parametrize("snapshot", [None, object()])
def test_validate_order_rejects_unverified_metadata_snapshot(snapshot):
    with pytest.raises(
        FilterValidationError,
        match="verified symbol metadata snapshot",
    ):
        validate_order(
            snapshot,
            "BTCUSDT",
            order_type="MARKET",
            side="BUY",
            quantity="1",
        )


def test_futures_service_uses_trusted_metadata_snapshot():
    module = ast.parse(
        Path(service.__file__).read_text(encoding="utf-8")
    )

    function = next(
        node for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_futures_trade_in_transaction"
    )

    calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "validate_order"
    ]

    assert len(calls) == 1

    call = calls[0]

    assert ast.unparse(call.args[0]) == (
        "quote_context.metadata_snapshot"
    )

    kwargs = {
        item.arg: ast.unparse(item.value)
        for item in call.keywords
    }

    assert kwargs["order_type"] == "'MARKET'"
    assert kwargs["side"] == "side.upper()"
    assert kwargs["quantity"] == "quantity"
    assert kwargs["now_monotonic"] == (
        "quote_context.current_monotonic"
    )

    reference = next(
        node for node in ast.walk(call)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ReferencePrice"
    )

    reference_kwargs = {
        item.arg: ast.unparse(item.value)
        for item in reference.keywords
    }

    assert reference_kwargs["price"] == "validated_price.price"
    assert reference_kwargs["fetched_at_monotonic"] == (
        "quote_context.received_at_monotonic"
    )


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.active = True
        self.session.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.session.exit_count += 1
        self.session.exit_exception = exc_type
        self.session.active = False

        if exc_type is None:
            self.session.commit_path_observed = True
        else:
            self.session.rollback_path_observed = True

        return False


class FakeSession:
    def __init__(self):
        self.active = False
        self.enter_count = 0
        self.exit_count = 0
        self.exit_exception = None
        self.commit_path_observed = False
        self.rollback_path_observed = False

    def in_transaction(self):
        return self.active

    def begin(self):
        return FakeTransaction(self)


def test_wrapper_normalizes_filter_validation_and_rolls_back(monkeypatch):
    async def scenario():
        session = FakeSession()

        async def failing_inner(received_session, **kwargs):
            assert received_session.in_transaction()
            raise FilterValidationError("LOT_SIZE: below minimum")

        monkeypatch.setattr(
            service,
            "execute_futures_trade_in_transaction",
            failing_inner,
        )

        with pytest.raises(
            service.FuturesTradeExecutionError
        ) as captured:
            await service.execute_futures_trade(
                session,
                market_data=object(),
                authenticated_user_id=object(),
                account_id=object(),
                portfolio_id=object(),
                idempotency_key="test-key",
                trade_intent={},
            )

        assert isinstance(
            captured.value.__cause__,
            FilterValidationError,
        )
        assert session.enter_count == 1
        assert session.exit_count == 1
        assert session.rollback_path_observed is True
        assert session.commit_path_observed is False
        assert session.active is False

    asyncio.run(scenario())
