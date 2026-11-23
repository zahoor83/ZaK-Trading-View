"""Pure, fail-closed decisions for Paper Trading idempotency replay."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class IdempotencyDecisionError(ValueError):
    """Raised when persisted or supplied idempotency data is invalid."""


class IdempotencyDecisionKind(str, Enum):
    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    kind: IdempotencyDecisionKind
    response_data: dict[str, object] | None = None


def _validate_fingerprint(value: object, *, field: str) -> str:
    if type(value) is not str or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise IdempotencyDecisionError(
            f"{field} must be a lowercase 64-character SHA-256 fingerprint."
        )
    return value


def _validated_response(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IdempotencyDecisionError(
            "A completed execution request must contain a JSON object response."
        )

    if any(type(key) is not str for key in value):
        raise IdempotencyDecisionError(
            "Stored response object keys must be strings."
        )

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IdempotencyDecisionError(
            "Stored execution response is not valid JSON data."
        ) from exc

    if type(decoded) is not dict:
        raise IdempotencyDecisionError(
            "Stored execution response must be a JSON object."
        )

    return deepcopy(decoded)


def decide_idempotency(
    *,
    existing_request_found: bool,
    existing_fingerprint: object,
    existing_response: object,
    requested_fingerprint: object,
) -> IdempotencyDecision:
    """Classify a request without performing database or financial operations.

    The caller must scope the existing-request lookup to the correct book or
    portfolio and must hold the required resource locks before calling this.
    """

    if type(existing_request_found) is not bool:
        raise IdempotencyDecisionError(
            "Existing-request lookup result must be a boolean."
        )

    requested = _validate_fingerprint(
        requested_fingerprint,
        field="Requested fingerprint",
    )

    if not existing_request_found:
        if existing_fingerprint is not None or existing_response is not None:
            raise IdempotencyDecisionError(
                "Absent execution request cannot contain persisted data."
            )
        return IdempotencyDecision(IdempotencyDecisionKind.NEW)

    stored = _validate_fingerprint(
        existing_fingerprint,
        field="Stored fingerprint",
    )
    response = _validated_response(existing_response)

    if stored != requested:
        return IdempotencyDecision(IdempotencyDecisionKind.CONFLICT)

    return IdempotencyDecision(
        IdempotencyDecisionKind.REPLAY,
        response_data=response,
    )