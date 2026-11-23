"""Atomic, idempotent posting of Spot and Futures paper-trade journals."""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.paper_ledger import PaperExecutionRequest, PaperLedgerAccount, PaperLedgerEntry, PaperLedgerTransaction
from app.security.paper_write_authorization import authorize_paper_write
from app.security.paper_idempotency import canonical_uuid_v4, hash_idempotency_key
from app.security.paper_idempotency_decision import IdempotencyDecisionKind, decide_idempotency
from app.security.permissions import Permission
from app.services.paper_ledger_rules import JournalEntryInput, PaperLedgerRulesError, validate_journal_entries, validate_transaction_journal
from app.services.paper_ledger_validation import validate_existing_paper_ledger
from app.services.paper_trade_fingerprint import fingerprint_trade_intent_request

class PaperLedgerPostingError(RuntimeError):
    """A paper-trade journal could not be posted safely."""

class PaperIdempotencyConflict(PaperLedgerPostingError):
    """An idempotency key was reused with different request semantics."""

@dataclass(frozen=True, slots=True)
class PaperPostingResult:
    """Result returned after a successful post or safe replay."""
    replayed: bool
    response_data: dict[str, Any]
_ALLOWED_TRANSACTION_TYPES = {'spot': frozenset({'spot_buy', 'spot_sell'}), 'futures': frozenset({'futures_open', 'futures_close'})}

