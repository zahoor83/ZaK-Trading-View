from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.identity import User, UserSession
from app.services.session_service import (
    InvalidSessionExpiryError,
    InvalidSessionUserError,
    SessionCreationResult,
    SessionTransactionOwnershipError,
    SessionUserNotEligibleError,
    create_user_session,
    revoke_user_session,
)
from app.security.session_authentication import hash_session_token


NOW = datetime.now(timezone.utc)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.transaction_active = True
        self.session.begin_count += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.session.transaction_active = False

        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True

        return False


_UNSET = object()


class FakeSession:
    def __init__(
        self,
        user=None,
        user_session=None,
        already_active=False,
        fail_on_flush=False,
        session_query_result=_UNSET,
    ):
        self.user = user
        self.user_session = user_session
        self.session_query_result = session_query_result
        self.transaction_active = already_active
        self.fail_on_flush = fail_on_flush
        self.begin_count = 0
        self.execute_count = 0
        self.flush_count = 0
        self.committed = False
        self.rolled_back = False
        self.added = []
        self.statements = []

    def in_transaction(self):
        return self.transaction_active

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        self.execute_count += 1
        self.statements.append(statement)

        if self.execute_count == 1:
            if self.user is not None:
                value = self.user
            elif self.session_query_result is not _UNSET:
                value = self.session_query_result
            else:
                value = self.user_session
        else:
            value = self.user_session

        return FakeResult(value)

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        self.flush_count += 1

        if self.fail_on_flush:
            raise RuntimeError("Simulated flush failure.")

        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = uuid4()


def active_verified_user():
    return SimpleNamespace(
        id=uuid4(),
        status="active",
        email_verified=True,
    )


