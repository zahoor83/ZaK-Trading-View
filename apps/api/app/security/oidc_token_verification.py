"""Strict Auth0 OIDC ID Token verification and controlled JWKS caching."""

from __future__ import annotations

import asyncio
import base64
import hmac
import math
import re
import time

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from joserfc import jwt, jws
from joserfc.jwk import RSAKey

from app.security.oidc_configuration import (
    Auth0Settings,
    OIDCConfigurationError,
    validate_jwks,
)
from app.services.oidc_transaction_service import _domain_hash


class OIDCVerificationError(Exception):
    """Generic fail-closed OIDC verification failure."""


class OIDCJWKSUnavailableError(OIDCVerificationError):
    """Trusted JWKS could not be obtained or validated."""


class OIDCUnknownSigningKeyError(OIDCVerificationError):
    """The requested signing key is not present in trusted JWKS."""


@dataclass(frozen=True, slots=True)
class VerifiedOIDCIdentity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool | None
    name: str | None


def _decode_b64url_uint(value: Any, field: str) -> int:
    if not isinstance(value, str):
        raise OIDCJWKSUnavailableError("Invalid RSA signing key.")

    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise OIDCJWKSUnavailableError("Invalid RSA signing key.")

    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        raise OIDCJWKSUnavailableError("Invalid RSA signing key.") from None

    if not decoded or len(decoded) > 1024:
        raise OIDCJWKSUnavailableError("Invalid RSA signing key.")

    return int.from_bytes(decoded, "big")


def _import_public_signing_key(data: Mapping[str, Any]) -> RSAKey:
    if not isinstance(data, Mapping):
        raise OIDCJWKSUnavailableError("Invalid RSA signing key.")

    # A public JWKS must never contain private RSA parameters.
    private_members = {"d", "p", "q", "dp", "dq", "qi", "oth"}

    if private_members.intersection(data.keys()):
        raise OIDCJWKSUnavailableError(
            "JWKS unexpectedly contains private key material."
        )

    if data.get("kty") != "RSA":
        raise OIDCJWKSUnavailableError("Unsupported signing key type.")

    if data.get("use") not in (None, "sig"):
        raise OIDCJWKSUnavailableError("Invalid signing key usage.")

    if data.get("alg") not in (None, "RS256"):
        raise OIDCJWKSUnavailableError("Invalid signing key algorithm.")

    operations = data.get("key_ops")

    if operations is not None:
        if (
            not isinstance(operations, list)
            or "verify" not in operations
            or any(not isinstance(item, str) for item in operations)
        ):
            raise OIDCJWKSUnavailableError("Invalid signing key operations.")

    modulus = _decode_b64url_uint(data.get("n"), "n")
    exponent = _decode_b64url_uint(data.get("e"), "e")

    if not 2048 <= modulus.bit_length() <= 8192:
        raise OIDCJWKSUnavailableError("RSA modulus size is not acceptable.")

    if exponent < 65537 or exponent % 2 == 0:
        raise OIDCJWKSUnavailableError("RSA public exponent is not acceptable.")

    try:
        return RSAKey.import_key(dict(data))
    except Exception:
        raise OIDCJWKSUnavailableError("RSA signing key import failed.") from None