async def post_paper_trade_journal_in_transaction(session: AsyncSession, *, authenticated_user_id: uuid.UUID, account_id: uuid.UUID, portfolio_id: uuid.UUID, idempotency_key: str, transaction_type: str, trade_intent: Mapping[str, Any], entries: tuple[JournalEntryInput, ...], response_data_extra: Mapping[str, Any] | None = None) -> PaperPostingResult:
    """Post a paper-trade journal inside a caller-owned transaction."""
    if not session.in_transaction():
        raise PaperLedgerPostingError('An active caller-owned transaction is required.')
    if not isinstance(authenticated_user_id, uuid.UUID):
        raise PaperLedgerPostingError('Invalid authenticated user identifier.')
    if not isinstance(account_id, uuid.UUID):
        raise PaperLedgerPostingError('Invalid account identifier.')
    if not isinstance(portfolio_id, uuid.UUID):
        raise PaperLedgerPostingError('Invalid portfolio identifier.')
    if not isinstance(transaction_type, str):
        raise PaperLedgerPostingError('Invalid paper transaction type.')
    if not isinstance(entries, tuple):
        raise PaperLedgerPostingError('Journal entries must be supplied as a tuple.')
    try:
        canonical_key = canonical_uuid_v4(idempotency_key)
        hashed_key = hash_idempotency_key(canonical_key)
    except (TypeError, ValueError) as exc:
        raise PaperLedgerPostingError('Invalid idempotency request key.') from exc
    access = await authorize_paper_write(session, authenticated_user_id=authenticated_user_id, account_id=account_id, portfolio_id=portfolio_id, permission=Permission.PAPER_TRADE)
    allowed_types = _ALLOWED_TRANSACTION_TYPES[access.market_type]
    if transaction_type not in allowed_types:
        raise PaperLedgerPostingError('Transaction type is not permitted for this portfolio market.')
    try:
        request_fingerprint = fingerprint_trade_intent_request(
            transaction_type=transaction_type,
            market_type=access.market_type,
            trade_intent=trade_intent,
        )
    except (TypeError, ValueError) as exc:
        raise PaperLedgerPostingError(
            "Paper-trade request fingerprint could not be created."
        ) from exc

    await validate_existing_paper_ledger(session, account_id=access.account_id, portfolio_id=access.portfolio_id, session_id=access.session_id, market_type=access.market_type)
    request_result = await session.execute(select(PaperExecutionRequest).where(PaperExecutionRequest.scope_type == 'book', PaperExecutionRequest.book_id == access.book_id, PaperExecutionRequest.idempotency_key == hashed_key))
    existing_request = request_result.scalar_one_or_none()
    decision = decide_idempotency(existing_request_found=existing_request is not None, existing_fingerprint=existing_request.request_fingerprint if existing_request is not None else None, existing_response=existing_request.response_data if existing_request is not None else None, requested_fingerprint=request_fingerprint)
    if decision.kind is IdempotencyDecisionKind.CONFLICT:
        raise PaperIdempotencyConflict('Idempotency key was already used for a different request.')
    if decision.kind is IdempotencyDecisionKind.REPLAY:
        if decision.response_data is None:
            raise PaperLedgerPostingError('Persisted replay response is unexpectedly missing.')
        return PaperPostingResult(replayed=True, response_data=decision.response_data)
    try:
        journal = validate_journal_entries(access.market_type, entries)
    except PaperLedgerRulesError as exc:
        raise PaperLedgerPostingError('Paper-trade journal validation failed.') from exc

    try:
        journal = validate_transaction_journal(transaction_type, journal)
    except PaperLedgerRulesError as exc:
        raise PaperLedgerPostingError('Transaction-specific accounting validation failed.') from exc

    orphan_result = await session.execute(select(PaperLedgerTransaction.id).where(PaperLedgerTransaction.book_id == access.book_id, PaperLedgerTransaction.idempotency_key == hashed_key))
    if orphan_result.scalar_one_or_none() is not None:
        raise PaperLedgerPostingError('An orphaned ledger transaction uses this idempotency key.')
    accounts_result = await session.execute(select(PaperLedgerAccount).where(PaperLedgerAccount.book_id == access.book_id))
    accounts = list(accounts_result.scalars().all())
    accounts_by_code = {item.code: item for item in accounts}
    if len(accounts) != 6 or len(accounts_by_code) != 6:
        raise PaperLedgerPostingError('Ledger account structure is incomplete or duplicated.')
    if any((item.market_type != access.market_type for item in accounts)):
        raise PaperLedgerPostingError('Ledger account market does not match the active book.')
    try:
        transaction = PaperLedgerTransaction(book_id=access.book_id, idempotency_key=hashed_key, request_fingerprint=request_fingerprint, transaction_type=transaction_type, status='draft', posted_at=None)
        session.add(transaction)
        await session.flush()
        ledger_entries: list[PaperLedgerEntry] = []
        for item in journal.entries:
            account = accounts_by_code.get(item.account_code)
            if account is None:
                raise PaperLedgerPostingError('A required journal account does not exist.')
            ledger_entries.append(PaperLedgerEntry(book_id=access.book_id, transaction_id=transaction.id, ledger_account_id=account.id, direction=item.direction, amount=item.amount))
        session.add_all(ledger_entries)
        await session.flush()
        transaction.status = 'posted'
        await session.flush()
        response_data: dict[str, Any] = {
            "transaction_id": str(transaction.id),
            "book_id": str(access.book_id),
            "market_type": access.market_type,
            "transaction_type": transaction_type,
            "status": "posted",
            "entry_count": len(journal.entries),
            "debit_total": format(journal.debit_total, "f"),
            "credit_total": format(journal.credit_total, "f"),
        }
        if response_data_extra is not None:
            if not isinstance(response_data_extra, Mapping):
                raise PaperLedgerPostingError(
                    "Execution response details must be a mapping."
                )
            if any(type(key) is not str for key in response_data_extra):
                raise PaperLedgerPostingError(
                    "Execution response detail keys must be strings."
                )
            if set(response_data).intersection(response_data_extra):
                raise PaperLedgerPostingError(
                    "Execution response details cannot override ledger fields."
                )
            response_data.update(response_data_extra)
        try:
            json.dumps(response_data, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PaperLedgerPostingError(
                "Execution response details are not JSON-compatible."
            ) from exc
        execution_request = PaperExecutionRequest(scope_type='book', account_id=access.account_id, book_id=access.book_id, portfolio_id=None, operation_type='trade', idempotency_key=hashed_key, request_fingerprint=request_fingerprint, status='completed', response_data=response_data)
        session.add(execution_request)
        await session.flush()
    except PaperLedgerPostingError:
        raise
    except Exception as exc:
        raise PaperLedgerPostingError('Paper-trade journal posting failed; transaction rolled back.') from exc
    return PaperPostingResult(replayed=False, response_data=response_data)

async def post_paper_trade_journal(session: AsyncSession, *, authenticated_user_id: uuid.UUID, account_id: uuid.UUID, portfolio_id: uuid.UUID, idempotency_key: str, transaction_type: str, trade_intent: Mapping[str, Any], entries: tuple[JournalEntryInput, ...], response_data_extra: Mapping[str, Any] | None = None) -> PaperPostingResult:
    async with session.begin():
        return await post_paper_trade_journal_in_transaction(session, authenticated_user_id=authenticated_user_id, account_id=account_id, portfolio_id=portfolio_id, idempotency_key=idempotency_key, transaction_type=transaction_type, trade_intent=trade_intent, entries=entries, response_data_extra=response_data_extra)
