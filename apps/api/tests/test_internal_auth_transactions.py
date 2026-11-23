from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import internal_auth
from app.api.v1.internal_auth import router
from app.database.session import get_db_session
from app.security.internal_service_auth import InternalServicePrincipal
from app.services.oidc_idempotency_service import (
    OIDCIdempotencyConflictError,
    OIDCIdempotencyPersistenceError,
    OIDCIdempotentTransactionResult,
)


REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"
AUTHORIZATION_URL = (
    "https://dev-wxmskulgynvly88l.us.auth0.com/authorize?test=1"
)


class AcceptingServiceAdapter:
    async def authenticate(self, request):
        return InternalServicePrincipal(service_id="test-bff")


class RejectingServiceAdapter:
    async def authenticate(self, request):
        return None


class WrongServiceAdapter:
    async def authenticate(self, request):
        return InternalServicePrincipal(service_id="unlisted-service")


class FakeSession:
    pass


class FakeRuntime:
    def __init__(self):
        self.settings = object()
        self.encryption = object()
        self.metadata_calls = 0
        self.fail_metadata = False
        self.fail_session = False
        self.session = FakeSession()
        self.session_entries = 0
        self.session_exits = 0

    async def get_metadata(self):
        self.metadata_calls += 1
        if self.fail_metadata:
            raise RuntimeError("private metadata failure")
        return {"authorization_endpoint": "https://issuer.example/authorize"}

    @asynccontextmanager
    async def _session_context(self):
        if self.fail_session:
            raise RuntimeError("private database session failure")
        self.session_entries += 1
        try:
            yield self.session
        finally:
            self.session_exits += 1

    def session_factory(self):
        return self._session_context()


def make_response_payload():
    return {
        "authorization_url": AUTHORIZATION_URL,
        "state": "state_test_value_123456789012345678901234",
        "browser_cookie_name": "__Host-zak_oidc_testbinding",
        "browser_cookie_value": "opaque_binding_value",
        "max_age": 300,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(),
    }


def make_app(*, authenticator="accept", runtime=True):
    app = FastAPI()
    app.include_router(router)

    if authenticator == "accept":
        app.state.internal_service_authenticator = AcceptingServiceAdapter()
    elif authenticator == "reject":
        app.state.internal_service_authenticator = RejectingServiceAdapter()
    elif authenticator == "wrong":
        app.state.internal_service_authenticator = WrongServiceAdapter()

    app.state.internal_service_allowed_ids = frozenset({"test-bff"})

    if runtime:
        app.state.oidc_runtime = FakeRuntime()

    return app


async def post_transaction(app, request_id=REQUEST_ID):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.post(
            "/internal/v1/auth/oidc/transactions",
            json={"request_id": request_id},
        )


def install_fake_idempotency(monkeypatch, *, replayed=False):
    calls = {}
    stored = make_response_payload()

    def fake_build(settings, metadata, transaction):
        calls["builder_settings"] = settings
        calls["builder_metadata"] = metadata
        calls["builder_transaction"] = transaction
        return AUTHORIZATION_URL

    monkeypatch.setattr(
        internal_auth,
        "build_authorization_url",
        fake_build,
    )

    async def fake_create(
        session,
        *,
        service_id,
        request_id,
        encryption,
        authorization_url_builder,
    ):
        calls["session"] = session
        calls["service_id"] = service_id
        calls["request_id"] = request_id
        calls["encryption"] = encryption
        calls["builder_result"] = authorization_url_builder(
            SimpleNamespace(
                state=stored["state"],
                nonce="test_nonce",
                code_challenge="test_challenge",
            )
        )
        return OIDCIdempotentTransactionResult(
            response=stored,
            replayed=replayed,
        )

    monkeypatch.setattr(
        internal_auth,
        "create_or_replay_oidc_transaction",
        fake_create,
    )
    return calls, stored


