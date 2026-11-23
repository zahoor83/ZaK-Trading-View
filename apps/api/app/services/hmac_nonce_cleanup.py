"""Bounded cleanup for expired internal-service HMAC replay nonces."""

from __future__ import annotations

import asyncio
from datetime import datetime
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.internal_service_replay import InternalServiceRequestNonce


HMAC_NONCE_CLEANUP_INTERVAL_SECONDS = 60
HMAC_NONCE_CLEANUP_BATCH_SIZE = 100
HMAC_NONCE_CLEANUP_MAX_BATCH_SIZE = 1000


def _validate_batch_size(batch_size: int) -> None:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise TypeError("batch_size must be an integer.")
    if not 1 <= batch_size <= HMAC_NONCE_CLEANUP_MAX_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 1000.")


def _validate_now(now: datetime | None) -> None:
    if now is None:
        return
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError("Cleanup time must be timezone-aware.")


async def cleanup_expired_hmac_nonces(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch_size: int = HMAC_NONCE_CLEANUP_BATCH_SIZE,
    service_id: str | None = None,
) -> int:
    """Delete one bounded batch of nonces whose acceptance window has ended.

    The database clock is authoritative by default. A timezone-aware ``now``
    may be supplied by deterministic tests.
    """
    _validate_batch_size(batch_size)
    _validate_now(now)
    if service_id is not None and (
        not isinstance(service_id, str) or not service_id.strip()
    ):
        raise ValueError("service_id must be a non-empty string when provided.")

    cutoff = func.current_timestamp() if now is None else now

    eligible_ids = (
        select(InternalServiceRequestNonce.id)
        .where(InternalServiceRequestNonce.expires_at <= cutoff)
        .order_by(InternalServiceRequestNonce.expires_at.asc())
        .limit(batch_size)
    )
    if service_id is not None:
        eligible_ids = eligible_ids.where(
            InternalServiceRequestNonce.service_id == service_id
        )

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(InternalServiceRequestNonce).where(
                    InternalServiceRequestNonce.id.in_(eligible_ids)
                )
            )
            return max(0, int(result.rowcount or 0))


async def run_hmac_nonce_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: float = HMAC_NONCE_CLEANUP_INTERVAL_SECONDS,
    batch_size: int = HMAC_NONCE_CLEANUP_BATCH_SIZE,
    sleep=asyncio.sleep,
) -> None:
    """Run bounded nonce cleanup periodically until cancelled."""
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or interval_seconds < 1
    ):
        raise ValueError("interval_seconds must be at least one second.")

    _validate_batch_size(batch_size)

    while True:
        try:
            await cleanup_expired_hmac_nonces(
                session_factory,
                batch_size=batch_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient database error must not permanently stop cleanup.
            pass

        await sleep(interval_seconds)
