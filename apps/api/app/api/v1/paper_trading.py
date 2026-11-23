"""Authenticated Paper Trading HTTP endpoints."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator, StrictBool
from sqlalchemy.exc import SQLAlchemyError

from app.market_data.service import MarketDataService
from app.security.paper_write_authorization import PaperWriteAuthorizationDenied
from app.security.session_authentication import AuthenticatedUser, get_authenticated_user
from app.database.session import get_db_session
from app.services.paper_trading_bootstrap import (
    PaperTradingAccountNotFound,
    PaperTradingBootstrapIntegrityError,
    load_paper_trading_bootstrap,
)
from app.services.paper_ledger_posting import PaperIdempotencyConflict
from app.services.paper_ledger_validation import PaperLedgerValidationError
from app.security.resource_authorization import PortfolioAccessDenied
from app.security.permissions import PermissionDenied
from app.services.paper_trade_execution import (
    PaperTradeExecutionError,
    execute_spot_trade,
)
from app.services.paper_futures_execution import (
    FuturesTradeExecutionError,
    execute_futures_trade,
)
from app.services.paper_portfolio_operations import (
    PaperPortfolioAuthorizationDenied,
    PaperPortfolioIdempotencyConflict,
    PaperPortfolioOperationError,
    manual_top_up_paper_portfolio,
)


from app.services.paper_portfolio_reset import (
    PaperPortfolioResetAuthorizationDenied,
    PaperPortfolioResetError,
    PaperPortfolioResetIdempotencyConflict,
    reset_paper_portfolio,
)

router = APIRouter(tags=["paper-trading"])


class PaperTradingSessionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    session_number: int = Field(ge=1)
    status: Literal["active", "archived"]


class PaperTradingLedgerSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: UUID
    cash_available: str
    spot_position_cost: str
    futures_margin_locked: str
    realized_pnl: str
    fee_expense: str
    contributed_capital: str


class PaperTradingPositionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    symbol: str
    side: Literal["long", "short"]
    status: Literal["open", "closed"]
    leverage: str | None
    initial_quantity: str
    remaining_quantity: str
    entry_price: str
    remaining_cost_basis: str | None
    margin_locked: str | None
    realized_pnl: str
    entry_fee_original: str
    entry_fee_posted: str
    opened_at: str
    closed_at: str | None


class PaperTradingPortfolioSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    market_type: Literal["spot", "futures"]
    status: Literal["active", "disabled"]
    position_mode: Literal["one_way", "hedge"] | None
    session: PaperTradingSessionSnapshot
    ledger: PaperTradingLedgerSnapshot
    positions: list[PaperTradingPositionSnapshot]


class PaperTradingAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    name: str
    role: Literal["owner", "admin", "trader", "viewer"]


class PaperTradingBootstrapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account: PaperTradingAccountSnapshot
    portfolios: list[PaperTradingPortfolioSnapshot] = Field(min_length=2, max_length=2)

class FuturesPaperFeeConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    policy_id: str
    version: int = Field(ge=1)
    market_type: Literal["futures"]
    entry_fee_rate: str
    exit_fee_rate: str


class FuturesPaperSymbolMetadataSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    market: Literal["usdm_futures"]
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    pair: str | None
    contract_type: str | None
    margin_asset: str | None
    delivery_date: int | None
    onboard_date: int | None
    order_types: list[str]
    filter_types: list[str]
    price_precision: int | None
    quantity_precision: int | None
    filters: list[dict[str, Any]]
    metadata_server_time_ms: int
    tick_size: str


class FuturesPaperTradeResponse(BaseModel):
    """Typed API representation of the persisted Futures execution envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: UUID
    book_id: UUID
    market_type: Literal["futures"]
    transaction_type: Literal["futures_open", "futures_close"]
    status: Literal["posted"]
    entry_count: int = Field(ge=1)
    debit_total: str
    credit_total: str

    position_id: UUID
    symbol: str
    side: Literal["buy", "sell"]
    position_side: Literal["long", "short"]
    position_action: Literal["open", "increase", "reduce", "close"]
    quantity: str
    execution_price: str
    gross_value: str
    fee_original: str
    fee_posted: str
    realized_pnl: str
    position_realized_pnl: str
    remaining_quantity: str
    remaining_margin: str
    entry_price: str
    leverage: str
    position_status: Literal["open", "closed"]
    quote_source: Literal["futures_depth_book"]
    quote_update_id: int = Field(gt=0)
    fee_config_snapshot: FuturesPaperFeeConfigSnapshot
    symbol_metadata_snapshot: FuturesPaperSymbolMetadataSnapshot
    replayed: bool


