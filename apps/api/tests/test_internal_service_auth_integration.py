from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

import app.api.v1.internal_auth_callbacks as callback_module
from app.api.v1.internal_auth_callbacks import router
from app.security.local_shared_secret_auth import (
    LOCAL_SERVICE_ID,
    LocalSharedSecretAuthenticator,
)


TEST_SECRET = "integration-test-only-internal-service-secret"


class FakeRuntime:
    available = True
    settings = object()
    encryption = object()
    jwks_cache = object()

    async def get_metadata(self):
        return {"issuer": "https://issuer.example/"}

    def session_factory(self):
        raise AssertionError("Not used by the mocked callback service.")


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.internal_service_authenticator = (
        LocalSharedSecretAuthenticator(TEST_SECRET)
    )
    app.state.internal_service_allowed_ids = frozenset({LOCAL_SERVICE_ID})
    app.state.oidc_runtime = FakeRuntime()
    return app


@pytest.mark.asyncio
async def test_actual_fastapi_callback_requires_local_bff_secret(monkeypatch):
    user_id = uuid4()

    async def fake_complete(**kwargs):
        assert kwargs["state"] == "A" * 43
        assert kwargs["browser_binding"] == "B" * 43
        assert kwargs["authorization_code"] == "mock-code"

        return SimpleNamespace(
            user_id=user_id,
            email="verified@example.test",
            raw_session_token="S" * 40,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )

    monkeypatch.setattr(
        callback_module,
        "complete_oidc_login",
        fake_complete,
    )

    app = make_app()
    transport = httpx.ASGITransport(app=app)

    payload = {
        "state": "A" * 43,
        "authorization_code": "mock-code",
        "browser_binding": "B" * 43,
    }

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        missing = await client.post(
            "/internal/v1/auth/oidc/callback",
            json=payload,
        )
        wrong = await client.post(
            "/internal/v1/auth/oidc/callback",
            json=payload,
            headers={"X-Zak-Internal-Service-Secret": "wrong-secret"},
        )
        accepted = await client.post(
            "/internal/v1/auth/oidc/callback",
            json=payload,
            headers={"X-Zak-Internal-Service-Secret": TEST_SECRET},
        )

    assert missing.status_code == 401
    assert missing.headers["cache-control"] == "no-store"

    assert wrong.status_code == 401
    assert wrong.headers["cache-control"] == "no-store"

    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["user"]["id"] == str(user_id)
    assert accepted.json()["session"]["token"] == "S" * 40