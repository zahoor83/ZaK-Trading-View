"""Atomic portfolio-scoped Paper Trading reset operations."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_ledger import PaperExecutionRequest, PaperPosition
from app.models.portfolio import PaperSession
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
from app.services.paper_ledger_provisioning import (
    PaperLedgerProvisioningError,
    provision_initial_paper_ledger,
)
from app.services.paper_ledger_validation import (
    PaperLedgerValidationError,
    validate_existing_paper_ledger,
)


INITIAL_BALANCE = "1000.00"


class PaperPortfolioResetError(RuntimeError):
    """A portfolio reset could not be completed safely."""


class PaperPortfolioResetIdempotencyConflict(PaperPortfolioResetError):
    """A reset idempotency key was reused with different semantics."""


class PaperPortfolioResetAuthorizationDenied(PaperPortfolioResetError):
    """The authenticated user is not permitted to reset this portfolio."""


@dataclass(frozen=True, slots=True)
class PaperPortfolioResetResult:
    replayed: bool
    response_data: dict[str, Any]


async def reset_paper_portfolio_in_transaction(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    idempotency_key: str,
    confirmed: bool,
) -> PaperPortfolioResetResult:
    """Archive the current session and provision a fresh $1,000 ledger.

    The caller owns the active database transaction. All session, ledger,
    provisioning and idempotency changes commit or roll back together.
    """

    if not session.in_transaction():
        raise PaperPortfolioResetError(
            "An active caller-owned transaction is required."
        )

    if not all(
        isinstance(value, uuid.UUID)
        for value in (authenticated_user_id, account_id, portfolio_id)
    ):
        raise PaperPortfolioResetError("Invalid portfolio reset identifiers.")

    if confirmed is not True:
        raise PaperPortfolioResetError(
            "Explicit confirmation is required for Portfolio Reset."
        )

    try:
        canonical_key = canonical_uuid_v4(idempotency_key)
        hashed_key = hash_idempotency_key(canonical_key)
    except (PaperIdempotencyInputError, TypeError, ValueError) as exc:
        raise PaperPortfolioResetError(
            "Invalid idempotency request key."
        ) from exc

    try:
        access = await authorize_paper_write(
            session,
            authenticated_user_id=authenticated_user_id,
            account_id=account_id,
            portfolio_id=portfolio_id,
            permission=Permission.PORTFOLIO_RESET,
        )
    except PaperWriteAuthorizationDenied as exc:
        raise PaperPortfolioResetAuthorizationDenied(
            "Paper Trading operation is not permitted."
        ) from exc

    try:
        request_fingerprint = fingerprint_request(
            "portfolio_reset",
            {
                "account_id": str(access.account_id),
                "portfolio_id": str(access.portfolio_id),
                "market_type": access.market_type,
                "initial_balance": INITIAL_BALANCE,
                "confirmed": True,
            },
        )
    except (PaperIdempotencyInputError, TypeError, ValueError) as exc:
        raise PaperPortfolioResetError(
            "Portfolio Reset request fingerprint could not be created."
        ) from exc

    try:
        validated_book_id = await validate_existing_paper_ledger(
            session,
            account_id=access.account_id,
            portfolio_id=access.portfolio_id,
            session_id=access.session_id,
            market_type=access.market_type,
        )
    except (PaperLedgerValidationError, ValueError) as exc:
        raise PaperPortfolioResetError(
            "The existing Paper Trading ledger is inconsistent."
        ) from exc

    if validated_book_id != access.book_id:
        raise PaperPortfolioResetError(
            "The active ledger book changed during authorization."
        )

    # Check portfolio-scoped idempotency before position eligibility.
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
        raise PaperPortfolioResetError(
            "Persisted Portfolio Reset idempotency data is invalid."
        ) from exc

    if decision.kind is IdempotencyDecisionKind.CONFLICT:
        raise PaperPortfolioResetIdempotencyConflict(
            "Idempotency key was already used for a different request."
        )

    if decision.kind is IdempotencyDecisionKind.REPLAY:
        if decision.response_data is None:
            raise PaperPortfolioResetError(
                "Persisted Portfolio Reset response is unexpectedly missing."
            )
        return PaperPortfolioResetResult(
            replayed=True,
            response_data=decision.response_data,
        )

    # A reset must never silently liquidate or discard open positions.
    open_position_result = await session.execute(
        select(PaperPosition.id)
        .where(
            PaperPosition.book_id == access.book_id,
            PaperPosition.status == "open",
        )
        .limit(1)
    )
    if open_position_result.scalar_one_or_none() is not None:
        raise PaperPortfolioResetError(
            "Portfolio Reset is unavailable while open positions exist. "
            "Close all open positions before resetting."
        )

    old_session_result = await session.execute(
        select(PaperSession).where(
            PaperSession.id == access.session_id,
            PaperSession.account_id == access.account_id,
            PaperSession.portfolio_id == access.portfolio_id,
            PaperSession.status == "active",
        )
    )
    old_session = old_session_result.scalar_one_or_none()

    if old_session is None:
        raise PaperPortfolioResetError(
            "The authorized active Paper Trading session could not be found."
        )

    old_session_id = old_session.id
    old_session_number = old_session.session_number
    old_book_id = access.book_id

    # The database-owned SECURITY DEFINER function is the only permitted
    # application path for archiving a ledger book.
    archive_result = await session.execute(
        text(
            """
            SELECT public.archive_paper_ledger_book(
                :account_id,
                :portfolio_id,
                :session_id,
                :book_id
            )
            """
        ),
        {
            "account_id": access.account_id,
            "portfolio_id": access.portfolio_id,
            "session_id": old_session_id,
            "book_id": old_book_id,
        },
    )
    archived_at = archive_result.scalar_one_or_none()

    if archived_at is None:
        raise PaperPortfolioResetError(
            "The active ledger book archive operation returned no timestamp."
        )

    old_session.status = "archived"
    old_session.archived_at = archived_at
    await session.flush()

    max_number_result = await session.execute(
        select(func.max(PaperSession.session_number)).where(
            PaperSession.account_id == access.account_id,
            PaperSession.portfolio_id == access.portfolio_id,
        )
    )
    max_session_number = max_number_result.scalar_one_or_none()
    next_session_number = (max_session_number or 0) + 1

    new_session = PaperSession(
        account_id=access.account_id,
        portfolio_id=access.portfolio_id,
        session_number=next_session_number,
        status="active",
    )
    session.add(new_session)
    await session.flush()

    try:
        new_book_id = await provision_initial_paper_ledger(
            session,
            account_id=access.account_id,
            portfolio_id=access.portfolio_id,
            session_id=new_session.id,
            market_type=access.market_type,
        )
    except (PaperLedgerProvisioningError, ValueError) as exc:
        raise PaperPortfolioResetError(
            "The new Paper Trading ledger could not be provisioned."
        ) from exc

    response_data: dict[str, Any] = {
        "operation_type": "reset",
        "status": "completed",
        "account_id": str(access.account_id),
        "portfolio_id": str(access.portfolio_id),
        "market_type": access.market_type,
        "previous_session_id": str(old_session_id),
        "previous_session_number": old_session_number,
        "previous_book_id": str(old_book_id),
        "previous_session_archived_at": archived_at.isoformat(),
        "new_session_id": str(new_session.id),
        "new_session_number": next_session_number,
        "new_book_id": str(new_book_id),
        "initial_balance": INITIAL_BALANCE,
    }

    try:
        json.dumps(response_data, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperPortfolioResetError(
            "Portfolio Reset response is not JSON-compatible."
        ) from exc

    execution_request = PaperExecutionRequest(
        scope_type="portfolio",
        account_id=access.account_id,
        book_id=None,
        portfolio_id=access.portfolio_id,
        operation_type="reset",
        idempotency_key=hashed_key,
        request_fingerprint=request_fingerprint,
        status="completed",
        response_data=response_data,
    )
    session.add(execution_request)
    await session.flush()

    return PaperPortfolioResetResult(
        replayed=False,
        response_data=response_data,
    )


async def reset_paper_portfolio(
    session: AsyncSession,
    **kwargs: Any,
) -> PaperPortfolioResetResult:
    """Run Portfolio Reset in a transaction owned by this wrapper."""
    async with session.begin():
        return await reset_paper_portfolio_in_transaction(
            session,
            **kwargs,
        )