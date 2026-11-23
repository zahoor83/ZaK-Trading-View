"""Paper Trading scoped idempotency primitives."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any


_KEY_DOMAIN = b"zak-paper-trading-idempotency-key-v1\x00"
_FINGERPRINT_DOMAIN = b"zak-paper-trading-idempotency-fingerprint-v1\x00"


class PaperIdempotencyInputError(ValueError):
    """Raised when a Paper Trading idempotency input is malformed."""


def canonical_uuid_v4(value: str) -> str:
    """Require a canonical lowercase RFC 4122 UUID v4."""
    if not isinstance(value, str) or len(value) != 36:
        raise PaperIdempotencyInputError("Invalid idempotency request key.")

    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise PaperIdempotencyInputError(
            "Invalid idempotency request key."
        ) from None

    if parsed.version != 4 or str(parsed) != value:
        raise PaperIdempotencyInputError("Invalid idempotency request key.")

    return value


def hash_idempotency_key(request_key: str) -> str:
    """Hash a canonical client UUID without storing the raw key."""
    canonical_key = canonical_uuid_v4(request_key)
    material = _KEY_DOMAIN + canonical_key.encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    """Allow deterministic JSON values; reject floats and custom objects."""
    if depth > 64:
        raise PaperIdempotencyInputError("Idempotency payload is too deeply nested.")

    if value is None or isinstance(value, (str, bool, int)):
        return

    if isinstance(value, float):
        raise PaperIdempotencyInputError(
            "Floating-point values are not allowed in idempotency payloads."
        )

    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PaperIdempotencyInputError(
                    "Idempotency payload keys must be strings."
                )
            _validate_json_value(item, depth=depth + 1)
        return

    raise PaperIdempotencyInputError(
        "Idempotency payload must contain JSON values only."
    )


def fingerprint_request(
    operation: str,
    semantic_payload: Mapping[str, Any],
) -> str:
    """Fingerprint canonical operation semantics, excluding the key."""
    if (
        not isinstance(operation, str)
        or not operation
        or operation != operation.strip()
        or len(operation) > 128
        or not operation.isascii()
    ):
        raise PaperIdempotencyInputError("Invalid Paper Trading operation.")

    if not isinstance(semantic_payload, Mapping):
        raise PaperIdempotencyInputError("Invalid idempotency payload.")

    _validate_json_value(semantic_payload)

    try:
        canonical_payload = json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise PaperIdempotencyInputError(
            "Invalid idempotency payload."
        ) from None

    material = (
        _FINGERPRINT_DOMAIN
        + operation.encode("ascii")
        + b"\x00"
        + canonical_payload
    )
    return hashlib.sha256(material).hexdigest()