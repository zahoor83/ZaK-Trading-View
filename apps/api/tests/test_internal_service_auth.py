from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.security.internal_service_auth import (
    InternalServicePrincipal,
    get_internal_service_principal,
)


def make_request() -> Request:
    app = FastAPI()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/internal/test",
        "raw_path": b"/internal/test",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
        "app": app,
    }
    return Request(scope)


class FakeAuthenticator:
    def __init__(self, principal):
        self.principal = principal

    async def authenticate(self, request):
        return self.principal


def allow(request, *service_ids):
    request.app.state.internal_service_allowed_ids = frozenset(service_ids)


@pytest.mark.asyncio
async def test_missing_authenticator_fails_closed():
    request = make_request()
    allow(request, "test-bff")

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 503
    assert captured.value.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_missing_allowlist_fails_closed():
    request = make_request()
    request.app.state.internal_service_authenticator = FakeAuthenticator(
        InternalServicePrincipal("test-bff")
    )

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [None, frozenset(), {"test-bff"}])
async def test_missing_empty_or_mutable_allowlist_fails_closed(allowed):
    request = make_request()
    request.app.state.internal_service_authenticator = FakeAuthenticator(
        InternalServicePrincipal("test-bff")
    )
    request.app.state.internal_service_allowed_ids = allowed

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 503


@pytest.mark.asyncio
async def test_rejected_identity_returns_401():
    request = make_request()
    allow(request, "test-bff")
    request.app.state.internal_service_authenticator = FakeAuthenticator(None)

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 401


@pytest.mark.asyncio
async def test_non_allowlisted_service_returns_403():
    request = make_request()
    allow(request, "test-bff")
    request.app.state.internal_service_authenticator = FakeAuthenticator(
        InternalServicePrincipal("other-service")
    )

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 403


@pytest.mark.asyncio
async def test_allowlisted_server_identity_is_accepted():
    request = make_request()
    allow(request, "test-bff")
    request.app.state.internal_service_authenticator = FakeAuthenticator(
        InternalServicePrincipal("test-bff")
    )

    principal = await get_internal_service_principal(request)

    assert principal.service_id == "test-bff"


@pytest.mark.asyncio
async def test_authenticator_exception_fails_closed_without_leak():
    request = make_request()
    allow(request, "test-bff")

    class BrokenAuthenticator:
        async def authenticate(self, request):
            raise RuntimeError("sensitive adapter detail")

    request.app.state.internal_service_authenticator = BrokenAuthenticator()

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 503
    assert "sensitive adapter detail" not in str(captured.value.detail)


@pytest.mark.asyncio
async def test_invalid_authenticator_return_type_is_rejected():
    request = make_request()
    allow(request, "test-bff")
    request.app.state.internal_service_authenticator = FakeAuthenticator(True)

    with pytest.raises(HTTPException) as captured:
        await get_internal_service_principal(request)

    assert captured.value.status_code == 401


def test_principal_rejects_invalid_service_id():
    with pytest.raises(ValueError):
        InternalServicePrincipal("")

    with pytest.raises(ValueError):
        InternalServicePrincipal("x" * 129)