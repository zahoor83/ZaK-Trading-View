"""Persistent, account-scoped Spot/Futures fee-policy versions."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PaperFeePolicyVersion(Base):
    """Immutable fee-policy version. Never update or delete a version."""

    __tablename__ = "paper_fee_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(String(10), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_fee_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    exit_fee_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id", "market_type", "version",
            name="uq_paper_fee_policy_versions_account_market_version",
        ),
        UniqueConstraint(
            "id", "account_id", "market_type", "version",
            name="uq_paper_fee_policy_versions_pointer_target",
        ),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_fee_policy_versions_market",
        ),
        CheckConstraint(
            "length(policy_id) BETWEEN 1 AND 64",
            name="ck_paper_fee_policy_versions_policy_id",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_paper_fee_policy_versions_version",
        ),
        CheckConstraint(
            "entry_fee_rate >= 0 AND entry_fee_rate < 1e36 "
            "AND scale(entry_fee_rate) <= 36 "
            "AND exit_fee_rate >= 0 AND exit_fee_rate < 1e36 "
            "AND scale(exit_fee_rate) <= 36",
            name="ck_paper_fee_policy_versions_rates",
        ),
    )


class PaperFeePolicyHead(Base):
    """Mutable pointer to the current immutable policy version."""

    __tablename__ = "paper_fee_policy_heads"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    market_type: Mapped[str] = mapped_column(String(10), primary_key=True)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "account_id", "market_type", "version"],
            [
                "paper_fee_policy_versions.id",
                "paper_fee_policy_versions.account_id",
                "paper_fee_policy_versions.market_type",
                "paper_fee_policy_versions.version",
            ],
            name="fk_paper_fee_policy_heads_current_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_fee_policy_heads_market",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_paper_fee_policy_heads_version",
        ),
    )
