"""Read-only, fail-closed Paper Trading bootstrap snapshot loader."""

from __future__ import annotations

import uuid
from decimal import Decimal
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import AccountMembership, TradingAccount
from app.models.paper_ledger import (
    PaperLedgerAccount,
    PaperLedgerAccountSnapshot,
    PaperPosition,
)
from app.models.portfolio import PaperPortfolio, PaperSession
from app.security.permissions import AccountRole, Permission, PermissionDenied
from app.security.resource_authorization import (
    PortfolioAccessDenied,
    authorize_portfolio_permission,
)
from app.services.paper_ledger_validation import (
    PaperLedgerValidationError,
    validate_existing_paper_ledger,
)


class PaperTradingAccountNotFound(LookupError):
    """No active Paper Trading account is available to this identity."""


class PaperTradingBootstrapIntegrityError(RuntimeError):
    """Paper Trading bootstrap records are missing, duplicated, or inconsistent."""


def _decimal_text(value: object, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or value is None:
        raise PaperTradingBootstrapIntegrityError(
            f"Paper Trading {field} is not a valid persisted decimal."
        )
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:
        raise PaperTradingBootstrapIntegrityError(
            f"Paper Trading {field} is not a valid persisted decimal."
        ) from None
    if not decimal_value.is_finite():
        raise PaperTradingBootstrapIntegrityError(
            f"Paper Trading {field} is not a finite persisted decimal."
        )
    return format(decimal_value, "f")


def _timestamp_text(value: object, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PaperTradingBootstrapIntegrityError(
            f"Paper Trading {field} is not a timezone-aware timestamp."
        )
    return value.isoformat()


async def load_paper_trading_bootstrap(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
) -> dict[str, Any]:
    """Load one account's Spot/Futures snapshots without mutating any record."""

    membership_result = await session.execute(
        select(AccountMembership, TradingAccount)
        .join(TradingAccount, TradingAccount.id == AccountMembership.account_id)
        .where(
            AccountMembership.user_id == authenticated_user_id,
            AccountMembership.status == "active",
            TradingAccount.status == "active",
        )
    )
    memberships = list(membership_result.all())

    if not memberships:
        raise PaperTradingAccountNotFound()
    if len(memberships) != 1:
        raise PaperTradingBootstrapIntegrityError(
            "The authenticated identity has multiple active Paper Trading accounts."
        )

    membership, account = memberships[0]
    try:
        role = AccountRole(membership.role)
    except (TypeError, ValueError):
        raise PaperTradingBootstrapIntegrityError(
            "The account membership role is invalid."
        ) from None

    if (
        (role is AccountRole.OWNER and account.owner_user_id != authenticated_user_id)
        or (account.owner_user_id == authenticated_user_id and role is not AccountRole.OWNER)
    ):
        raise PaperTradingBootstrapIntegrityError(
            "The account owner and active membership role are inconsistent."
        )

    portfolios_result = await session.execute(
        select(PaperPortfolio).where(PaperPortfolio.account_id == account.id)
    )
    portfolios = list(portfolios_result.scalars().all())

    if len(portfolios) != 2:
        raise PaperTradingBootstrapIntegrityError(
            "The account must contain exactly one Spot and one Futures portfolio."
        )

    portfolios_by_market: dict[str, PaperPortfolio] = {}
    for portfolio in portfolios:
        if portfolio.market_type not in {"spot", "futures"}:
            raise PaperTradingBootstrapIntegrityError(
                "A portfolio has an unsupported market type."
            )
        if portfolio.market_type in portfolios_by_market:
            raise PaperTradingBootstrapIntegrityError(
                "Duplicate market portfolios exist."
            )
        portfolios_by_market[portfolio.market_type] = portfolio

    if set(portfolios_by_market) != {"spot", "futures"}:
        raise PaperTradingBootstrapIntegrityError(
            "The account must contain one Spot and one Futures portfolio."
        )

    output_portfolios: list[dict[str, Any]] = []

    for market_type in ("spot", "futures"):
        portfolio = portfolios_by_market[market_type]

        if portfolio.status != "active":
            raise PaperTradingBootstrapIntegrityError(
                "A required Paper Trading portfolio is not active."
            )
        if market_type == "spot" and portfolio.position_mode is not None:
            raise PaperTradingBootstrapIntegrityError(
                "Spot portfolio has an invalid position mode."
            )
        if market_type == "futures" and portfolio.position_mode not in {"one_way", "hedge"}:
            raise PaperTradingBootstrapIntegrityError(
                "Futures portfolio has an invalid position mode."
            )

        access = await authorize_portfolio_permission(
            session,
            authenticated_user_id=authenticated_user_id,
            account_id=account.id,
            portfolio_id=portfolio.id,
            permission=Permission.PORTFOLIO_READ,
        )
        if access.role is not role or access.market_type != market_type:
            raise PaperTradingBootstrapIntegrityError(
                "Portfolio authorization does not match account membership."
            )

        sessions_result = await session.execute(
            select(PaperSession).where(
                PaperSession.account_id == account.id,
                PaperSession.portfolio_id == portfolio.id,
            )
        )
        paper_sessions = list(sessions_result.scalars().all())
        if any(item.status not in {"active", "archived"} for item in paper_sessions):
            raise PaperTradingBootstrapIntegrityError(
                "A paper session has an unsupported lifecycle state."
            )
        active_sessions = [item for item in paper_sessions if item.status == "active"]
        if len(active_sessions) != 1:
            raise PaperTradingBootstrapIntegrityError(
                "Each portfolio must have exactly one active session."
            )
        active_session = active_sessions[0]
        if (
            active_session.account_id != account.id
            or active_session.portfolio_id != portfolio.id
            or not isinstance(active_session.session_number, int)
            or active_session.session_number < 1
        ):
            raise PaperTradingBootstrapIntegrityError(
                "The active paper session is inconsistent."
            )

        book_id = await validate_existing_paper_ledger(
            session,
            account_id=account.id,
            portfolio_id=portfolio.id,
            session_id=active_session.id,
            market_type=market_type,
        )

        snapshot_result = await session.execute(
            select(PaperLedgerAccount, PaperLedgerAccountSnapshot)
            .join(
                PaperLedgerAccountSnapshot,
                PaperLedgerAccountSnapshot.ledger_account_id == PaperLedgerAccount.id,
            )
            .where(
                PaperLedgerAccount.book_id == book_id,
                PaperLedgerAccountSnapshot.book_id == book_id,
            )
        )
        snapshot_rows = list(snapshot_result.all())
        balances: dict[str, str] = {}
        for ledger_account, snapshot in snapshot_rows:
            if ledger_account.code in balances:
                raise PaperTradingBootstrapIntegrityError(
                    "Duplicate ledger account snapshots exist."
                )
            balances[ledger_account.code] = _decimal_text(
                snapshot.balance,
                field=f"{market_type}.{ledger_account.code}",
            )

        expected_codes = {
            "cash_available",
            "spot_position_cost",
            "futures_margin_locked",
            "realized_pnl",
            "fee_expense",
            "contributed_capital",
        }
        if set(balances) != expected_codes:
            raise PaperTradingBootstrapIntegrityError(
                "The ledger snapshot account set is incomplete."
            )

        positions_result = await session.execute(
            select(PaperPosition)
            .where(
                PaperPosition.book_id == book_id,
                PaperPosition.market_type == market_type,
            )
            .order_by(PaperPosition.opened_at.asc(), PaperPosition.id.asc())
        )
        positions = list(positions_result.scalars().all())
        position_payload: list[dict[str, Any]] = []

        for position in positions:
            if position.status not in {"open", "closed"}:
                raise PaperTradingBootstrapIntegrityError(
                    "A paper position has an unsupported lifecycle state."
                )
            if position.side not in {"long", "short"}:
                raise PaperTradingBootstrapIntegrityError(
                    "A paper position has an unsupported side."
                )

            if market_type == "spot":
                if (
                    position.side != "long"
                    or position.leverage_snapshot is not None
                    or position.margin_locked is not None
                    or position.remaining_cost_basis is None
                ):
                    raise PaperTradingBootstrapIntegrityError(
                        "A Spot position has inconsistent market-specific fields."
                    )
                leverage_text = None
            else:
                if (
                    position.leverage_snapshot is None
                    or position.margin_locked is None
                    or position.remaining_cost_basis is not None
                ):
                    raise PaperTradingBootstrapIntegrityError(
                        "A Futures position has incomplete market-specific fields."
                    )

                leverage_text = _decimal_text(
                    position.leverage_snapshot,
                    field="position.leverage_snapshot",
                )
                if Decimal(leverage_text) <= 0:
                    raise PaperTradingBootstrapIntegrityError(
                        "A Futures position has a non-positive leverage snapshot."
                    )

            position_payload.append(
                {
                    "id": position.id,
                    "symbol": position.symbol,
                    "side": position.side,
                    "status": position.status,
                    "leverage": leverage_text,
                    "initial_quantity": _decimal_text(
                        position.initial_quantity, field="position.initial_quantity"
                    ),
                    "remaining_quantity": _decimal_text(
                        position.remaining_quantity, field="position.remaining_quantity"
                    ),
                    "entry_price": _decimal_text(
                        position.entry_price, field="position.entry_price"
                    ),
                    "remaining_cost_basis": _decimal_text(
                        position.remaining_cost_basis,
                        field="position.remaining_cost_basis",
                        nullable=True,
                    ),
                    "margin_locked": _decimal_text(
                        position.margin_locked,
                        field="position.margin_locked",
                        nullable=True,
                    ),
                    "realized_pnl": _decimal_text(
                        position.realized_pnl, field="position.realized_pnl"
                    ),
                    "entry_fee_original": _decimal_text(
                        position.entry_fee_original, field="position.entry_fee_original"
                    ),
                    "entry_fee_posted": _decimal_text(
                        position.entry_fee_posted, field="position.entry_fee_posted"
                    ),
                    "opened_at": _timestamp_text(
                        position.opened_at, field="position.opened_at"
                    ),
                    "closed_at": _timestamp_text(
                        position.closed_at,
                        field="position.closed_at",
                        nullable=True,
                    ),
                }
            )

        output_portfolios.append(
            {
                "id": portfolio.id,
                "market_type": market_type,
                "status": portfolio.status,
                "position_mode": portfolio.position_mode,
                "session": {
                    "id": active_session.id,
                    "session_number": active_session.session_number,
                    "status": active_session.status,
                },
                "ledger": {
                    "book_id": book_id,
                    **balances,
                },
                "positions": position_payload,
            }
        )

    return {
        "account": {
            "id": account.id,
            "name": account.name,
            "role": role.value,
        },
        "portfolios": output_portfolios,
    }
