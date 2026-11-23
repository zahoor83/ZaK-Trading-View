from __future__ import annotations

import base64
import json
import time

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from joserfc import jwt
from joserfc.jwk import RSAKey

from app.security.oidc_configuration import Auth0Settings
from app.security.oidc_token_verification import (
    OIDCJWKSCache,
    OIDCJWKSUnavailableError,
    OIDCUnknownSigningKeyError,
    OIDCVerificationError,
    verify_id_token,
)
from app.services.oidc_transaction_service import _domain_hash


NOW = 1_700_000_000.0


@pytest.fixture
def settings():
    return Auth0Settings(
        domain="dev-wxmskulgynvly88l.us.auth0.com",
        client_id="test-client-id",
        client_secret="test-client-secret",
        callback_url="https://app.example.com/api/v1/auth/callback",
        app_public_url="https://app.example.com",
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_key(kid="test-key"):
    private = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    numbers = private.public_key().public_numbers()

    public_jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64url(
            numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        ),
        "e": b64url(
            numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
        ),
    }

    return RSAKey.import_key(private_pem), public_jwk


def make_claims(settings, nonce, **overrides):
    claims = {
        "iss": settings.issuer,
        "sub": "auth0|user-123",
        "aud": settings.client_id,
        "iat": NOW - 10,
        "exp": NOW + 300,
        "nonce": nonce,
        "email": "verified@example.com",
        "email_verified": True,
        "name": "Test User",
    }
    claims.update(overrides)
    return claims


def make_token(private_key, kid, claims, algorithm="RS256"):
    return jwt.encode(
        {"alg": algorithm, "kid": kid, "typ": "JWT"},
        claims,
        private_key,
        algorithms=[algorithm],
    )


def make_cache(jwks, *, clock=None, ttl=300, refresh_interval=5):
    calls = {"count": 0}

    async def fetch():
        calls["count"] += 1
        return jwks()

    cache = OIDCJWKSCache(
        fetch,
        cache_ttl_seconds=ttl,
        unknown_kid_refresh_seconds=refresh_interval,
        monotonic=clock or time.monotonic,
    )
    return cache, calls