@pytest.mark.asyncio
async def test_create_session_returns_raw_token_and_persists_only_hash():
    user = active_verified_user()
    session = FakeSession(user=user)

    result = await create_user_session(
        session,
        user_id=user.id,
        expires_at=NOW + timedelta(hours=2),
        now=NOW,
    )

    assert isinstance(result, SessionCreationResult)
    assert result.session_id is not None
    assert len(result.raw_token) >= 32
    assert result.expires_at == NOW + timedelta(hours=2)

    stored = next(x for x in session.added if isinstance(x, UserSession))

    assert stored.token_hash == hash_session_token(result.raw_token)
    assert stored.token_hash != result.raw_token
    assert stored.user_id == user.id
    assert stored.expires_at == result.expires_at
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_create_session_locks_user_row():
    user = active_verified_user()
    session = FakeSession(user=user)

    await create_user_session(
        session,
        user_id=user.id,
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )

    assert session.statements[0]._for_update_arg is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        None,
        SimpleNamespace(status="suspended", email_verified=True),
        SimpleNamespace(status="active", email_verified=False),
    ],
)
async def test_ineligible_user_cannot_receive_session(user):
    session = FakeSession(user=user)

    with pytest.raises(SessionUserNotEligibleError):
        await create_user_session(
            session,
            user_id=uuid4(),
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    assert session.rolled_back is True
    assert session.added == []


@pytest.mark.asyncio
async def test_expired_session_expiry_is_rejected_before_transaction():
    session = FakeSession(user=active_verified_user())

    with pytest.raises(InvalidSessionExpiryError):
        await create_user_session(
            session,
            user_id=session.user.id,
            expires_at=NOW,
            now=NOW,
        )

    assert session.begin_count == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_naive_expiry_is_rejected():
    session = FakeSession(user=active_verified_user())

    with pytest.raises(InvalidSessionExpiryError):
        await create_user_session(
            session,
            user_id=session.user.id,
            expires_at=datetime(2027, 1, 1),
            now=NOW,
        )

    assert session.begin_count == 0


@pytest.mark.asyncio
async def test_invalid_user_id_is_rejected():
    session = FakeSession()

    with pytest.raises(InvalidSessionUserError):
        await create_user_session(
            session,
            user_id="not-a-uuid",
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    assert session.begin_count == 0


@pytest.mark.asyncio
async def test_existing_transaction_is_rejected():
    user = active_verified_user()
    session = FakeSession(user=user, already_active=True)

    with pytest.raises(SessionTransactionOwnershipError):
        await create_user_session(
            session,
            user_id=user.id,
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    assert session.begin_count == 0


@pytest.mark.asyncio
async def test_flush_failure_rolls_back_session_creation():
    user = active_verified_user()
    session = FakeSession(user=user, fail_on_flush=True)

    with pytest.raises(RuntimeError, match="Simulated flush failure"):
        await create_user_session(
            session,
            user_id=user.id,
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
        )

    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_revoke_own_session():
    user_id = uuid4()
    token = "A" * 40

    stored = SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        token_hash=hash_session_token(token),
        revoked_at=None,
    )

    session = FakeSession(user_session=stored)

    result = await revoke_user_session(
        session,
        authenticated_user_id=user_id,
        raw_token=token,
        now=NOW,
    )

    assert result is True
    assert stored.revoked_at == NOW
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_revoke_unknown_session_returns_false():
    session = FakeSession(user_session=None)

    result = await revoke_user_session(
        session,
        authenticated_user_id=uuid4(),
        raw_token="B" * 40,
        now=NOW,
    )

    assert result is False
    assert session.committed is True


@pytest.mark.asyncio
async def test_revoke_foreign_session_returns_false():
    token = "C" * 40
    foreign_user_id = uuid4()
    authenticated_user_id = uuid4()
    assert foreign_user_id != authenticated_user_id

    stored = SimpleNamespace(
        id=uuid4(),
        user_id=foreign_user_id,
        token_hash=hash_session_token(token),
        revoked_at=None,
    )

    # The database returns no row because the SQL query is scoped to
    # authenticated_user_id, even though a foreign row has the same token hash.
    session = FakeSession(
        user_session=stored,
        session_query_result=None,
    )

    result = await revoke_user_session(
        session,
        authenticated_user_id=authenticated_user_id,
        raw_token=token,
        now=NOW,
    )

    assert result is False
    assert stored.revoked_at is None


@pytest.mark.asyncio
async def test_revoke_already_revoked_session_is_idempotent():
    token = "D" * 40
    previous_time = NOW - timedelta(minutes=5)

    stored = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        token_hash=hash_session_token(token),
        revoked_at=previous_time,
    )

    session = FakeSession(user_session=stored)

    result = await revoke_user_session(
        session,
        authenticated_user_id=stored.user_id,
        raw_token=token,
        now=NOW,
    )

    assert result is False
    assert stored.revoked_at == previous_time
    assert session.flush_count == 0


@pytest.mark.asyncio
async def test_revoke_invalid_token_does_not_query_database():
    session = FakeSession()

    result = await revoke_user_session(
        session,
        authenticated_user_id=uuid4(),
        raw_token="short",
        now=NOW,
    )

    assert result is False
    assert session.execute_count == 0
    assert session.begin_count == 0


@pytest.mark.asyncio
async def test_revoke_query_is_scoped_to_user_and_token_hash():
    user_id = uuid4()
    token = "E" * 40
    session = FakeSession(user_session=None)

    await revoke_user_session(
        session,
        authenticated_user_id=user_id,
        raw_token=token,
        now=NOW,
    )

    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "user_sessions.user_id" in compiled
    assert "user_sessions.token_hash" in compiled
    assert "FOR UPDATE" in compiled


@pytest.mark.asyncio
async def test_revoke_expired_session_is_allowed():
    token = "F" * 40
    stored = SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        token_hash=hash_session_token(token),
        expires_at=NOW - timedelta(days=1),
        revoked_at=None,
    )

    session = FakeSession(user_session=stored)

    result = await revoke_user_session(
        session,
        authenticated_user_id=stored.user_id,
        raw_token=token,
        now=NOW,
    )

    assert result is True
    assert stored.revoked_at == NOW
    assert stored.expires_at < NOW


@pytest.mark.asyncio
async def test_revoke_rejects_existing_transaction():
    session = FakeSession(already_active=True)

    with pytest.raises(SessionTransactionOwnershipError):
        await revoke_user_session(
            session,
            authenticated_user_id=uuid4(),
            raw_token="G" * 40,
            now=NOW,
        )

    assert session.begin_count == 0
