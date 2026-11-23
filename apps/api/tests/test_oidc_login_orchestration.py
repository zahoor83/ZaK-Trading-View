from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.services.oidc_login_orchestration as orchestration
from app.security.oidc_configuration import Auth0Settings
from app.services.oidc_transaction_service import OIDCTransactionEncryptionSettings


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


class FakeSession:
    pass


class FakeSessionFactory:
    def __init__(self):
        self.sessions = []

    def __call__(self):
        session = FakeSession()
        self.sessions.append(session)

        @asynccontextmanager
        async def manager():
            yield session

        return manager()


@pytest.fixture
def settings():
    return Auth0Settings(
        domain="dev-wxmskulgynvly88l.us.auth0.com",
        client_id="test-client-id",
        client_secret="test-client-secret",
        callback_url="https://app.example.com/api/v1/auth/callback",
        app_public_url="https://app.example.com",
    )


@pytest.fixture
def encryption():
    return OIDCTransactionEncryptionSettings(
        keys={"test-v1": b"K" * 32},
        active_key_id="test-v1",
    )


@pytest.fixture
def identity():
    return SimpleNamespace(
        issuer="https://dev-wxmskulgynvly88l.us.auth0.com/",
        subject="auth0|subject-123",
        email="person@example.com",
        email_verified=True,
        name="Example Person",
    )


@pytest.fixture
def metadata():
    return {
        "token_endpoint":
            "https://dev-wxmskulgynvly88l.us.auth0.com/oauth/token"
    }


@pytest.fixture
def successful_dependencies(monkeypatch, identity):
    calls = {}
    user_id = uuid4()
    session_id = uuid4()
    raw_token = "opaque-session-token-never-stored"
    expires_at = NOW + timedelta(hours=12)
    consumed = SimpleNamespace(
        code_verifier="v" * 64,
        nonce_hash="a" * 64,
    )

    async def consume(session, **kwargs):
        calls["consume_session"] = session
        calls["consume_kwargs"] = kwargs
        return consumed

    async def exchange(*args, **kwargs):
        calls["exchange_args"] = args
        calls["exchange_kwargs"] = kwargs
        return "signed-id-token"

    async def verify(*args, **kwargs):
        calls["verify_args"] = args
        calls["verify_kwargs"] = kwargs
        return identity

    async def provision(factory, verified_identity):
        calls["provision_factory"] = factory
        calls["provision_identity"] = verified_identity
        return user_id, "canonical-db@example.com"

    async def ensure_account(session, *, owner_user_id):
        calls["ensure_account_session"] = session
        calls["ensure_account_user_id"] = owner_user_id

    async def create_session(session, **kwargs):
        calls["create_session"] = session
        calls["create_session_kwargs"] = kwargs
        return SimpleNamespace(
            session_id=session_id,
            raw_token=raw_token,
            expires_at=expires_at,
        )

    monkeypatch.setattr(
        orchestration, "consume_oidc_auth_transaction", consume
    )
    monkeypatch.setattr(
        orchestration, "exchange_authorization_code", exchange
    )
    monkeypatch.setattr(orchestration, "verify_id_token", verify)
    monkeypatch.setattr(
        orchestration, "resolve_or_provision_user", provision
    )
    monkeypatch.setattr(
        orchestration, "ensure_default_account_with_initial_portfolios",
        ensure_account,
    )
    monkeypatch.setattr(
        orchestration, "create_user_session", create_session
    )

    return {
        "calls": calls,
        "user_id": user_id,
        "session_id": session_id,
        "raw_token": raw_token,
        "expires_at": expires_at,
        "consumed": consumed,
    }


@pytest.mark.asyncio
async def test_complete_login_consumes_transaction_verifies_identity_and_creates_session(
    settings, encryption, metadata, successful_dependencies
):
    factory = FakeSessionFactory()
    cache = object()

    result = await orchestration.complete_oidc_login(
        settings=settings,
        metadata=metadata,
        encryption=encryption,
        session_factory=factory,
        jwks_cache=cache,
        state="state-value",
        browser_binding="browser-binding-value",
        authorization_code="authorization-code",
        now=NOW,
    )

    calls = successful_dependencies["calls"]

    assert result.user_id == successful_dependencies["user_id"]
    assert result.email == "canonical-db@example.com"
    assert result.session_id == successful_dependencies["session_id"]
    assert result.raw_session_token == successful_dependencies["raw_token"]
    assert result.expires_at == successful_dependencies["expires_at"]

    assert len(factory.sessions) == 3
    assert calls["consume_session"] is factory.sessions[0]
    assert calls["ensure_account_session"] is factory.sessions[1]
    assert calls["create_session"] is factory.sessions[2]
    assert calls["consume_session"] is not calls["ensure_account_session"]
    assert calls["ensure_account_session"] is not calls["create_session"]
    assert calls["ensure_account_user_id"] == result.user_id

    assert calls["consume_kwargs"]["state"] == "state-value"
    assert calls["consume_kwargs"]["browser_binding"] == "browser-binding-value"
    assert calls["consume_kwargs"]["encryption"] is encryption
    assert calls["consume_kwargs"]["now"] == NOW

    assert calls["exchange_args"] == (
        settings,
        metadata,
        "authorization-code",
        successful_dependencies["consumed"].code_verifier,
    )

    assert calls["verify_args"] == (
        "signed-id-token",
        settings,
        cache,
        successful_dependencies["consumed"].nonce_hash,
    )
    assert calls["verify_kwargs"]["now_epoch"] == NOW.timestamp()

    assert calls["provision_factory"] is factory
    assert calls["provision_identity"] is not None
    assert calls["verify_args"][0] == "signed-id-token"

    assert calls["create_session_kwargs"]["user_id"] == result.user_id
    assert calls["create_session_kwargs"]["expires_at"] == NOW + timedelta(hours=12)
    assert calls["create_session_kwargs"]["now"] == NOW


