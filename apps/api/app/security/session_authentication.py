"""Provider-neutral opaque session authentication primitives."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db_session
from app.models.identity import User, UserSession


SESSION_COOKIE_NAME = "zak_session"
SESSION_TOKEN_MIN_LENGTH = 32
SESSION_TOKEN_MAX_LENGTH = 512


@dataclass(frozen=True, slots=True)
class GeneratedSessionToken:
    """Raw token is returned only to the caller for secure cookie delivery."""

    raw_token: str
    token_hash: str


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Trusted identity established from a valid database session."""

    user_id: UUID
    session_id: UUID
    email: str
    email_verified: bool
    expires_at: datetime


def hash_session_token(token: str) -> str:
    """Return a SHA-256 digest for a valid opaque session token."""

    if not isinstance(token, str):
        raise ValueError("Invalid session token.")

    if not SESSION_TOKEN_MIN_LENGTH <= len(token) <= SESSION_TOKEN_MAX_LENGTH:
        raise ValueError("Invalid session token.")

    if not token.isascii() or any(character.isspace() for character in token):
        raise ValueError("Invalid session token.")

    return hashlib.sha256(token.encode("ascii")).hexdigest()


def generate_session_token() -> GeneratedSessionToken:
    """Generate a cryptographically secure opaque session token."""

    raw_token = secrets.token_urlsafe(32)

    return GeneratedSessionToken(
        raw_token=raw_token,
        token_hash=hash_session_token(raw_token),
    )


def _valid_aware_datetime(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


async def load_authenticated_user(
    session: AsyncSession,
    raw_token: str,
    *,
    now: datetime | None = None,
) -> AuthenticatedUser | None:
    """Resolve a raw session token into a trusted active user identity.

    This function is read-only. It never creates, commits, revokes,
    refreshes, or extends a database session.
    """

    try:
        token_hash = hash_session_token(raw_token)
    except ValueError:
        return None

    current_time = now if now is not None else datetime.now(timezone.utc)

    if not _valid_aware_datetime(current_time):
        raise ValueError("now must be timezone-aware.")

    statement = (
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.token_hash == token_hash,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > current_time,
            User.status == "active",
            User.email_verified.is_(True),
        )
    )

    result = await session.execute(statement)
    row = result.one_or_none()

    if row is None:
        return None

    user_session, user = row

    # Revalidate returned data. Fail closed on inconsistent or malformed rows.
    if user_session.revoked_at is not None:
        return None

    if not _valid_aware_datetime(user_session.expires_at):
        return None

    if user_session.expires_at <= current_time:
        return None

    if user_session.token_hash != token_hash:
        return None

    if user.status != "active" or user.email_verified is not True:
        return None

    if not isinstance(user.id, UUID) or not isinstance(user_session.id, UUID):
        return None

    if user_session.user_id != user.id:
        return None

    if not isinstance(user.email, str) or not user.email:
        return None

    return AuthenticatedUser(
        user_id=user.id,
        session_id=user_session.id,
        email=user.email,
        email_verified=True,
        expires_at=user_session.expires_at,
    )


async def load_session_owner_id(
    session: AsyncSession,
    raw_token: str,
) -> UUID | None:
    """Resolve a token's owner without requiring an unexpired session.

    This is used only by the trusted internal revocation endpoint.
    The raw token is hashed before any database lookup.
    """

    try:
        token_hash = hash_session_token(raw_token)
    except ValueError:
        return None

    result = await session.execute(
        select(UserSession.user_id).where(
            UserSession.token_hash == token_hash
        )
    )
    owner_id = result.scalar_one_or_none()

    return owner_id if isinstance(owner_id, UUID) else None


async def get_authenticated_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> AuthenticatedUser:
    """FastAPI dependency requiring a valid opaque session cookie."""

    raw_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"Cache-Control": "no-store"},
        )

    authenticated_user = await load_authenticated_user(
        session,
        raw_token,
    )

    if authenticated_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"Cache-Control": "no-store"},
        )

    return authenticated_user