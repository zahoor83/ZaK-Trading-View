from __future__ import annotations

import httpx
import app.security.oidc_configuration as oidc_module
import pytest

from app.security.oidc_configuration import (
    Auth0Settings,
    OIDCConfigurationError,
    fetch_provider_jwks,
    fetch_provider_metadata,
    validate_jwks,
    validate_provider_metadata,
)


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
def metadata(settings):
    return {
        "issuer": settings.issuer,
        "authorization_endpoint": f"https://{settings.domain}/authorize",
        "token_endpoint": f"https://{settings.domain}/oauth/token",
        "jwks_uri": f"https://{settings.domain}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "id_token_signing_alg_values_supported": ["HS256", "RS256"],
    }


def test_settings_build_expected_security_policy(settings):
    validated = settings.validated()

    assert validated.issuer == (
        "https://dev-wxmskulgynvly88l.us.auth0.com/"
    )
    assert validated.allowed_signing_algorithms == ("RS256",)
    assert validated.token_endpoint_auth_method == "client_secret_post"
    assert validated.scopes == ("openid", "email", "profile")


def test_environment_configuration_requires_all_values(monkeypatch):
    for name in (
        "AUTH0_DOMAIN",
        "AUTH0_CLIENT_ID",
        "AUTH0_CLIENT_SECRET",
        "AUTH0_CALLBACK_URL",
        "APP_PUBLIC_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(OIDCConfigurationError):
        Auth0Settings.from_environment()


def test_environment_configuration_loads_values():
    values = {
        "AUTH0_DOMAIN": "dev-wxmskulgynvly88l.us.auth0.com",
        "AUTH0_CLIENT_ID": "client-id",
        "AUTH0_CLIENT_SECRET": "secret",
        "AUTH0_CALLBACK_URL": "https://app.example.com/api/v1/auth/callback",
        "APP_PUBLIC_URL": "https://app.example.com",
    }

    settings = Auth0Settings.from_environment(values)

    assert settings.client_id == "client-id"
    assert settings.domain.endswith(".auth0.com")


@pytest.mark.parametrize(
    "domain",
    [
        "https://example.auth0.com",
        "example.auth0.com/path",
        "EXAMPLE.auth0.com",
        "example.auth0.com:443",
        "not a domain",
    ],
)
def test_invalid_auth0_domain_rejected(settings, domain):
    with pytest.raises(OIDCConfigurationError):
        Auth0Settings(
            domain=domain,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            callback_url=settings.callback_url,
            app_public_url=settings.app_public_url,
        ).validated()


@pytest.mark.parametrize(
    "callback",
    [
        "http://app.example.com/api/v1/auth/callback",
        "https://evil.example/api/v1/auth/callback",
        "https://user@app.example.com/api/v1/auth/callback",
        "https://app.example.com/",
        "https://app.example.com/callback?next=evil",
    ],
)
def test_unsafe_callback_url_rejected(settings, callback):
    with pytest.raises(OIDCConfigurationError):
        Auth0Settings(
            domain=settings.domain,
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            callback_url=callback,
            app_public_url=settings.app_public_url,
        ).validated()


def test_provider_metadata_accepts_rs256_and_pkce(settings, metadata):
    result = validate_provider_metadata(settings, metadata)

    assert result["issuer"] == settings.issuer


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "https://evil.example/"),
        ("authorization_endpoint", "http://auth0.example/authorize"),
        ("token_endpoint", "https://evil.example/token"),
        ("jwks_uri", "https://evil.example/jwks"),
        ("response_types_supported", ["token"]),
        ("code_challenge_methods_supported", ["plain"]),
        ("token_endpoint_auth_methods_supported", ["none"]),
        ("id_token_signing_alg_values_supported", ["HS256"]),
    ],
)
def test_provider_metadata_rejects_unsafe_capability(
    settings,
    metadata,
    field,
    value,
):
    metadata[field] = value

    with pytest.raises(OIDCConfigurationError):
        validate_provider_metadata(settings, metadata)