@pytest.mark.asyncio
async def test_complete_login_maps_transaction_failure_to_generic_error(
    monkeypatch, settings, encryption, metadata
):
    async def fail(*args, **kwargs):
        raise RuntimeError("sensitive transaction details")

    monkeypatch.setattr(
        orchestration, "consume_oidc_auth_transaction", fail
    )

    with pytest.raises(orchestration.OIDCLoginError) as exc:
        await orchestration.complete_oidc_login(
            settings=settings,
            metadata=metadata,
            encryption=encryption,
            session_factory=FakeSessionFactory(),
            jwks_cache=object(),
            state="state",
            browser_binding="binding",
            authorization_code="code",
            now=NOW,
        )

    assert str(exc.value) == "Authentication could not be completed."
    assert "sensitive" not in str(exc.value)


@pytest.mark.asyncio
async def test_complete_login_maps_unverified_identity_failure_to_generic_error(
    monkeypatch, settings, encryption, metadata
):
    async def consume(*args, **kwargs):
        return SimpleNamespace(code_verifier="v" * 64, nonce_hash="a" * 64)

    async def exchange(*args, **kwargs):
        return "id-token"

    async def verify(*args, **kwargs):
        return SimpleNamespace(
            issuer="https://issuer.example/",
            subject="subject",
            email=None,
            email_verified=False,
            name=None,
        )

    async def provision(*args, **kwargs):
        raise orchestration.IdentityProvisioningError(
            "private identity provisioning detail"
        )

    monkeypatch.setattr(orchestration, "consume_oidc_auth_transaction", consume)
    monkeypatch.setattr(orchestration, "exchange_authorization_code", exchange)
    monkeypatch.setattr(orchestration, "verify_id_token", verify)
    monkeypatch.setattr(orchestration, "resolve_or_provision_user", provision)

    with pytest.raises(orchestration.OIDCLoginError) as exc:
        await orchestration.complete_oidc_login(
            settings=settings,
            metadata=metadata,
            encryption=encryption,
            session_factory=FakeSessionFactory(),
            jwks_cache=object(),
            state="state",
            browser_binding="binding",
            authorization_code="code",
            now=NOW,
        )

    assert str(exc.value) == "Authentication could not be completed."
    assert "private" not in str(exc.value)


@pytest.mark.asyncio
async def test_complete_login_rejects_naive_datetime_before_database_work(
    settings, encryption, metadata
):
    factory = FakeSessionFactory()

    with pytest.raises(ValueError, match="timezone-aware"):
        await orchestration.complete_oidc_login(
            settings=settings,
            metadata=metadata,
            encryption=encryption,
            session_factory=factory,
            jwks_cache=object(),
            state="state",
            browser_binding="binding",
            authorization_code="code",
            now=datetime(2026, 11, 11, 12, 0),
        )

    assert factory.sessions == []


@pytest.mark.asyncio
async def test_complete_login_does_not_create_session_if_code_exchange_fails(
    monkeypatch, settings, encryption, metadata
):
    async def consume(session, **kwargs):
        return SimpleNamespace(code_verifier="v" * 64, nonce_hash="a" * 64)

    async def exchange(*args, **kwargs):
        raise orchestration.OIDCAuthenticationError("provider detail")

    monkeypatch.setattr(orchestration, "consume_oidc_auth_transaction", consume)
    monkeypatch.setattr(orchestration, "exchange_authorization_code", exchange)

    with pytest.raises(orchestration.OIDCLoginError) as exc:
        await orchestration.complete_oidc_login(
            settings=settings,
            metadata=metadata,
            encryption=encryption,
            session_factory=FakeSessionFactory(),
            jwks_cache=object(),
            state="state",
            browser_binding="binding",
            authorization_code="code",
            now=NOW,
        )

    assert str(exc.value) == "Authentication could not be completed."

@pytest.mark.asyncio
async def test_complete_login_does_not_create_session_if_account_provisioning_fails(
    monkeypatch, settings, encryption, metadata, successful_dependencies
):
    factory = FakeSessionFactory()
    calls = successful_dependencies["calls"]

    async def fail_account_provisioning(session, *, owner_user_id):
        calls["ensure_account_session"] = session
        calls["ensure_account_user_id"] = owner_user_id
        raise RuntimeError("private account provisioning detail")

    monkeypatch.setattr(
        orchestration,
        "ensure_default_account_with_initial_portfolios",
        fail_account_provisioning,
    )

    with pytest.raises(orchestration.OIDCLoginError) as exc:
        await orchestration.complete_oidc_login(
            settings=settings,
            metadata=metadata,
            encryption=encryption,
            session_factory=factory,
            jwks_cache=object(),
            state="state-value",
            browser_binding="browser-binding-value",
            authorization_code="authorization-code",
            now=NOW,
        )

    assert str(exc.value) == "Authentication could not be completed."
    assert "private" not in str(exc.value)
    assert "create_session" not in calls
    assert len(factory.sessions) == 2
    assert calls["ensure_account_session"] is factory.sessions[1]
    assert calls["ensure_account_user_id"] == successful_dependencies["user_id"]