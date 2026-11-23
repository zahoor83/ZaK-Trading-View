import uuid

import pytest

from app.security.paper_idempotency import (
    PaperIdempotencyInputError,
    canonical_uuid_v4,
    fingerprint_request,
    hash_idempotency_key,
)


def test_canonical_uuid_v4_is_accepted():
    request_key = str(uuid.uuid4())
    assert canonical_uuid_v4(request_key) == request_key


@pytest.mark.parametrize(
    "request_key",
    [
        "not-a-uuid",
        str(uuid.uuid1()),
        str(uuid.uuid4()).upper(),
        "00000000-0000-0000-0000-000000000000",
        "",
        None,
    ],
)
def test_invalid_uuid_v4_is_rejected(request_key):
    with pytest.raises(PaperIdempotencyInputError):
        canonical_uuid_v4(request_key)


def test_key_hash_is_deterministic_and_sha256_sized():
    request_key = str(uuid.uuid4())

    first = hash_idempotency_key(request_key)
    second = hash_idempotency_key(request_key)

    assert first == second
    assert len(first) == 64
    assert all(char in "0123456789abcdef" for char in first)


def test_key_hash_does_not_expose_raw_uuid():
    request_key = str(uuid.uuid4())
    result = hash_idempotency_key(request_key)

    assert request_key not in result


def test_key_hash_uses_paper_specific_domain():
    request_key = str(uuid.uuid4())
    result = hash_idempotency_key(request_key)

    # Same UUID must not reuse the OIDC key hash.
    from app.security.oidc_idempotency import hash_idempotency_key as oidc_hash

    assert result != oidc_hash("paper-trading", request_key)


def test_fingerprint_is_independent_of_mapping_key_order():
    first = fingerprint_request(
        "spot_buy",
        {"symbol": "BTCUSDT", "quantity": "0.0100", "version": 1},
    )
    second = fingerprint_request(
        "spot_buy",
        {"version": 1, "quantity": "0.0100", "symbol": "BTCUSDT"},
    )

    assert first == second


def test_fingerprint_changes_when_semantics_change():
    first = fingerprint_request(
        "spot_buy",
        {"symbol": "BTCUSDT", "quantity": "0.0100"},
    )
    second = fingerprint_request(
        "spot_buy",
        {"symbol": "BTCUSDT", "quantity": "0.0200"},
    )

    assert first != second


def test_fingerprint_changes_when_operation_changes():
    payload = {"symbol": "BTCUSDT", "quantity": "1"}

    first = fingerprint_request("spot_buy", payload)
    second = fingerprint_request("spot_sell", payload)

    assert first != second


def test_decimal_values_must_be_canonical_strings():
    first = fingerprint_request("spot_buy", {"quantity": "1.00"})
    second = fingerprint_request("spot_buy", {"quantity": "1.0"})

    assert first != second


@pytest.mark.parametrize("value", [1.25, float("nan"), float("inf")])
def test_fingerprint_rejects_floating_point_values(value):
    with pytest.raises(PaperIdempotencyInputError):
        fingerprint_request("spot_buy", {"quantity": value})


def test_fingerprint_rejects_non_json_objects():
    with pytest.raises(PaperIdempotencyInputError):
        fingerprint_request("spot_buy", {"invalid": object()})


def test_fingerprint_rejects_non_string_mapping_keys():
    with pytest.raises(PaperIdempotencyInputError):
        fingerprint_request("spot_buy", {1: "invalid"})


@pytest.mark.parametrize(
    "operation",
    ["", " spot_buy", "spot_buy ", "x" * 129, "münchen"],
)
def test_invalid_operation_is_rejected(operation):
    with pytest.raises(PaperIdempotencyInputError):
        fingerprint_request(operation, {"quantity": "1"})


def test_fingerprint_rejects_non_mapping_payload():
    with pytest.raises(PaperIdempotencyInputError):
        fingerprint_request("spot_buy", ["not", "a", "mapping"])


def test_nested_json_payload_is_supported():
    result = fingerprint_request(
        "futures_open",
        {
            "symbol": "BTCUSDT",
            "metadata": {
                "leverage": "5",
                "flags": [True, False, None],
            },
        },
    )

    assert len(result) == 64