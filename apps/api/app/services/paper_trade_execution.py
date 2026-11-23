"""Atomic Spot paper-trade execution across positions and immutable ledger posting."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.market_data.service import MarketDataService
from app.market_data.service_models import SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS
from app.market_data.symbol_filters import ReferencePrice, validate_order
from app.models.paper_ledger import PaperExecutionRequest, PaperPosition
from app.security.paper_idempotency import canonical_uuid_v4, hash_idempotency_key
from app.security.paper_idempotency_decision import (
    IdempotencyDecisionKind,
    decide_idempotency,
)
from app.security.paper_write_authorization import authorize_paper_write
from app.security.permissions import Permission
from app.services.paper_fee_policy import (
    PaperFeePolicyError,
    fee_policy_from_snapshot,
)
from app.services.paper_fee_policy_persistence import (
    get_current_paper_fee_policy,
)
from app.services.paper_ledger_posting import (
    PaperIdempotencyConflict,
    PaperLedgerPostingError,
    PaperPostingResult,
    post_paper_trade_journal_in_transaction,
)
from app.services.paper_ledger_rules import (
    PaperLedgerRulesError,
    validate_decimal,
)
from app.services.paper_ledger_validation import validate_existing_paper_ledger
from app.services.paper_trade_calculations import (
    calculate_spot_buy,
    calculate_spot_sell,
)
from app.services.paper_trade_fingerprint import fingerprint_trade_intent_request
from app.services.spot_book_ticker_execution_adapter import (
    SpotBookTickerExecutionAdapter,
)


class PaperTradeExecutionError(RuntimeError):
    """A Spot paper trade could not be executed safely."""


@dataclass(frozen=True, slots=True)
class PaperTradeExecutionResult:
    replayed: bool
    response_data: dict[str, Any]


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


async def execute_spot_trade(
    session: AsyncSession,
    *,
    market_data: MarketDataService,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    idempotency_key: str,
    trade_intent: Mapping[str, Any],
) -> PaperTradeExecutionResult:
    """Execute one Spot market trade in a single caller-visible transaction."""
    if session.in_transaction():
        raise PaperTradeExecutionError(
            "Spot execution requires a session without an active transaction."
        )

    try:
        async with session.begin():
            return await execute_spot_trade_in_transaction(
                session,
                market_data=market_data,
                authenticated_user_id=authenticated_user_id,
                account_id=account_id,
                portfolio_id=portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent=trade_intent,
            )
    except PaperTradeExecutionError:
        raise
    except PaperIdempotencyConflict:
        raise
    except (
        PaperLedgerRulesError,
        PaperFeePolicyError,
        PaperLedgerPostingError,
        ValueError,
        TypeError,
    ) as exc:
        raise PaperTradeExecutionError(
            "Spot paper trade was rejected; no financial changes were committed."
        ) from exc


async def execute_spot_trade_in_transaction(
    session: AsyncSession,
    *,
    market_data: MarketDataService,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    idempotency_key: str,
    trade_intent: Mapping[str, Any],
) -> PaperTradeExecutionResult:
    """Execute Spot trade under a caller-owned transaction; never commits."""
    if not session.in_transaction():
        raise PaperTradeExecutionError(
            "An active caller-owned transaction is required."
        )
    if not isinstance(market_data, MarketDataService):
        raise PaperTradeExecutionError("Trusted market-data service is required.")

    try:
        canonical_key = canonical_uuid_v4(idempotency_key)
        hashed_key = hash_idempotency_key(canonical_key)
        transaction_type = (
            "spot_buy" if trade_intent.get("side") == "buy" else "spot_sell"
        )
        request_fingerprint = fingerprint_trade_intent_request(
            transaction_type=transaction_type,
            market_type="spot",
            trade_intent=trade_intent,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise PaperTradeExecutionError("Invalid Spot trade intent.") from exc

    access = await authorize_paper_write(
        session,
        authenticated_user_id=authenticated_user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        permission=Permission.PAPER_TRADE,
    )
    if access.market_type != "spot":
        raise PaperTradeExecutionError("Spot execution requires a Spot portfolio.")

    # Existing-account integrity is checked before replay and before any quote.
    validated_book_id = await validate_existing_paper_ledger(
        session,
        account_id=access.account_id,
        portfolio_id=access.portfolio_id,
        session_id=access.session_id,
        market_type="spot",
    )
    if validated_book_id != access.book_id:
        raise PaperTradeExecutionError("Validated ledger book does not match authorization.")

    request_result = await session.execute(
        select(PaperExecutionRequest).where(
            PaperExecutionRequest.scope_type == "book",
            PaperExecutionRequest.book_id == access.book_id,
            PaperExecutionRequest.idempotency_key == hashed_key,
        )
    )
    existing_request = request_result.scalar_one_or_none()
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
    if decision.kind is IdempotencyDecisionKind.CONFLICT:
        raise PaperIdempotencyConflict(
            "Idempotency key was already used for a different request."
        )
    if decision.kind is IdempotencyDecisionKind.REPLAY:
        if decision.response_data is None:
            raise PaperTradeExecutionError("Persisted replay response is missing.")
        return PaperTradeExecutionResult(
            replayed=True,
            response_data=decision.response_data,
        )

    side = trade_intent["side"]
    symbol = trade_intent["symbol"]
    quantity = trade_intent["quantity"]
    order_type = trade_intent["order_type"]
    position_id_text = trade_intent["position_id"]

    # The current verified adapter supports immediate BookTicker market fills.
    # Limit orders require a persisted pending-order lifecycle and are rejected.
    if order_type != "market":
        raise PaperTradeExecutionError(
            "Only Spot market orders are currently executable."
        )
    if trade_intent["position_side"] is not None or trade_intent["leverage"] is not None:
        raise PaperTradeExecutionError("Spot trade contains Futures-only fields.")
    if trade_intent["reduce_only"] is not False:
        raise PaperTradeExecutionError("Spot reduce_only must be false.")
    if side == "buy" and position_id_text is not None:
        raise PaperTradeExecutionError("Spot Buy must create a new position.")
    if side == "sell" and position_id_text is None:
        raise PaperTradeExecutionError("Spot Sell requires an existing position ID.")

    adapter = SpotBookTickerExecutionAdapter(market_data)
    validated_price, quote_context = adapter.validate_with_context(
        symbol,
        side=side,
    )
    validate_order(
        quote_context.metadata_snapshot,
        symbol,
        order_type="MARKET",
        side=side.upper(),
        quantity=quantity,
        reference_price=ReferencePrice(
            price=validated_price.price,
            fetched_at_monotonic=quote_context.received_at_monotonic,
            max_age_seconds=SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS,
        ),
        now_monotonic=quote_context.current_monotonic,
    )

    position: PaperPosition
    if side == "buy":
        fee_policy = await get_current_paper_fee_policy(
            session,
            account_id=access.account_id,
            market_type="spot",
        )
        fee_policy.require_market("spot")
        calculation = calculate_spot_buy(
            quantity=quantity,
            price=validated_price.price,
            fee_rate=fee_policy.entry_fee_rate,
        )
        position = PaperPosition(
            book_id=access.book_id,
            market_type="spot",
            symbol=symbol,
            side="long",
            leverage_snapshot=None,
            initial_quantity=calculation.quantity,
            remaining_quantity=calculation.remaining_quantity,
            entry_price=calculation.execution_price,
            remaining_cost_basis=calculation.remaining_cost_basis,
            margin_locked=None,
            entry_fee_original=calculation.fee_original,
            entry_fee_posted=calculation.fee_posted,
            entry_fee_rate=fee_policy.entry_fee_rate,
            exit_fee_rate=fee_policy.exit_fee_rate,
            fee_config_snapshot=fee_policy.to_snapshot(),
            symbol_metadata_snapshot=dict(quote_context.symbol_metadata_snapshot),
            realized_pnl=Decimal("0"),
            status="open",
        )
        session.add(position)
        await session.flush()
        if not isinstance(position.id, uuid.UUID):
            raise PaperTradeExecutionError("Database did not return the new position ID.")
    else:
        try:
            position_id = uuid.UUID(position_id_text)
        except (TypeError, ValueError, AttributeError) as exc:
            raise PaperTradeExecutionError("Invalid Spot position ID.") from exc

        position_result = await session.execute(
            select(PaperPosition)
            .where(
                PaperPosition.id == position_id,
                PaperPosition.book_id == access.book_id,
                PaperPosition.market_type == "spot",
            )
            .with_for_update()
        )
        position = position_result.scalar_one_or_none()
        if (
            position is None
            or position.status != "open"
            or position.side != "long"
            or position.symbol != symbol
            or position.remaining_quantity <= 0
            or position.remaining_cost_basis is None
        ):
            raise PaperTradeExecutionError(
                "Spot position is missing, closed, or incompatible with this sell."
            )

        try:
            stored_policy = fee_policy_from_snapshot(
                position.fee_config_snapshot,
                expected_market="spot",
            )
        except PaperFeePolicyError as exc:
            raise PaperTradeExecutionError(
                "Persisted position fee policy is invalid."
            ) from exc
        if (
            stored_policy.entry_fee_rate != position.entry_fee_rate
            or stored_policy.exit_fee_rate != position.exit_fee_rate
        ):
            raise PaperTradeExecutionError(
                "Persisted position fee rates do not match its immutable snapshot."
            )
        metadata_snapshot = position.symbol_metadata_snapshot
        if (
            not isinstance(metadata_snapshot, dict)
            or metadata_snapshot.get("schema_version") != 1
            or metadata_snapshot.get("market") != "spot"
            or metadata_snapshot.get("symbol") != symbol
            or metadata_snapshot.get("status") != "TRADING"
            or not isinstance(metadata_snapshot.get("filters"), list)
            or not metadata_snapshot["filters"]
            or not isinstance(metadata_snapshot.get("tick_size"), str)
        ):
            raise PaperTradeExecutionError(
                "Persisted position symbol metadata snapshot is invalid."
            )

        calculation = calculate_spot_sell(
            quantity=quantity,
            price=validated_price.price,
            remaining_quantity=position.remaining_quantity,
            remaining_cost_basis=position.remaining_cost_basis,
            fee_rate=position.exit_fee_rate,
        )
        position.remaining_quantity = calculation.remaining_quantity
        position.remaining_cost_basis = calculation.remaining_cost_basis
        position.realized_pnl = validate_decimal(
            position.realized_pnl + calculation.realized_pnl
        )
        if calculation.remaining_quantity == 0:
            position.status = "closed"
            position.closed_at = datetime.now(timezone.utc)

        await session.flush()

    response_extra = {
        "position_id": str(position.id),
        "symbol": symbol,
        "side": side,
        "quantity": _decimal_text(calculation.quantity),
        "execution_price": _decimal_text(calculation.execution_price),
        "gross_value": _decimal_text(calculation.gross_value),
        "fee_original": _decimal_text(calculation.fee_original),
        "fee_posted": _decimal_text(calculation.fee_posted),
        "realized_pnl": _decimal_text(calculation.realized_pnl),
        "remaining_quantity": _decimal_text(position.remaining_quantity),
        "remaining_cost_basis": (
            _decimal_text(position.remaining_cost_basis)
            if position.remaining_cost_basis is not None
            else None
        ),
        "position_status": position.status,
        "quote_source": validated_price.source,
        "quote_update_id": quote_context.update_id,
        "fee_config_snapshot": dict(position.fee_config_snapshot),
        "symbol_metadata_snapshot": dict(position.symbol_metadata_snapshot),
    }

    posting_result: PaperPostingResult = await post_paper_trade_journal_in_transaction(
        session,
        authenticated_user_id=authenticated_user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        idempotency_key=canonical_key,
        transaction_type=calculation.transaction_type,
        trade_intent=trade_intent,
        entries=calculation.journal_entries,
        response_data_extra=response_extra,
    )
    if posting_result.replayed:
        # The position was already mutated in this transaction. An unexpected
        # late replay must force rollback rather than commit a second mutation.
        raise PaperTradeExecutionError(
            "Unexpected late idempotency replay; transaction must roll back."
        )

    return PaperTradeExecutionResult(
        replayed=False,
        response_data=posting_result.response_data,
    )
