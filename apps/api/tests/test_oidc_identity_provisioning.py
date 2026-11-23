from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.identity import AuthIdentity, User
from app.services.oidc_auth_service import (
    IdentityProvisioningError,
    resolve_or_provision_user,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.session.transaction_error = exc_type
        return False


class FakeSession:
    def __init__(self, results, *, fail_identity_flush=False):
        self.results = list(results)
        self.added = []
        self.flush_count = 0
        self.fail_identity_flush = fail_identity_flush
        self.transaction_error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        if not self.results:
            raise AssertionError("Unexpected database query in fake session.")
        return FakeResult(self.results.pop(0))

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        self.flush_count += 1
        instance = self.added[-1]

        if isinstance(instance, User) and instance.id is None:
            instance.id = uuid4()

        if isinstance(instance, AuthIdentity) and self.fail_identity_flush:
            raise IntegrityError(
                "simulated unique constraint",
                {},
                Exception("simulated unique violation"),
            )


class FakeSessionFactory:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.calls = 0

    def __call__(self):
        if self.calls >= len(self.sessions):
            raise AssertionError("Unexpected session factory call.")
        session = self.sessions[self.calls]
        self.calls += 1
        return session


def identity(
    *,
    issuer="https://issuer.example/",
    subject="provider-subject-123",
    email="person@example.com",
    email_verified=True,
):
    return type(
        "Identity",
        (),
        {
            "issuer": issuer,
            "subject": subject,
            "email": email,
            "email_verified": email_verified,
            "name": "Example Person",
        },
    )()


@pytest.mark.asyncio
async def test_new_verified_identity_creates_user_and_auth_identity():
    session = FakeSession([None, None])
    factory = FakeSessionFactory([session])

    result = await resolve_or_provision_user(factory, identity())

    assert result[0] is not None
    assert result[1] == "person@example.com"
    assert len(session.added) == 2
    user, auth_identity = session.added

    assert isinstance(user, User)
    assert user.email == "person@example.com"
    assert user.email_normalized == "person@example.com"
    assert user.email_verified is True

    assert isinstance(auth_identity, AuthIdentity)
    assert auth_identity.user_id == result[0]
    assert auth_identity.issuer == "https://issuer.example/"
    assert auth_identity.subject == "provider-subject-123"
    assert session.transaction_error is None


@pytest.mark.asyncio
async def test_existing_exact_provider_identity_returns_existing_user():
    user_id = uuid4()
    existing_user = type(
        "ExistingUser",
        (),
        {
            "id": user_id,
            "status": "active",
            "email_verified": True,
            "email": "canonical-db@example.com",
        },
    )()

    session = FakeSession([existing_user])
    factory = FakeSessionFactory([session])

    result = await resolve_or_provision_user(factory, identity())

    assert result == (user_id, "canonical-db@example.com")
    assert session.added == []


@pytest.mark.asyncio
async def test_existing_email_does_not_auto_link_different_identity():
    session = FakeSession([None, uuid4()])
    factory = FakeSessionFactory([session])

    with pytest.raises(IdentityProvisioningError):
        await resolve_or_provision_user(factory, identity())

    assert session.added == []
    assert session.transaction_error is IdentityProvisioningError


@pytest.mark.asyncio
async def test_unverified_email_is_rejected_before_session_creation():
    factory = FakeSessionFactory([])

    with pytest.raises(IdentityProvisioningError):
        await resolve_or_provision_user(
            factory,
            identity(email_verified=False),
        )

    assert factory.calls == 0


@pytest.mark.asyncio
async def test_concurrent_same_identity_race_recovers_exact_existing_user():
    recovered_user_id = uuid4()
    recovered_user = type(
        "ExistingUser",
        (),
        {
            "id": recovered_user_id,
            "status": "active",
            "email_verified": True,
            "email": "recovered-db@example.com",
        },
    )()

    first_session = FakeSession(
        [None, None],
        fail_identity_flush=True,
    )
    recovery_session = FakeSession([recovered_user])

    factory = FakeSessionFactory(
        [first_session, recovery_session]
    )

    result = await resolve_or_provision_user(factory, identity())

    assert result == (recovered_user_id, "recovered-db@example.com")
    assert factory.calls == 2
    assert first_session.transaction_error is IntegrityError


@pytest.mark.asyncio
async def test_concurrent_different_identity_email_collision_is_rejected():
    first_session = FakeSession(
        [None, None],
        fail_identity_flush=True,
    )
    recovery_session = FakeSession([None])

    factory = FakeSessionFactory(
        [first_session, recovery_session]
    )

    with pytest.raises(IdentityProvisioningError):
        await resolve_or_provision_user(factory, identity())

    assert factory.calls == 2
    assert first_session.transaction_error is IntegrityError
