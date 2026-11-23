"""Deterministic, service-scoped OIDC idempotency primitives."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any


_KEY_DOMAIN = b"zak-oidc-idempotency-key-v1\x00"
_FINGERPRINT_DOMAIN = b"zak-oidc-idempotency-fingerprint-v1\x00"


class OIDCIdempotencyInputError(ValueError):
    """Raised when an idempotency input is malformed."""


def canonical_uuid_v4(value: str) -> str:
    """Validate and return a canonical lowercase RFC 4122 UUID v4."""
    if not isinstance(value, str) or len(value) != 36:
        raise OIDCIdempotencyInputError("Invalid idempotency request ID.")

    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise OIDCIdempotencyInputError(
            "Invalid idempotency request ID."
        ) from None

    if parsed.version != 4 or str(parsed) != value:
        raise OIDCIdempotencyInputError("Invalid idempotency request ID.")

    return value


def _validate_service_id(service_id: str) -> str:
    if (
        not isinstance(service_id, str)
        or not service_id
        or service_id != service_id.strip()
        or len(service_id) > 128
        or not service_id.isascii()
    ):
        raise OIDCIdempotencyInputError("Invalid internal service identity.")

    return service_id


def hash_idempotency_key(service_id: str, request_id: str) -> str:
    """Hash a UUID v4 key within its authenticated service scope."""
    service = _validate_service_id(service_id)
    canonical_key = canonical_uuid_v4(request_id)

    material = (
        _KEY_DOMAIN
        + service.encode("ascii")
        + b"\x00"
        + canonical_key.encode("ascii")
    )
    return hashlib.sha256(material).hexdigest()


def fingerprint_request(
    operation: str,
    semantic_payload: Mapping[str, Any],
) -> str:
    """Fingerprint canonical operation semantics, excluding the idempotency key."""
    if (
        not isinstance(operation, str)
        or not operation
        or operation != operation.strip()
        or len(operation) > 256
        or not operation.isascii()
    ):
        raise OIDCIdempotencyInputError("Invalid idempotency operation.")

    if not isinstance(semantic_payload, Mapping):
        raise OIDCIdempotencyInputError("Invalid idempotency request payload.")

    try:
        canonical_payload = json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise OIDCIdempotencyInputError(
            "Invalid idempotency request payload."
        ) from None

    material = (
        _FINGERPRINT_DOMAIN
        + operation.encode("ascii")
        + b"\x00"
        + canonical_payload
    )
    return hashlib.sha256(material).hexdigest()