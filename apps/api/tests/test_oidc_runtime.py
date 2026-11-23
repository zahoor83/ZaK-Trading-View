from __future__ import annotations

import asyncio
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import app.security.oidc_runtime as runtime_module
from app.security.oidc_runtime import (
    OIDCRuntimeUnavailableError,
    create_oidc_runtime,
)


VALID_ENV = {
    "AUTH0_DOMAIN": "dev-wxmskulgynvly88l.us.auth0.com",
    "AUTH0_CLIENT_ID": "test-client-id",
    "AUTH0_CLIENT_SECRET": "test-client-secret",
    "AUTH0_CALLBACK_URL": "https://app.example.com/api/v1/auth/callback",
    "APP_PUBLIC_URL": "https://app.example.com",
    "OIDC_TRANSACTION_ENCRYPTION_KEYS": (
        '{"local-dev":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}'
    ),
    "OIDC_TRANSACTION_ACTIVE_KEY_ID": "local-dev",
}


class FakeDatabaseManager:
    is_initialized = True

    def session_factory(self):
        def fake_session_factory():
            return object()

        return fake_session_factory


def make_jwks():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    numbers = private_key.public_key().public_numbers()

    def encode(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "runtime-test-key",
                "use": "sig",
                "alg": "RS256",
                "n": encode(numbers.n),
                "e": encode(numbers.e),
            }
        ]
    }


def test_missing_oidc_environment_disables_runtime():
    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ={},
    )

    assert runtime.available is False

    with pytest.raises(OIDCRuntimeUnavailableError):
        _ = runtime.settings


def test_invalid_encryption_configuration_disables_runtime():
    values = dict(VALID_ENV)
    values["OIDC_TRANSACTION_ENCRYPTION_KEYS"] = "not-json"

    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ=values,
    )

    assert runtime.available is False


def test_missing_database_disables_runtime():
    class MissingDatabase:
        is_initialized = False

        def session_factory(self):
            raise AssertionError("Must not request a missing session factory.")

    runtime = create_oidc_runtime(
        MissingDatabase(),
        environ=VALID_ENV,
    )

    assert runtime.available is False


@pytest.mark.asyncio
async def test_runtime_creation_performs_no_provider_network_requests(
    monkeypatch,
):
    calls = {"metadata": 0, "jwks": 0}

    async def metadata_fetch(settings):
        calls["metadata"] += 1
        return {}

    async def jwks_fetch(settings, metadata):
        calls["jwks"] += 1
        return {"keys": []}

    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_metadata",
        metadata_fetch,
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_jwks",
        jwks_fetch,
    )

    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ=VALID_ENV,
    )

    assert runtime.available is True
    assert calls == {"metadata": 0, "jwks": 0}


@pytest.mark.asyncio
async def test_first_metadata_request_is_lazy(monkeypatch):
    calls = {"count": 0}
    expected = {"issuer": "https://example.auth0.com/"}

    async def metadata_fetch(settings):
        calls["count"] += 1
        return expected

    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_metadata",
        metadata_fetch,
    )

    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ=VALID_ENV,
    )

    assert calls["count"] == 0

    result = await runtime.get_metadata()

    assert result == expected
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_concurrent_metadata_initialization_fetches_once(monkeypatch):
    calls = {"count": 0}
    started = asyncio.Event()
    release = asyncio.Event()
    expected = {"issuer": "https://example.auth0.com/"}

    async def metadata_fetch(settings):
        calls["count"] += 1
        started.set()
        await release.wait()
        return expected

    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_metadata",
        metadata_fetch,
    )

    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ=VALID_ENV,
    )

    tasks = [
        asyncio.create_task(runtime.get_metadata())
        for _ in range(8)
    ]

    await started.wait()
    release.set()

    results = await asyncio.gather(*tasks)

    assert calls["count"] == 1
    assert all(result == expected for result in results)


@pytest.mark.asyncio
async def test_jwks_cache_uses_lazy_metadata_and_provider_jwks(
    monkeypatch,
):
    calls = {"metadata": 0, "jwks": 0}
    jwks = make_jwks()

    async def metadata_fetch(settings):
        calls["metadata"] += 1
        return {
            "issuer": settings.issuer,
            "jwks_uri": f"https://{settings.domain}/.well-known/jwks.json",
            "authorization_endpoint": f"https://{settings.domain}/authorize",
            "token_endpoint": f"https://{settings.domain}/oauth/token",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    async def jwks_fetch(settings, metadata):
        calls["jwks"] += 1
        return jwks

    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_metadata",
        metadata_fetch,
    )
    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_jwks",
        jwks_fetch,
    )

    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ=VALID_ENV,
    )

    assert calls == {"metadata": 0, "jwks": 0}

    key = await runtime.jwks_cache.get_key("runtime-test-key")

    assert key is not None
    assert calls == {"metadata": 1, "jwks": 1}


@pytest.mark.asyncio
async def test_metadata_failure_does_not_expose_provider_error_or_secret(
    monkeypatch,
):
    secret = "sensitive-provider-error-value"

    async def metadata_fetch(settings):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        runtime_module,
        "fetch_provider_metadata",
        metadata_fetch,
    )

    runtime = create_oidc_runtime(
        FakeDatabaseManager(),
        environ=VALID_ENV,
    )

    with pytest.raises(OIDCRuntimeUnavailableError) as captured:
        await runtime.get_metadata()

    assert secret not in str(captured.value)
    assert secret not in repr(runtime)
    assert "OIDCRuntime(available=True)" == repr(runtime)