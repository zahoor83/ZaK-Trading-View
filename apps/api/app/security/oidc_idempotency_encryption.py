"""Authenticated encryption for OIDC idempotency replay responses."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.oidc_transaction_service import (
    OIDCTransactionEncryptionSettings,
)

NONCE_BYTES = 12
TAG_BYTES = 16
MAX_RESPONSE_BYTES = 16 * 1024
AAD_DOMAIN = b"zak-oidc-idempotency-response-v1\x00"


class OIDCIdempotencyEncryptionError(Exception):
    """Raised when an idempotency response cannot be safely encrypted or read."""


def _require_uuid(value: uuid.UUID, name: str) -> None:
    if not isinstance(value, uuid.UUID):
        raise OIDCIdempotencyEncryptionError(f"Invalid {name}.")


def _associated_data(
    *,
    record_id: uuid.UUID,
    service_id: str,
    key_hash: str,
    fingerprint: str,
    transaction_id: uuid.UUID,
    key_id: str,
) -> bytes:
    _require_uuid(record_id, "record ID")
    _require_uuid(transaction_id, "transaction ID")

    if (
        not isinstance(service_id, str)
        or not service_id
        or not service_id.isascii()
        or service_id != service_id.strip()
        or len(service_id) > 128
    ):
        raise OIDCIdempotencyEncryptionError("Invalid service identity.")

    for value, name in (
        (key_hash, "idempotency key hash"),
        (fingerprint, "request fingerprint"),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise OIDCIdempotencyEncryptionError(f"Invalid {name}.")

    if (
        not isinstance(key_id, str)
        or not 1 <= len(key_id) <= 64
        or not key_id.isascii()
        or any(not (c.isalnum() or c in "._-") for c in key_id)
    ):
        raise OIDCIdempotencyEncryptionError("Invalid encryption key ID.")

    fields = {
        "record_id": str(record_id),
        "service_id": service_id,
        "key_hash": key_hash,
        "fingerprint": fingerprint,
        "transaction_id": str(transaction_id),
        "key_id": key_id,
    }

    return AAD_DOMAIN + json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _serialize_payload(payload: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, Mapping):
        raise OIDCIdempotencyEncryptionError("Invalid replay response.")

    if not all(isinstance(key, str) for key in payload):
        raise OIDCIdempotencyEncryptionError("Invalid replay response.")

    try:
        encoded = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise OIDCIdempotencyEncryptionError(
            "Invalid replay response."
        ) from None

    if not encoded or len(encoded) > MAX_RESPONSE_BYTES:
        raise OIDCIdempotencyEncryptionError(
            "Replay response exceeds the permitted size."
        )

    return encoded


def encrypt_idempotency_response(
    payload: Mapping[str, Any],
    *,
    record_id: uuid.UUID,
    service_id: str,
    key_hash: str,
    fingerprint: str,
    transaction_id: uuid.UUID,
    settings: OIDCTransactionEncryptionSettings,
) -> tuple[bytes, bytes, str]:
    """Encrypt a replay response and return nonce, ciphertext, key ID."""

    if not isinstance(settings, OIDCTransactionEncryptionSettings):
        raise OIDCIdempotencyEncryptionError(
            "Encryption configuration is unavailable."
        )

    try:
        settings.validated()
        key_id = settings.active_key_id
        key = settings.keys[key_id]
        plaintext = _serialize_payload(payload)
        aad = _associated_data(
            record_id=record_id,
            service_id=service_id,
            key_hash=key_hash,
            fingerprint=fingerprint,
            transaction_id=transaction_id,
            key_id=key_id,
        )
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    except OIDCIdempotencyEncryptionError:
        raise
    except Exception:
        raise OIDCIdempotencyEncryptionError(
            "Replay response encryption failed."
        ) from None

    return nonce, ciphertext, key_id


def decrypt_idempotency_response(
    nonce: bytes,
    ciphertext: bytes,
    *,
    key_id: str,
    record_id: uuid.UUID,
    service_id: str,
    key_hash: str,
    fingerprint: str,
    transaction_id: uuid.UUID,
    settings: OIDCTransactionEncryptionSettings,
) -> dict[str, Any]:
    """Authenticate and decrypt a previously stored replay response."""

    if not isinstance(settings, OIDCTransactionEncryptionSettings):
        raise OIDCIdempotencyEncryptionError(
            "Encryption configuration is unavailable."
        )

    if (
        not isinstance(nonce, bytes)
        or len(nonce) != NONCE_BYTES
        or not isinstance(ciphertext, bytes)
        or not TAG_BYTES < len(ciphertext) <= MAX_RESPONSE_BYTES + TAG_BYTES
    ):
        raise OIDCIdempotencyEncryptionError(
            "Stored replay response is invalid."
        )

    try:
        settings.validated()
        key = settings.keys.get(key_id)
        if key is None:
            raise OIDCIdempotencyEncryptionError(
                "Replay response encryption key is unavailable."
            )

        aad = _associated_data(
            record_id=record_id,
            service_id=service_id,
            key_hash=key_hash,
            fingerprint=fingerprint,
            transaction_id=transaction_id,
            key_id=key_id,
        )

        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)

        if not plaintext or len(plaintext) > MAX_RESPONSE_BYTES:
            raise OIDCIdempotencyEncryptionError(
                "Stored replay response is invalid."
            )

        decoded = json.loads(plaintext.decode("utf-8"))

        if not isinstance(decoded, dict):
            raise OIDCIdempotencyEncryptionError(
                "Stored replay response is invalid."
            )

        return decoded

    except OIDCIdempotencyEncryptionError:
        raise
    except (InvalidTag, ValueError, TypeError, UnicodeError, RecursionError):
        raise OIDCIdempotencyEncryptionError(
            "Replay response authentication failed."
        ) from None
    except Exception:
        raise OIDCIdempotencyEncryptionError(
            "Replay response decryption failed."
        ) from None