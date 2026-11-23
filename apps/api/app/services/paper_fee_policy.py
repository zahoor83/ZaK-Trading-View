"""Immutable, fail-closed server-managed paper-trading fee policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.services.paper_ledger_rules import (
    PaperLedgerRulesError,
    validate_decimal,
)


class PaperFeePolicyError(ValueError):
    """Fee policy is missing, invalid, or incompatible with the trade."""


_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SNAPSHOT_KEYS = frozenset({
    "schema_version",
    "policy_id",
    "version",
    "market_type",
    "entry_fee_rate",
    "exit_fee_rate",
})


def _rate(value: object, field: str) -> Decimal:
    try:
        result = validate_decimal(
            value,
            allow_negative=False,
            allow_zero=True,
        )
    except PaperLedgerRulesError as exc:
        raise PaperFeePolicyError(f"{field} is invalid.") from exc


    if result >= Decimal("1"):
        raise PaperFeePolicyError(f"{field} must be less than 1.")

    return result


@dataclass(frozen=True, slots=True)
class PaperFeePolicy:
    """Trusted server-side fee rates for one market and policy version."""

    policy_id: str
    version: int
    market_type: str
    entry_fee_rate: Decimal
    exit_fee_rate: Decimal

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_id, str)
            or not _POLICY_ID_RE.fullmatch(self.policy_id)
        ):
            raise PaperFeePolicyError("Invalid fee policy identifier.")

        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise PaperFeePolicyError("Fee policy version must be a positive integer.")

        if self.market_type not in {"spot", "futures"}:
            raise PaperFeePolicyError("Unsupported fee policy market.")

        object.__setattr__(
            self,
            "entry_fee_rate",
            _rate(self.entry_fee_rate, "entry_fee_rate"),
        )
        object.__setattr__(
            self,
            "exit_fee_rate",
            _rate(self.exit_fee_rate, "exit_fee_rate"),
        )

    def require_market(self, market_type: str) -> None:
        if market_type != self.market_type:
            raise PaperFeePolicyError(
                "Fee policy market does not match the target portfolio."
            )

    def to_snapshot(self) -> dict[str, object]:
        """Return a JSONB-safe immutable-by-copy snapshot representation."""
        return {
            "schema_version": 1,
            "policy_id": self.policy_id,
            "version": self.version,
            "market_type": self.market_type,
            "entry_fee_rate": format(self.entry_fee_rate, "f"),
            "exit_fee_rate": format(self.exit_fee_rate, "f"),
        }


def fee_policy_from_snapshot(
    snapshot: object,
    *,
    expected_market: str,
) -> PaperFeePolicy:
    """Revalidate a persisted position snapshot; never infer missing fields."""
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_KEYS:
        raise PaperFeePolicyError("Fee policy snapshot has an invalid schema.")

    if (
        isinstance(snapshot["schema_version"], bool)
        or snapshot["schema_version"] != 1
    ):
        raise PaperFeePolicyError("Unsupported fee policy snapshot version.")

    if snapshot["market_type"] != expected_market:
        raise PaperFeePolicyError("Persisted fee policy market mismatch.")

    for field in ("entry_fee_rate", "exit_fee_rate"):
        if not isinstance(snapshot[field], str):
            raise PaperFeePolicyError(
                f"Persisted {field} must be exact Decimal text."
            )

    try:
        policy = PaperFeePolicy(
            policy_id=snapshot["policy_id"],
            version=snapshot["version"],
            market_type=snapshot["market_type"],
            entry_fee_rate=Decimal(snapshot["entry_fee_rate"]),
            exit_fee_rate=Decimal(snapshot["exit_fee_rate"]),
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        if isinstance(exc, PaperFeePolicyError):
            raise
        raise PaperFeePolicyError("Persisted fee policy snapshot is invalid.") from exc

    policy.require_market(expected_market)
    return policy

