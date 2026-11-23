import ast
import asyncio
import inspect
from pathlib import Path

from sqlalchemy.exc import OperationalError
import pytest

from app.services import paper_futures_execution as service


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
    def __init__(self, *, active=False):
        self.active = active
        self.enter_count = 0
        self.exit_count = 0
        self.exit_exception = None
        self.commit_path_observed = False
        self.rollback_path_observed = False

    def in_transaction(self):
        return self.active

    def begin(self):
        return FakeTransaction(self)


def execution_kwargs():
    return {
        "market_data": object(),
        "authenticated_user_id": object(),
        "account_id": object(),
        "portfolio_id": object(),
        "idempotency_key": "test-key",
        "trade_intent": {},
    }


def test_wrapper_calls_inner_inside_owned_transaction(monkeypatch):
    async def scenario():
        session = FakeSession()
        expected = service.FuturesTradeExecutionResult(
            replayed=False,
            response_data={"test": "success"},
        )
        observed = {}

        async def fake_inner(received_session, **kwargs):
            observed["active"] = received_session.in_transaction()
            observed["session"] = received_session
            return expected

        monkeypatch.setattr(
            service,
            "execute_futures_trade_in_transaction",
            fake_inner,
        )

        result = await service.execute_futures_trade(
            session,
            **execution_kwargs(),
        )

        assert result is expected
        assert observed["active"] is True
        assert observed["session"] is session
        assert session.enter_count == 1
        assert session.exit_count == 1
        assert session.active is False
        assert session.commit_path_observed is True
        assert session.rollback_path_observed is False

    asyncio.run(scenario())


def test_wrapper_exception_exits_transaction_through_rollback_path(monkeypatch):
    async def scenario():
        session = FakeSession()

        async def failing_inner(received_session, **kwargs):
            assert received_session.in_transaction()
            raise service.FuturesTradeExecutionError("expected test failure")

        monkeypatch.setattr(
            service,
            "execute_futures_trade_in_transaction",
            failing_inner,
        )

        with pytest.raises(service.FuturesTradeExecutionError):
            await service.execute_futures_trade(
                session,
                **execution_kwargs(),
            )

        assert session.enter_count == 1
        assert session.exit_count == 1
        assert session.exit_exception is service.FuturesTradeExecutionError
        assert session.rollback_path_observed is True
        assert session.commit_path_observed is False
        assert session.active is False

    asyncio.run(scenario())


def test_wrapper_rejects_preexisting_transaction_without_opening_another():
    async def scenario():
        session = FakeSession(active=True)

        with pytest.raises(service.FuturesTradeExecutionError):
            await service.execute_futures_trade(
                session,
                **execution_kwargs(),
            )

        assert session.enter_count == 0
        assert session.exit_count == 0
        assert session.active is True

    asyncio.run(scenario())


def test_inner_function_requires_caller_owned_transaction():
    async def scenario():
        session = FakeSession(active=False)

        with pytest.raises(service.FuturesTradeExecutionError):
            await service.execute_futures_trade_in_transaction(
                session,
                **execution_kwargs(),
            )

        assert session.enter_count == 0
        assert session.exit_count == 0
        assert session.active is False

    asyncio.run(scenario())


def test_wrapper_propagates_database_error_and_rolls_back(monkeypatch):
    async def scenario():
        session = FakeSession()
        database_error = OperationalError(
            "SELECT 1",
            {},
            RuntimeError("simulated database failure"),
        )
        observed = {"calls": 0}

        async def failing_inner(received_session, **kwargs):
            observed["calls"] += 1
            assert received_session.in_transaction()
            raise database_error

        monkeypatch.setattr(
            service,
            "execute_futures_trade_in_transaction",
            failing_inner,
        )

        with pytest.raises(OperationalError) as exc_info:
            await service.execute_futures_trade(
                session,
                **execution_kwargs(),
            )

        assert exc_info.value is database_error
        assert observed["calls"] == 1
        assert session.enter_count == 1
        assert session.exit_count == 1
        assert session.exit_exception is OperationalError
        assert session.rollback_path_observed is True
        assert session.commit_path_observed is False
        assert session.active is False

    asyncio.run(scenario())

def test_inner_function_contains_no_commit_or_rollback_calls():
    source_path = (
        Path(service.__file__).resolve()
    )
    source = source_path.read_text(encoding="utf-8")
    module = ast.parse(source)

    function = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_futures_trade_in_transaction"
    )

    forbidden = []

    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback"}
        ):
            forbidden.append(node.func.attr)

    assert forbidden == [], (
        "Caller-owned transaction function must not commit or rollback."
    )

    assert inspect.iscoroutinefunction(
        service.execute_futures_trade_in_transaction
    )