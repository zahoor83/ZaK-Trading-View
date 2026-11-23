from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.paper_ledger import PaperExecutionRequest
from app.models.portfolio import PaperSession
from app.security.permissions import Permission
from app.services import paper_portfolio_reset as reset


class FakeResult:
    def __init__(self, scalar=None):
        self.scalar = scalar

    def scalar_one_or_none(self):
        return self.scalar


class FakeSession:
    def __init__(self, results, *, active=True):
        self.results = list(results)
        self.added = []
        self.statements = []
        self.flush_count = 0
        self.active = active

    def in_transaction(self):
        return self.active

    async def execute(self, statement, params=None):
        if not self.results:
            raise AssertionError("Unexpected database query.")
        self.statements.append((statement, params))
        return self.results.pop(0)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flush_count += 1
        for item in self.added:
            if isinstance(item, PaperSession) and item.id is None:
                item.id = uuid4()


def make_access(market_type="spot"):
    return SimpleNamespace(
        user_id=uuid4(),
        account_id=uuid4(),
        portfolio_id=uuid4(),
        session_id=uuid4(),
        book_id=uuid4(),
        role="owner",
        market_type=market_type,
        position_mode=None if market_type == "spot" else "hedge",
    )


def operation_kwargs(access, *, confirmed=True, key=None):
    return {
        "authenticated_user_id": access.user_id,
        "account_id": access.account_id,
        "portfolio_id": access.portfolio_id,
        "idempotency_key": key or str(uuid4()),
        "confirmed": confirmed,
    }


def make_old_session(access):
    return PaperSession(
        id=access.session_id,
        account_id=access.account_id,
        portfolio_id=access.portfolio_id,
        session_number=4,
        status="active",
    )


def install_dependencies(monkeypatch, access, *, denied=False):
    async def authorize(*args, **kwargs):
        assert kwargs["permission"] is Permission.PORTFOLIO_RESET
        if denied:
            raise reset.PaperWriteAuthorizationDenied("denied")
        return access

    async def validate(*args, **kwargs):
        assert kwargs["account_id"] == access.account_id
        assert kwargs["portfolio_id"] == access.portfolio_id
        assert kwargs["session_id"] == access.session_id
        assert kwargs["market_type"] == access.market_type
        return access.book_id

    async def provision(*args, **kwargs):
        assert kwargs["account_id"] == access.account_id
        assert kwargs["portfolio_id"] == access.portfolio_id
        assert kwargs["market_type"] == access.market_type
        assert kwargs["session_id"] is not None
        return uuid4()

    monkeypatch.setattr(reset, "authorize_paper_write", authorize)
    monkeypatch.setattr(reset, "validate_existing_paper_ledger", validate)
    monkeypatch.setattr(reset, "provision_initial_paper_ledger", provision)


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_reset_archives_old_session_and_provisions_new_ledger(
    monkeypatch, market_type
):
    access = make_access(market_type)
    install_dependencies(monkeypatch, access)

    archived_at = datetime.now(timezone.utc)
    old_session = make_old_session(access)
    new_book_id = uuid4()

    async def provision(*args, **kwargs):
        assert kwargs["session_id"] is not None
        assert kwargs["market_type"] == market_type
        return new_book_id

    monkeypatch.setattr(reset, "provision_initial_paper_ledger", provision)

    session = FakeSession([
        FakeResult(),                    # No existing idempotency request.
        FakeResult(),                    # No open positions.
        FakeResult(old_session),         # Current active session.
        FakeResult(archived_at),         # DB archive function result.
        FakeResult(4),                   # Maximum session number.
    ])

    result = await reset.reset_paper_portfolio_in_transaction(
        session,
        **operation_kwargs(access),
    )

    assert result.replayed is False
    assert result.response_data["operation_type"] == "reset"
    assert result.response_data["previous_session_id"] == str(access.session_id)
    assert result.response_data["previous_session_number"] == 4
    assert result.response_data["previous_book_id"] == str(access.book_id)
    assert result.response_data["new_session_number"] == 5
    assert result.response_data["new_book_id"] == str(new_book_id)
    assert result.response_data["initial_balance"] == "1000.00"

    assert old_session.status == "archived"
    assert old_session.archived_at == archived_at

    new_sessions = [
        item for item in session.added
        if isinstance(item, PaperSession)
    ]
    assert len(new_sessions) == 1
    assert new_sessions[0].status == "active"
    assert new_sessions[0].session_number == 5

    requests = [
        item for item in session.added
        if isinstance(item, PaperExecutionRequest)
    ]
    assert len(requests) == 1
    assert requests[0].scope_type == "portfolio"
    assert requests[0].operation_type == "reset"
    assert requests[0].portfolio_id == access.portfolio_id
    assert requests[0].book_id is None
    assert requests[0].status == "completed"

    archive_statement, archive_params = session.statements[3]
    assert "archive_paper_ledger_book" in str(archive_statement)
    assert archive_params == {
        "account_id": access.account_id,
        "portfolio_id": access.portfolio_id,
        "session_id": access.session_id,
        "book_id": access.book_id,
    }

    assert not session.results


