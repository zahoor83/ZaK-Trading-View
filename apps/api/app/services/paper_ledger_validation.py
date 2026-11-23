"""Fail-closed validation and reconciliation of existing paper ledgers."""

from __future__ import annotations

import uuid
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_ledger import (
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperLedgerBook,
    PaperLedgerEntry,
    PaperLedgerTransaction,
)
from app.services.paper_ledger_provisioning import (
    INITIAL_BALANCE,
    INITIAL_FUNDING_KEY,
    MANDATORY_ACCOUNTS,
    _initial_funding_fingerprint,
)
from app.services.paper_ledger_rules import (
    PaperLedgerRulesError,
    validate_decimal,
)


class PaperLedgerValidationError(RuntimeError):
    """Existing paper ledger is missing, inconsistent, or unsafe to use."""


def _validate_persisted_decimal(
    value: object,
    *,
    allow_negative: bool,
    allow_zero: bool,
    description: str,
) -> Decimal:
    """Validate a persisted financial Decimal without coercion or rounding."""
    try:
        return validate_decimal(
            value,
            allow_negative=allow_negative,
            allow_zero=allow_zero,
        )
    except PaperLedgerRulesError as exc:
        raise PaperLedgerValidationError(
            f"Ledger contains an invalid persisted {description}."
        ) from exc


