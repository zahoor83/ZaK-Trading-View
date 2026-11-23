"""Server-side account membership authorization.

The authenticated user ID must originate from a trusted authentication
layer. This module never accepts a client-supplied role as authority.

This module performs authorization lookup only. Sensitive write services
must revalidate authorization inside their own database transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import AccountMembership, TradingAccount, User
from app.security.permissions import (
    AccountRole,
    Permission,
    PermissionDenied,
    is_allowed,
)


class AccountAccessDenied(PermissionError):
    """Generic denial for missing or inactive account access."""


@dataclass(frozen=True, slots=True)
class AccountAccess:
    account_id: uuid.UUID
    user_id: uuid.UUID
    role: AccountRole


async def load_account_access(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
) -> AccountAccess:
    """Load an active account membership for a trusted authenticated user.

    Missing membership, inactive membership, inactive user, inactive
    account, and unknown database role all fail closed with one generic
    exception to avoid disclosing account membership information.
    """

    if (
        not isinstance(authenticated_user_id, uuid.UUID)
        or not isinstance(account_id, uuid.UUID)
    ):
        raise AccountAccessDenied("Account access is not permitted.")

    statement = (
        select(
            AccountMembership.role,
            AccountMembership.status,
            TradingAccount.status,
            User.status,
        )
        .join(
            TradingAccount,
            TradingAccount.id == AccountMembership.account_id,
        )
        .join(
            User,
            User.id == AccountMembership.user_id,
        )
        .where(
            AccountMembership.account_id == account_id,
            AccountMembership.user_id == authenticated_user_id,
            AccountMembership.status == "active",
        )
    )

    result = await session.execute(statement)
    row = result.one_or_none()

    if row is None:
        raise AccountAccessDenied("Account access is not permitted.")

    role_value, membership_status, account_status, user_status = row

    if (
        membership_status != "active"
        or account_status != "active"
        or user_status != "active"
    ):
        raise AccountAccessDenied("Account access is not permitted.")

    try:
        role = AccountRole(role_value)
    except (TypeError, ValueError):
        raise AccountAccessDenied(
            "Account access is not permitted."
        ) from None

    return AccountAccess(
        account_id=account_id,
        user_id=authenticated_user_id,
        role=role,
    )


async def authorize_account_permission(
    session: AsyncSession,
    *,
    authenticated_user_id: uuid.UUID,
    account_id: uuid.UUID,
    permission: Permission,
) -> AccountAccess:
    """Verify active membership and then enforce the central RBAC policy."""

    access = await load_account_access(
        session,
        authenticated_user_id=authenticated_user_id,
        account_id=account_id,
    )

    if not is_allowed(access.role, permission):
        raise PermissionDenied("The requested action is not permitted.")

    return access
