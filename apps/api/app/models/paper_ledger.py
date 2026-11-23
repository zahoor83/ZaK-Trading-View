"""Independent Spot/Futures paper-trading financial ledger models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


# Numeric values deliberately have no fixed precision/scale typmod.
# Database and service validation enforce the approved Decimal limits.


class PaperLedgerBook(Base):
    """One independent financial book for one portfolio session."""

    __tablename__ = "paper_ledger_books"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trading_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(String(10), nullable=False)
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
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "portfolio_id"],
            ["paper_portfolios.account_id", "paper_portfolios.id"],
            name="fk_paper_ledger_books_account_portfolio",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "portfolio_id", "session_id"],
            [
                "paper_sessions.account_id",
                "paper_sessions.portfolio_id",
                "paper_sessions.id",
            ],
            name="fk_paper_ledger_books_account_portfolio_session",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "account_id",
            "portfolio_id",
            "session_id",
            name="uq_paper_ledger_books_portfolio_session",
        ),
        UniqueConstraint(
            "id",
            "market_type",
            name="uq_paper_ledger_books_id_market",
        ),
        UniqueConstraint(
            "account_id",
            "id",
            name="uq_paper_ledger_books_account_id_id",
        ),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_ledger_books_market",
        ),
        CheckConstraint(
            "(status = 'active' AND archived_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL)",
            name="ck_paper_ledger_books_archive_state",
        ),
        Index(
            "ix_paper_ledger_books_account_portfolio",
            "account_id",
            "portfolio_id",
        ),
    )


class PaperLedgerAccount(Base):
    """One of the six controlled accounting accounts in a book."""

    __tablename__ = "paper_ledger_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(String(10), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    normal_side: Mapped[str] = mapped_column(String(6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["book_id", "market_type"],
            ["paper_ledger_books.id", "paper_ledger_books.market_type"],
            name="fk_paper_ledger_accounts_book_market",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "book_id",
            "code",
            name="uq_paper_ledger_accounts_book_code",
        ),
        UniqueConstraint(
            "book_id",
            "id",
            name="uq_paper_ledger_accounts_book_id",
        ),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_ledger_accounts_market",
        ),
        CheckConstraint(
            "code IN ('cash_available', 'spot_position_cost', "
            "'futures_margin_locked', 'realized_pnl', 'fee_expense', "
            "'contributed_capital')",
            name="ck_paper_ledger_accounts_code",
        ),
        CheckConstraint(
            "(code IN ('realized_pnl', 'contributed_capital') "
            "AND normal_side = 'credit') OR "
            "(code IN ("
            "'cash_available', 'spot_position_cost', "
            "'futures_margin_locked', 'fee_expense'"
            ") AND normal_side = 'debit')",
            name="ck_paper_ledger_accounts_normal_side",
        ),

    )


class PaperLedgerTransaction(Base):
    """Journal transaction header; draft is posted before commit."""

    __tablename__ = "paper_ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("paper_ledger_books.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    transaction_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        server_default=text("'draft'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "book_id",
            "id",
            name="uq_paper_ledger_transactions_book_id",
        ),
        UniqueConstraint(
            "book_id",
            "idempotency_key",
            name="uq_paper_ledger_transactions_book_key",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_paper_ledger_transactions_key_length",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_paper_ledger_transactions_fingerprint",
        ),
        CheckConstraint(
            "transaction_type IN ("
            "'initial_funding', 'spot_buy', 'spot_sell', "
            "'futures_open', 'futures_close', "
            "'manual_top_up', 'correction'"
            ")",
            name="ck_paper_ledger_transactions_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'posted')",
            name="ck_paper_ledger_transactions_status",
        ),
        CheckConstraint(
            "(status = 'draft' AND posted_at IS NULL) OR "
            "(status = 'posted' AND posted_at IS NOT NULL)",
            name="ck_paper_ledger_transactions_posting_time",
        ),
        Index(
            "ix_paper_ledger_transactions_book_created",
            "book_id",
            "created_at",
        ),
        Index(
            "uq_paper_ledger_transactions_one_initial_funding",
            "book_id",
            unique=True,
            postgresql_where=text("transaction_type = 'initial_funding'"),
        ),
    )


class PaperLedgerEntry(Base):
    """Positive debit or credit amount belonging to one journal book."""

    __tablename__ = "paper_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["book_id", "transaction_id"],
            [
                "paper_ledger_transactions.book_id",
                "paper_ledger_transactions.id",
            ],
            name="fk_paper_ledger_entries_transaction_book",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["book_id", "ledger_account_id"],
            [
                "paper_ledger_accounts.book_id",
                "paper_ledger_accounts.id",
            ],
            name="fk_paper_ledger_entries_account_book",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "direction IN ('debit', 'credit')",
            name="ck_paper_ledger_entries_direction",
        ),
        CheckConstraint(
            "amount > 0 AND amount < 1e36 AND scale(amount) <= 36",
            name="ck_paper_ledger_entries_amount",
        ),
        Index(
            "ix_paper_ledger_entries_transaction",
            "book_id",
            "transaction_id",
        ),
        Index(
            "ix_paper_ledger_entries_account",
            "book_id",
            "ledger_account_id",
        ),
    )


class PaperLedgerAccountSnapshot(Base):
    """Derived signed normal-balance read model for one ledger account."""

    __tablename__ = "paper_ledger_account_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
        server_default=text("0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["book_id", "ledger_account_id"],
            [
                "paper_ledger_accounts.book_id",
                "paper_ledger_accounts.id",
            ],
            name="fk_paper_ledger_snapshots_account_book",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "book_id",
            "ledger_account_id",
            name="uq_paper_ledger_snapshots_account",
        ),
        CheckConstraint(
            "balance > -1e36 AND balance < 1e36 "
            "AND scale(balance) <= 36",
            name="ck_paper_ledger_snapshots_balance",
        ),
    )


class PaperPosition(Base):
    """Spot or Futures position with frozen entry fee configuration."""

    __tablename__ = "paper_positions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    book_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    market_type: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    leverage_snapshot: Mapped[Decimal | None] = mapped_column(Numeric)

    initial_quantity: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )
    remaining_quantity: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )
    entry_price: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )

    remaining_cost_basis: Mapped[Decimal | None] = mapped_column(Numeric)
    margin_locked: Mapped[Decimal | None] = mapped_column(Numeric)

    entry_fee_original: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )
    entry_fee_posted: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )
    entry_fee_rate: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )
    exit_fee_rate: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
    )
    fee_config_snapshot: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )
    symbol_metadata_snapshot: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )

    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric,
        nullable=False,
        server_default=text("0"),
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        server_default=text("'open'"),
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["book_id", "market_type"],
            ["paper_ledger_books.id", "paper_ledger_books.market_type"],
            name="fk_paper_positions_book_market",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "market_type IN ('spot', 'futures')",
            name="ck_paper_positions_market",
        ),
        CheckConstraint(
            "side IN ('long', 'short')",
            name="ck_paper_positions_side",
        ),
        CheckConstraint(
            "(market_type = 'spot' AND side = 'long' "
            "AND remaining_cost_basis IS NOT NULL "
            "AND margin_locked IS NULL AND leverage_snapshot IS NULL) "
            "OR "
            "(market_type = 'futures' "
            "AND remaining_cost_basis IS NULL "
            "AND margin_locked IS NOT NULL "
            "AND leverage_snapshot IS NOT NULL "
            "AND leverage_snapshot > 0 "
            "AND leverage_snapshot < 1e36 "
            "AND scale(leverage_snapshot) <= 36)",
            name="ck_paper_positions_market_fields",
        ),
        CheckConstraint(
            "length(symbol) BETWEEN 1 AND 64",
            name="ck_paper_positions_symbol",
        ),
        CheckConstraint(
            "jsonb_typeof(symbol_metadata_snapshot) = 'object' "
            "AND symbol_metadata_snapshot <> '{}'::jsonb",
            name="ck_paper_positions_symbol_metadata",
        ),
        CheckConstraint(
            "initial_quantity > 0 AND initial_quantity < 1e36 "
            "AND scale(initial_quantity) <= 36",
            name="ck_paper_positions_initial_quantity",
        ),
        CheckConstraint(
            "remaining_quantity >= 0 AND remaining_quantity < 1e36 "
            "AND scale(remaining_quantity) <= 36 "
            "AND remaining_quantity <= initial_quantity",
            name="ck_paper_positions_remaining_quantity",
        ),
        CheckConstraint(
            "entry_price > 0 AND entry_price < 1e36 "
            "AND scale(entry_price) <= 36",
            name="ck_paper_positions_entry_price",
        ),
        CheckConstraint(
            "(remaining_cost_basis IS NULL OR "
            "(remaining_cost_basis >= 0 AND remaining_cost_basis < 1e36 "
            "AND scale(remaining_cost_basis) <= 36)) "
            "AND "
            "(margin_locked IS NULL OR "
            "(margin_locked >= 0 AND margin_locked < 1e36 "
            "AND scale(margin_locked) <= 36))",
            name="ck_paper_positions_cost_margin",
        ),
        CheckConstraint(
            "entry_fee_original >= 0 AND entry_fee_original < 1e36 "
            "AND scale(entry_fee_original) <= 36 "
            "AND entry_fee_posted >= 0 AND entry_fee_posted < 1e36 "
            "AND scale(entry_fee_posted) <= 2",
            name="ck_paper_positions_entry_fees",
        ),
        CheckConstraint(
            "entry_fee_rate >= 0 AND entry_fee_rate < 1e36 "
            "AND scale(entry_fee_rate) <= 36 "
            "AND exit_fee_rate >= 0 AND exit_fee_rate < 1e36 "
            "AND scale(exit_fee_rate) <= 36",
            name="ck_paper_positions_fee_rates",
        ),
        CheckConstraint(
            "realized_pnl > -1e36 AND realized_pnl < 1e36 "
            "AND scale(realized_pnl) <= 36",
            name="ck_paper_positions_realized_pnl",
        ),
        CheckConstraint(
            "(status = 'open' AND closed_at IS NULL "
            "AND remaining_quantity > 0) OR "
            "(status = 'closed' AND closed_at IS NOT NULL "
            "AND remaining_quantity = 0)",
            name="ck_paper_positions_lifecycle",
        ),
        Index(
            "ix_paper_positions_book_status",
            "book_id",
            "status",
        ),
        Index(
            "ix_paper_positions_book_symbol",
            "book_id",
            "symbol",
        ),
    )


class PaperExecutionRequest(Base):
    """Idempotency record for book-scoped trades or portfolio operations."""

    __tablename__ = "paper_execution_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    scope_type: Mapped[str] = mapped_column(String(12), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    book_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    operation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    request_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        server_default=text("'completed'"),
    )
    response_data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "book_id"],
            ["paper_ledger_books.account_id", "paper_ledger_books.id"],
            name="fk_paper_execution_requests_account_book",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "portfolio_id"],
            ["paper_portfolios.account_id", "paper_portfolios.id"],
            name="fk_paper_execution_requests_account_portfolio",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(scope_type = 'book' AND book_id IS NOT NULL "
            "AND portfolio_id IS NULL) OR "
            "(scope_type = 'portfolio' AND portfolio_id IS NOT NULL "
            "AND book_id IS NULL)",
            name="ck_paper_execution_requests_scope",
        ),
        CheckConstraint(
            "(scope_type = 'book' AND operation_type IN "
            "('trade', 'journal')) OR "
            "(scope_type = 'portfolio' AND operation_type IN "
            "('reset', 'manual_top_up'))",
            name="ck_paper_execution_requests_operation_scope",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_paper_execution_requests_key_length",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_paper_execution_requests_fingerprint",
        ),
        CheckConstraint(
            "status = 'completed'",
            name="ck_paper_execution_requests_completed",
        ),
        Index(
            "uq_paper_execution_requests_book_key",
            "book_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("scope_type = 'book'"),
        ),
        Index(
            "uq_paper_execution_requests_portfolio_key",
            "portfolio_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("scope_type = 'portfolio'"),
        ),
        Index(
            "ix_paper_execution_requests_created",
            "created_at",
        ),
    )


