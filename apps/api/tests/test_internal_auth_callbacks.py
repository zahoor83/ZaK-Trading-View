from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.internal_auth_callbacks import router
from app.security.internal_service_auth import InternalServicePrincipal
from app.services import oidc_login_orchestration
import app.api.v1.internal_auth_callbacks as callback_module


class AcceptingServiceAdapter:
    async def authenticate(self, request):
        return InternalServicePrincipal(service_id="test-bff")


class RejectingServiceAdapter:
    async def authenticate(self, request):
        return None


class FakeRuntime:
    available = True
    settings = object()
    encryption = object()
    jwks_cache = object()

    async def get_metadata(self):
        return {"issuer": "https://issuer.example/"}

    def session_factory(self):
        raise AssertionError("Session factory should not be needed in this test.")


def make_app(*, authenticator="accept", runtime=True):
    app = FastAPI()
    app.include_router(router)

    if authenticator == "accept":
        app.state.internal_service_authenticator = AcceptingServiceAdapter()
    elif authenticator == "reject":
        app.state.internal_service_authenticator = RejectingServiceAdapter()

    app.state.internal_service_allowed_ids = frozenset({"test-bff"})

    if runtime:
        app.state.oidc_runtime = FakeRuntime()

    return app


async def post_callback(app, payload=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.post(
            "/internal/v1/auth/oidc/callback",
            json=payload or {
                "state": "test-state",
                "authorization_code": "test-authorization-code",
                "browser_binding": "test-browser-binding",
            },
        )


@pytest.mark.asyncio
async def test_callback_returns_internal_session_and_verified_identity(
    monkeypatch,
):
    user_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
    calls = {}

    async def fake_complete(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            user_id=user_id,
            email="canonical-db@example.com",
            session_id=uuid4(),
            raw_session_token="opaque-internal-session-token",
            expires_at=expires_at,
        )


    monkeypatch.setattr(
        callback_module,
        "complete_oidc_login",
        fake_complete,
    )


    app = make_app()
    response = await post_callback(app)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert calls["state"] == "test-state"
    assert calls["browser_binding"] == "test-browser-binding"
    assert calls["authorization_code"] == "test-authorization-code"
    assert response.json()["session"]["token"] == (
        "opaque-internal-session-token"
    )
    assert response.json()["user"] == {
        "id": str(user_id),
        "email": "canonical-db@example.com",
    }


@pytest.mark.asyncio
async def test_missing_internal_authenticator_fails_closed():
    response = await post_callback(make_app(authenticator=None))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_rejected_internal_service_is_unauthorized():
    response = await post_callback(make_app(authenticator="reject"))
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_missing_runtime_returns_generic_unavailable():
    response = await post_callback(make_app(runtime=False))
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_oidc_login_failure_returns_generic_401(monkeypatch):
    async def fail(**kwargs):
        raise oidc_login_orchestration.OIDCLoginError(
            "private provider or transaction details"
        )

    monkeypatch.setattr(
        callback_module,
        "complete_oidc_login",
        fail,
    )

    response = await post_callback(make_app())

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert "private provider" not in response.text


@pytest.mark.asyncio
async def test_unexpected_failure_returns_generic_503(monkeypatch):
    async def fail(**kwargs):
        raise RuntimeError("private database details")

    monkeypatch.setattr(
        callback_module,
        "complete_oidc_login",
        fail,
    )

    response = await post_callback(make_app())

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "private database details" not in response.text


@pytest.mark.asyncio
async def test_callback_rejects_unknown_request_fields():
    response = await post_callback(
        make_app(),
        {
            "state": "test-state",
            "authorization_code": "test-code",
            "browser_binding": "test-binding",
            "unexpected": "value",
        },
    )

    assert response.status_code == 422