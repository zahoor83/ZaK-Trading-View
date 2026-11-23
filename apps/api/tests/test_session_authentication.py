from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.security.session_authentication import (
    AuthenticatedUser,
    SESSION_COOKIE_NAME,
    generate_session_token,
    get_authenticated_user,
    hash_session_token,
    load_authenticated_user,
)


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class FakeSession:
    def __init__(self, row=None):
        self.row = row
        self.statement = None
        self.calls = 0

    async def execute(self, statement):
        self.calls += 1
        self.statement = statement
        return FakeResult(self.row)


def make_row(
    token,
    *,
    expires_at=None,
    revoked_at=None,
    user_status="active",
    email_verified=True,
    user_id=None,
    session_user_id=None,
):
    uid = user_id or uuid4()

    user = SimpleNamespace(
        id=uid,
        email="verified@example.test",
        email_verified=email_verified,
        status=user_status,
    )

    user_session = SimpleNamespace(
        id=uuid4(),
        user_id=session_user_id or uid,
        token_hash=hash_session_token(token),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
        revoked_at=revoked_at,
    )

    return user_session, user


def test_generated_token_is_opaque_and_hash_is_sha256():
    generated = generate_session_token()

    assert len(generated.raw_token) >= 32
    assert generated.raw_token.isascii()
    assert len(generated.token_hash) == 64
    assert generated.token_hash == hash_session_token(generated.raw_token)
    assert generated.raw_token != generated.token_hash


def test_generated_tokens_are_unique():
    first = generate_session_token()
    second = generate_session_token()

    assert first.raw_token != second.raw_token
    assert first.token_hash != second.token_hash



@pytest.mark.asyncio
async def test_invalid_token_returns_none_without_query():
    session = FakeSession()

    result = await load_authenticated_user(session, "short", now=NOW)

    assert result is None
    assert session.calls == 0


@pytest.mark.asyncio
async def test_valid_session_returns_trusted_identity():
    token = generate_session_token().raw_token
    row = make_row(token)
    session = FakeSession(row)

    result = await load_authenticated_user(session, token, now=NOW)

    assert isinstance(result, AuthenticatedUser)
    assert result.user_id == row[1].id
    assert result.session_id == row[0].id
    assert result.email == row[1].email
    assert result.email_verified is True
    assert session.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"expires_at": NOW},
        {"expires_at": NOW - timedelta(seconds=1)},
        {"revoked_at": NOW - timedelta(minutes=1)},
        {"user_status": "suspended"},
        {"user_status": "disabled"},
        {"email_verified": False},
    ],
)
async def test_invalid_session_or_user_state_fails_closed(overrides):
    token = generate_session_token().raw_token
    session = FakeSession(make_row(token, **overrides))

    result = await load_authenticated_user(session, token, now=NOW)

    assert result is None


@pytest.mark.asyncio
async def test_naive_expiry_fails_closed():
    token = generate_session_token().raw_token
    row = make_row(token)
    row[0].expires_at = datetime(2027, 1, 1)

    result = await load_authenticated_user(
        FakeSession(row),
        token,
        now=NOW,
    )

    assert result is None


@pytest.mark.asyncio
async def test_mismatched_user_session_identity_fails_closed():
    token = generate_session_token().raw_token
    row = make_row(token, session_user_id=uuid4())

    result = await load_authenticated_user(
        FakeSession(row),
        token,
        now=NOW,
    )

    assert result is None


@pytest.mark.asyncio
async def test_malformed_now_is_rejected():
    token = generate_session_token().raw_token

    with pytest.raises(ValueError):
        await load_authenticated_user(
            FakeSession(make_row(token)),
            token,
            now=datetime(2026, 11, 11),
        )


@pytest.mark.asyncio
async def test_query_is_scoped_to_hash_revocation_expiry_and_active_verified_user():
    token = generate_session_token().raw_token
    session = FakeSession(make_row(token))

    await load_authenticated_user(session, token, now=NOW)

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "user_sessions.token_hash" in compiled
    assert "user_sessions.revoked_at IS NULL" in compiled
    assert "user_sessions.expires_at" in compiled
    assert "users.status" in compiled
    assert "users.email_verified IS true" in compiled


@pytest.mark.asyncio
async def test_dependency_rejects_missing_cookie():
    request = SimpleNamespace(cookies={})

    with pytest.raises(HTTPException) as captured:
        await get_authenticated_user(request, FakeSession())

    assert captured.value.status_code == 401
    assert captured.value.detail == "Authentication required."


@pytest.mark.asyncio
async def test_dependency_rejects_invalid_cookie():
    request = SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: "invalid"}
    )

    with pytest.raises(HTTPException) as captured:
        await get_authenticated_user(request, FakeSession())

    assert captured.value.status_code == 401


@pytest.mark.asyncio
async def test_dependency_returns_authenticated_user():
    token = generate_session_token().raw_token
    row = make_row(token)

    request = SimpleNamespace(
        cookies={SESSION_COOKIE_NAME: token}
    )

    result = await get_authenticated_user(request, FakeSession(row))

    assert result.user_id == row[1].id
    assert result.session_id == row[0].id