@pytest.mark.asyncio
async def test_reset_returns_persisted_idempotent_replay_before_position_check(
    monkeypatch,
):
    access = make_access()
    install_dependencies(monkeypatch, access)

    response = {
        "operation_type": "reset",
        "status": "completed",
        "new_session_number": 5,
    }
    existing = SimpleNamespace(
        request_fingerprint="a" * 64,
        response_data=response,
    )

    monkeypatch.setattr(
        reset,
        "decide_idempotency",
        lambda **kwargs: SimpleNamespace(
            kind=reset.IdempotencyDecisionKind.REPLAY,
            response_data=response,
        ),
    )

    session = FakeSession([FakeResult(existing)])

    result = await reset.reset_paper_portfolio_in_transaction(
        session,
        **operation_kwargs(access),
    )

    assert result.replayed is True
    assert result.response_data == response
    assert len(session.statements) == 1
    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_reset_rejects_idempotency_conflict(monkeypatch):
    access = make_access()
    install_dependencies(monkeypatch, access)

    existing = SimpleNamespace(
        request_fingerprint="b" * 64,
        response_data={"operation_type": "reset"},
    )
    monkeypatch.setattr(
        reset,
        "decide_idempotency",
        lambda **kwargs: SimpleNamespace(
            kind=reset.IdempotencyDecisionKind.CONFLICT,
            response_data=None,
        ),
    )

    session = FakeSession([FakeResult(existing)])

    with pytest.raises(reset.PaperPortfolioResetIdempotencyConflict):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert len(session.statements) == 1
    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_reset_rejects_open_positions_without_archiving(monkeypatch):
    access = make_access()
    install_dependencies(monkeypatch, access)

    session = FakeSession([
        FakeResult(),             # No existing idempotency request.
        FakeResult(uuid4()),      # An open position exists.
    ])

    with pytest.raises(reset.PaperPortfolioResetError, match="open positions"):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert len(session.statements) == 2
    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_reset_rejects_authorization_denial(monkeypatch):
    access = make_access()
    install_dependencies(monkeypatch, access, denied=True)

    session = FakeSession([])

    with pytest.raises(reset.PaperPortfolioResetAuthorizationDenied):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert not session.statements
    assert not session.added


@pytest.mark.asyncio
async def test_reset_requires_active_caller_owned_transaction(monkeypatch):
    access = make_access()
    install_dependencies(monkeypatch, access)

    session = FakeSession([], active=False)

    with pytest.raises(reset.PaperPortfolioResetError, match="caller-owned"):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert not session.statements


@pytest.mark.asyncio
async def test_reset_requires_explicit_confirmation(monkeypatch):
    access = make_access()
    install_dependencies(monkeypatch, access)

    session = FakeSession([])

    with pytest.raises(reset.PaperPortfolioResetError, match="confirmation"):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access, confirmed=False),
        )

    assert not session.statements


@pytest.mark.asyncio
async def test_reset_fails_closed_when_database_archive_returns_no_timestamp(
    monkeypatch,
):
    access = make_access()
    install_dependencies(monkeypatch, access)

    old_session = make_old_session(access)
    session = FakeSession([
        FakeResult(),
        FakeResult(),
        FakeResult(old_session),
        FakeResult(None),
    ])

    with pytest.raises(reset.PaperPortfolioResetError, match="no timestamp"):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert old_session.status == "active"
    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_reset_fails_closed_when_new_ledger_provisioning_fails(
    monkeypatch,
):
    access = make_access()
    install_dependencies(monkeypatch, access)

    async def fail_provision(*args, **kwargs):
        raise reset.PaperLedgerProvisioningError("provisioning failed")

    monkeypatch.setattr(
        reset,
        "provision_initial_paper_ledger",
        fail_provision,
    )

    archived_at = datetime.now(timezone.utc)
    old_session = make_old_session(access)
    session = FakeSession([
        FakeResult(),
        FakeResult(),
        FakeResult(old_session),
        FakeResult(archived_at),
        FakeResult(4),
    ])

    with pytest.raises(reset.PaperPortfolioResetError, match="provisioned"):
        await reset.reset_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    # The in-transaction function leaves rollback to its caller.
    # No idempotency request may be persisted after failed provisioning.
    assert old_session.status == "archived"
    assert not any(
        isinstance(item, PaperExecutionRequest)
        for item in session.added
    )