from __future__ import annotations

import base64
import json

import pytest

from app.security.hmac_internal_service_auth_factory import (
    build_internal_service_authentication,
)
from app.security.hmac_internal_service_auth import HMACConfigurationError


def configure_factory(monkeypatch, encoded_secret: str) -> None:
    monkeypatch.setenv("OIDC_INTERNAL_SERVICE_ID", "web-bff")
    monkeypatch.setenv("OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID", "key-2026-01")
    monkeypatch.setenv(
        "OIDC_INTERNAL_SERVICE_HMAC_KEYS",
        json.dumps({"key-2026-01": encoded_secret}),
    )


def test_factory_accepts_canonical_unpadded_base64url(monkeypatch):
    secret = bytes(range(32))
    encoded = base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")
    configure_factory(monkeypatch, encoded)

    authenticator, allowlist = build_internal_service_authentication()

    assert authenticator is not None
    assert allowlist == frozenset({"web-bff"})
    from app.security.hmac_internal_service_auth import HMACInternalServiceAuthenticator
    assert isinstance(authenticator, HMACInternalServiceAuthenticator)


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "A" * 42 + "+",
        "A" * 42 + "/",
        "A" * 42 + "=",
        "A" * 42 + "!",
        "A" * 41,
    ],
)
def test_factory_rejects_noncanonical_or_malformed_base64url(
    monkeypatch,
    secret,
):
    configure_factory(monkeypatch, secret)

    with pytest.raises(HMACConfigurationError):
        build_internal_service_authentication()


def test_factory_rejects_padded_form_of_valid_secret(monkeypatch):
    secret = bytes(range(32))
    padded = base64.urlsafe_b64encode(secret).decode("ascii")
    configure_factory(monkeypatch, padded)

    with pytest.raises(HMACConfigurationError):
        build_internal_service_authentication()


@pytest.mark.parametrize(
    "secret",
    [
        base64.urlsafe_b64encode(b"short").decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(b"x" * 31).decode("ascii").rstrip("="),
    ],
)
def test_factory_rejects_secrets_shorter_than_32_bytes(monkeypatch, secret):
    configure_factory(monkeypatch, secret)

    with pytest.raises(HMACConfigurationError, match="too short"):
        build_internal_service_authentication()


def test_factory_accepts_multi_key_rotation_ring(monkeypatch):
    import base64

    previous_secret = bytes([7]) * 32
    active_secret = bytes([9]) * 32

    encoded_keys = {
        "key-previous-2026": base64.urlsafe_b64encode(
            previous_secret
        ).decode("ascii").rstrip("="),
        "key-active-2026": base64.urlsafe_b64encode(
            active_secret
        ).decode("ascii").rstrip("="),
    }

    monkeypatch.setenv("OIDC_INTERNAL_SERVICE_ID", "web-bff")
    monkeypatch.setenv(
        "OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID",
        "key-active-2026",
    )
    monkeypatch.setenv(
        "OIDC_INTERNAL_SERVICE_HMAC_KEYS",
        json.dumps(encoded_keys),
    )

    authenticator, allowlist = build_internal_service_authentication()

    assert allowlist == frozenset({"web-bff"})
    assert authenticator is not None
    assert authenticator._keys == {
        "key-previous-2026": previous_secret,
        "key-active-2026": active_secret,
    }
