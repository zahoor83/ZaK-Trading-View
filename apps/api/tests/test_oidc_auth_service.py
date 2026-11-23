from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import app.services.oidc_auth_service as oidc_module
import pytest

from app.security.oidc_configuration import Auth0Settings
from app.services.oidc_auth_service import (
    IdentityProvisioningError,
    OIDCAuthenticationError,
    build_authorization_url,
    exchange_authorization_code,
    normalize_email,
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


def test_authorization_url_contains_pkce_and_expected_parameters(settings):
    metadata = {
        "authorization_endpoint":
            "https://dev-wxmskulgynvly88l.us.auth0.com/authorize"
    }

    transaction = SimpleNamespace(
        state="s" * 43,
        nonce="n" * 43,
        code_challenge="c" * 43,
    )

    result = build_authorization_url(settings, metadata, transaction)
    parsed = urlparse(result)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.hostname == settings.domain
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [settings.client_id]
    assert query["redirect_uri"] == [settings.callback_url]
    assert query["scope"] == ["openid email profile"]
    assert query["state"] == ["s" * 43]
    assert query["nonce"] == ["n" * 43]
    assert query["code_challenge"] == ["c" * 43]
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" not in query


def test_authorization_url_rejects_untrusted_endpoint(settings):
    metadata = {
        "authorization_endpoint": "https://evil.example/authorize"
    }

    transaction = SimpleNamespace(
        state="state",
        nonce="nonce",
        code_challenge="challenge",
    )

    with pytest.raises(OIDCAuthenticationError):
        build_authorization_url(settings, metadata, transaction)


@pytest.mark.asyncio
async def test_token_exchange_returns_only_id_token(monkeypatch, settings):
    captured = {}

    async def handler(request):
        captured["form"] = dict(
            item.split("=", 1)
            for item in request.content.decode().split("&")
        )
        return httpx.Response(
            200,
            json={
                "id_token": "signed-id-token",
                "access_token": "must-not-be-returned",
                "refresh_token": "must-not-be-returned",
            },
        )

    client = httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    )

    try:
        monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
        result = await exchange_authorization_code(
            settings,
            {
                "token_endpoint":
                    "https://dev-wxmskulgynvly88l.us.auth0.com/oauth/token"
            },
            "authorization-code",
            "v" * 64,
        )
    finally:
        await client.aclose()

    assert result == "signed-id-token"
    assert "authorization-code" in captured["form"]["code"]
    assert "client_secret" in captured["form"]
    assert "code_verifier" in captured["form"]


@pytest.mark.asyncio
async def test_token_exchange_rejects_untrusted_endpoint(settings):
    with pytest.raises(OIDCAuthenticationError):
        await exchange_authorization_code(
            settings,
            {"token_endpoint": "https://evil.example/token"},
            "code",
            "v" * 64,
        )




@pytest.mark.asyncio
async def test_token_exchange_rejects_nonstandard_endpoint_port(settings):
    with pytest.raises(OIDCAuthenticationError):
        await exchange_authorization_code(
            settings,
            {
                "token_endpoint":
                    f"https://{settings.domain}:8443/oauth/token"
            },
            "authorization-code",
            "v" * 64,
        )


@pytest.mark.asyncio
async def test_token_exchange_rejects_missing_id_token(monkeypatch, settings):
    async def handler(request):
        return httpx.Response(200, json={"access_token": "only-access-token"})

    client = httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    )

    try:
        with pytest.raises(OIDCAuthenticationError):
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await exchange_authorization_code(
                settings,
                {
                    "token_endpoint":
                        "https://dev-wxmskulgynvly88l.us.auth0.com/oauth/token"
                },
                "code",
                "v" * 64,
            )
    finally:
        await client.aclose()


def test_email_normalization():
    assert normalize_email("  USER@Example.COM ") == "user@example.com"


@pytest.mark.parametrize("value", ["", " ", "x" * 321, None, 123])
def test_email_normalization_rejects_invalid_values(value):
    with pytest.raises(IdentityProvisioningError):
        normalize_email(value)


