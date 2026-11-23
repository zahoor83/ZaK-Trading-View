import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.security.permissions import (
    AccountRole,
    Permission,
    PermissionDenied,
)
from app.security.resource_authorization import (
    PortfolioAccess,
    PortfolioAccessDenied,
    authorize_portfolio_permission,
)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class FakeSession:
    def __init__(self, row):
        self.row = row
        self.statement = None
        self.execute_count = 0

    async def execute(self, statement):
        self.statement = statement
        self.execute_count += 1
        return FakeResult(self.row)


def make_row(
    role="owner",
    membership_status="active",
    account_status="active",
    user_status="active",
    portfolio_status="active",
    market_type="spot",
):
    return (
        role,
        membership_status,
        account_status,
        user_status,
        portfolio_status,
        market_type,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(AccountRole))
async def test_active_portfolio_read_returns_verified_resource(role):
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    portfolio_id = uuid.uuid4()

    session = FakeSession(make_row(role=role.value))

    access = await authorize_portfolio_permission(
        session,
        authenticated_user_id=user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        permission=Permission.PORTFOLIO_READ,
    )

    assert access == PortfolioAccess(
        account_id=account_id,
        portfolio_id=portfolio_id,
        user_id=user_id,
        role=role,
        market_type="spot",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "permission", "market_type"),
    [
        ("owner", Permission.PAPER_TRADE, "spot"),
        ("admin", Permission.PAPER_TRADE, "futures"),
        ("trader", Permission.PAPER_TRADE, "spot"),
        ("trader", Permission.POSITION_MANAGE, "futures"),
    ],
)
async def test_approved_roles_can_access_paper_trading_resources(
    role,
    permission,
    market_type,
):
    session = FakeSession(make_row(role=role, market_type=market_type))

    access = await authorize_portfolio_permission(
        session,
        authenticated_user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        portfolio_id=uuid.uuid4(),
        permission=permission,
    )

    assert access.role.value == role
    assert access.market_type == market_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("membership_status", "suspended"),
        ("membership_status", "revoked"),
        ("account_status", "suspended"),
        ("account_status", "deletion_pending"),
        ("user_status", "disabled"),
        ("portfolio_status", "disabled"),
    ],
)
async def test_inactive_resource_chain_is_denied(field, value):
    values = {
        "role": "owner",
        "membership_status": "active",
        "account_status": "active",
        "user_status": "active",
        "portfolio_status": "active",
        "market_type": "spot",
    }
    values[field] = value

    session = FakeSession(make_row(**values))

    with pytest.raises(PortfolioAccessDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission=Permission.PORTFOLIO_READ,
        )


@pytest.mark.asyncio
async def test_missing_or_wrong_account_portfolio_is_denied():
    session = FakeSession(None)

    with pytest.raises(PortfolioAccessDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission=Permission.PORTFOLIO_READ,
        )


@pytest.mark.asyncio
async def test_unknown_database_role_is_denied():
    session = FakeSession(make_row(role="superuser"))

    with pytest.raises(PortfolioAccessDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission=Permission.PORTFOLIO_READ,
        )


@pytest.mark.asyncio
async def test_viewer_cannot_paper_trade():
    session = FakeSession(make_row(role="viewer"))

    with pytest.raises(PermissionDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission=Permission.PAPER_TRADE,
        )


@pytest.mark.asyncio
async def test_admin_cannot_request_account_deletion_as_portfolio_action():
    session = FakeSession(make_row(role="admin"))

    with pytest.raises(PermissionDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission=Permission.ACCOUNT_DELETE,
        )

    assert session.execute_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission",
    [
        Permission.PORTFOLIO_RESET,
        Permission.PORTFOLIO_REFRESH,
        Permission.MANUAL_TOP_UP,
    ],
)
async def test_trader_reset_and_refresh_are_denied_by_role_policy(
    permission,
):
    session = FakeSession(make_row(role="trader"))

    with pytest.raises(PermissionDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission=permission,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "permission"),
    [
        ("owner", Permission.PORTFOLIO_RESET),
        ("admin", Permission.PORTFOLIO_RESET),
        ("owner", Permission.PORTFOLIO_REFRESH),
        ("admin", Permission.PORTFOLIO_REFRESH),
        ("owner", Permission.MANUAL_TOP_UP),
        ("admin", Permission.MANUAL_TOP_UP),
    ],
)
async def test_owner_and_admin_can_reset_or_refresh_active_portfolio(
    role,
    permission,
):
    session = FakeSession(make_row(role=role))

    access = await authorize_portfolio_permission(
        session,
        authenticated_user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        portfolio_id=uuid.uuid4(),
        permission=permission,
    )

    assert access.role.value == role


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "account_id", "portfolio_id"),
    [
        (None, uuid.uuid4(), uuid.uuid4()),
        (uuid.uuid4(), None, uuid.uuid4()),
        (uuid.uuid4(), uuid.uuid4(), None),
        ("invalid", uuid.uuid4(), uuid.uuid4()),
        (uuid.uuid4(), "invalid", uuid.uuid4()),
        (uuid.uuid4(), uuid.uuid4(), "invalid"),
    ],
)
async def test_invalid_resource_identifiers_are_denied(
    user_id,
    account_id,
    portfolio_id,
):
    session = FakeSession(make_row())

    with pytest.raises(PortfolioAccessDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=user_id,
            account_id=account_id,
            portfolio_id=portfolio_id,
            permission=Permission.PORTFOLIO_READ,
        )

    assert session.execute_count == 0


@pytest.mark.asyncio
async def test_invalid_permission_type_is_denied_without_database_query():
    session = FakeSession(make_row())

    with pytest.raises(PermissionDenied):
        await authorize_portfolio_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            portfolio_id=uuid.uuid4(),
            permission="portfolio.read",
        )

    assert session.execute_count == 0


@pytest.mark.asyncio
async def test_query_binds_user_account_and_portfolio_scope():
    session = FakeSession(make_row())

    await authorize_portfolio_permission(
        session,
        authenticated_user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        portfolio_id=uuid.uuid4(),
        permission=Permission.PORTFOLIO_READ,
    )

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
        )
    ).lower()

    assert "account_memberships" in sql
    assert "trading_accounts" in sql
    assert "users" in sql
    assert "paper_portfolios" in sql
    assert "paper_portfolios.id" in sql
    assert "paper_portfolios.account_id" in sql
    assert "account_memberships.user_id" in sql
    assert "account_memberships.account_id" in sql


def test_portfolio_access_result_is_immutable():
    access = PortfolioAccess(
        account_id=uuid.uuid4(),
        portfolio_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=AccountRole.VIEWER,
        market_type="spot",
    )

    with pytest.raises(AttributeError):
        access.role = AccountRole.OWNER
