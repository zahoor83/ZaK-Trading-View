from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import internal_auth
from app.api.v1.internal_auth import router
from app.database.session import get_db_session
from app.security.local_shared_secret_auth import (
    HEADER_NAME,
    LOCAL_SERVICE_ID,
    LocalSharedSecretAuthenticator,
)


TEST_SECRET = "integration-test-only-secret-32-characters-minimum"
REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.internal_service_authenticator = (
        LocalSharedSecretAuthenticator(TEST_SECRET)
    )
    app.state.internal_service_allowed_ids = frozenset({LOCAL_SERVICE_ID})
    return app


async def post_internal(app, path, payload, secret=None):
    transport = httpx.ASGITransport(app=app)

    headers = {}
    if secret is not None:
        headers[HEADER_NAME] = secret

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.post(
            path,
            json=payload,
            headers=headers,
        )


ROUTES = [
    (
        "/internal/v1/auth/oidc/transactions",
        {"request_id": REQUEST_ID},
    ),
    (
        "/internal/v1/auth/session/validate",
        {"session_token": "integration-session-token"},
    ),
    (
        "/internal/v1/auth/session/revoke",
        {"session_token": "integration-session-token"},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,payload", ROUTES)
@pytest.mark.parametrize("secret", [None, "incorrect-integration-secret"])
async def test_all_internal_routes_reject_missing_or_wrong_real_secret(
    path,
    payload,
    secret,
):
    app = make_app()

    response = await post_internal(
        app,
        path,
        payload,
        secret=secret,
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_transaction_route_accepts_real_local_bff_secret(monkeypatch):
    app = make_app()

    @asynccontextmanager
    async def session_context():
        yield object()

    class Runtime:
        settings = object()
        encryption = object()

        async def get_metadata(self):
            return {"authorization_endpoint": "https://issuer.example/authorize"}

        def session_factory(self):
            return session_context()

    app.state.oidc_runtime = Runtime()

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    stored = {
        "authorization_url": "https://issuer.example/authorize?state=test",
        "state": "integration-state",
        "browser_cookie_name": "__Host-zak_oidc_testbinding",
        "browser_cookie_value": "opaque-integration-binding",
        "max_age": 300,
        "expires_at": expires_at.isoformat(),
    }

    async def fake_create(*args, **kwargs):
        assert kwargs["service_id"] == LOCAL_SERVICE_ID
        return SimpleNamespace(response=stored)

    monkeypatch.setattr(
        internal_auth,
        "create_or_replay_oidc_transaction",
        fake_create,
    )

    response = await post_internal(
        app,
        "/internal/v1/auth/oidc/transactions",
        {"request_id": REQUEST_ID},
        secret=TEST_SECRET,
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] == "integration-state"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_session_validate_accepts_real_local_bff_secret(monkeypatch):
    app = make_app()

    fake_session = object()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
    user_id = uuid4()

    async def fake_db_dependency():
        yield fake_session

    async def fake_load_authenticated_user(session, raw_token):
        assert session is fake_session
        assert raw_token == "integration-session-token"

        return SimpleNamespace(
            user_id=user_id,
            email="integration@example.test",
            expires_at=expires_at,
        )

    app.dependency_overrides[get_db_session] = fake_db_dependency

    monkeypatch.setattr(
        internal_auth,
        "load_authenticated_user",
        fake_load_authenticated_user,
    )

    response = await post_internal(
        app,
        "/internal/v1/auth/session/validate",
        {"session_token": "integration-session-token"},
        secret=TEST_SECRET,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["authenticated"] is True
    assert response.json()["user"]["id"] == str(user_id)


@pytest.mark.asyncio
async def test_session_revoke_accepts_real_local_bff_secret(monkeypatch):
    app = make_app()

    @asynccontextmanager
    async def session_context():
        yield object()

    class FakeManager:
        is_initialized = True

        def session_factory(self):
            return session_context

    app.state.database_manager = FakeManager()

    async def fake_load_owner(session, raw_token):
        assert raw_token == "integration-session-token"
        return None

    monkeypatch.setattr(
        internal_auth,
        "load_session_owner_id",
        fake_load_owner,
    )

    response = await post_internal(
        app,
        "/internal/v1/auth/session/revoke",
        {"session_token": "integration-session-token"},
        secret=TEST_SECRET,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json() == {"revoked": False}


@pytest.mark.asyncio
async def test_real_secret_authentication_rejects_unlisted_service(monkeypatch):
    app = make_app()

    app.state.internal_service_allowed_ids = frozenset({"another-service"})

    response = await post_internal(
        app,
        "/internal/v1/auth/session/revoke",
        {"session_token": "integration-session-token"},
        secret=TEST_SECRET,
    )

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert not hasattr(app.state, "database_manager")


@pytest.mark.asyncio
async def test_asgi_lifespan_installs_development_internal_authenticator(
    monkeypatch,
):
    import app.asgi as asgi

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("OIDC_INTERNAL_AUTH_MODE", "local_shared_secret")
    monkeypatch.setenv("OIDC_INTERNAL_SERVICE_SECRET", TEST_SECRET)

    class FakeDatabaseManager:
        is_initialized = False

        def initialize(self):
            pass

        async def dispose(self):
            pass

    class FakeMarketDataService:
        async def start(self):
            pass

        async def stop(self):
            pass

    manager = FakeDatabaseManager()

    monkeypatch.setattr(
        asgi.DatabaseManager,
        "from_environment",
        classmethod(lambda cls: manager),
    )

    monkeypatch.setattr(
        asgi,
        "create_oidc_runtime",
        lambda database_manager: SimpleNamespace(available=False),
    )

    app = asgi.create_app(
        service_factory=lambda config: FakeMarketDataService(),
        config_factory=lambda: None,
    )

    async with app.router.lifespan_context(app):
        authenticator = app.state.internal_service_authenticator

        assert isinstance(
            authenticator,
            LocalSharedSecretAuthenticator,
        )

        assert (
            app.state.internal_service_allowed_ids
            == frozenset({LOCAL_SERVICE_ID})
        )

    assert manager.is_initialized is False
