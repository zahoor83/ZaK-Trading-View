"""Transaction-scoped, fail-closed authorization for Paper Trading writes.

The caller owns an already-open transaction. This helper never begins,
commits, or rolls back a transaction. It locks and validates the complete
resource chain in deterministic order before returning write access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import AccountMembership, TradingAccount, User
from app.models.paper_ledger import PaperLedgerBook
from app.models.portfolio import PaperPortfolio, PaperSession
from app.security.permissions import AccountRole, Permission, is_allowed


class PaperWriteAuthorizationDenied(PermissionError):
    """Generic denial for invalid or unauthorized Paper Trading writes."""


class PaperWriteTransactionRequired(RuntimeError):
    """The caller must establish the transaction before authorization."""


_WRITE_PERMISSIONS = frozenset(
    {
        Permission.PAPER_TRADE,
        Permission.POSITION_MANAGE,
        Permission.PORTFOLIO_RESET,
        Permission.PORTFOLIO_REFRESH,
        Permission.MANUAL_TOP_UP,
        Permission.FEE_POLICY_MANAGE,
    }
)


@dataclass(frozen=True, slots=True)
class LockedPaperWriteAccess:
    """Verified identifiers and role; row locks remain held by caller transaction."""

    user_id: uuid.UUID
    account_id: uuid.UUID
    portfolio_id: uuid.UUID
    session_id: uuid.UUID
    book_id: uuid.UUID
    role: AccountRole
    market_type: str
    position_mode: str | None


def _deny() -> PaperWriteAuthorizationDenied:
    return PaperWriteAuthorizationDenied(
        "Paper Trading operation is not permitted."
    )


async def authorize_paper_write(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    permission: Permission,
) -> LockedPaperWriteAccess:
    """Lock User -> Account -> Membership -> Portfolio -> Session; read and verify Book.

    Call only inside the financial service's already-open transaction.
    All participating lifecycle/write services must follow the same lock order.
    This function does not independently protect against mutation services
    that bypass the shared parent-row locking protocol.
    """

    if (
        not isinstance(authenticated_user_id, uuid.UUID)
        or not isinstance(account_id, uuid.UUID)
        or not isinstance(portfolio_id, uuid.UUID)
    ):
        raise _deny()

    if not isinstance(permission, Permission) or permission not in _WRITE_PERMISSIONS:
        raise _deny()

    if not session.in_transaction():
        raise PaperWriteTransactionRequired(
            "Paper write authorization requires an active caller-owned transaction."
        )

    # 1. User: matches account provisioning's existing first lock.
    user_result = await session.execute(
        select(User)
        .where(User.id == authenticated_user_id)
        .with_for_update(of=User)
    )
    user = user_result.scalar_one_or_none()

    if user is None or user.id != authenticated_user_id or user.status != "active":
        raise _deny()

    # 2. Account: common serialization point for account-scoped mutations.
    account_result = await session.execute(
        select(TradingAccount)
        .where(TradingAccount.id == account_id)
        .with_for_update(of=TradingAccount)
    )
    account = account_result.scalar_one_or_none()

    if account is None or account.id != account_id or account.status != "active":
        raise _deny()

    # 3. Membership: lock the exact account/user membership row.
    membership_result = await session.execute(
        select(AccountMembership)
        .where(
            AccountMembership.account_id == account_id,
            AccountMembership.user_id == authenticated_user_id,
        )
        .with_for_update(of=AccountMembership)
    )
    membership = membership_result.scalar_one_or_none()

    if (
        membership is None
        or membership.account_id != account_id
        or membership.user_id != authenticated_user_id
        or membership.status != "active"
    ):
        raise _deny()

    try:
        role = AccountRole(membership.role)
    except (TypeError, ValueError):
        raise _deny() from None

    if not is_allowed(role, permission):
        raise _deny()

    # 4. Portfolio: exact account/portfolio pair.
    portfolio_result = await session.execute(
        select(PaperPortfolio)
        .where(
            PaperPortfolio.account_id == account_id,
            PaperPortfolio.id == portfolio_id,
        )
        .with_for_update(of=PaperPortfolio)
    )
    portfolio = portfolio_result.scalar_one_or_none()

    if (
        portfolio is None
        or portfolio.account_id != account_id
        or portfolio.id != portfolio_id
        or portfolio.status != "active"
        or portfolio.market_type not in {"spot", "futures"}
        or (
            portfolio.market_type == "spot"
            and portfolio.position_mode is not None
        )
        or (
            portfolio.market_type == "futures"
            and portfolio.position_mode not in {"one_way", "hedge"}
        )
    ):
        raise _deny()

    # 5. Active session belonging to this exact account and portfolio.
    session_result = await session.execute(
        select(PaperSession)
        .where(
            PaperSession.account_id == account_id,
            PaperSession.portfolio_id == portfolio_id,
            PaperSession.status == "active",
        )
        .with_for_update(of=PaperSession)
    )
    paper_session = session_result.scalar_one_or_none()

    if (
        paper_session is None
        or paper_session.account_id != account_id
        or paper_session.portfolio_id != portfolio_id
        or paper_session.status != "active"
    ):
        raise _deny()

    # 6. Read and validate the active financial book for this session and market.
    book_result = await session.execute(
        select(PaperLedgerBook)
        .where(
            PaperLedgerBook.account_id == account_id,
            PaperLedgerBook.portfolio_id == portfolio_id,
            PaperLedgerBook.session_id == paper_session.id,
            PaperLedgerBook.status == "active",
        )
        
    )
    book = book_result.scalar_one_or_none()

    if (
        book is None
        or book.account_id != account_id
        or book.portfolio_id != portfolio_id
        or book.session_id != paper_session.id
        or book.status != "active"
        or book.market_type != portfolio.market_type
    ):
        raise _deny()

    return LockedPaperWriteAccess(
        user_id=authenticated_user_id,
        account_id=account_id,
        portfolio_id=portfolio_id,
        session_id=paper_session.id,
        book_id=book.id,
        role=role,
        market_type=portfolio.market_type,
        position_mode=portfolio.position_mode,
    )
