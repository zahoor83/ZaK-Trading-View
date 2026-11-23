"""Deployment factory for the production HMAC internal authenticator."""

from __future__ import annotations

import base64
import binascii
import json
import os

from app.security.hmac_internal_service_auth import (
    HMACConfigurationError,
    HMACInternalServiceAuthenticator,
)


def build_internal_service_authentication() -> tuple[object, object]:
    """Build the production authenticator from protected environment config.

    HMAC key ring format:
      OIDC_INTERNAL_SERVICE_HMAC_KEYS={"key-2026-01":"<base64url-secret>"}

    Secrets must decode to at least 32 bytes.
    """
    service_id = os.environ.get("OIDC_INTERNAL_SERVICE_ID", "")
    active_key_id = os.environ.get(
        "OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID",
        "",
    )
    raw_keys = os.environ.get("OIDC_INTERNAL_SERVICE_HMAC_KEYS", "")

    if not service_id or not active_key_id or not raw_keys:
        raise HMACConfigurationError("Production HMAC configuration is incomplete.")

    try:
        encoded_keys = json.loads(raw_keys)
    except (TypeError, ValueError):
        raise HMACConfigurationError("Production HMAC key ring is invalid.") from None

    if not isinstance(encoded_keys, dict) or not encoded_keys:
        raise HMACConfigurationError("Production HMAC key ring is invalid.")

    decoded_keys: dict[str, bytes] = {}

    for key_id, encoded_secret in encoded_keys.items():
        if not isinstance(key_id, str) or not isinstance(encoded_secret, str):
            raise HMACConfigurationError("Production HMAC key ring is invalid.")

        if (
            not encoded_secret
            or len(encoded_secret) % 4 == 1
            or any(
                character not in
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in encoded_secret
            )
        ):
            raise HMACConfigurationError("Production HMAC key ring is invalid.")

        try:
            padding = "=" * ((4 - len(encoded_secret) % 4) % 4)
            secret = base64.b64decode(
                (encoded_secret + padding).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError):
            raise HMACConfigurationError("Production HMAC key ring is invalid.") from None

        canonical_secret = base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")
        if canonical_secret != encoded_secret:
            raise HMACConfigurationError("Production HMAC key ring is invalid.")

        if len(secret) < 32:
            raise HMACConfigurationError("Production HMAC key is too short.")

        decoded_keys[key_id] = secret

    if active_key_id not in decoded_keys:
        raise HMACConfigurationError("Active production HMAC key is unavailable.")

    authenticator = HMACInternalServiceAuthenticator(
        service_id=service_id,
        keys=decoded_keys,
    )

    return authenticator, frozenset({service_id})