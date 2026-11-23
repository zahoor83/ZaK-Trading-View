"""Atomic portfolio-scoped Paper Trading operations."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_ledger import (
    PaperExecutionRequest,
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperLedgerEntry,
    PaperLedgerTransaction,
)
from app.security.paper_idempotency import (
    PaperIdempotencyInputError,
    canonical_uuid_v4,
    fingerprint_request,
    hash_idempotency_key,
)
from app.security.paper_idempotency_decision import (
    IdempotencyDecisionError,
    IdempotencyDecisionKind,
    decide_idempotency,
)
from app.security.paper_write_authorization import (
    PaperWriteAuthorizationDenied,
    authorize_paper_write,
)
from app.security.permissions import Permission
from app.services.paper_ledger_rules import (
    JournalEntryInput,
    PaperLedgerRulesError,
    validate_decimal,
    validate_journal_entries,
    validate_transaction_journal,
)
from app.services.paper_ledger_validation import (
    PaperLedgerValidationError,
    validate_existing_paper_ledger,
)


INITIAL_BALANCE = Decimal("1000.00")
MANUAL_TOP_UP_THRESHOLD = Decimal("100.00")


class PaperPortfolioOperationError(RuntimeError):
    """A portfolio-level financial operation could not be completed safely."""


class PaperPortfolioIdempotencyConflict(PaperPortfolioOperationError):
    """A portfolio operation key was reused with different semantics."""


class PaperPortfolioAuthorizationDenied(PaperPortfolioOperationError):
    """The authenticated user is not permitted to perform this operation."""


@dataclass(frozen=True, slots=True)
class PaperPortfolioOperationResult:
    replayed: bool
    response_data: dict[str, Any]


def calculate_manual_top_up(
    current_balance: Decimal | str,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return exact (previous balance, top-up amount, resulting balance)."""
    try:
        previous_balance = validate_decimal(
            current_balance,
            allow_negative=True,
            allow_zero=True,
        )

        if previous_balance > MANUAL_TOP_UP_THRESHOLD:
            raise PaperPortfolioOperationError(
                "Manual Top-up is available only when cash balance is 100 USDT or less."
            )

        with localcontext() as context:
            context.prec = 80
            amount = INITIAL_BALANCE - previous_balance
            new_balance = previous_balance + amount

        amount = validate_decimal(
            amount,
            allow_negative=False,
            allow_zero=False,
        )
        new_balance = validate_decimal(
            new_balance,
            allow_negative=False,
            allow_zero=False,
        )

        if new_balance != INITIAL_BALANCE:
            raise PaperPortfolioOperationError(
                "Manual Top-up calculation did not reach the exact target balance."
            )

        return previous_balance, amount, new_balance

    except PaperLedgerRulesError as exc:
        raise PaperPortfolioOperationError(
            "The persisted cash balance or calculated top-up amount is invalid."
        ) from exc


