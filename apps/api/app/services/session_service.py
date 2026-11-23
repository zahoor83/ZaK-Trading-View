"""Atomic lifecycle operations for opaque user sessions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import User, UserSession
from app.security.session_authentication import generate_session_token, hash_session_token


class SessionServiceError(Exception):
    """Base exception for session lifecycle failures."""


class InvalidSessionExpiryError(SessionServiceError, ValueError):
    """The requested expiry is invalid."""


class InvalidSessionUserError(SessionServiceError, ValueError):
    """The supplied user identifier is invalid."""


class SessionUserNotEligibleError(SessionServiceError):
    """The user is missing, inactive or email-unverified."""


class SessionTransactionOwnershipError(SessionServiceError):
    """The session service requires a fresh database session."""


@dataclass(frozen=True, slots=True)
class SessionCreationResult:
    session_id: uuid.UUID
    raw_token: str
    expires_at: datetime


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidSessionExpiryError(
            f"{field_name} must be a timezone-aware datetime."
        )


def _validate_user_id(user_id: uuid.UUID) -> None:
    if not isinstance(user_id, uuid.UUID):
        raise InvalidSessionUserError("user_id must be a UUID.")


def _validate_transaction_ownership(session: AsyncSession) -> None:
    if session.in_transaction():
        raise SessionTransactionOwnershipError(
            "Session lifecycle operations require a fresh session "
            "with no active transaction."
        )


async def create_user_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    expires_at: datetime,
    now: datetime | None = None,
) -> SessionCreationResult:
    """Create a new opaque session for an active verified user.

    The service owns the transaction. The raw token is returned exactly
    once and is never persisted in the database.
    """

    _validate_user_id(user_id)

    current_time = now if now is not None else datetime.now(timezone.utc)

    _require_aware_datetime(current_time, "now")
    _require_aware_datetime(expires_at, "expires_at")

    if expires_at <= current_time:
        raise InvalidSessionExpiryError(
            "expires_at must be later than now."
        )

    _validate_transaction_ownership(session)

    generated = generate_session_token()

    async with session.begin():
        user_result = await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
        )

        user = user_result.scalar_one_or_none()

        if user is None:
            raise SessionUserNotEligibleError(
                "The session user does not exist or is not eligible."
            )

        if user.status != "active" or user.email_verified is not True:
            raise SessionUserNotEligibleError(
                "The session user does not exist or is not eligible."
            )

        user_session = UserSession(
            user_id=user_id,
            token_hash=generated.token_hash,
            expires_at=expires_at,
        )

        session.add(user_session)
        await session.flush()

        if not isinstance(user_session.id, uuid.UUID):
            raise SessionServiceError(
                "The database did not provide a valid session ID."
            )

        result = SessionCreationResult(
            session_id=user_session.id,
            raw_token=generated.raw_token,
            expires_at=expires_at,
        )

    return result


async def revoke_user_session(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    raw_token: str,
    now: datetime | None = None,
) -> bool:
    """Revoke one session belonging to the authenticated user.

    Returns True only when this call changes an unrevoked session.
    Returns False for unknown, foreign or already-revoked sessions.
    Expired sessions may also be revoked.

    The service owns the transaction.
    """

    _validate_user_id(authenticated_user_id)

    current_time = now if now is not None else datetime.now(timezone.utc)
    _require_aware_datetime(current_time, "now")

    try:
        token_hash = hash_session_token(raw_token)
    except ValueError:
        return False

    _validate_transaction_ownership(session)

    changed = False

    async with session.begin():
        result = await session.execute(
            select(UserSession)
            .where(
                UserSession.user_id == authenticated_user_id,
                UserSession.token_hash == token_hash,
            )
            .with_for_update()
        )

        user_session = result.scalar_one_or_none()

        if user_session is not None and user_session.revoked_at is None:
            user_session.revoked_at = current_time
            await session.flush()
            changed = True

    return changed