@pytest.mark.asyncio
async def test_transaction_creation_returns_bff_payload_and_no_browser_cookie(
    monkeypatch,
):
    calls, stored = install_fake_idempotency(monkeypatch)
    app = make_app()

    response = await post_transaction(app)

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    body = response.json()
    assert body["authorization_url"] == stored["authorization_url"]
    assert body["state"] == stored["state"]
    assert body["browser_binding_cookie"] == {
        "name": stored["browser_cookie_name"],
        "value": stored["browser_cookie_value"],
        "max_age_seconds": 300,
    }
    assert datetime.fromisoformat(body["expires_at"]) == (
        datetime.fromisoformat(stored["expires_at"])
    )
    assert "request_id" not in body
    assert calls["service_id"] == "test-bff"
    assert calls["request_id"] == REQUEST_ID
    assert calls["session"] is app.state.oidc_runtime.session
    assert calls["encryption"] is app.state.oidc_runtime.encryption
    assert calls["builder_result"] == AUTHORIZATION_URL
    assert app.state.oidc_runtime.session_entries == 1
    assert app.state.oidc_runtime.session_exits == 1


@pytest.mark.asyncio
async def test_replay_returns_same_201_contract_without_browser_cookie(
    monkeypatch,
):
    install_fake_idempotency(monkeypatch, replayed=True)
    app = make_app()

    response = await post_transaction(app)

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert response.json()["browser_binding_cookie"]["value"] == (
        "opaque_binding_value"
    )