class SpotPaperFeeConfigSnapshot(BaseModel):
    """Immutable fee-policy snapshot persisted with a Spot position."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    policy_id: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    )
    version: int = Field(ge=1)
    market_type: Literal["spot"]
    entry_fee_rate: str
    exit_fee_rate: str


class SpotPaperSymbolMetadataSnapshot(BaseModel):
    """Immutable Spot symbol metadata used for the execution."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    market: Literal["spot"]
    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    order_types: list[str]
    filter_types: list[str]
    price_precision: int | None
    quantity_precision: int | None
    filters: list[dict[str, Any]]
    metadata_server_time_ms: int
    tick_size: str


class SpotPaperTradeResponse(BaseModel):
    """Typed API representation of the persisted Spot execution envelope."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: UUID
    book_id: UUID
    market_type: Literal["spot"]
    transaction_type: Literal["spot_buy", "spot_sell"]
    status: Literal["posted"]
    entry_count: int = Field(ge=1)
    debit_total: str
    credit_total: str
    position_id: UUID
    symbol: str
    side: Literal["buy", "sell"]
    quantity: str
    execution_price: str
    gross_value: str
    fee_original: str
    fee_posted: str
    realized_pnl: str
    remaining_quantity: str
    remaining_cost_basis: str | None
    position_status: Literal["open", "closed"]
    quote_source: Literal["spot_book_ticker"]
    quote_update_id: int = Field(gt=0)
    fee_config_snapshot: SpotPaperFeeConfigSnapshot
    symbol_metadata_snapshot: SpotPaperSymbolMetadataSnapshot
    replayed: bool

_DECIMAL_TEXT = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,36})?$")


class ManualTopUpRequest(BaseModel):
    """Manual Top-up requires an explicit, strict boolean confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed: StrictBool


class ManualTopUpResponse(BaseModel):
    """Strict persisted response envelope for a portfolio Manual Top-up."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: UUID
    book_id: UUID
    market_type: Literal["spot", "futures"]
    transaction_type: Literal["manual_top_up"]
    status: Literal["posted"]
    entry_count: Literal[2]
    debit_total: str
    credit_total: str
    amount: str
    previous_balance: str
    new_balance: str
    replayed: bool


class PortfolioResetRequest(BaseModel):
    """Strict confirmation request for Paper Trading portfolio reset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed: StrictBool


class PortfolioResetResponse(BaseModel):
    """Portfolio reset operation response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_type: Literal["reset"]
    status: Literal["completed"]
    account_id: UUID
    portfolio_id: UUID
    market_type: Literal["spot", "futures"]
    previous_session_id: UUID
    previous_session_number: int = Field(ge=1)
    previous_book_id: UUID
    previous_session_archived_at: str = Field(min_length=1, max_length=64)
    new_session_id: UUID
    new_session_number: int = Field(ge=1)
    new_book_id: UUID
    initial_balance: str = Field(strict=True, min_length=1, max_length=64)
    replayed: bool

class SpotPaperTradeRequest(BaseModel):
    """Spot market-order request; quantity is exact decimal text, never a float."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Z0-9_-]{1,64}$")
    side: Literal["buy", "sell"]
    quantity: str = Field(strict=True, min_length=1, max_length=64)
    position_id: UUID4 | None = None

    @field_validator("quantity")
    @classmethod
    def validate_quantity_text(cls, value: str) -> str:
        if not _DECIMAL_TEXT.fullmatch(value):
            raise ValueError("quantity must be plain decimal text with at most 36 fractional digits")
        try:
            quantity = Decimal(value)
        except InvalidOperation:
            raise ValueError("quantity is invalid") from None
        if not quantity.is_finite() or quantity <= 0 or quantity >= Decimal("1e36"):
            raise ValueError("quantity must be finite, positive, and below 1e36")
        return value

    @model_validator(mode="after")
    def validate_position_semantics(self) -> "SpotPaperTradeRequest":
        if self.side == "buy" and self.position_id is not None:
            raise ValueError("Spot Buy creates a new position and must not specify position_id")
        if self.side == "sell" and self.position_id is None:
            raise ValueError("Spot Sell requires position_id")
        return self