class OIDCJWKSCache:
    """Async, bounded, injected-source JWKS cache.

    The caller must supply a fetcher that obtains JWKS from the already
    validated Auth0 discovery metadata. This class never follows token URLs.
    """

    def __init__(
        self,
        fetch_jwks: Callable[[], Awaitable[Mapping[str, Any]]],
        *,
        cache_ttl_seconds: float = 300.0,
        unknown_kid_refresh_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(fetch_jwks):
            raise ValueError("fetch_jwks must be callable.")

        if (
            not math.isfinite(cache_ttl_seconds)
            or cache_ttl_seconds <= 0
            or cache_ttl_seconds > 3600
        ):
            raise ValueError("Invalid JWKS cache TTL.")

        if (
            not math.isfinite(unknown_kid_refresh_seconds)
            or unknown_kid_refresh_seconds < 1
            or unknown_kid_refresh_seconds > 60
        ):
            raise ValueError("Invalid unknown-kid refresh interval.")

        self._fetch_jwks = fetch_jwks
        self._ttl = cache_ttl_seconds
        self._unknown_refresh_interval = unknown_kid_refresh_seconds
        self._monotonic = monotonic
        self._keys: dict[str, RSAKey] = {}
        self._expires_at = 0.0
        self._last_unknown_refresh = float("-inf")
        self._lock = asyncio.Lock()

    async def _refresh_locked(self, now: float) -> None:
        try:
            raw = await self._fetch_jwks()
            validated = validate_jwks(raw)
            imported: dict[str, RSAKey] = {}

            for item in validated["keys"]:
                kid = item.get("kid")

                if not isinstance(kid, str) or not kid:
                    raise OIDCJWKSUnavailableError(
                        "JWKS signing key has no valid kid."
                    )

                if kid in imported:
                    raise OIDCJWKSUnavailableError(
                        "JWKS contains duplicate signing key identifiers."
                    )

                imported[kid] = _import_public_signing_key(item)

            if not imported:
                raise OIDCJWKSUnavailableError(
                    "JWKS contains no acceptable signing keys."
                )

        except OIDCJWKSUnavailableError:
            raise
        except Exception:
            raise OIDCJWKSUnavailableError(
                "Trusted signing keys are temporarily unavailable."
            ) from None

        # Replace the cache atomically only after the entire set validates.
        self._keys = imported
        self._expires_at = now + self._ttl

    async def get_key(self, kid: str) -> RSAKey:
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise OIDCUnknownSigningKeyError("Unknown signing key.")

        async with self._lock:
            now = self._monotonic()

            if not math.isfinite(now):
                raise OIDCJWKSUnavailableError("Invalid cache clock.")

            if not self._keys or now >= self._expires_at:
                await self._refresh_locked(now)

                # An initial/expiry fetch that still misses the requested
                # kid counts as the first unknown-kid refresh attempt.
                if kid not in self._keys:
                    self._last_unknown_refresh = now

            elif kid not in self._keys:
                if now - self._last_unknown_refresh >= self._unknown_refresh_interval:
                    self._last_unknown_refresh = now
                    await self._refresh_locked(now)

            key = self._keys.get(kid)

            if key is None:
                raise OIDCUnknownSigningKeyError("Unknown signing key.")

            return key


def _numeric_date(claims: Mapping[str, Any], name: str) -> float:
    value = claims.get(name)

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise OIDCVerificationError("Required time claim is invalid.")

    return float(value)


def _validate_claims(
    claims: Mapping[str, Any],
    settings: Auth0Settings,
    expected_nonce_hash: str,
    *,
    now_epoch: float,
    clock_skew_seconds: float,
) -> VerifiedOIDCIdentity:
    settings = settings.validated()

    if not isinstance(claims, Mapping):
        raise OIDCVerificationError("Invalid ID Token claims.")

    if claims.get("iss") != settings.issuer:
        raise OIDCVerificationError("ID Token issuer mismatch.")

    audience = claims.get("aud")

    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list):
        audiences = audience
    else:
        raise OIDCVerificationError("ID Token audience is invalid.")

    if (
        not audiences
        or any(not isinstance(item, str) or not item for item in audiences)
        or len(set(audiences)) != len(audiences)
        or settings.client_id not in audiences
    ):
        raise OIDCVerificationError("ID Token audience mismatch.")

    authorized_party = claims.get("azp")

    if len(audiences) > 1:
        if authorized_party != settings.client_id:
            raise OIDCVerificationError("ID Token authorized party mismatch.")
    elif authorized_party is not None and authorized_party != settings.client_id:
        raise OIDCVerificationError("ID Token authorized party mismatch.")

    expiration = _numeric_date(claims, "exp")
    issued_at = _numeric_date(claims, "iat")

    if expiration <= issued_at:
        raise OIDCVerificationError("ID Token time interval is invalid.")

    if now_epoch >= expiration + clock_skew_seconds:
        raise OIDCVerificationError("ID Token has expired.")

    if issued_at > now_epoch + clock_skew_seconds:
        raise OIDCVerificationError("ID Token issued-at time is in the future.")

    if "nbf" in claims:
        not_before = _numeric_date(claims, "nbf")

        if now_epoch + clock_skew_seconds < not_before:
            raise OIDCVerificationError("ID Token is not yet valid.")

    subject = claims.get("sub")

    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 255
        or any(ord(char) < 0x20 for char in subject)
    ):
        raise OIDCVerificationError("ID Token subject is invalid.")

    nonce = claims.get("nonce")

    if not isinstance(nonce, str):
        raise OIDCVerificationError("ID Token nonce is missing or invalid.")

    try:
        actual_nonce_hash = _domain_hash("nonce", nonce)
    except Exception:
        raise OIDCVerificationError("ID Token nonce is invalid.") from None

    if not hmac.compare_digest(actual_nonce_hash, expected_nonce_hash):
        raise OIDCVerificationError("ID Token nonce mismatch.")

    email = claims.get("email")
    email_verified = claims.get("email_verified")
    name = claims.get("name")

    if email is not None and not isinstance(email, str):
        raise OIDCVerificationError("ID Token email claim is invalid.")

    if email_verified is not None and not isinstance(email_verified, bool):
        raise OIDCVerificationError("ID Token email verification claim is invalid.")

    if name is not None and not isinstance(name, str):
        raise OIDCVerificationError("ID Token name claim is invalid.")

    return VerifiedOIDCIdentity(
        issuer=settings.issuer,
        subject=subject,
        email=email,
        email_verified=email_verified,
        name=name,
    )


