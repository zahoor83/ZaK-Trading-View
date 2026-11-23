"""Bounded cleanup for short-lived OIDC authorization transactions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.oidc import OIDCAuthTransaction, OIDCIdempotencyRecord


OIDC_CONSUMED_TRANSACTION_RETENTION = timedelta(hours=1)
OIDC_CLEANUP_INTERVAL_SECONDS = 60
OIDC_CLEANUP_BATCH_SIZE = 100


def _aware_now(value: datetime | None) -> datetime:
    current = value if value is not None else datetime.now(timezone.utc)
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("Cleanup time must be timezone-aware.")
    return current


async def delete_unconsumed_oidc_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    transaction_id: UUID,
) -> bool:
    """Delete only the specified transaction while it remains unconsumed."""
    if not isinstance(transaction_id, UUID):
        raise TypeError("transaction_id must be a UUID.")

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(OIDCAuthTransaction).where(
                    OIDCAuthTransaction.id == transaction_id,
                    OIDCAuthTransaction.consumed_at.is_(None),
                )
            )
            return result.rowcount == 1


async def cleanup_expired_oidc_transactions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = OIDC_CLEANUP_BATCH_SIZE,
) -> int:
    """Delete one bounded batch of eligible transactions."""
    current = _aware_now(now)

    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise TypeError("batch_size must be an integer.")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000.")

    consumed_cutoff = current - OIDC_CONSUMED_TRANSACTION_RETENTION

    eligible_ids = (
        select(OIDCAuthTransaction.id)
        .where(
            or_(
                (
                    OIDCAuthTransaction.consumed_at.is_(None)
                    & (OIDCAuthTransaction.expires_at <= current)
                ),
                OIDCAuthTransaction.consumed_at <= consumed_cutoff,
            )
        )
        .order_by(OIDCAuthTransaction.expires_at.asc())
        .limit(batch_size)
    )

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(OIDCAuthTransaction).where(
                    OIDCAuthTransaction.id.in_(eligible_ids)
                )
            )
            return max(0, int(result.rowcount or 0))


async def cleanup_expired_oidc_idempotency_records(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = OIDC_CLEANUP_BATCH_SIZE,
    service_id: str | None = None,
) -> int:
    """Delete one bounded batch of expired idempotency reservations."""
    current = func.current_timestamp() if now is None else _aware_now(now)

    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise TypeError("batch_size must be an integer.")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000.")

    if service_id is not None and (
        not isinstance(service_id, str) or not service_id.strip()
    ):
        raise ValueError("service_id must be a non-empty string when provided.")

    eligible_query = (
        select(OIDCIdempotencyRecord.id)
        .where(OIDCIdempotencyRecord.expires_at <= current)
    )

    if service_id is not None:
        eligible_query = eligible_query.where(
            OIDCIdempotencyRecord.service_id == service_id
        )

    eligible_ids = (
        eligible_query
        .order_by(OIDCIdempotencyRecord.expires_at.asc())
        .limit(batch_size)
    )

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(OIDCIdempotencyRecord).where(
                    OIDCIdempotencyRecord.id.in_(eligible_ids)
                )
            )
            return max(0, int(result.rowcount or 0))


async def run_oidc_transaction_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = OIDC_CLEANUP_INTERVAL_SECONDS,
    batch_size: int = OIDC_CLEANUP_BATCH_SIZE,
    sleep=asyncio.sleep,
) -> None:
    """Run bounded cleanup periodically until cancelled."""
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or interval_seconds < 1
    ):
        raise ValueError("interval_seconds must be at least one second.")

    while True:
        try:
            await cleanup_expired_oidc_transactions(
                session_factory,
                batch_size=batch_size,
            )
            await cleanup_expired_oidc_idempotency_records(
                session_factory,
                batch_size=batch_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transient database failures do not terminate the worker.
            pass

        await sleep(interval_seconds)

