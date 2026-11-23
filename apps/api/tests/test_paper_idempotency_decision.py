import pytest

from app.security.paper_idempotency_decision import (
    IdempotencyDecisionError,
    IdempotencyDecisionKind,
    decide_idempotency,
)


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


def test_absent_request_is_new():
    result = decide_idempotency(
        existing_request_found=False,
        existing_fingerprint=None,
        existing_response=None,
        requested_fingerprint=FINGERPRINT_A,
    )

    assert result.kind is IdempotencyDecisionKind.NEW
    assert result.response_data is None


def test_same_fingerprint_replays_original_response():
    original = {
        "transaction_id": "txn-123",
        "status": "posted",
        "details": {"filled_quantity": "0.125"},
    }

    result = decide_idempotency(
        existing_request_found=True,
        existing_fingerprint=FINGERPRINT_A,
        existing_response=original,
        requested_fingerprint=FINGERPRINT_A,
    )

    assert result.kind is IdempotencyDecisionKind.REPLAY
    assert result.response_data == original
    assert result.response_data is not original
    assert result.response_data["details"] is not original["details"]


def test_different_fingerprint_is_conflict_without_response():
    result = decide_idempotency(
        existing_request_found=True,
        existing_fingerprint=FINGERPRINT_A,
        existing_response={"status": "posted"},
        requested_fingerprint=FINGERPRINT_B,
    )

    assert result.kind is IdempotencyDecisionKind.CONFLICT
    assert result.response_data is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "A" * 64,
        "a" * 63,
        "g" * 64,
        123,
        b"a" * 64,
    ],
)
def test_invalid_requested_fingerprint_fails_closed(value):
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=False,
            existing_fingerprint=None,
            existing_response=None,
            requested_fingerprint=value,
        )


@pytest.mark.parametrize("value", [None, "", "A" * 64, "a" * 63, 123])
def test_invalid_stored_fingerprint_fails_closed(value):
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=True,
            existing_fingerprint=value,
            existing_response={"status": "posted"},
            requested_fingerprint=FINGERPRINT_A,
        )


@pytest.mark.parametrize(
    ("fingerprint", "response"),
    [
        (None, None),
        (FINGERPRINT_A, None),
        (None, {"status": "posted"}),
    ],
)
def test_found_request_with_missing_or_inconsistent_data_fails_closed(
    fingerprint,
    response,
):
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=True,
            existing_fingerprint=fingerprint,
            existing_response=response,
            requested_fingerprint=FINGERPRINT_A,
        )


@pytest.mark.parametrize(
    ("fingerprint", "response"),
    [
        (FINGERPRINT_A, None),
        (FINGERPRINT_A, {"status": "posted"}),
    ],
)
def test_absent_request_cannot_have_persisted_data(fingerprint, response):
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=False,
            existing_fingerprint=fingerprint,
            existing_response=response,
            requested_fingerprint=FINGERPRINT_A,
        )


@pytest.mark.parametrize("response", [None, [], "posted", 123, object()])
def test_non_object_stored_response_fails_closed(response):
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=True,
            existing_fingerprint=FINGERPRINT_A,
            existing_response=response,
            requested_fingerprint=FINGERPRINT_A,
        )


def test_non_string_response_keys_fail_closed():
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=True,
            existing_fingerprint=FINGERPRINT_A,
            existing_response={1: "invalid"},
            requested_fingerprint=FINGERPRINT_A,
        )


def test_non_json_response_values_fail_closed():
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=True,
            existing_fingerprint=FINGERPRINT_A,
            existing_response={"value": object()},
            requested_fingerprint=FINGERPRINT_A,
        )


def test_non_finite_response_number_fails_closed():
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=True,
            existing_fingerprint=FINGERPRINT_A,
            existing_response={"value": float("nan")},
            requested_fingerprint=FINGERPRINT_A,
        )


@pytest.mark.parametrize("value", [1, "true", None, [], object()])
def test_existing_request_flag_must_be_boolean(value):
    with pytest.raises(IdempotencyDecisionError):
        decide_idempotency(
            existing_request_found=value,
            existing_fingerprint=None,
            existing_response=None,
            requested_fingerprint=FINGERPRINT_A,
        )


def test_replay_response_is_detached_from_original_data():
    original = {"nested": {"status": "posted"}}

    result = decide_idempotency(
        existing_request_found=True,
        existing_fingerprint=FINGERPRINT_A,
        existing_response=original,
        requested_fingerprint=FINGERPRINT_A,
    )

    result.response_data["nested"]["status"] = "changed"
    assert original["nested"]["status"] == "posted"