class FuturesPaperTradeRequest(BaseModel):
    """Strict Futures market-order request; decimal values are text, never floats."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z0-9_-]{1,64}$",
    )
    side: Literal["buy", "sell"]
    position_side: Literal["long", "short"]
    quantity: str = Field(strict=True, min_length=1, max_length=64)
    leverage: str = Field(strict=True, min_length=1, max_length=64)
    order_type: Literal["market"] = "market"
    position_id: UUID4 | None = None
    reduce_only: bool = Field(default=False, strict=True)
    limit_price: None = None

    @field_validator("quantity", "leverage")
    @classmethod
    def validate_positive_decimal_text(cls, value: str) -> str:
        if not _DECIMAL_TEXT.fullmatch(value):
            raise ValueError(
                "Value must be plain decimal text with at most 36 fractional digits"
            )

        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError("Value is invalid") from None

        if not number.is_finite() or number <= 0 or number >= Decimal("1e36"):
            raise ValueError("Value must be finite, positive, and below 1e36")

        return value

    @model_validator(mode="after")
    def validate_futures_order_semantics(self) -> "FuturesPaperTradeRequest":
        opening_side = {
            "long": "buy",
            "short": "sell",
        }[self.position_side]

        closing_side = {
            "long": "sell",
            "short": "buy",
        }[self.position_side]

        if self.reduce_only:
            if self.position_id is None:
                raise ValueError("Reduce-only Futures orders require position_id")
            if self.side != closing_side:
                raise ValueError(
                    "Reduce-only side must close the selected position_side"
                )
        elif self.side != opening_side:
            raise ValueError(
                "Non-reduce-only side must open or increase position_side"
            )

        return self

@router.get(
    "/api/v1/paper-trading/bootstrap",
    response_model=PaperTradingBootstrapResponse,
    status_code=status.HTTP_200_OK,
)
async def get_paper_trading_bootstrap(
    response: Response,
    authenticated_user: AuthenticatedUser = Depends(get_authenticated_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return a validated, read-only snapshot of the authenticated user's paper portfolios."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"

    try:
        return await load_paper_trading_bootstrap(
            session,
            authenticated_user_id=authenticated_user.user_id,
        )
    except PaperTradingAccountNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paper Trading account was not found.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except (PortfolioAccessDenied, PermissionDenied):
        # Resource authorization deliberately hides whether another account exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paper Trading account was not found.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except (PaperTradingBootstrapIntegrityError, PaperLedgerValidationError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Paper Trading account state is inconsistent and requires review.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper Trading storage is temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None


@router.post(
    "/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/manual-top-up",
    response_model=ManualTopUpResponse,
    status_code=status.HTTP_200_OK,
)
async def manual_top_up_paper_trading_portfolio(
    account_id: UUID,
    portfolio_id: UUID,
    payload: ManualTopUpRequest,
    request: Request,
    response: Response,
    authenticated_user: AuthenticatedUser = Depends(get_authenticated_user),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
) -> dict[str, Any]:
    """Restore an eligible portfolio cash balance to 1,000 USDT."""
    response.headers["Cache-Control"] = "no-store"

    manager = getattr(request.app.state, "database_manager", None)
    if manager is None or not getattr(manager, "is_initialized", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        factory = manager.session_factory()
        async with factory() as session:
            result = await manual_top_up_paper_portfolio(
                session,
                authenticated_user_id=authenticated_user.user_id,
                account_id=account_id,
                portfolio_id=portfolio_id,
                idempotency_key=idempotency_key,
                confirmed=payload.confirmed,
            )
    except PaperPortfolioAuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Paper Trading operation is not permitted.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperPortfolioIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key conflicts with a previously submitted operation.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperPortfolioOperationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manual Top-up could not be completed; verify portfolio eligibility and ledger state.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper Trading storage is temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Manual Top-up prerequisites are temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None

    return {**result.response_data, "replayed": result.replayed}


@router.post(
    "/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/reset",
    response_model=PortfolioResetResponse,
    status_code=status.HTTP_200_OK,
)
async def reset_paper_trading_portfolio(
    account_id: UUID,
    portfolio_id: UUID,
    payload: PortfolioResetRequest,
    request: Request,
    response: Response,
    authenticated_user: AuthenticatedUser = Depends(get_authenticated_user),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
) -> dict[str, Any]:
    """Reset a Paper Trading portfolio using the transactional service."""
    response.headers["Cache-Control"] = "no-store"

    manager = getattr(request.app.state, "database_manager", None)
    if manager is None or not getattr(manager, "is_initialized", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        factory = manager.session_factory()
        async with factory() as session:
            result = await reset_paper_portfolio(
                session,
                authenticated_user_id=authenticated_user.user_id,
                account_id=account_id,
                portfolio_id=portfolio_id,
                idempotency_key=idempotency_key,
                confirmed=payload.confirmed,
            )
    except PaperPortfolioResetAuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Paper Trading operation is not permitted.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperPortfolioResetIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key conflicts with a previously submitted operation.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperPortfolioResetError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Portfolio Reset could not be completed; verify portfolio eligibility and ledger state.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper Trading storage is temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Portfolio Reset prerequisites are temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None

    return {**result.response_data, "replayed": result.replayed}

@router.post(
    "/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/paper-trades/spot",
    status_code=status.HTTP_200_OK,
    response_model=SpotPaperTradeResponse,
)
async def execute_spot_paper_trade(
    account_id: UUID,
    portfolio_id: UUID,
    payload: SpotPaperTradeRequest,
    request: Request,
    response: Response,
    authenticated_user: AuthenticatedUser = Depends(get_authenticated_user),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
) -> dict[str, Any]:
    """Execute a Spot market trade; positions and journal commit atomically."""
    response.headers["Cache-Control"] = "no-store"

    market_data = getattr(request.app.state, "market_data_service", None)
    if not isinstance(market_data, MarketDataService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market-data execution service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    manager = getattr(request.app.state, "database_manager", None)
    if manager is None or not getattr(manager, "is_initialized", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        factory = manager.session_factory()
        async with factory() as session:
            result = await execute_spot_trade(
                session,
                market_data=market_data,
                authenticated_user_id=authenticated_user.user_id,
                account_id=account_id,
                portfolio_id=portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent={
                    "position_id": str(payload.position_id) if payload.position_id else None,
                    "symbol": payload.symbol,
                    "side": payload.side,
                    "position_side": None,
                    "quantity": Decimal(payload.quantity),
                    "order_type": "market",
                    "leverage": None,
                    "limit_price": None,
                    "reduce_only": False,
                },
            )
    except PaperIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key conflicts with a previously submitted trade.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperWriteAuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Paper Trading operation is not permitted.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperTradeExecutionError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Spot paper trade was rejected; no financial changes were committed.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper Trading storage is temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spot execution prerequisites are temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None

    return {**result.response_data, "replayed": result.replayed}

@router.post(
    "/api/v1/accounts/{account_id}/portfolios/{portfolio_id}/paper-trades/futures",
    response_model=FuturesPaperTradeResponse,
    status_code=status.HTTP_200_OK,
)
async def execute_futures_paper_trade(
    account_id: UUID,
    portfolio_id: UUID,
    payload: FuturesPaperTradeRequest,
    request: Request,
    response: Response,
    authenticated_user: AuthenticatedUser = Depends(get_authenticated_user),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
) -> dict[str, Any]:
    """Execute a USD-M Futures market trade using the service-owned transaction."""
    response.headers["Cache-Control"] = "no-store"

    market_data = getattr(request.app.state, "market_data_service", None)
    if not isinstance(market_data, MarketDataService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market-data execution service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    manager = getattr(request.app.state, "database_manager", None)
    if manager is None or not getattr(manager, "is_initialized", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        factory = manager.session_factory()
        async with factory() as session:
            result = await execute_futures_trade(
                session,
                market_data=market_data,
                authenticated_user_id=authenticated_user.user_id,
                account_id=account_id,
                portfolio_id=portfolio_id,
                idempotency_key=idempotency_key,
                trade_intent={
                    "position_id": (
                        str(payload.position_id)
                        if payload.position_id is not None
                        else None
                    ),
                    "symbol": payload.symbol,
                    "side": payload.side,
                    "position_side": payload.position_side,
                    "quantity": Decimal(payload.quantity),
                    "order_type": payload.order_type,
                    "leverage": Decimal(payload.leverage),
                    "limit_price": payload.limit_price,
                    "reduce_only": payload.reduce_only,
                },
            )
    except PaperIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key conflicts with a previously submitted trade.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except PaperWriteAuthorizationDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Paper Trading operation is not permitted.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except FuturesTradeExecutionError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Futures paper trade was rejected; no financial changes were committed.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Paper Trading storage is temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Futures execution prerequisites are temporarily unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None

    return {**result.response_data, "replayed": result.replayed}

