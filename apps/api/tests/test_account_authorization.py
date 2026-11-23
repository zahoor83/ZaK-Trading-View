import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.security.authorization import (
    AccountAccess,
    AccountAccessDenied,
    authorize_account_permission,
    load_account_access,
)
from app.security.permissions import (
    AccountRole,
    Permission,
    PermissionDenied,
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
):
    return (
        role,
        membership_status,
        account_status,
        user_status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(AccountRole))
async def test_active_membership_returns_database_role(role):
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    session = FakeSession(make_row(role=role.value))

    access = await load_account_access(
        session,
        authenticated_user_id=user_id,
        account_id=account_id,
    )

    assert access == AccountAccess(
        account_id=account_id,
        user_id=user_id,
        role=role,
    )
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_missing_membership_is_denied():
    session = FakeSession(None)

    with pytest.raises(AccountAccessDenied):
        await load_account_access(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("membership_status", "suspended"),
        ("membership_status", "revoked"),
        ("membership_status", "pending"),
        ("account_status", "suspended"),
        ("account_status", "deletion_pending"),
        ("account_status", "deleted"),
        ("user_status", "suspended"),
        ("user_status", "disabled"),
    ],
)
async def test_inactive_membership_account_or_user_is_denied(field, value):
    values = {
        "role": "owner",
        "membership_status": "active",
        "account_status": "active",
        "user_status": "active",
    }
    values[field] = value

    session = FakeSession(make_row(**values))

    with pytest.raises(AccountAccessDenied):
        await load_account_access(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["unknown", "Owner", "", None, 123])
async def test_unknown_database_role_is_denied(role):
    session = FakeSession(make_row(role=role))

    with pytest.raises(AccountAccessDenied):
        await load_account_access(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "account_id"),
    [
        (None, uuid.uuid4()),
        (uuid.uuid4(), None),
        ("not-a-uuid", uuid.uuid4()),
        (uuid.uuid4(), "not-a-uuid"),
    ],
)
async def test_invalid_identity_or_account_id_is_denied(user_id, account_id):
    session = FakeSession(make_row())

    with pytest.raises(AccountAccessDenied):
        await load_account_access(
            session,
            authenticated_user_id=user_id,
            account_id=account_id,
        )

    assert session.execute_count == 0


@pytest.mark.asyncio
async def test_query_joins_membership_account_and_user():
    session = FakeSession(make_row())

    await load_account_access(
        session,
        authenticated_user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
    )

    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
        )
    ).lower()

    assert "account_memberships" in sql
    assert "trading_accounts" in sql
    assert "users" in sql
    assert "account_memberships.status" in sql
    assert "account_memberships.user_id" in sql
    assert "account_memberships.account_id" in sql


@pytest.mark.asyncio
async def test_owner_account_permission_is_allowed():
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    session = FakeSession(make_row(role="owner"))

    access = await authorize_account_permission(
        session,
        authenticated_user_id=user_id,
        account_id=account_id,
        permission=Permission.ACCOUNT_DELETE,
    )

    assert access.role is AccountRole.OWNER


@pytest.mark.asyncio
async def test_admin_account_deletion_is_denied():
    session = FakeSession(make_row(role="admin"))

    with pytest.raises(PermissionDenied):
        await authorize_account_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            permission=Permission.ACCOUNT_DELETE,
        )


@pytest.mark.asyncio
async def test_viewer_paper_trading_is_denied():
    session = FakeSession(make_row(role="viewer"))

    with pytest.raises(PermissionDenied):
        await authorize_account_permission(
            session,
            authenticated_user_id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            permission=Permission.PAPER_TRADE,
        )


@pytest.mark.asyncio
async def test_trader_paper_trading_is_allowed():
    session = FakeSession(make_row(role="trader"))

    access = await authorize_account_permission(
        session,
        authenticated_user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        permission=Permission.PAPER_TRADE,
    )

    assert access.role is AccountRole.TRADER


@pytest.mark.asyncio
async def test_membership_denial_does_not_reveal_account_existence():
    missing = FakeSession(None)
    inactive = FakeSession(
        make_row(account_status="suspended")
    )

    messages = []

    for session in (missing, inactive):
        with pytest.raises(AccountAccessDenied) as captured:
            await load_account_access(
                session,
                authenticated_user_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
            )
        messages.append(str(captured.value))

    assert messages[0] == messages[1]


def test_account_access_result_is_immutable():
    access = AccountAccess(
        account_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role=AccountRole.VIEWER,
    )

    with pytest.raises(AttributeError):
        access.role = AccountRole.OWNER