def test_authorization_url_rejects_invalid_pkce_challenge(settings):
    transaction = SimpleNamespace(
        state="s" * 43,
        nonce="n" * 43,
        code_challenge="invalid-challenge",
    )

    with pytest.raises(OIDCAuthenticationError):
        build_authorization_url(
            settings,
            {
                "authorization_endpoint":
                    "https://dev-wxmskulgynvly88l.us.auth0.com/authorize"
            },
            transaction,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://dev-wxmskulgynvly88l.us.auth0.com/authorize?next=evil",
        "https://dev-wxmskulgynvly88l.us.auth0.com/authorize#fragment",
        "https://dev-wxmskulgynvly88l.us.auth0.com.evil.example/authorize",
        "https://user@dev-wxmskulgynvly88l.us.auth0.com/authorize",
        "http://dev-wxmskulgynvly88l.us.auth0.com/authorize",
        "https://dev-wxmskulgynvly88l.us.auth0.com:444/authorize",
    ],
)
def test_authorization_url_rejects_noncanonical_provider_endpoint(
    settings, endpoint
):
    transaction = SimpleNamespace(
        state="s" * 43,
        nonce="n" * 43,
        code_challenge="c" * 43,
    )

    with pytest.raises(OIDCAuthenticationError):
        build_authorization_url(
            settings,
            {"authorization_endpoint": endpoint},
            transaction,
        )


@pytest.mark.asyncio
async def test_token_exchange_rejects_invalid_pkce_verifier(settings):
    with pytest.raises(OIDCAuthenticationError):
        await exchange_authorization_code(
            settings,
            {
                "token_endpoint":
                    "https://dev-wxmskulgynvly88l.us.auth0.com/oauth/token"
            },
            "authorization-code",
            "!" * 64,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_grant", "error_description": "sensitive"}),
        httpx.Response(302, headers={"Location": "https://evil.example/"}),
        httpx.Response(
            200,
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        ),
    ],
)
async def test_token_exchange_handles_provider_error_responses(monkeypatch, 
    settings, response
):
    async def handler(request):
        return response

    client = httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    )

    try:
        with pytest.raises(OIDCAuthenticationError) as exc:
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await exchange_authorization_code(
                settings,
                {
                    "token_endpoint":
                        "https://dev-wxmskulgynvly88l.us.auth0.com/oauth/token"
                },
                "authorization-code",
                "v" * 64,
            )

        assert str(exc.value) == "Authorization code exchange failed."
        assert "sensitive" not in str(exc.value)
    finally:
        await client.aclose()




@pytest.mark.asyncio
async def test_token_exchange_rejects_oversized_response(monkeypatch, settings):
    oversized = b"x" * (64 * 1024 + 1)

    async def handler(request):
        return httpx.Response(200, content=oversized)

    client = httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(OIDCAuthenticationError) as exc:
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await exchange_authorization_code(
                settings,
                {
                    "token_endpoint":
                        f"https://{settings.domain}/oauth/token"
                },
                "authorization-code",
                "v" * 64,
            )

        assert str(exc.value) == "Authorization code exchange failed."
    finally:
        await client.aclose()




@pytest.mark.asyncio
async def test_token_exchange_handles_timeout_without_leaking_details(monkeypatch, settings):
    async def handler(request):
        raise httpx.ReadTimeout("sensitive timeout detail")

    client = httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler)
    )

    try:
        with pytest.raises(OIDCAuthenticationError) as exc:
            monkeypatch.setattr(oidc_module, "create_oidc_http_client", lambda *, timeout: httpx.AsyncClient(timeout=timeout, trust_env=False, transport=httpx.MockTransport(handler)))
            await exchange_authorization_code(
                settings,
                {
                    "token_endpoint":
                        "https://dev-wxmskulgynvly88l.us.auth0.com/oauth/token"
                },
                "authorization-code",
                "v" * 64,
            )

        assert str(exc.value) == "Authorization code exchange failed."
        assert "sensitive" not in str(exc.value)
    finally:
        await client.aclose()


def test_token_exchange_public_api_does_not_accept_client():
    import inspect

    assert "client" not in inspect.signature(
        exchange_authorization_code
    ).parameters