async def verify_id_token(
    encoded_token: str,
    settings: Auth0Settings,
    jwks_cache: OIDCJWKSCache,
    expected_nonce_hash: str,
    *,
    now_epoch: float | None = None,
    clock_skew_seconds: float = 60.0,
) -> VerifiedOIDCIdentity:
    """Verify an Auth0 ID Token and return only the required identity fields."""

    settings = settings.validated()

    if not isinstance(encoded_token, str) or not encoded_token:
        raise OIDCVerificationError("ID Token is invalid.")

    if len(encoded_token) > 16384 or not encoded_token.isascii():
        raise OIDCVerificationError("ID Token is invalid.")

    if encoded_token.count(".") != 2:
        raise OIDCVerificationError("ID Token is invalid.")

    if (
        not isinstance(expected_nonce_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_nonce_hash)
    ):
        raise OIDCVerificationError("Expected nonce hash is invalid.")

    if (
        not isinstance(clock_skew_seconds, (int, float))
        or isinstance(clock_skew_seconds, bool)
        or not math.isfinite(clock_skew_seconds)
        or clock_skew_seconds < 0
        or clock_skew_seconds > 300
    ):
        raise OIDCVerificationError("Clock skew configuration is invalid.")

    if now_epoch is None:
        now_epoch = time.time()

    if (
        isinstance(now_epoch, bool)
        or not isinstance(now_epoch, (int, float))
        or not math.isfinite(now_epoch)
    ):
        raise OIDCVerificationError("Verification clock is invalid.")

    try:
        compact = jws.extract_compact(encoded_token.encode("ascii"))
        header = compact.protected

        if not isinstance(header, Mapping):
            raise OIDCVerificationError("ID Token header is invalid.")

        # The algorithm is a fixed policy, never selected from the token.
        if header.get("alg") != "RS256":
            raise OIDCVerificationError("ID Token algorithm is not permitted.")

        if header.get("typ") not in (None, "JWT"):
            raise OIDCVerificationError("ID Token type is not permitted.")

        # Do not accept token-directed key retrieval or critical extensions.
        forbidden_headers = {
            "jku", "jwk", "x5u", "x5c", "crit", "b64"
        }

        if forbidden_headers.intersection(header.keys()):
            raise OIDCVerificationError("ID Token header contains forbidden fields.")

        kid = header.get("kid")

        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise OIDCVerificationError("ID Token key identifier is invalid.")

        key = await jwks_cache.get_key(kid)

        token = jwt.decode(
            encoded_token,
            key,
            algorithms=["RS256"],
        )

        if token.header.get("alg") != "RS256":
            raise OIDCVerificationError("ID Token algorithm mismatch.")

        return _validate_claims(
            token.claims,
            settings,
            expected_nonce_hash,
            now_epoch=float(now_epoch),
            clock_skew_seconds=float(clock_skew_seconds),
        )

    except OIDCVerificationError:
        raise
    except Exception:
        # Never expose token contents, signature errors or provider internals.
        raise OIDCVerificationError("ID Token verification failed.") from None