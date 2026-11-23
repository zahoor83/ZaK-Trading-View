import ast
import asyncio
from pathlib import Path

import pytest

from app.services import paper_futures_execution as service


@pytest.fixture(scope="module")
def inner_function():
    source_path = Path(service.__file__).resolve()
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    return next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_futures_trade_in_transaction"
    )


def call_lines(function, name):
    return [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == name
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == name
        )
    ]


def test_late_replay_check_occurs_after_ledger_posting(inner_function):
    posting_lines = call_lines(
        inner_function,
        "post_paper_trade_journal_in_transaction",
    )

    assert posting_lines, "Ledger posting call is missing."

    replay_branch = next(
        (
            node
            for node in ast.walk(inner_function)
            if isinstance(node, ast.If)
            and "posting_result.replayed" in ast.unparse(node.test)
        ),
        None,
    )

    assert replay_branch is not None, "Late replay guard is missing."
    assert min(posting_lines) < replay_branch.lineno


def test_late_replay_guard_raises_execution_error(inner_function):
    replay_branch = next(
        (
            node
            for node in ast.walk(inner_function)
            if isinstance(node, ast.If)
            and "posting_result.replayed" in ast.unparse(node.test)
        ),
        None,
    )

    assert replay_branch is not None

    raises_execution_error = any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "FuturesTradeExecutionError"
        for node in ast.walk(replay_branch)
    )

    assert raises_execution_error

    returns_success = any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "FuturesTradeExecutionResult"
        for node in ast.walk(replay_branch)
    )

    assert not returns_success


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.active = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.session.active = False
        self.session.exit_exception = exc_type

        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True

        return False


class FakeSession:
    def __init__(self):
        self.active = False
        self.committed = False
        self.rolled_back = False
        self.exit_exception = None

    def in_transaction(self):
        return self.active

    def begin(self):
        return FakeTransaction(self)


def test_late_replay_error_rolls_back_wrapper_transaction(monkeypatch):
    async def scenario():
        session = FakeSession()

        async def simulated_late_replay(received_session, **kwargs):
            assert received_session.in_transaction()
            raise service.FuturesTradeExecutionError(
                "Unexpected late Futures idempotency replay; "
                "transaction must roll back."
            )

        monkeypatch.setattr(
            service,
            "execute_futures_trade_in_transaction",
            simulated_late_replay,
        )

        with pytest.raises(
            service.FuturesTradeExecutionError,
            match="late Futures idempotency replay",
        ):
            await service.execute_futures_trade(
                session,
                market_data=object(),
                authenticated_user_id=object(),
                account_id=object(),
                portfolio_id=object(),
                idempotency_key="test-key",
                trade_intent={},
            )

        assert session.active is False
        assert session.committed is False
        assert session.rolled_back is True
        assert session.exit_exception is service.FuturesTradeExecutionError

    asyncio.run(scenario())