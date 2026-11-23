"""Independent Spot/Futures paper portfolios and trading sessions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class PaperPortfolio(Base):
    __tablename__ = "paper_portfolios"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )
    position_mode: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "id",
            name="uq_paper_portfolios_account_id_id",
        ),
        UniqueConstraint(
            "account_id",
            "market_type",
            name="uq_paper_portfolios_account_market",
        ),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_portfolios_market_type",
        ),
        CheckConstraint(
            "(market_type = 'spot' AND position_mode IS NULL) OR "
            "(market_type = 'futures' AND "
            "position_mode IN ('one_way', 'hedge'))",
            name="ck_paper_portfolios_position_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_paper_portfolios_status",
        ),
        Index(
            "ix_paper_portfolios_account_status",
            "account_id",
            "status",
        ),
    )


class PaperSession(Base):
    __tablename__ = "paper_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    session_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "portfolio_id"],
            ["paper_portfolios.account_id", "paper_portfolios.id"],
            name="fk_paper_sessions_account_portfolio",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "account_id",
            "portfolio_id",
            "session_number",
            name="uq_paper_sessions_portfolio_number",
        ),
        UniqueConstraint(
            "account_id",
            "portfolio_id",
            "id",
            name="uq_paper_sessions_account_portfolio_id",
        ),
        CheckConstraint(
            "session_number > 0",
            name="ck_paper_sessions_number_positive",
        ),
        CheckConstraint(
            "(status = 'active' AND archived_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL)",
            name="ck_paper_sessions_archive_state",
        ),
        Index(
            "uq_paper_sessions_one_active",
            "portfolio_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_paper_sessions_account_portfolio",
            "account_id",
            "portfolio_id",
        ),
    )