from __future__ import annotations

import pytest
from starlette.requests import Request

from app.security.internal_service_auth import InternalServicePrincipal
from app.security.local_shared_secret_auth import (
    HEADER_NAME,
    LOCAL_SERVICE_ID,
    LocalSharedSecretAuthenticator,
    create_local_internal_authentication,
)


SECRET = "local-development-test-secret-32-characters-minimum"


def make_request(headers: list[tuple[bytes, bytes]]) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/internal/v1/auth/oidc/transactions",
        "raw_path": b"/internal/v1/auth/oidc/transactions",
        "query_string": b"",
        "headers": headers,
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_valid_secret_returns_fixed_bff_identity() -> None:
    adapter = LocalSharedSecretAuthenticator(SECRET)
    request = make_request([(HEADER_NAME.lower().encode(), SECRET.encode())])

    principal = await adapter.authenticate(request)

    assert principal == InternalServicePrincipal(service_id=LOCAL_SERVICE_ID)


@pytest.mark.asyncio
async def test_missing_secret_is_rejected() -> None:
    adapter = LocalSharedSecretAuthenticator(SECRET)
    assert await adapter.authenticate(make_request([])) is None


@pytest.mark.asyncio
async def test_invalid_secret_is_rejected() -> None:
    adapter = LocalSharedSecretAuthenticator(SECRET)
    request = make_request([(HEADER_NAME.lower().encode(), b"wrong-secret")])
    assert await adapter.authenticate(request) is None


@pytest.mark.asyncio
async def test_empty_secret_header_is_rejected() -> None:
    adapter = LocalSharedSecretAuthenticator(SECRET)
    request = make_request([(HEADER_NAME.lower().encode(), b"")])
    assert await adapter.authenticate(request) is None


def test_short_adapter_secret_is_rejected() -> None:
    with pytest.raises(ValueError):
        LocalSharedSecretAuthenticator("too-short")


def test_mode_not_enabled_fails_closed() -> None:
    adapter, allowed = create_local_internal_authentication(
        {"APP_ENV": "development"}
    )
    assert adapter is None
    assert allowed == frozenset()


def test_explicit_local_mode_creates_only_bff_identity() -> None:
    adapter, allowed = create_local_internal_authentication(
        {
            "APP_ENV": "development",
            "OIDC_INTERNAL_AUTH_MODE": "local_shared_secret",
            "OIDC_INTERNAL_SERVICE_SECRET": SECRET,
        }
    )
    assert isinstance(adapter, LocalSharedSecretAuthenticator)
    assert allowed == frozenset({"web-bff"})


def test_local_mode_rejected_outside_development() -> None:
    with pytest.raises(RuntimeError):
        create_local_internal_authentication(
            {
                "APP_ENV": "production",
                "OIDC_INTERNAL_AUTH_MODE": "local_shared_secret",
                "OIDC_INTERNAL_SERVICE_SECRET": SECRET,
            }
        )


def test_local_mode_rejected_when_app_environment_missing() -> None:
    with pytest.raises(RuntimeError):
        create_local_internal_authentication(
            {
                "OIDC_INTERNAL_AUTH_MODE": "local_shared_secret",
                "OIDC_INTERNAL_SERVICE_SECRET": SECRET,
            }
        )


def test_local_mode_rejects_missing_secret() -> None:
    with pytest.raises(RuntimeError):
        create_local_internal_authentication(
            {
                "APP_ENV": "development",
                "OIDC_INTERNAL_AUTH_MODE": "local_shared_secret",
            }
        )


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(RuntimeError):
        create_local_internal_authentication(
            {
                "APP_ENV": "development",
                "OIDC_INTERNAL_AUTH_MODE": "unknown",
                "OIDC_INTERNAL_SERVICE_SECRET": SECRET,
            }
        )