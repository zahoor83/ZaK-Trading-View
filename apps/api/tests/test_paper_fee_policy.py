from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.services.paper_fee_policy import (
    PaperFeePolicy,
    PaperFeePolicyError,
    fee_policy_from_snapshot,
)


def make_policy(**overrides):
    values = {
        "policy_id": "default-paper-fees",
        "version": 1,
        "market_type": "spot",
        "entry_fee_rate": Decimal("0.001"),
        "exit_fee_rate": Decimal("0.001"),
    }
    values.update(overrides)
    return PaperFeePolicy(**values)


def test_valid_policy_creates_exact_json_safe_snapshot():
    policy = make_policy()
    snapshot = policy.to_snapshot()

    assert snapshot == {
        "schema_version": 1,
        "policy_id": "default-paper-fees",
        "version": 1,
        "market_type": "spot",
        "entry_fee_rate": "0.001",
        "exit_fee_rate": "0.001",
    }


def test_policy_is_immutable():
    policy = make_policy()
    with pytest.raises(FrozenInstanceError):
        policy.version = 2


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_snapshot_round_trip(market):
    policy = make_policy(market_type=market)
    restored = fee_policy_from_snapshot(
        policy.to_snapshot(),
        expected_market=market,
    )
    assert restored == policy


def test_market_mismatch_fails_closed():
    policy = make_policy()
    with pytest.raises(PaperFeePolicyError):
        policy.require_market("futures")


@pytest.mark.parametrize("rate", [
    Decimal("-0.0001"),
    Decimal("1"),
    Decimal("2"),
    Decimal("NaN"),
    Decimal("Infinity"),
    0.001,
    True,
])
def test_invalid_fee_rates_rejected(rate):
    with pytest.raises(PaperFeePolicyError):
        make_policy(entry_fee_rate=rate)


@pytest.mark.parametrize("version", [0, -1, True, "1"])
def test_invalid_policy_versions_rejected(version):
    with pytest.raises(PaperFeePolicyError):
        make_policy(version=version)


@pytest.mark.parametrize("policy_id", ["", " bad", "bad ", "x" * 65])
def test_invalid_policy_identifiers_rejected(policy_id):
    with pytest.raises(PaperFeePolicyError):
        make_policy(policy_id=policy_id)


def test_snapshot_rejects_missing_or_extra_fields():
    snapshot = make_policy().to_snapshot()

    with pytest.raises(PaperFeePolicyError):
        fee_policy_from_snapshot(
            {key: value for key, value in snapshot.items() if key != "version"},
            expected_market="spot",
        )

    with pytest.raises(PaperFeePolicyError):
        fee_policy_from_snapshot(
            {**snapshot, "unexpected": "value"},
            expected_market="spot",
        )


def test_snapshot_rejects_float_rate_and_market_mismatch():
    snapshot = make_policy().to_snapshot()

    with pytest.raises(PaperFeePolicyError):
        fee_policy_from_snapshot(
            {**snapshot, "entry_fee_rate": 0.001},
            expected_market="spot",
        )

    with pytest.raises(PaperFeePolicyError):
        fee_policy_from_snapshot(
            snapshot,
            expected_market="futures",
        )


def test_snapshot_rejects_unsupported_schema_version():
    snapshot = make_policy().to_snapshot()

    with pytest.raises(PaperFeePolicyError):
        fee_policy_from_snapshot(
            {**snapshot, "schema_version": 2},
            expected_market="spot",
        )