@pytest.mark.asyncio
async def test_missing_internal_authenticator_fails_closed():
    response = await post_transaction(make_app(authenticator=None))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_rejected_internal_service_is_unauthorized():
    response = await post_transaction(make_app(authenticator="reject"))
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_unlisted_internal_service_is_forbidden():
    response = await post_transaction(make_app(authenticator="wrong"))
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_missing_runtime_returns_generic_unavailable():
    response = await post_transaction(make_app(runtime=False))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_metadata_failure_does_not_call_idempotency_service(monkeypatch):
    called = False

    async def forbidden_create(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("idempotency service must not run")

    monkeypatch.setattr(
        internal_auth,
        "create_or_replay_oidc_transaction",
        forbidden_create,
    )
    app = make_app()
    app.state.oidc_runtime.fail_metadata = True

    response = await post_transaction(app)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert called is False
    assert app.state.oidc_runtime.session_entries == 0


@pytest.mark.asyncio
async def test_idempotency_conflict_returns_409_no_store(monkeypatch):
    async def conflict(*args, **kwargs):
        raise OIDCIdempotencyConflictError("private conflict detail")

    monkeypatch.setattr(
        internal_auth, "create_or_replay_oidc_transaction", conflict
    )
    response = await post_transaction(make_app())

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert "private conflict detail" not in response.text


@pytest.mark.asyncio
async def test_persistence_failure_returns_generic_503(monkeypatch):
    async def fail(*args, **kwargs):
        raise OIDCIdempotencyPersistenceError("private persistence detail")

    monkeypatch.setattr(
        internal_auth, "create_or_replay_oidc_transaction", fail
    )
    response = await post_transaction(make_app())

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "private persistence detail" not in response.text


@pytest.mark.asyncio
async def test_unexpected_failure_returns_generic_503(monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("private unexpected detail")

    monkeypatch.setattr(
        internal_auth, "create_or_replay_oidc_transaction", fail
    )
    response = await post_transaction(make_app())

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "private unexpected detail" not in response.text


@pytest.mark.asyncio
async def test_session_factory_failure_returns_generic_503(monkeypatch):
    async def forbidden_create(*args, **kwargs):
        pytest.fail("service must not run without a session")

    monkeypatch.setattr(
        internal_auth, "create_or_replay_oidc_transaction", forbidden_create
    )
    app = make_app()
    app.state.oidc_runtime.fail_session = True

    response = await post_transaction(app)

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_application_registers_internal_auth_router():
    from app.asgi import create_app

    app = create_app()
    paths = app.openapi()["paths"]
    assert "/internal/v1/auth/oidc/transactions" in paths


class FakeSessionDatabaseManager:
    def __init__(self):
        self.is_initialized = True
        self.entries = 0
        self.sessions = []

    @asynccontextmanager
    async def _session_context(self):
        session = object()
        self.entries += 1
        self.sessions.append(session)
        yield session

    def session_factory(self):
        return self._session_context


async def post_session_revoke(app, token="test-session-token"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.post(
            "/internal/v1/auth/session/revoke",
            json={"session_token": token},
        )


@pytest.mark.asyncio
async def test_revoke_session_rejects_internal_service_before_database():
    app = make_app(authenticator="reject")

    response = await post_session_revoke(app)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    # Auth dependency rejects before endpoint body sets Pragma.
    assert not hasattr(app.state, "database_manager")


@pytest.mark.asyncio
async def test_revoke_session_uses_separate_lookup_and_revoke_sessions(
    monkeypatch,
):
    owner_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    manager = FakeSessionDatabaseManager()
    calls = {}

    async def fake_load_owner(session, raw_token):
        calls["lookup_session"] = session
        calls["lookup_token"] = raw_token
        return owner_id

    async def fake_revoke(
        session,
        *,
        authenticated_user_id,
        raw_token,
    ):
        calls["revoke_session"] = session
        calls["owner_id"] = authenticated_user_id
        calls["revoke_token"] = raw_token
        return True

    monkeypatch.setattr(internal_auth, "load_session_owner_id", fake_load_owner)
    monkeypatch.setattr(internal_auth, "revoke_user_session", fake_revoke)

    app = make_app(authenticator="accept")
    app.state.database_manager = manager

    response = await post_session_revoke(app, "valid-revoke-token")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json() == {"revoked": True}

    assert manager.entries == 2
    assert calls["lookup_session"] is manager.sessions[0]
    assert calls["revoke_session"] is manager.sessions[1]
    assert calls["lookup_session"] is not calls["revoke_session"]
    assert calls["lookup_token"] == "valid-revoke-token"
    assert calls["revoke_token"] == "valid-revoke-token"
    assert calls["owner_id"] == owner_id


@pytest.mark.asyncio
async def test_revoke_session_unknown_token_returns_false_without_second_session(
    monkeypatch,
):
    manager = FakeSessionDatabaseManager()

    async def fake_load_owner(session, raw_token):
        return None

    async def forbidden_revoke(*args, **kwargs):
        pytest.fail("Revocation service must not run for an unknown token.")

    monkeypatch.setattr(internal_auth, "load_session_owner_id", fake_load_owner)
    monkeypatch.setattr(internal_auth, "revoke_user_session", forbidden_revoke)

    app = make_app(authenticator="accept")
    app.state.database_manager = manager

    response = await post_session_revoke(app, "unknown-revoke-token")

    assert response.status_code == 200
    assert response.json() == {"revoked": False}
    assert manager.entries == 1


async def post_session_validate(app, token="test-session-token"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.post(
            "/internal/v1/auth/session/validate",
            json={"session_token": token},
        )


@pytest.mark.asyncio
async def test_validate_session_rejects_internal_service_before_database():
    # No database_manager is configured. If get_db_session executes first,
    # the response will be 503 instead of the expected authentication 401.
    app = make_app(authenticator="reject")

    response = await post_session_validate(app)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    # Internal auth rejects before the endpoint body sets Pragma.


@pytest.mark.asyncio
async def test_validate_session_returns_verified_identity_after_internal_auth(
    monkeypatch,
):
    from app.database.session import get_db_session

    fake_session = object()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
    calls = {}

    async def fake_db_dependency():
        calls["database_dependency"] = True
        yield fake_session

    async def fake_load_authenticated_user(session, raw_token):
        calls["session"] = session
        calls["token"] = raw_token
        return SimpleNamespace(
            user_id="550e8400-e29b-41d4-a716-446655440000",
            email="verified@example.com",
            expires_at=expires_at,
        )

    app = make_app(authenticator="accept")
    app.dependency_overrides[get_db_session] = fake_db_dependency

    monkeypatch.setattr(
        internal_auth,
        "load_authenticated_user",
        fake_load_authenticated_user,
    )

    response = await post_session_validate(app, "valid-test-session-token")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert calls["database_dependency"] is True
    assert calls["session"] is fake_session
    assert calls["token"] == "valid-test-session-token"

    body = response.json()
    assert body["authenticated"] is True
    assert body["user"] == {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "email": "verified@example.com",
    }
    actual_expiry = datetime.fromisoformat(
        body["session"]["expires_at"].replace("Z", "+00:00")
    )
    assert actual_expiry == expires_at