async def validate_existing_paper_ledger(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    session_id: uuid.UUID,
    market_type: str,
) -> uuid.UUID:
    """Validate book structure, initial funding, journal balance and snapshots.

    This function is read-only. It never creates, repairs, resets, or funds
    an existing ledger. The caller owns the surrounding transaction.
    """

    if market_type not in {"spot", "futures"}:
        raise ValueError("market_type must be 'spot' or 'futures'.")

    books_result = await session.execute(
        select(PaperLedgerBook).where(
            PaperLedgerBook.account_id == account_id,
            PaperLedgerBook.portfolio_id == portfolio_id,
            PaperLedgerBook.session_id == session_id,
        )
    )
    books = list(books_result.scalars().all())

    if len(books) != 1:
        raise PaperLedgerValidationError(
            "Expected exactly one ledger book for the active portfolio session."
        )

    book = books[0]
    if book.market_type != market_type or book.status != "active":
        raise PaperLedgerValidationError(
            "Existing ledger book market or status is inconsistent."
        )

    accounts_result = await session.execute(
        select(PaperLedgerAccount).where(
            PaperLedgerAccount.book_id == book.id
        )
    )
    accounts = list(accounts_result.scalars().all())

    if any(item.market_type != book.market_type for item in accounts):
        raise PaperLedgerValidationError(
            "Existing ledger account market does not match its book."
        )

    expected_accounts = dict(MANDATORY_ACCOUNTS)
    actual_accounts = {
        item.code: item.normal_side for item in accounts
    }

    if len(accounts) != 6 or actual_accounts != expected_accounts:
        raise PaperLedgerValidationError(
            "Existing ledger must contain exactly the six mandatory accounts."
        )

    accounts_by_code = {item.code: item for item in accounts}
    accounts_by_id = {item.id: item for item in accounts}
    account_ids = {item.id for item in accounts}

    snapshots_result = await session.execute(
        select(PaperLedgerAccountSnapshot).where(
            PaperLedgerAccountSnapshot.book_id == book.id
        )
    )
    snapshots = list(snapshots_result.scalars().all())

    if (
        len(snapshots) != 6
        or {item.ledger_account_id for item in snapshots} != account_ids
    ):
        raise PaperLedgerValidationError(
            "Existing ledger snapshots are missing or duplicated."
        )

    snapshots_by_account = {
        item.ledger_account_id: item for item in snapshots
    }

    transactions_result = await session.execute(
        select(PaperLedgerTransaction).where(
            PaperLedgerTransaction.book_id == book.id
        )
    )
    transactions = list(transactions_result.scalars().all())

    if not transactions:
        raise PaperLedgerValidationError(
            "Existing ledger has no journal transactions."
        )

    transaction_ids = {item.id for item in transactions}

    if any(
        item.status != "posted" or item.posted_at is None
        for item in transactions
    ):
        raise PaperLedgerValidationError(
            "Existing ledger contains an unposted or invalid transaction."
        )

    entries_result = await session.execute(
        select(PaperLedgerEntry).where(
            PaperLedgerEntry.book_id == book.id
        )
    )
    entries = list(entries_result.scalars().all())

    if any(
        item.transaction_id not in transaction_ids
        or item.ledger_account_id not in account_ids
        for item in entries
    ):
        raise PaperLedgerValidationError(
            "Ledger entry references an unexpected transaction or account."
        )

    entries_by_transaction: dict[uuid.UUID, list[PaperLedgerEntry]] = {
        transaction_id: [] for transaction_id in transaction_ids
    }
    debit_amounts_by_account = {account_id: [] for account_id in account_ids}
    credit_amounts_by_account = {account_id: [] for account_id in account_ids}

    for entry in entries:
        _validate_persisted_decimal(
            entry.amount,
            allow_negative=False,
            allow_zero=False,
            description="entry amount",
        )

        ledger_account = accounts_by_id[entry.ledger_account_id]

        if (
            book.market_type == "spot"
            and ledger_account.code == "futures_margin_locked"
        ) or (
            book.market_type == "futures"
            and ledger_account.code == "spot_position_cost"
        ):
            raise PaperLedgerValidationError(
                "Ledger contains a prohibited cross-market account posting."
            )

        entries_by_transaction[entry.transaction_id].append(entry)

        if entry.direction == "debit":
            debit_amounts_by_account[entry.ledger_account_id].append(entry.amount)
        elif entry.direction == "credit":
            credit_amounts_by_account[entry.ledger_account_id].append(entry.amount)
        else:
            raise PaperLedgerValidationError(
                "Ledger contains an unsupported journal direction."
            )

    precision = 72 + len(str(len(entries))) + 2
    with localcontext() as context:
        context.prec = precision
        debit_totals = {
            account_id: sum(values, Decimal("0"))
            for account_id, values in debit_amounts_by_account.items()
        }
        credit_totals = {
            account_id: sum(values, Decimal("0"))
            for account_id, values in credit_amounts_by_account.items()
        }
        try:
            debit_totals = {
                account_id: validate_decimal(
                    amount, allow_negative=False, allow_zero=True
                )
                for account_id, amount in debit_totals.items()
            }
            credit_totals = {
                account_id: validate_decimal(
                    amount, allow_negative=False, allow_zero=True
                )
                for account_id, amount in credit_totals.items()
            }
        except PaperLedgerRulesError as exc:
            raise PaperLedgerValidationError(
                "Ledger contains an invalid aggregated account total."
            ) from exc

    for transaction_id, transaction_entries in entries_by_transaction.items():
        if len(transaction_entries) < 2:
            raise PaperLedgerValidationError(
                "A journal transaction has fewer than two entries."
            )

        with localcontext() as context:
            context.prec = 72 + len(str(len(transaction_entries))) + 2
            debit = sum(
                (item.amount for item in transaction_entries
                 if item.direction == "debit"),
                Decimal("0"),
            )
            credit = sum(
                (item.amount for item in transaction_entries
                 if item.direction == "credit"),
                Decimal("0"),
            )
            try:
                debit = validate_decimal(
                    debit, allow_negative=False, allow_zero=False
                )
                credit = validate_decimal(
                    credit, allow_negative=False, allow_zero=False
                )
            except PaperLedgerRulesError as exc:
                raise PaperLedgerValidationError(
                    "Ledger contains an invalid transaction aggregate total."
                ) from exc

        if debit <= 0 or credit <= 0 or debit != credit:
            raise PaperLedgerValidationError(
                "A journal transaction is not exactly balanced."
            )

    for ledger_account_id, ledger_account in (
        (item.id, item) for item in accounts
    ):
        snapshot = snapshots_by_account[ledger_account_id]
        _validate_persisted_decimal(
            snapshot.balance,
            allow_negative=True,
            allow_zero=True,
            description="snapshot balance",
        )

        with localcontext() as context:
            context.prec = precision
            if ledger_account.normal_side == "debit":
                expected_balance = (
                    debit_totals[ledger_account_id]
                    - credit_totals[ledger_account_id]
                )
            else:
                expected_balance = (
                    credit_totals[ledger_account_id]
                    - debit_totals[ledger_account_id]
                )

        if snapshot.balance != expected_balance:
            raise PaperLedgerValidationError(
                f"Snapshot reconciliation failed for {ledger_account.code}."
            )

    # Reject duplicate initial funding even if a different idempotency
    # key was used. Checking only the expected key is not sufficient.
    initial_transactions = [
        item for item in transactions
        if item.idempotency_key == INITIAL_FUNDING_KEY
    ]
    funding_type_transactions = [
        item for item in transactions
        if item.transaction_type == "initial_funding"
    ]

    if (
        len(initial_transactions) != 1
        or len(funding_type_transactions) != 1
        or initial_transactions[0].id != funding_type_transactions[0].id
    ):
        raise PaperLedgerValidationError(
            "Expected exactly one initial funding transaction and no duplicates."
        )

    initial = initial_transactions[0]
    expected_fingerprint = _initial_funding_fingerprint(
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=session_id,
        market_type=market_type,
    )

    if (
        initial.transaction_type != "initial_funding"
        or initial.request_fingerprint != expected_fingerprint
    ):
        raise PaperLedgerValidationError(
            "Initial funding transaction identity is invalid."
        )

    initial_entries = entries_by_transaction[initial.id]
    if len(initial_entries) != 2:
        raise PaperLedgerValidationError(
            "Initial funding must contain exactly two journal entries."
        )

    expected_initial_entries = {
        (
            accounts_by_code["cash_available"].id,
            "debit",
            INITIAL_BALANCE,
        ),
        (
            accounts_by_code["contributed_capital"].id,
            "credit",
            INITIAL_BALANCE,
        ),
    }
    actual_initial_entries = {
        (item.ledger_account_id, item.direction, item.amount)
        for item in initial_entries
    }

    if actual_initial_entries != expected_initial_entries:
        raise PaperLedgerValidationError(
            "Initial funding journal does not match the approved 1000 USDT funding."
        )

    return book.id