async def manual_top_up_paper_portfolio_in_transaction(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    idempotency_key: str,
    confirmed: bool,
) -> PaperPortfolioOperationResult:
    """Restore eligible portfolio cash to 1,000 USDT exactly once.

    The caller owns the active database transaction. Ledger posting,
    snapshot-trigger updates and the portfolio idempotency record commit
    or roll back together.
    """
    if not session.in_transaction():
        raise PaperPortfolioOperationError(
            "An active caller-owned transaction is required."
        )

    if (
        not isinstance(authenticated_user_id, uuid.UUID)
        or not isinstance(account_id, uuid.UUID)
        or not isinstance(portfolio_id, uuid.UUID)
    ):
        raise PaperPortfolioOperationError("Invalid portfolio operation identifiers.")

    if confirmed is not True:
        raise PaperPortfolioOperationError(
            "Explicit confirmation is required for Manual Top-up."
        )

    try:
        canonical_key = canonical_uuid_v4(idempotency_key)
        hashed_key = hash_idempotency_key(canonical_key)
    except (PaperIdempotencyInputError, TypeError, ValueError) as exc:
        raise PaperPortfolioOperationError(
            "Invalid idempotency request key."
        ) from exc

    try:
        access = await authorize_paper_write(
            session,
            authenticated_user_id=authenticated_user_id,
            account_id=account_id,
            portfolio_id=portfolio_id,
            permission=Permission.MANUAL_TOP_UP,
        )
    except PaperWriteAuthorizationDenied as exc:
        raise PaperPortfolioAuthorizationDenied(
            "Paper Trading operation is not permitted."
        ) from exc

    try:
        request_fingerprint = fingerprint_request(
            "manual_top_up",
            {
                "account_id": str(access.account_id),
                "portfolio_id": str(access.portfolio_id),
                "market_type": access.market_type,
                "target_balance": format(INITIAL_BALANCE, "f"),
                "confirmed": True,
            },
        )
    except (PaperIdempotencyInputError, TypeError, ValueError) as exc:
        raise PaperPortfolioOperationError(
            "Manual Top-up request fingerprint could not be created."
        ) from exc

    # Validate the existing financial source of truth before any mutation.
    try:
        validated_book_id = await validate_existing_paper_ledger(
            session,
            account_id=access.account_id,
            portfolio_id=access.portfolio_id,
            session_id=access.session_id,
            market_type=access.market_type,
        )
    except (PaperLedgerValidationError, ValueError) as exc:
        raise PaperPortfolioOperationError(
            "The existing Paper Trading ledger is inconsistent."
        ) from exc

    if validated_book_id != access.book_id:
        raise PaperPortfolioOperationError(
            "The active ledger book changed during authorization."
        )

    # Portfolio-scoped idempotency is checked before balance eligibility.
    request_result = await session.execute(
        select(PaperExecutionRequest).where(
            PaperExecutionRequest.scope_type == "portfolio",
            PaperExecutionRequest.portfolio_id == access.portfolio_id,
            PaperExecutionRequest.idempotency_key == hashed_key,
        )
    )
    existing_request = request_result.scalar_one_or_none()

    try:
        decision = decide_idempotency(
            existing_request_found=existing_request is not None,
            existing_fingerprint=(
                existing_request.request_fingerprint
                if existing_request is not None
                else None
            ),
            existing_response=(
                existing_request.response_data
                if existing_request is not None
                else None
            ),
            requested_fingerprint=request_fingerprint,
        )
    except IdempotencyDecisionError as exc:
        raise PaperPortfolioOperationError(
            "Persisted Manual Top-up idempotency data is invalid."
        ) from exc

    if decision.kind is IdempotencyDecisionKind.CONFLICT:
        raise PaperPortfolioIdempotencyConflict(
            "Idempotency key was already used for a different request."
        )

    if decision.kind is IdempotencyDecisionKind.REPLAY:
        if decision.response_data is None:
            raise PaperPortfolioOperationError(
                "Persisted Manual Top-up response is unexpectedly missing."
            )
        return PaperPortfolioOperationResult(
            replayed=True,
            response_data=decision.response_data,
        )

    # Read the six validated accounts and their corresponding snapshots.
    account_result = await session.execute(
        select(PaperLedgerAccount, PaperLedgerAccountSnapshot)
        .join(
            PaperLedgerAccountSnapshot,
            PaperLedgerAccountSnapshot.ledger_account_id
            == PaperLedgerAccount.id,
        )
        .where(
            PaperLedgerAccount.book_id == access.book_id,
            PaperLedgerAccountSnapshot.book_id == access.book_id,
        )
    )
    account_rows = list(account_result.all())

    if len(account_rows) != 6:
        raise PaperPortfolioOperationError(
            "The active ledger account and snapshot structure is incomplete."
        )

    accounts_by_code: dict[str, PaperLedgerAccount] = {}
    snapshots_by_code: dict[str, PaperLedgerAccountSnapshot] = {}

    for ledger_account, snapshot in account_rows:
        if (
            ledger_account.code in accounts_by_code
            or ledger_account.market_type != access.market_type
            or snapshot.book_id != access.book_id
            or snapshot.ledger_account_id != ledger_account.id
        ):
            raise PaperPortfolioOperationError(
                "The active ledger account and snapshot structure is inconsistent."
            )

        accounts_by_code[ledger_account.code] = ledger_account
        snapshots_by_code[ledger_account.code] = snapshot

    if len(accounts_by_code) != 6 or "cash_available" not in snapshots_by_code:
        raise PaperPortfolioOperationError(
            "The cash account or mandatory ledger accounts are missing."
        )

    previous_balance, amount, new_balance = calculate_manual_top_up(
        snapshots_by_code["cash_available"].balance
    )

    entries = (
        JournalEntryInput(
            account_code="cash_available",
            direction="debit",
            amount=amount,
        ),
        JournalEntryInput(
            account_code="contributed_capital",
            direction="credit",
            amount=amount,
        ),
    )

    try:
        journal = validate_journal_entries(access.market_type, entries)
        journal = validate_transaction_journal("manual_top_up", journal)
    except PaperLedgerRulesError as exc:
        raise PaperPortfolioOperationError(
            "Manual Top-up journal validation failed."
        ) from exc

    # Detect a conflicting or orphaned book-level ledger transaction.
    orphan_result = await session.execute(
        select(PaperLedgerTransaction.id).where(
            PaperLedgerTransaction.book_id == access.book_id,
            PaperLedgerTransaction.idempotency_key == hashed_key,
        )
    )
    if orphan_result.scalar_one_or_none() is not None:
        raise PaperPortfolioOperationError(
            "An orphaned ledger transaction uses this idempotency key."
        )

    transaction = PaperLedgerTransaction(
        book_id=access.book_id,
        idempotency_key=hashed_key,
        request_fingerprint=request_fingerprint,
        transaction_type="manual_top_up",
        status="draft",
        posted_at=None,
    )
    session.add(transaction)
    await session.flush()

    ledger_entries: list[PaperLedgerEntry] = []
    for item in journal.entries:
        ledger_account = accounts_by_code.get(item.account_code)
        if ledger_account is None:
            raise PaperPortfolioOperationError(
                "A required Manual Top-up ledger account is missing."
            )

        ledger_entries.append(
            PaperLedgerEntry(
                book_id=access.book_id,
                transaction_id=transaction.id,
                ledger_account_id=ledger_account.id,
                direction=item.direction,
                amount=item.amount,
            )
        )

    session.add_all(ledger_entries)
    await session.flush()

    # The database's posted-transaction trigger applies the snapshot delta.
    transaction.status = "posted"
    await session.flush()

    # Read the persisted snapshot again; do not trust an in-memory calculation.
    snapshot_result = await session.execute(
        select(PaperLedgerAccountSnapshot.balance).where(
            PaperLedgerAccountSnapshot.book_id == access.book_id,
            PaperLedgerAccountSnapshot.ledger_account_id
            == accounts_by_code["cash_available"].id,
        )
    )
    persisted_balance = snapshot_result.scalar_one_or_none()

    try:
        persisted_balance = validate_decimal(
            persisted_balance,
            allow_negative=False,
            allow_zero=False,
        )
    except PaperLedgerRulesError as exc:
        raise PaperPortfolioOperationError(
            "The posted Manual Top-up snapshot is invalid."
        ) from exc

    if persisted_balance != new_balance:
        raise PaperPortfolioOperationError(
            "Manual Top-up snapshot verification failed; transaction must roll back."
        )

    response_data: dict[str, Any] = {
        "transaction_id": str(transaction.id),
        "book_id": str(access.book_id),
        "market_type": access.market_type,
        "transaction_type": "manual_top_up",
        "status": "posted",
        "entry_count": len(journal.entries),
        "debit_total": format(journal.debit_total, "f"),
        "credit_total": format(journal.credit_total, "f"),
        "amount": format(amount, "f"),
        "previous_balance": format(previous_balance, "f"),
        "new_balance": format(persisted_balance, "f"),
    }

    try:
        json.dumps(response_data, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperPortfolioOperationError(
            "Manual Top-up response is not JSON-compatible."
        ) from exc

    execution_request = PaperExecutionRequest(
        scope_type="portfolio",
        account_id=access.account_id,
        book_id=None,
        portfolio_id=access.portfolio_id,
        operation_type="manual_top_up",
        idempotency_key=hashed_key,
        request_fingerprint=request_fingerprint,
        status="completed",
        response_data=response_data,
    )
    session.add(execution_request)
    await session.flush()

    return PaperPortfolioOperationResult(
        replayed=False,
        response_data=response_data,
    )


async def manual_top_up_paper_portfolio(
    session: AsyncSession,
    **kwargs: Any,
) -> PaperPortfolioOperationResult:
    """Run Manual Top-up in a transaction owned by this wrapper."""
    async with session.begin():
        return await manual_top_up_paper_portfolio_in_transaction(
            session,
            **kwargs,
        )

