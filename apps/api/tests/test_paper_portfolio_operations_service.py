from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.security.permissions import Permission
from app.services import paper_portfolio_operations as operations


class FakeResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.scalar

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.added = []
        self.flush_count = 0
        self.active = True

    def in_transaction(self):
        return self.active

    async def execute(self, statement):
        if not self.results:
            raise AssertionError("Unexpected database query.")
        return self.results.pop(0)

    def add(self, item):
        self.added.append(item)

    def add_all(self, items):
        self.added.extend(items)

    async def flush(self):
        self.flush_count += 1
        for item in self.added:
            if isinstance(item, operations.PaperLedgerTransaction) and item.id is None:
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


def make_account_rows(access, cash="100.00"):
    codes = (
        "cash_available",
        "spot_position_cost",
        "futures_margin_locked",
        "realized_pnl",
        "fee_expense",
        "contributed_capital",
    )
    rows = []
    for code in codes:
        account = SimpleNamespace(
            id=uuid4(),
            book_id=access.book_id,
            market_type=access.market_type,
            code=code,
        )
        balance = Decimal(cash) if code == "cash_available" else Decimal("0")
        snapshot = SimpleNamespace(
            book_id=access.book_id,
            ledger_account_id=account.id,
            balance=balance,
        )
        rows.append((account, snapshot))
    return rows


def install_service_dependencies(monkeypatch, access, *, denied=False):
    async def authorize(*args, **kwargs):
        assert kwargs["permission"] is Permission.MANUAL_TOP_UP
        if denied:
            raise operations.PaperWriteAuthorizationDenied("denied")
        return access

    async def validate(*args, **kwargs):
        assert kwargs["market_type"] == access.market_type
        return access.book_id

    monkeypatch.setattr(operations, "authorize_paper_write", authorize)
    monkeypatch.setattr(operations, "validate_existing_paper_ledger", validate)


def operation_kwargs(access, *, confirmed=True, key=None):
    return {
        "authenticated_user_id": access.user_id,
        "account_id": access.account_id,
        "portfolio_id": access.portfolio_id,
        "idempotency_key": key or str(uuid4()),
        "confirmed": confirmed,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["spot", "futures"])
async def test_manual_top_up_posts_portfolio_request_and_balanced_ledger(
    monkeypatch, market_type
):
    access = make_access(market_type)
    install_service_dependencies(monkeypatch, access)

    session = FakeSession([
        FakeResult(),  # No existing portfolio-scoped request.
        FakeResult(rows=make_account_rows(access)),  # Accounts and snapshots.
        FakeResult(),  # No orphan book transaction.
        FakeResult(scalar=Decimal("1000.00")),  # Trigger-updated cash snapshot.
    ])

    result = await operations.manual_top_up_paper_portfolio_in_transaction(
        session,
        **operation_kwargs(access),
    )

    assert result.replayed is False
    assert result.response_data["transaction_type"] == "manual_top_up"
    assert result.response_data["market_type"] == market_type
    assert result.response_data["amount"] == "900.00"
    assert result.response_data["previous_balance"] == "100.00"
    assert result.response_data["new_balance"] == "1000.00"
    assert result.response_data["entry_count"] == 2
    assert result.response_data["debit_total"] == "900.00"
    assert result.response_data["credit_total"] == "900.00"

    transactions = [
        item for item in session.added
        if isinstance(item, operations.PaperLedgerTransaction)
    ]
    entries = [
        item for item in session.added
        if isinstance(item, operations.PaperLedgerEntry)
    ]
    requests = [
        item for item in session.added
        if isinstance(item, operations.PaperExecutionRequest)
    ]

    assert len(transactions) == 1
    assert transactions[0].status == "posted"
    assert transactions[0].transaction_type == "manual_top_up"
    assert len(entries) == 2
    assert {(entry.direction, entry.amount) for entry in entries} == {
        ("debit", Decimal("900.00")),
        ("credit", Decimal("900.00")),
    }
    assert len(requests) == 1
    assert requests[0].scope_type == "portfolio"
    assert requests[0].operation_type == "manual_top_up"
    assert requests[0].portfolio_id == access.portfolio_id
    assert requests[0].book_id is None
    assert requests[0].status == "completed"
    assert requests[0].response_data == result.response_data
    assert not session.results


