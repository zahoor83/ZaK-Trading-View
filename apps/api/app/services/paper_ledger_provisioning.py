"""Transaction-bound provisioning of independent paper-trading ledgers."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_ledger import (
    PaperLedgerAccount,
    PaperLedgerBook,
    PaperLedgerEntry,
    PaperLedgerTransaction,
)


INITIAL_BALANCE = Decimal("1000.00")
INITIAL_FUNDING_KEY = "initial-funding:v1"

MANDATORY_ACCOUNTS = (
    ("cash_available", "debit"),
    ("spot_position_cost", "debit"),
    ("futures_margin_locked", "debit"),
    ("realized_pnl", "credit"),
    ("fee_expense", "debit"),
    ("contributed_capital", "credit"),
)


class PaperLedgerProvisioningError(RuntimeError):
    """Ledger provisioning failed or an existing ledger needs review."""


def _initial_funding_fingerprint(
    *,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    session_id: uuid.UUID,
    market_type: str,
) -> str:
    canonical = (
        "paper-ledger-initial-funding-v1|"
        f"{account_id}|{portfolio_id}|{session_id}|"
        f"{market_type}|{INITIAL_BALANCE}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def provision_initial_paper_ledger(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    session_id: uuid.UUID,
    market_type: str,
) -> uuid.UUID:
    """Create one independent book, six accounts, snapshots and initial funding.

    The caller owns the surrounding database transaction.
    This function never begins, commits, or rolls back a transaction.

    Existing books are rejected. This function is only for a newly-created
    portfolio session; it never repairs or automatically funds existing books.
    """

    if not session.in_transaction():
        raise PaperLedgerProvisioningError(
            "Ledger provisioning requires an active caller-owned transaction."
        )
    if market_type not in {"spot", "futures"}:
        raise ValueError("market_type must be 'spot' or 'futures'.")

    existing_result = await session.execute(
        select(PaperLedgerBook).where(
            PaperLedgerBook.account_id == account_id,
            PaperLedgerBook.portfolio_id == portfolio_id,
            PaperLedgerBook.session_id == session_id,
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise PaperLedgerProvisioningError(
            "A ledger book already exists for this portfolio session. "
            "Existing ledgers must be validated, never re-provisioned."
        )

    book = PaperLedgerBook(
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=session_id,
        market_type=market_type,
        status="active",
    )
    session.add(book)
    await session.flush()

    accounts_by_code = {
        code: PaperLedgerAccount(
            book_id=book.id,
            market_type=market_type,
            code=code,
            normal_side=normal_side,
        )
        for code, normal_side in MANDATORY_ACCOUNTS
    }
    session.add_all(list(accounts_by_code.values()))
    await session.flush()


    transaction = PaperLedgerTransaction(
        book_id=book.id,
        idempotency_key=INITIAL_FUNDING_KEY,
        request_fingerprint=_initial_funding_fingerprint(
            account_id=account_id,
            portfolio_id=portfolio_id,
            session_id=session_id,
            market_type=market_type,
        ),
        transaction_type="initial_funding",
        status="draft",
        posted_at=None,
    )
    session.add(transaction)
    await session.flush()

    session.add_all(
        [
            PaperLedgerEntry(
                book_id=book.id,
                transaction_id=transaction.id,
                ledger_account_id=accounts_by_code["cash_available"].id,
                direction="debit",
                amount=INITIAL_BALANCE,
            ),
            PaperLedgerEntry(
                book_id=book.id,
                transaction_id=transaction.id,
                ledger_account_id=accounts_by_code["contributed_capital"].id,
                direction="credit",
                amount=INITIAL_BALANCE,
            ),
        ]
    )
    await session.flush()

    # The journal is posted only after both entries have been inserted.
    transaction.status = "posted"
    # Database triggers assign posted_at and update snapshots.
    await session.flush()

    return book.id