@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("authorization_endpoint", "authorize"),
        ("token_endpoint", "oauth/token"),
        ("jwks_uri", ".well-known/jwks.json"),
    ],
)
def test_provider_metadata_rejects_nonstandard_endpoint_port(
    settings,
    metadata,
    field,
    path,
):
    metadata[field] = f"https://{settings.domain}:8443/{path}"

    with pytest.raises(OIDCConfigurationError):
        validate_provider_metadata(settings, metadata)


@pytest.mark.asyncio
async def test_fetch_provider_metadata_uses_discovery_endpoint(monkeypatch, 
    settings,
    metadata,
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    ) as client:
        monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
        result = await fetch_provider_metadata(settings)

    assert result["issuer"] == settings.issuer
    assert len(requests) == 1
    assert requests[0].url.path == "/.well-known/openid-configuration"


@pytest.mark.asyncio
async def test_fetch_provider_metadata_rejects_http_error(monkeypatch, settings):
    def handler(request):
        return httpx.Response(503)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OIDCConfigurationError):
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await fetch_provider_metadata(settings)




@pytest.mark.asyncio
async def test_fetch_provider_metadata_rejects_oversized_response(monkeypatch, settings):
    oversized = b"x" * (64 * 1024 + 1)

    def handler(request):
        return httpx.Response(200, content=oversized)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OIDCConfigurationError):
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await fetch_provider_metadata(settings)


@pytest.mark.asyncio
async def test_fetch_provider_jwks_rejects_oversized_response(monkeypatch, 
    settings,
    metadata,
):
    oversized = b"x" * (256 * 1024 + 1)

    def handler(request):
        return httpx.Response(200, content=oversized)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OIDCConfigurationError):
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await fetch_provider_jwks(
                settings,
                metadata,
            )


def test_jwks_accepts_only_rsa_rs256_signing_key():
    data = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "key-1",
                "use": "sig",
                "alg": "RS256",
                "n": "test-modulus",
                "e": "AQAB",
            },
            {
                "kty": "RSA",
                "kid": "encryption-key",
                "use": "enc",
                "alg": "RSA-OAEP",
            },
            {
                "kty": "EC",
                "kid": "ec-key",
                "use": "sig",
            },
        ]
    }

    result = validate_jwks(data)

    assert len(result["keys"]) == 1
    assert result["keys"][0]["kid"] == "key-1"


def test_jwks_rejects_duplicate_key_ids():
    key = {
        "kty": "RSA",
        "kid": "duplicate",
        "use": "sig",
        "alg": "RS256",
    }

    with pytest.raises(OIDCConfigurationError):
        validate_jwks({"keys": [key, dict(key)]})


def test_jwks_rejects_empty_acceptable_key_set():
    with pytest.raises(OIDCConfigurationError):
        validate_jwks({"keys": [{"kty": "EC", "kid": "ec-key"}]})


@pytest.mark.asyncio
async def test_fetch_provider_jwks_uses_validated_jwks_uri(monkeypatch, 
    settings,
    metadata,
):
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "auth0-key",
                "use": "sig",
                "alg": "RS256",
                "n": "test-modulus",
                "e": "AQAB",
            }
        ]
    }

    def handler(request):
        assert request.url.path == "/.well-known/jwks.json"
        return httpx.Response(200, json=jwks)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    ) as client:
        monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
        result = await fetch_provider_jwks(
            settings,
            metadata,
        )

    assert result["keys"][0]["kid"] == "auth0-key"


@pytest.mark.asyncio
async def test_fetch_provider_jwks_rejects_untrusted_metadata(
    settings,
    metadata,
):
    metadata["jwks_uri"] = "https://evil.example/jwks"

    async with httpx.AsyncClient() as client:
        with pytest.raises(OIDCConfigurationError):
            await fetch_provider_jwks(
                settings,
                metadata,
            )