@pytest.mark.asyncio
async def test_manual_top_up_requires_explicit_confirmation(monkeypatch):
    access = make_access()
    install_service_dependencies(monkeypatch, access)
    session = FakeSession([])

    with pytest.raises(operations.PaperPortfolioOperationError, match="confirmation"):
        await operations.manual_top_up_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access, confirmed=False),
        )

    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_manual_top_up_rejects_authorization_denial(monkeypatch):
    access = make_access()
    install_service_dependencies(monkeypatch, access, denied=True)
    session = FakeSession([])

    with pytest.raises(operations.PaperPortfolioOperationError, match="not permitted"):
        await operations.manual_top_up_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_manual_top_up_returns_persisted_idempotent_replay(monkeypatch):
    access = make_access()
    install_service_dependencies(monkeypatch, access)

    response = {
        "transaction_id": str(uuid4()),
        "transaction_type": "manual_top_up",
        "status": "posted",
        "amount": "900.00",
    }
    existing = SimpleNamespace(
        request_fingerprint="a" * 64,
        response_data=response,
    )

    monkeypatch.setattr(
        operations,
        "decide_idempotency",
        lambda **kwargs: SimpleNamespace(
            kind=operations.IdempotencyDecisionKind.REPLAY,
            response_data=response,
        ),
    )

    session = FakeSession([FakeResult(scalar=existing)])

    result = await operations.manual_top_up_paper_portfolio_in_transaction(
        session,
        **operation_kwargs(access),
    )

    assert result.replayed is True
    assert result.response_data == response
    assert session.flush_count == 0
    assert not session.added
    assert not session.results


@pytest.mark.asyncio
async def test_manual_top_up_rejects_idempotency_conflict(monkeypatch):
    access = make_access()
    install_service_dependencies(monkeypatch, access)

    existing = SimpleNamespace(
        request_fingerprint="b" * 64,
        response_data={"transaction_id": str(uuid4())},
    )
    monkeypatch.setattr(
        operations,
        "decide_idempotency",
        lambda **kwargs: SimpleNamespace(
            kind=operations.IdempotencyDecisionKind.CONFLICT,
            response_data=None,
        ),
    )

    session = FakeSession([FakeResult(scalar=existing)])

    with pytest.raises(operations.PaperPortfolioIdempotencyConflict):
        await operations.manual_top_up_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert session.flush_count == 0
    assert not session.added


@pytest.mark.asyncio
async def test_manual_top_up_fails_closed_on_snapshot_mismatch(monkeypatch):
    access = make_access()
    install_service_dependencies(monkeypatch, access)

    session = FakeSession([
        FakeResult(),
        FakeResult(rows=make_account_rows(access)),
        FakeResult(),
        FakeResult(scalar=Decimal("999.99")),
    ])

    with pytest.raises(operations.PaperPortfolioOperationError, match="snapshot verification"):
        await operations.manual_top_up_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert any(
        isinstance(item, operations.PaperLedgerTransaction)
        for item in session.added
    )
    assert not any(
        isinstance(item, operations.PaperExecutionRequest)
        for item in session.added
    )


@pytest.mark.asyncio
async def test_manual_top_up_rejects_missing_active_transaction(monkeypatch):
    access = make_access()
    install_service_dependencies(monkeypatch, access)

    session = FakeSession([])
    session.active = False

    with pytest.raises(operations.PaperPortfolioOperationError, match="active caller-owned"):
        await operations.manual_top_up_paper_portfolio_in_transaction(
            session,
            **operation_kwargs(access),
        )

    assert not session.added
