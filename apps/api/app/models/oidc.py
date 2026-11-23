"""Server-side OIDC authorization transaction model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class OIDCAuthTransaction(Base):
    """Short-lived, single-use OIDC authorization transaction."""

    __tablename__ = "oidc_auth_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    state_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )

    nonce_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    browser_binding_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    code_verifier_nonce: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
    )

    code_verifier_ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
    )

    encryption_key_id: Mapped[str] = mapped_column(
        String(64),
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

    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    __table_args__ = (
        CheckConstraint(
            "state_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_transactions_state_hash",
        ),
        CheckConstraint(
            "nonce_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_transactions_nonce_hash",
        ),
        CheckConstraint(
            "browser_binding_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_transactions_browser_hash",
        ),
        CheckConstraint(
            "octet_length(code_verifier_nonce) = 12",
            name="ck_oidc_transactions_gcm_nonce_length",
        ),
        CheckConstraint(
            "octet_length(code_verifier_ciphertext) > 16",
            name="ck_oidc_transactions_ciphertext_length",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_oidc_transactions_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at",
            name="ck_oidc_transactions_consumed_time",
        ),
        Index(
            "ix_oidc_auth_transactions_expires_at",
            "expires_at",
        ),
    )

class OIDCIdempotencyRecord(Base):
    """Encrypted idempotency response retained for its reservation period."""

    __tablename__ = "oidc_idempotency_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    service_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    idempotency_key_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    request_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oidc_auth_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )

    response_nonce: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
    )

    response_ciphertext: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
    )

    encryption_key_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'completed'"),
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
            "idempotency_key_hash",
            name="uq_oidc_idempotency_service_key",
        ),
        CheckConstraint(
            "length(service_id) BETWEEN 1 AND 128",
            name="ck_oidc_idempotency_service_length",
        ),
        CheckConstraint(
            "service_id = btrim(service_id)",
            name="ck_oidc_idempotency_service_trimmed",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_idempotency_key_hash",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_oidc_idempotency_fingerprint",
        ),
        CheckConstraint(
            "octet_length(response_nonce) = 12",
            name="ck_oidc_idempotency_nonce_length",
        ),
        CheckConstraint(
            "octet_length(response_ciphertext) > 16",
            name="ck_oidc_idempotency_ciphertext_length",
        ),
        CheckConstraint(
            "encryption_key_id ~ '^[A-Za-z0-9._-]{1,64}$'",
            name="ck_oidc_idempotency_encryption_key_id",
        ),
        CheckConstraint(
            "status = 'completed'",
            name="ck_oidc_idempotency_status",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_oidc_idempotency_expiry",
        ),
        Index(
            "ix_oidc_idempotency_records_expires_at",
            "expires_at",
        ),
    )