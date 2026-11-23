import uuid

import pytest

from app.security.oidc_idempotency_encryption import (
    MAX_RESPONSE_BYTES,
    OIDCIdempotencyEncryptionError,
    decrypt_idempotency_response,
    encrypt_idempotency_response,
)
from app.services.oidc_transaction_service import (
    OIDCTransactionEncryptionSettings,
)


@pytest.fixture
def settings():
    return OIDCTransactionEncryptionSettings(
        keys={
            "key-old": b"O" * 32,
            "key-current": b"C" * 32,
        },
        active_key_id="key-current",
    )


@pytest.fixture
def context():
    return {
        "record_id": uuid.uuid4(),
        "service_id": "zak-api",
        "key_hash": "a" * 64,
        "fingerprint": "b" * 64,
        "transaction_id": uuid.uuid4(),
    }


@pytest.fixture
def payload():
    return {
        "authorization_url": "https://issuer.example/authorize?state=secret",
        "state": "opaque-state",
        "browser_cookie_name": "__Host-zak_oidc_test",
        "browser_cookie_value": "opaque-binding",
        "max_age": 300,
        "expires_at": "2026-11-12T12:00:00+00:00",
    }


def encrypt(payload, context, settings):
    return encrypt_idempotency_response(
        payload,
        **context,
        settings=settings,
    )


def decrypt(encrypted, context, settings):
    nonce, ciphertext, key_id = encrypted
    return decrypt_idempotency_response(
        nonce,
        ciphertext,
        key_id=key_id,
        **context,
        settings=settings,
    )


def test_round_trip(settings, context, payload):
    encrypted = encrypt(payload, context, settings)

    assert len(encrypted[0]) == 12
    assert len(encrypted[1]) > 16
    assert encrypted[2] == "key-current"
    assert decrypt(encrypted, context, settings) == payload


def test_each_encryption_uses_a_fresh_nonce(settings, context, payload):
    first = encrypt(payload, context, settings)
    second = encrypt(payload, context, settings)

    assert first[0] != second[0]


def test_ciphertext_tampering_is_rejected(settings, context, payload):
    nonce, ciphertext, key_id = encrypt(payload, context, settings)
    altered = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])

    with pytest.raises(OIDCIdempotencyEncryptionError):
        decrypt_idempotency_response(
            nonce,
            altered,
            key_id=key_id,
            **context,
            settings=settings,
        )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("record_id", uuid.uuid4()),
        ("service_id", "another-service"),
        ("key_hash", "c" * 64),
        ("fingerprint", "d" * 64),
        ("transaction_id", uuid.uuid4()),
    ],
)
def test_aad_context_mismatch_is_rejected(
    settings, context, payload, field, replacement
):
    encrypted = encrypt(payload, context, settings)
    altered_context = dict(context)
    altered_context[field] = replacement

    with pytest.raises(OIDCIdempotencyEncryptionError):
        decrypt(encrypted, altered_context, settings)


def test_wrong_key_id_is_rejected(settings, context, payload):
    encrypted = encrypt(payload, context, settings)

    with pytest.raises(OIDCIdempotencyEncryptionError):
        decrypt_idempotency_response(
            encrypted[0],
            encrypted[1],
            key_id="missing-key",
            **context,
            settings=settings,
        )


def test_key_rotation_keeps_old_records_decryptable(context, payload):
    old_settings = OIDCTransactionEncryptionSettings(
        keys={"key-old": b"O" * 32},
        active_key_id="key-old",
    )
    encrypted = encrypt(payload, context, old_settings)

    rotated_settings = OIDCTransactionEncryptionSettings(
        keys={
            "key-old": b"O" * 32,
            "key-current": b"C" * 32,
        },
        active_key_id="key-current",
    )

    assert encrypted[2] == "key-old"
    assert decrypt(encrypted, context, rotated_settings) == payload


def test_missing_old_key_fails_closed(context, payload):
    old_settings = OIDCTransactionEncryptionSettings(
        keys={"key-old": b"O" * 32},
        active_key_id="key-old",
    )
    encrypted = encrypt(payload, context, old_settings)

    current_only = OIDCTransactionEncryptionSettings(
        keys={"key-current": b"C" * 32},
        active_key_id="key-current",
    )

    with pytest.raises(OIDCIdempotencyEncryptionError):
        decrypt(encrypted, context, current_only)


def test_invalid_nonce_is_rejected(settings, context, payload):
    _, ciphertext, key_id = encrypt(payload, context, settings)

    with pytest.raises(OIDCIdempotencyEncryptionError):
        decrypt_idempotency_response(
            b"short",
            ciphertext,
            key_id=key_id,
            **context,
            settings=settings,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"value": float("nan")},
        {"value": object()},
        {"value": "x" * MAX_RESPONSE_BYTES},
        ["not", "an", "object"],
        {1: "non-string-key"},
    ],
)
def test_invalid_or_oversized_payload_is_rejected(
    settings, context, payload
):
    with pytest.raises(OIDCIdempotencyEncryptionError):
        encrypt(payload, context, settings)