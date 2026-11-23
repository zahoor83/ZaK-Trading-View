from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.models.identity import AccountMembership, TradingAccount, User
from app.models.paper_ledger import PaperLedgerBook
from app.models.portfolio import PaperPortfolio, PaperSession
from app.security.permissions import AccountRole, Permission
from app.security.paper_write_authorization import (
    LockedPaperWriteAccess,
    PaperWriteAuthorizationDenied,
    PaperWriteTransactionRequired,
    authorize_paper_write,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, records, *, active_transaction=True):
        self.records = list(records)
        self.statements = []
        self.active_transaction = active_transaction

    def in_transaction(self):
        return self.active_transaction

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.records:
            raise AssertionError("Unexpected extra database query.")
        return FakeResult(self.records.pop(0))


def make_records(
    *,
    role="trader",
    user_status="active",
    account_status="active",
    membership_status="active",
    portfolio_status="active",
    session_status="active",
    book_status="active",
    book_market="spot",
    portfolio_market="spot",
    position_mode=None,
):
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()
    session_id = uuid.uuid4()
    book_id = uuid.uuid4()

    records = [
        SimpleNamespace(id=user_id, status=user_status),
        SimpleNamespace(id=account_id, status=account_status),
        SimpleNamespace(
            account_id=account_id,
            user_id=user_id,
            role=role,
            status=membership_status,
        ),
        SimpleNamespace(
            id=portfolio_id,
            account_id=account_id,
            status=portfolio_status,
            market_type=portfolio_market,
            position_mode=position_mode,
        ),
        SimpleNamespace(
            id=session_id,
            account_id=account_id,
            portfolio_id=portfolio_id,
            status=session_status,
        ),
        SimpleNamespace(
            id=book_id,
            account_id=account_id,
            portfolio_id=portfolio_id,
            session_id=session_id,
            status=book_status,
            market_type=book_market,
        ),
    ]

    identifiers = {
        "user_id": user_id,
        "account_id": account_id,
        "portfolio_id": portfolio_id,
        "session_id": session_id,
        "book_id": book_id,
    }
    return records, identifiers


@pytest.mark.asyncio
async def test_authorization_locks_all_six_rows_in_required_order():
    records, ids = make_records(role="trader")
    session = FakeSession(records)

    access = await authorize_paper_write(
        session,
        authenticated_user_id=ids["user_id"],
        account_id=ids["account_id"],
        portfolio_id=ids["portfolio_id"],
        permission=Permission.PAPER_TRADE,
    )

    assert isinstance(access, LockedPaperWriteAccess)
    assert access.user_id == ids["user_id"]
    assert access.account_id == ids["account_id"]
    assert access.portfolio_id == ids["portfolio_id"]
    assert access.session_id == ids["session_id"]
    assert access.book_id == ids["book_id"]
    assert access.role is AccountRole.TRADER
    assert access.market_type == "spot"
    assert access.position_mode is None
    assert session.records == []

    expected_models = [
        User,
        TradingAccount,
        AccountMembership,
        PaperPortfolio,
        PaperSession,
        PaperLedgerBook,
    ]

    assert len(session.statements) == len(expected_models)

    for statement, model in zip(session.statements, expected_models):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert model.__tablename__ in sql

        if model is PaperLedgerBook:
            # The ledger book is read-only here. Account-level mutations
            # are serialized by the parent TradingAccount row lock.
            assert "FOR UPDATE OF " not in sql
        else:
            assert "FOR UPDATE OF " in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record_index", "field", "invalid_value"),
    [
        (0, "status", "disabled"),
        (1, "status", "suspended"),
        (2, "status", "revoked"),
        (3, "status", "disabled"),
        (4, "status", "archived"),
        (5, "status", "archived"),
    ],
)
async def test_inactive_resource_chain_fails_closed(
    record_index,
    field,
    invalid_value,
):
    records, ids = make_records()
    setattr(records[record_index], field, invalid_value)
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert len(session.statements) == record_index + 1


@pytest.mark.asyncio
async def test_unknown_membership_role_fails_closed():
    records, ids = make_records(role="superuser")
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert len(session.statements) == 3


@pytest.mark.asyncio
async def test_viewer_cannot_trade():
    records, ids = make_records(role="viewer")
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert len(session.statements) == 3


@pytest.mark.asyncio
async def test_trader_cannot_reset_without_portfolio_assignment():
    records, ids = make_records(role="trader")
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PORTFOLIO_RESET,
        )

    assert len(session.statements) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize(
    "permission",
    [
        Permission.PAPER_TRADE,
        Permission.POSITION_MANAGE,
        Permission.PORTFOLIO_RESET,
        Permission.PORTFOLIO_REFRESH,
        Permission.MANUAL_TOP_UP,
    ],
)
async def test_owner_and_admin_can_reach_authorized_write_scope(role, permission):
    records, ids = make_records(role=role)
    session = FakeSession(records)

    access = await authorize_paper_write(
        session,
        authenticated_user_id=ids["user_id"],
        account_id=ids["account_id"],
        portfolio_id=ids["portfolio_id"],
        permission=permission,
    )

    assert access.role is AccountRole(role)
    assert len(session.statements) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["one_way", "hedge"])
async def test_futures_authorization_returns_locked_position_mode(mode):
    records, ids = make_records(
        portfolio_market="futures",
        position_mode=mode,
        book_market="futures",
    )
    session = FakeSession(records)

    access = await authorize_paper_write(
        session,
        authenticated_user_id=ids["user_id"],
        account_id=ids["account_id"],
        portfolio_id=ids["portfolio_id"],
        permission=Permission.PAPER_TRADE,
    )

    assert access.market_type == "futures"
    assert access.position_mode == mode
    assert len(session.statements) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "invalid", "spot"])
async def test_futures_invalid_position_mode_fails_closed(mode):
    records, ids = make_records(
        portfolio_market="futures",
        position_mode=mode,
        book_market="futures",
    )
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert len(session.statements) == 4


@pytest.mark.asyncio
async def test_spot_rejects_non_null_position_mode():
    records, ids = make_records(position_mode="one_way")
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert len(session.statements) == 4


@pytest.mark.asyncio
async def test_book_market_must_match_portfolio_market():
    records, ids = make_records(book_market="futures")
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert len(session.statements) == 6


@pytest.mark.asyncio
async def test_read_permission_is_not_accepted_by_write_helper():
    records, ids = make_records()
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PORTFOLIO_READ,
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_write_helper_requires_caller_owned_active_transaction():
    records, ids = make_records()
    session = FakeSession(records, active_transaction=False)

    with pytest.raises(PaperWriteTransactionRequired):
        await authorize_paper_write(
            session,
            authenticated_user_id=ids["user_id"],
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_invalid_identifiers_fail_before_database_access():
    records, ids = make_records()
    session = FakeSession(records)

    with pytest.raises(PaperWriteAuthorizationDenied):
        await authorize_paper_write(
            session,
            authenticated_user_id="not-a-uuid",
            account_id=ids["account_id"],
            portfolio_id=ids["portfolio_id"],
            permission=Permission.PAPER_TRADE,
        )

    assert session.statements == []