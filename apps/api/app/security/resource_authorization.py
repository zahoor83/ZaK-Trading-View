"""Fail-closed authorization for account-owned paper portfolios.

The authenticated user ID must come from a trusted authentication layer.
The requested portfolio must belong to the requested account and the
authenticated user must have an active membership in that account.

Trader-specific reset/refresh is blocked until portfolio-level assignment
and authorization rules are implemented. Manual top-up is Owner/Admin only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import AccountMembership, TradingAccount, User
from app.models.portfolio import PaperPortfolio
from app.security.permissions import (
    AccountRole,
    Permission,
    PermissionDenied,
    is_allowed,
)


class PortfolioAccessDenied(PermissionError):
    """Generic denial for missing, inactive, or unauthorized resources."""


@dataclass(frozen=True, slots=True)
class PortfolioAccess:
    account_id: uuid.UUID
    portfolio_id: uuid.UUID
    user_id: uuid.UUID
    role: AccountRole
    market_type: str


_PORTFOLIO_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.PORTFOLIO_READ,
        Permission.PAPER_TRADE,
        Permission.POSITION_MANAGE,
        Permission.PORTFOLIO_RESET,
        Permission.PORTFOLIO_REFRESH,
        Permission.MANUAL_TOP_UP,
    }
)

_TRADER_REQUIRES_PORTFOLIO_ASSIGNMENT: Final[frozenset[Permission]] = (
    frozenset(
        {
            Permission.PORTFOLIO_RESET,
            Permission.PORTFOLIO_REFRESH,
        }
    )
)


async def authorize_portfolio_permission(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    permission: Permission,
) -> PortfolioAccess:
    """Verify membership, account ownership, portfolio status and permission.

    A missing or mismatched portfolio is indistinguishable from missing
    membership. No portfolio is authorized solely by its client-supplied ID.

    This function performs a read-only authorization lookup. Financial
    write services must repeat authorization inside their own transaction.
    """

    if (
        not isinstance(authenticated_user_id, uuid.UUID)
        or not isinstance(account_id, uuid.UUID)
        or not isinstance(portfolio_id, uuid.UUID)
    ):
        raise PortfolioAccessDenied("Portfolio access is not permitted.")

    if not isinstance(permission, Permission):
        raise PermissionDenied("The requested action is not permitted.")

    if permission not in _PORTFOLIO_PERMISSIONS:
        raise PermissionDenied("The requested action is not permitted.")

    statement = (
        select(
            AccountMembership.role,
            AccountMembership.status,
            TradingAccount.status,
            User.status,
            PaperPortfolio.status,
            PaperPortfolio.market_type,
        )
        .join(
            TradingAccount,
            TradingAccount.id == AccountMembership.account_id,
        )
        .join(
            User,
            User.id == AccountMembership.user_id,
        )
        .join(
            PaperPortfolio,
            PaperPortfolio.account_id == TradingAccount.id,
        )
        .where(
            AccountMembership.account_id == account_id,
            AccountMembership.user_id == authenticated_user_id,
            AccountMembership.status == "active",
            TradingAccount.status == "active",
            User.status == "active",
            PaperPortfolio.account_id == account_id,
            PaperPortfolio.id == portfolio_id,
            PaperPortfolio.status == "active",
        )
    )

    result = await session.execute(statement)
    row = result.one_or_none()

    if row is None:
        raise PortfolioAccessDenied("Portfolio access is not permitted.")

    (
        role_value,
        membership_status,
        account_status,
        user_status,
        portfolio_status,
        market_type,
    ) = row

    if (
        membership_status != "active"
        or account_status != "active"
        or user_status != "active"
        or portfolio_status != "active"
        or market_type not in {"spot", "futures"}
    ):
        raise PortfolioAccessDenied("Portfolio access is not permitted.")

    try:
        role = AccountRole(role_value)
    except (TypeError, ValueError):
        raise PortfolioAccessDenied(
            "Portfolio access is not permitted."
        ) from None

    if not is_allowed(role, permission):
        raise PermissionDenied("The requested action is not permitted.")

    # No portfolio-level membership/assignment table exists yet.
    # Do not interpret account-wide Trader membership as authorization
    # to reset or refresh every portfolio in that account.
    if (
        role is AccountRole.TRADER
        and permission in _TRADER_REQUIRES_PORTFOLIO_ASSIGNMENT
    ):
        raise PortfolioAccessDenied("Portfolio access is not permitted.")

    return PortfolioAccess(
        account_id=account_id,
        portfolio_id=portfolio_id,
        user_id=authenticated_user_id,
        role=role,
        market_type=market_type,
    )