@pytest.mark.asyncio
async def test_valid_rs256_id_token_returns_minimal_identity(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = make_token(private, "test-key", make_claims(settings, nonce))

    cache, _ = make_cache(lambda: {"keys": [public]})

    identity = await verify_id_token(
        token,
        settings,
        cache,
        _domain_hash("nonce", nonce),
        now_epoch=NOW,
    )

    assert identity.issuer == settings.issuer
    assert identity.subject == "auth0|user-123"
    assert identity.email == "verified@example.com"
    assert identity.email_verified is True
    assert identity.name == "Test User"
    assert not hasattr(identity, "nonce")


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected(settings):
    trusted_private, public = make_key()
    attacker_private, _ = make_key()
    nonce = "N" * 43

    token = make_token(
        attacker_private,
        "test-key",
        make_claims(settings, nonce),
    )

    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_wrong_nonce_is_rejected(settings):
    private, public = make_key()
    token = make_token(
        private,
        "test-key",
        make_claims(settings, "A" * 43),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", "B" * 43),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_wrong_issuer_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = make_token(
        private, "test-key",
        make_claims(settings, nonce, iss="https://evil.example/"),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = make_token(
        private, "test-key",
        make_claims(settings, nonce, aud="another-client"),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_multiple_audiences_require_matching_azp(settings):
    private, public = make_key()
    nonce = "N" * 43

    claims = make_claims(
        settings, nonce,
        aud=[settings.client_id, "another-audience"],
    )
    token = make_token(private, "test-key", claims)
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_multiple_audiences_accept_matching_azp(settings):
    private, public = make_key()
    nonce = "N" * 43

    claims = make_claims(
        settings, nonce,
        aud=[settings.client_id, "another-audience"],
        azp=settings.client_id,
    )
    token = make_token(private, "test-key", claims)
    cache, _ = make_cache(lambda: {"keys": [public]})

    result = await verify_id_token(
        token, settings, cache,
        _domain_hash("nonce", nonce),
        now_epoch=NOW,
    )

    assert result.subject == "auth0|user-123"


@pytest.mark.asyncio
async def test_wrong_azp_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43

    claims = make_claims(
        settings, nonce,
        aud=[settings.client_id, "another-audience"],
        azp="attacker-client",
    )
    token = make_token(private, "test-key", claims)
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_expired_token_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43

    token = make_token(
        private, "test-key",
        make_claims(settings, nonce, exp=NOW - 100),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
            clock_skew_seconds=0,
        )


@pytest.mark.asyncio
async def test_future_iat_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43

    token = make_token(
        private, "test-key",
        make_claims(settings, nonce, iat=NOW + 500),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_missing_required_claim_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43
    claims = make_claims(settings, nonce)
    del claims["sub"]

    token = make_token(private, "test-key", claims)
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_hs256_algorithm_is_rejected_before_jwks_fetch(settings):
    private, public = make_key()
    nonce = "N" * 43
    # Construct a compact HS256 token directly. Its signature is
    # intentionally irrelevant: the fixed algorithm policy must reject
    # the header before any JWKS lookup or signature verification.
    header = b64url(
        json.dumps(
            {"alg": "HS256", "kid": "test-key", "typ": "JWT"},
            separators=(",", ":"),
        ).encode("ascii")
    )
    payload = b64url(
        json.dumps(
            make_claims(settings, nonce),
            separators=(",", ":"),
        ).encode("ascii")
    )
    token = f"{header}.{payload}.{b64url(b'not-a-real-signature')}"

    cache, calls = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )

    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_token_directed_jwks_header_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = jwt.encode(
        {
            "alg": "RS256",
            "kid": "test-key",
            "jku": "https://attacker.example/jwks.json",
        },
        make_claims(settings, nonce),
        private,
        algorithms=["RS256"],
    )

    cache, calls = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )

    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_unknown_kid_triggers_one_controlled_refresh():
    private_a, public_a = make_key("key-a")
    private_b, public_b = make_key("key-b")
    current = {"keys": [public_a]}
    clock = [100.0]

    cache, calls = make_cache(
        lambda: current,
        clock=lambda: clock[0],
    )

    assert await cache.get_key("key-a")
    assert calls["count"] == 1

    current["keys"] = [public_b]

    assert await cache.get_key("key-b")
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_unknown_kid_refresh_is_rate_limited():
    _, public = make_key()
    clock = [100.0]

    cache, calls = make_cache(
        lambda: {"keys": [public]},
        clock=lambda: clock[0],
        refresh_interval=5,
    )

    with pytest.raises(OIDCUnknownSigningKeyError):
        await cache.get_key("unknown-a")

    assert calls["count"] == 1

    with pytest.raises(OIDCUnknownSigningKeyError):
        await cache.get_key("unknown-b")

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_expired_cache_refreshes():
    _, public_a = make_key("key-a")
    _, public_b = make_key("key-b")
    current = {"keys": [public_a]}
    clock = [100.0]

    cache, calls = make_cache(
        lambda: current,
        clock=lambda: clock[0],
        ttl=10,
    )

    await cache.get_key("key-a")
    current["keys"] = [public_b]
    clock[0] = 111.0

    assert await cache.get_key("key-b")
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_private_material_in_jwks_is_rejected():
    _, public = make_key()
    malformed = dict(public)
    malformed["d"] = "private-material"

    cache, _ = make_cache(lambda: {"keys": [malformed]})

    with pytest.raises(OIDCJWKSUnavailableError):
        await cache.get_key(public["kid"])


@pytest.mark.asyncio
async def test_jwks_fetch_failure_fails_closed():
    async def fetch():
        raise RuntimeError("provider internal detail")

    cache = OIDCJWKSCache(fetch)

    with pytest.raises(OIDCJWKSUnavailableError):
        await cache.get_key("test-key")


@pytest.mark.asyncio
async def test_malformed_token_is_rejected_without_fetch(settings):
    cache, calls = make_cache(lambda: {"keys": []})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            "not-a-jwt",
            settings,
            cache,
            "a" * 64,
            now_epoch=NOW,
        )

    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_invalid_nonce_hash_is_rejected(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = make_token(
        private, "test-key",
        make_claims(settings, nonce),
    )
    cache, calls = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            "not-a-sha256-hash",
            now_epoch=NOW,
        )

    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_email_verified_must_be_boolean(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = make_token(
        private, "test-key",
        make_claims(settings, nonce, email_verified="true"),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


@pytest.mark.asyncio
async def test_not_before_claim_is_enforced(settings):
    private, public = make_key()
    nonce = "N" * 43
    token = make_token(
        private, "test-key",
        make_claims(settings, nonce, nbf=NOW + 500),
    )
    cache, _ = make_cache(lambda: {"keys": [public]})

    with pytest.raises(OIDCVerificationError):
        await verify_id_token(
            token, settings, cache,
            _domain_hash("nonce", nonce),
            now_epoch=NOW,
        )


def test_invalid_cache_configuration_rejected():
    async def fetch():
        return {"keys": []}

    with pytest.raises(ValueError):
        OIDCJWKSCache(fetch, cache_ttl_seconds=0)

    with pytest.raises(ValueError):
        OIDCJWKSCache(fetch, unknown_kid_refresh_seconds=0)


def test_nonce_hash_contract_matches_transaction_service():
    nonce = "N" * 43
    expected = _domain_hash("nonce", nonce)

    assert len(expected) == 64
    assert expected == _domain_hash("nonce", nonce)