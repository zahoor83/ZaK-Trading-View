"""Transactional persistence for immutable paper fee-policy versions."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_fee_policy import PaperFeePolicyHead, PaperFeePolicyVersion
from app.security.paper_write_authorization import authorize_paper_write
from app.security.permissions import Permission
from app.services.paper_fee_policy import PaperFeePolicy, PaperFeePolicyError


class PaperFeePolicyPersistenceError(ValueError):
    """Persistent fee-policy operation failed closed."""


async def set_paper_fee_policy(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    entry_fee_rate: Decimal,
    exit_fee_rate: Decimal,
) -> PaperFeePolicy:
    """Create a new immutable version and atomically move its current pointer.

    The caller must start and commit/rollback the transaction. The existing
    authorization helper locks User -> Account -> Membership -> Portfolio ->
    Session -> Book before this service mutates fee-policy state.
    """
    if not session.in_transaction():
        raise PaperFeePolicyPersistenceError(
            "Fee-policy update requires a caller-owned active transaction."
        )

    access = await authorize_paper_write(
        session,
        authenticated_user_id=authenticated_user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        permission=Permission.FEE_POLICY_MANAGE,
    )

    try:
        market = access.market_type
        if market not in {"spot", "futures"}:
            raise PaperFeePolicyError("Unsupported fee-policy market.")

        head_result = await session.execute(
            select(PaperFeePolicyHead)
            .where(
                PaperFeePolicyHead.account_id == account_id,
                PaperFeePolicyHead.market_type == market,
            )
            .with_for_update()
        )
        head = head_result.scalar_one_or_none()

        latest_result = await session.execute(
            select(func.max(PaperFeePolicyVersion.version)).where(
                PaperFeePolicyVersion.account_id == account_id,
                PaperFeePolicyVersion.market_type == market,
            )
        )
        latest_version = latest_result.scalar_one_or_none() or 0
        next_version = latest_version + 1

        if head is None:
            policy_id = str(uuid.uuid4())
        else:
            current_result = await session.execute(
                select(PaperFeePolicyVersion).where(
                    PaperFeePolicyVersion.id == head.version_id,
                    PaperFeePolicyVersion.account_id == account_id,
                    PaperFeePolicyVersion.market_type == market,
                    PaperFeePolicyVersion.version == head.version,
                )
            )
            current = current_result.scalar_one_or_none()
            if current is None:
                raise PaperFeePolicyPersistenceError(
                    "Current fee-policy pointer is inconsistent."
                )
            policy_id = current.policy_id

        policy = PaperFeePolicy(
            policy_id=policy_id,
            version=next_version,
            market_type=market,
            entry_fee_rate=entry_fee_rate,
            exit_fee_rate=exit_fee_rate,
        )

        version_row = PaperFeePolicyVersion(
            account_id=account_id,
            market_type=market,
            policy_id=policy.policy_id,
            version=policy.version,
            entry_fee_rate=policy.entry_fee_rate,
            exit_fee_rate=policy.exit_fee_rate,
            created_by_user_id=authenticated_user_id,
        )
        session.add(version_row)
        await session.flush()

        if head is None:
            session.add(
                PaperFeePolicyHead(
                    account_id=account_id,
                    market_type=market,
                    version_id=version_row.id,
                    version=next_version,
                )
            )
        else:
            head.version_id = version_row.id
            head.version = next_version
            await session.flush()

        return policy

    except PaperFeePolicyError:
        raise
    except PaperFeePolicyPersistenceError:
        raise


async def get_current_paper_fee_policy(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    market_type: str,
) -> PaperFeePolicy:
    """Read the current policy; missing or inconsistent configuration fails closed."""
    if not isinstance(account_id, uuid.UUID):
        raise PaperFeePolicyPersistenceError("Invalid account identifier.")
    if market_type not in {"spot", "futures"}:
        raise PaperFeePolicyPersistenceError("Unsupported fee-policy market.")

    result = await session.execute(
        select(PaperFeePolicyVersion)
        .join(
            PaperFeePolicyHead,
            (PaperFeePolicyHead.version_id == PaperFeePolicyVersion.id)
            & (PaperFeePolicyHead.account_id == PaperFeePolicyVersion.account_id)
            & (PaperFeePolicyHead.market_type == PaperFeePolicyVersion.market_type)
            & (PaperFeePolicyHead.version == PaperFeePolicyVersion.version),
        )
        .where(
            PaperFeePolicyHead.account_id == account_id,
            PaperFeePolicyHead.market_type == market_type,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise PaperFeePolicyPersistenceError(
            "No configured fee policy exists for this account and market."
        )

    return PaperFeePolicy(
        policy_id=row.policy_id,
        version=row.version,
        market_type=row.market_type,
        entry_fee_rate=row.entry_fee_rate,
        exit_fee_rate=row.exit_fee_rate,
    )
