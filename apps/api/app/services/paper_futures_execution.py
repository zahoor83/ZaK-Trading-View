"""Atomic USD-M Futures paper-trade execution and immutable journal posting."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Mapping

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.market_data.futures_order_book import (
    FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS,
)
from app.market_data.service import MarketDataService
from app.market_data.symbol_filters import ReferencePrice, validate_order
from app.models.paper_ledger import (
    PaperExecutionRequest,
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperPosition,
)
from app.security.paper_idempotency import (
    canonical_uuid_v4,
    hash_idempotency_key,
)
from app.security.paper_idempotency_decision import (
    IdempotencyDecisionKind,
    decide_idempotency,
)
from app.security.paper_write_authorization import authorize_paper_write
from app.security.permissions import Permission
from app.services.futures_depth_execution_adapter import (
    FuturesDepthExecutionAdapter,
)
from app.services.futures_depth_fill_engine import (
    calculate_futures_depth_fill,
)
from app.services.paper_fee_policy import (
    PaperFeePolicy,
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
    JournalEntryInput,
    PaperLedgerRulesError,
    validate_decimal,
)
from app.services.paper_ledger_validation import validate_existing_paper_ledger
from app.services.paper_trade_calculations import (
    TradeCalculation,
    calculate_futures_close,
    calculate_futures_open,
)
from app.services.paper_trade_fingerprint import fingerprint_trade_intent_request
from app.services.paper_futures_position_policy import (
    FuturesPositionDecision,
    resolve_futures_position_action,
)


class FuturesTradeExecutionError(RuntimeError):
    """A USD-M Futures paper trade could not be executed safely."""


@dataclass(frozen=True, slots=True)
class FuturesTradeExecutionResult:
    replayed: bool
    response_data: dict[str, Any]


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _decimal(
    value: object,
    field: str,
    *,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    try:
        return validate_decimal(
            value,
            allow_negative=allow_negative,
            allow_zero=allow_zero,
        )
    except PaperLedgerRulesError as exc:
        raise FuturesTradeExecutionError(
            f"Persisted or requested {field} is invalid."
        ) from exc


def _validate_position_snapshot(
    position: PaperPosition,
    *,
    expected_symbol: str,
) -> PaperFeePolicy:
    if (
        position.market_type != "futures"
        or position.symbol != expected_symbol
        or position.status != "open"
        or position.side not in {"long", "short"}
        or position.remaining_cost_basis is not None
    ):
        raise FuturesTradeExecutionError(
            "Existing Futures position has incompatible identity or state."
        )

    _decimal(
        position.initial_quantity,
        "initial quantity",
        allow_zero=False,
    )
    _decimal(
        position.remaining_quantity,
        "remaining quantity",
        allow_zero=False,
    )
    _decimal(position.entry_price, "entry price", allow_zero=False)
    _decimal(position.margin_locked, "locked margin", allow_zero=False)
    leverage = _decimal(
        position.leverage_snapshot,
        "leverage snapshot",
        allow_zero=False,
    )
    _decimal(
        position.entry_fee_original,
        "original entry fee",
    )
    posted_entry_fee = _decimal(
        position.entry_fee_posted,
        "posted entry fee",
    )
    _decimal(position.entry_fee_rate, "entry fee rate")
    _decimal(position.exit_fee_rate, "exit fee rate")
    _decimal(
        position.realized_pnl,
        "realized PnL",
        allow_negative=True,
    )

    if max(0, -posted_entry_fee.as_tuple().exponent) > 2:
        raise FuturesTradeExecutionError(
            "Persisted entry fee posting has invalid precision."
        )

    if leverage <= 0:
        raise FuturesTradeExecutionError("Persisted leverage must be positive.")

    try:
        policy = fee_policy_from_snapshot(
            position.fee_config_snapshot,
            expected_market="futures",
        )
    except PaperFeePolicyError as exc:
        raise FuturesTradeExecutionError(
            "Persisted Futures fee policy snapshot is invalid."
        ) from exc

    if (
        policy.entry_fee_rate != position.entry_fee_rate
        or policy.exit_fee_rate != position.exit_fee_rate
    ):
        raise FuturesTradeExecutionError(
            "Persisted fee rates do not match the immutable fee snapshot."
        )

    metadata = position.symbol_metadata_snapshot
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != 1
        or metadata.get("market") != "usdm_futures"
        or metadata.get("symbol") != expected_symbol
        or metadata.get("status") != "TRADING"
        or not isinstance(metadata.get("filters"), list)
        or not metadata["filters"]
        or not isinstance(metadata.get("tick_size"), str)
    ):
        raise FuturesTradeExecutionError(
            "Persisted Futures symbol metadata snapshot is invalid."
        )

    return policy


async def _require_cash_available(
    session: AsyncSession,
    *,
    book_id: uuid.UUID,
    required: Decimal,
) -> None:
    required = _decimal(required, "required cash")

    if required == 0:
        return

    # Financial writers hold the shared TradingAccount lock through authorization.
    # Under READ COMMITTED, this SELECT sees the latest committed ledger snapshot.
    # Snapshot row locking is intentionally delegated to the protected DB trigger.
    result = await session.execute(
        select(PaperLedgerAccountSnapshot)
        .join(
            PaperLedgerAccount,
            and_(
                PaperLedgerAccount.id
                == PaperLedgerAccountSnapshot.ledger_account_id,
                PaperLedgerAccount.book_id
                == PaperLedgerAccountSnapshot.book_id,
            ),
        )
        .where(
            PaperLedgerAccountSnapshot.book_id == book_id,
            PaperLedgerAccount.code == "cash_available",
            PaperLedgerAccount.market_type == "futures",
            PaperLedgerAccount.normal_side == "debit",
        )
        
    )
    snapshot = result.scalar_one_or_none()

    if snapshot is None or snapshot.book_id != book_id:
        raise FuturesTradeExecutionError(
            "Futures cash-available snapshot is missing."
        )

    available = _decimal(snapshot.balance, "cash-available balance")

    if available < required:
        raise FuturesTradeExecutionError(
            "Insufficient available cash for Futures margin or settlement."
        )


def _cash_credit_requirement(
    calculation: TradeCalculation,
) -> Decimal:
    """Return additional cash required by this journal, if any."""
    requirements = [
        item.amount
        for item in calculation.journal_entries
        if item.account_code == "cash_available"
        and item.direction == "credit"
    ]

    if len(requirements) > 1:
        raise FuturesTradeExecutionError(
            "Futures calculation produced an invalid cash journal."
        )

    return requirements[0] if requirements else Decimal("0")


def _validate_request(intent: Mapping[str, Any]) -> tuple[
    str, str, str, Decimal, Decimal, str | None, bool
]:
    if not isinstance(intent, Mapping):
        raise FuturesTradeExecutionError("Futures trade intent must be a mapping.")

    try:
        symbol = intent["symbol"]
        side = intent["side"]
        position_side = intent["position_side"]
        quantity = _decimal(
            intent["quantity"],
            "quantity",
            allow_zero=False,
        )
        leverage = _decimal(
            intent["leverage"],
            "leverage",
            allow_zero=False,
        )
        position_id = intent["position_id"]
        reduce_only = intent["reduce_only"]
    except (KeyError, TypeError) as exc:
        raise FuturesTradeExecutionError("Futures trade intent is incomplete.") from exc

    if (
        type(symbol) is not str
        or not symbol
        or symbol != symbol.upper()
        or side not in {"buy", "sell"}
        or position_side not in {"long", "short"}
        or type(reduce_only) is not bool
        or intent.get("order_type") != "market"
        or intent.get("limit_price") is not None
    ):
        raise FuturesTradeExecutionError(
            "Futures trade intent violates the market-order contract."
        )

    if position_id is not None and type(position_id) is not str:
        raise FuturesTradeExecutionError("Invalid Futures position identifier.")

    return (
        symbol,
        side,
        position_side,
        quantity,
        leverage,
        position_id,
        reduce_only,
    )


async def execute_futures_trade(
    session: AsyncSession,
    *,
    market_data: MarketDataService,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    idempotency_key: str,
    trade_intent: Mapping[str, Any],
) -> FuturesTradeExecutionResult:
    """Own one transaction for a complete Futures paper execution."""
    if session.in_transaction():
        raise FuturesTradeExecutionError(
            "Futures wrapper requires a session without an active transaction."
        )

    try:
        async with session.begin():
            return await execute_futures_trade_in_transaction(
                session,
                market_data=market_data,
                authenticated_user_id=authenticated_user_id,
                account_id=account_id,
                portfolio_id=portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent=trade_intent,
            )
    except FuturesTradeExecutionError:
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
        raise FuturesTradeExecutionError(
            "Futures paper trade was rejected; no financial changes were committed."
        ) from exc


async def execute_futures_trade_in_transaction(
    session: AsyncSession,
    *,
    market_data: MarketDataService,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    idempotency_key: str,
    trade_intent: Mapping[str, Any],
) -> FuturesTradeExecutionResult:
    """Execute Futures under a caller-owned transaction; never commits."""
    if not session.in_transaction():
        raise FuturesTradeExecutionError(
            "An active caller-owned transaction is required."
        )

    if not isinstance(market_data, MarketDataService):
        raise FuturesTradeExecutionError(
            "Trusted market-data service is required."
        )

    (
        symbol,
        side,
        position_side,
        quantity,
        leverage,
        position_id_text,
        reduce_only,
    ) = _validate_request(trade_intent)

    try:
        canonical_key = canonical_uuid_v4(idempotency_key)
        hashed_key = hash_idempotency_key(canonical_key)
        transaction_type = (
            "futures_close" if reduce_only else "futures_open"
        )
        normalized_trade_intent = dict(trade_intent)
        normalized_trade_intent["quantity"] = quantity
        normalized_trade_intent["leverage"] = leverage
        request_fingerprint = fingerprint_trade_intent_request(
            transaction_type=transaction_type,
            market_type="futures",
            trade_intent=normalized_trade_intent,
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise FuturesTradeExecutionError(
            "Invalid Futures idempotency key or request fingerprint."
        ) from exc

    access = await authorize_paper_write(
        session,
        authenticated_user_id=authenticated_user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        permission=Permission.PAPER_TRADE,
    )

    if (
        access.market_type != "futures"
        or access.position_mode not in {"one_way", "hedge"}
    ):
        raise FuturesTradeExecutionError(
            "Futures execution requires an authorized Futures portfolio and position mode."
        )

    validated_book_id = await validate_existing_paper_ledger(
        session,
        account_id=access.account_id,
        portfolio_id=access.portfolio_id,
        session_id=access.session_id,
        market_type="futures",
    )

    if validated_book_id != access.book_id:
        raise FuturesTradeExecutionError(
            "Validated Futures ledger book does not match authorization."
        )

    # Idempotency is checked before quote retrieval or position mutation.
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
            "Idempotency key was already used for a different Futures request."
        )

    if decision.kind is IdempotencyDecisionKind.REPLAY:
        if decision.response_data is None:
            raise FuturesTradeExecutionError(
                "Persisted Futures replay response is missing."
            )
        return FuturesTradeExecutionResult(
            replayed=True,
            response_data=decision.response_data,
        )

    # The portfolio row is already locked by authorization. Lock every open
    # position for this symbol in deterministic primary-key order.
    positions_result = await session.execute(
        select(PaperPosition)
        .where(
            PaperPosition.book_id == access.book_id,
            PaperPosition.market_type == "futures",
            PaperPosition.symbol == symbol,
            PaperPosition.status == "open",
        )
        .order_by(PaperPosition.id)
        .with_for_update()
    )
    open_positions = list(positions_result.scalars().all())

    try:
        position_decision: FuturesPositionDecision = (
            resolve_futures_position_action(
                position_mode=access.position_mode,
                symbol=symbol,
                side=side,
                position_side=position_side,
                quantity=quantity,
                reduce_only=reduce_only,
                position_id=position_id_text,
                open_positions=open_positions,
            )
        )
    except (TypeError, ValueError) as exc:
        raise FuturesTradeExecutionError(
            "Futures position transition is not permitted."
        ) from exc

    # Only a trusted synchronized USD-M Futures depth context can supply
    # the executable price. BUY consumes Ask; SELL consumes Bid.
    adapter = FuturesDepthExecutionAdapter(market_data)
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
            max_age_seconds=FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS,
        ),
        now_monotonic=quote_context.current_monotonic,
    )

    try:
        depth_fill = calculate_futures_depth_fill(
            side=side,
            quantity=quantity,
            bids=quote_context.bids,
            asks=quote_context.asks,
            tick_size=quote_context.tick_size,
        )
    except (TypeError, ValueError) as exc:
        raise FuturesTradeExecutionError(
            "Futures order book cannot satisfy a complete depth fill."
        ) from exc

    execution_price = depth_fill.vwap

    position = position_decision.position
    action = position_decision.action

    if action in {"open", "increase"}:
        if action == "open":
            fee_policy = await get_current_paper_fee_policy(
                session,
                account_id=access.account_id,
                market_type="futures",
            )
            fee_policy.require_market("futures")
        else:
            if position is None:
                raise FuturesTradeExecutionError(
                    "Futures increase target is missing."
                )
            fee_policy = _validate_position_snapshot(
                position,
                expected_symbol=symbol,
            )
            existing_leverage = _decimal(
                position.leverage_snapshot,
                "leverage snapshot",
                allow_zero=False,
            )
            if existing_leverage != leverage:
                raise FuturesTradeExecutionError(
                    "Increasing a Futures position with different leverage is prohibited."
                )

        calculation = calculate_futures_open(
            quantity=quantity,
            price=execution_price,
            leverage=leverage,
            fee_rate=fee_policy.entry_fee_rate,
        )

        await _require_cash_available(
            session,
            book_id=access.book_id,
            required=_cash_credit_requirement(calculation),
        )

        if action == "open":
            position = PaperPosition(
                book_id=access.book_id,
                market_type="futures",
                symbol=symbol,
                side=position_side,
                leverage_snapshot=leverage,
                initial_quantity=quantity,
                remaining_quantity=quantity,
                entry_price=execution_price,
                remaining_cost_basis=None,
                margin_locked=calculation.remaining_margin,
                entry_fee_original=calculation.fee_original,
                entry_fee_posted=calculation.fee_posted,
                entry_fee_rate=fee_policy.entry_fee_rate,
                exit_fee_rate=fee_policy.exit_fee_rate,
                fee_config_snapshot=fee_policy.to_snapshot(),
                symbol_metadata_snapshot=dict(
                    quote_context.symbol_metadata_snapshot
                ),
                realized_pnl=Decimal("0"),
                status="open",
            )
            session.add(position)
            await session.flush()

            if not isinstance(position.id, uuid.UUID):
                raise FuturesTradeExecutionError(
                    "Database did not return a new Futures position ID."
                )

        else:
            old_quantity = _decimal(
                position.remaining_quantity,
                "remaining quantity",
                allow_zero=False,
            )
            old_price = _decimal(
                position.entry_price,
                "entry price",
                allow_zero=False,
            )
            old_margin = _decimal(
                position.margin_locked,
                "locked margin",
                allow_zero=False,
            )

            new_quantity = _decimal(
                old_quantity + quantity,
                "increased remaining quantity",
                allow_zero=False,
            )
            new_initial_quantity = _decimal(
                position.initial_quantity + quantity,
                "increased initial quantity",
                allow_zero=False,
            )

            with localcontext() as context:
                context.prec = 120
                weighted_entry_price = (
                    old_quantity * old_price
                    + quantity * execution_price
                ) / new_quantity

            position.entry_price = _decimal(
                weighted_entry_price,
                "weighted entry price",
                allow_zero=False,
            )
            position.initial_quantity = new_initial_quantity
            position.remaining_quantity = new_quantity
            position.margin_locked = _decimal(
                old_margin + calculation.remaining_margin,
                "increased locked margin",
                allow_zero=False,
            )
            position.entry_fee_original = _decimal(
                position.entry_fee_original + calculation.fee_original,
                "accumulated original entry fee",
            )
            position.entry_fee_posted = _decimal(
                position.entry_fee_posted + calculation.fee_posted,
                "accumulated posted entry fee",
            )

            if max(
                0,
                -position.entry_fee_posted.as_tuple().exponent,
            ) > 2:
                raise FuturesTradeExecutionError(
                    "Accumulated posted entry fee exceeds two decimal places."
                )

            await session.flush()

    elif action in {"reduce", "close"}:
        if position is None:
            raise FuturesTradeExecutionError(
                "Futures close target is missing."
            )

        fee_policy = _validate_position_snapshot(
            position,
            expected_symbol=symbol,
        )

        existing_leverage = _decimal(
            position.leverage_snapshot,
            "leverage snapshot",
            allow_zero=False,
        )

        if existing_leverage != leverage:
            raise FuturesTradeExecutionError(
                "Futures close leverage does not match the position snapshot."
            )

        calculation = calculate_futures_close(
            side=position.side,
            quantity=quantity,
            entry_price=position.entry_price,
            exit_price=execution_price,
            remaining_quantity=position.remaining_quantity,
            remaining_margin=position.margin_locked,
            leverage=existing_leverage,
            fee_rate=fee_policy.exit_fee_rate,
        )

        # A losing close can require additional cash beyond the released
        # margin. Verify it before changing the position or posting the journal.
        await _require_cash_available(
            session,
            book_id=access.book_id,
            required=_cash_credit_requirement(calculation),
        )

        position.remaining_quantity = calculation.remaining_quantity
        position.margin_locked = calculation.remaining_margin
        position.realized_pnl = _decimal(
            position.realized_pnl + calculation.realized_pnl,
            "accumulated realized PnL",
            allow_negative=True,
        )

        if calculation.remaining_quantity == 0:
            position.status = "closed"
            position.closed_at = datetime.now(timezone.utc)

        await session.flush()

    else:
        raise FuturesTradeExecutionError(
            "Unsupported Futures position action."
        )

    if position is None:
        raise FuturesTradeExecutionError(
            "Futures execution did not produce a position."
        )

    response_extra = {
        "position_id": str(position.id),
        "symbol": symbol,
        "side": side,
        "position_side": position.side,
        "position_action": action,
        "quantity": _decimal_text(calculation.quantity),
        "execution_price": _decimal_text(calculation.execution_price),
        "gross_value": _decimal_text(calculation.gross_value),
        "fee_original": _decimal_text(calculation.fee_original),
        "fee_posted": _decimal_text(calculation.fee_posted),
        "realized_pnl": _decimal_text(calculation.realized_pnl),
        "position_realized_pnl": _decimal_text(position.realized_pnl),
        "remaining_quantity": _decimal_text(position.remaining_quantity),
        "remaining_margin": _decimal_text(position.margin_locked),
        "entry_price": _decimal_text(position.entry_price),
        "leverage": _decimal_text(position.leverage_snapshot),
        "position_status": position.status,
        "fill_method": "depth_vwap",
        "fill_level_count": depth_fill.level_count,
        "fill_levels": [
            {
                "price": _decimal_text(level.price),
                "quantity": _decimal_text(level.quantity),
                "notional": _decimal_text(level.notional),
            }
            for level in depth_fill.levels
        ],
        "quote_source": validated_price.source,
        "quote_update_id": quote_context.last_update_id,
        "fee_config_snapshot": dict(position.fee_config_snapshot),
        "symbol_metadata_snapshot": dict(position.symbol_metadata_snapshot),
    }

    posting_result: PaperPostingResult = (
        await post_paper_trade_journal_in_transaction(
            session,
            authenticated_user_id=authenticated_user_id,
            account_id=account_id,
            portfolio_id=portfolio_id,
            idempotency_key=canonical_key,
            transaction_type=calculation.transaction_type,
            trade_intent=normalized_trade_intent,
            entries=calculation.journal_entries,
            response_data_extra=response_extra,
        )
    )

    if posting_result.replayed:
        # A late replay after the position mutation must abort the whole
        # transaction; it must never commit a second financial mutation.
        raise FuturesTradeExecutionError(
            "Unexpected late Futures idempotency replay; transaction must roll back."
        )

    return FuturesTradeExecutionResult(
        replayed=False,
        response_data=posting_result.response_data,
    )