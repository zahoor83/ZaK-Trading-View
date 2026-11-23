"""PostgreSQL-backed replay protection for authenticated internal requests."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class InternalServiceRequestNonce(Base):
    """Retain a signed request nonce for its complete acceptance window."""

    __tablename__ = "internal_service_request_nonces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )

    service_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    key_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    nonce_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "service_id",
            "key_id",
            "nonce_hash",
            name="uq_internal_service_nonce_identity",
        ),
        CheckConstraint(
            "length(service_id) BETWEEN 1 AND 128",
            name="ck_internal_service_nonce_service_length",
        ),
        CheckConstraint(
            "service_id = btrim(service_id)",
            name="ck_internal_service_nonce_service_trimmed",
        ),
        CheckConstraint(
            "key_id ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_internal_service_nonce_key_id",
        ),
        CheckConstraint(
            "nonce_hash ~ '^[0-9a-f]{64}$'",
            name="ck_internal_service_nonce_hash",
        ),
        CheckConstraint(
            "expires_at > signed_at",
            name="ck_internal_service_nonce_expiry_after_signed_at",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_internal_service_nonce_expiry_after_created_at",
        ),
        Index(
            "ix_internal_service_request_nonces_expires_at",
            "expires_at",
        ),
    )