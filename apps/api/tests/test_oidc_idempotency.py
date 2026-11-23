import uuid

import pytest

from app.security.oidc_idempotency import (
    OIDCIdempotencyInputError,
    canonical_uuid_v4,
    fingerprint_request,
    hash_idempotency_key,
)


def test_canonical_uuid_v4_is_accepted():
    request_id = str(uuid.uuid4())
    assert canonical_uuid_v4(request_id) == request_id


@pytest.mark.parametrize(
    "request_id",
    [
        "not-a-uuid",
        str(uuid.uuid1()),
        str(uuid.uuid4()).upper(),
        "00000000-0000-0000-0000-000000000000",
        "",
        None,
    ],
)
def test_invalid_uuid_v4_is_rejected(request_id):
    with pytest.raises(OIDCIdempotencyInputError):
        canonical_uuid_v4(request_id)


def test_idempotency_hash_is_deterministic():
    request_id = str(uuid.uuid4())

    first = hash_idempotency_key("test-bff", request_id)
    second = hash_idempotency_key("test-bff", request_id)

    assert first == second
    assert len(first) == 64
    assert all(character in "0123456789abcdef" for character in first)


def test_idempotency_hash_is_scoped_to_service():
    request_id = str(uuid.uuid4())

    first = hash_idempotency_key("test-bff", request_id)
    second = hash_idempotency_key("other-bff", request_id)

    assert first != second


def test_idempotency_hash_does_not_expose_raw_uuid():
    request_id = str(uuid.uuid4())
    result = hash_idempotency_key("test-bff", request_id)

    assert request_id not in result


def test_fingerprint_is_independent_of_mapping_key_order():
    first = fingerprint_request(
        "POST:/internal/v1/auth/oidc/transactions:v1",
        {"operation": "create", "version": 1},
    )
    second = fingerprint_request(
        "POST:/internal/v1/auth/oidc/transactions:v1",
        {"version": 1, "operation": "create"},
    )

    assert first == second


def test_fingerprint_changes_when_semantics_change():
    operation = "POST:/internal/v1/auth/oidc/transactions:v1"

    first = fingerprint_request(operation, {"version": 1})
    second = fingerprint_request(operation, {"version": 2})

    assert first != second


def test_fingerprint_changes_when_operation_changes():
    payload = {"version": 1}

    first = fingerprint_request("operation-a", payload)
    second = fingerprint_request("operation-b", payload)

    assert first != second


def test_fingerprint_rejects_non_json_payload():
    with pytest.raises(OIDCIdempotencyInputError):
        fingerprint_request("operation-a", {"invalid": object()})


def test_service_id_validation_fails_closed():
    request_id = str(uuid.uuid4())

    for service_id in ("", "  ", " test ", "x" * 129, "münchen"):
        with pytest.raises(OIDCIdempotencyInputError):
            hash_idempotency_key(service_id, request_id)