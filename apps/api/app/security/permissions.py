"""Central, fail-closed role-based permission policy.

This module evaluates permissions only. It does not authenticate users,
load memberships, verify account ownership, or authorize resource scope.

Callers must obtain the role from a verified server-side membership record.
Never trust a role supplied by a client.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping


class AccountRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    TRADER = "trader"
    VIEWER = "viewer"


class Permission(str, Enum):
    ACCOUNT_READ = "account.read"
    PORTFOLIO_READ = "portfolio.read"

    PAPER_TRADE = "paper_trade.execute"
    POSITION_MANAGE = "position.manage"
    PORTFOLIO_RESET = "portfolio.reset"
    PORTFOLIO_REFRESH = "portfolio.refresh"
    MANUAL_TOP_UP = "portfolio.manual_top_up"
    FEE_POLICY_MANAGE = "paper_fee_policy.manage"

    MEMBERSHIP_READ = "membership.read"
    MEMBERSHIP_INVITE_TRADER_VIEWER = "membership.invite_trader_viewer"
    MEMBERSHIP_MANAGE_TRADER_VIEWER = "membership.manage_trader_viewer"
    MEMBERSHIP_MANAGE_ADMINS = "membership.manage_admins"

    OWNERSHIP_TRANSFER = "account.transfer_ownership"
    ACCOUNT_DELETE = "account.delete"

    AUDIT_READ_FULL = "audit.read_full"
    AUDIT_READ_OWN = "audit.read_own"
    AUDIT_READ_LIMITED = "audit.read_limited"


class PermissionDenied(PermissionError):
    """Raised when a role is not allowed to perform an action."""


_ALL_READ: Final[frozenset[Permission]] = frozenset(
    {
        Permission.ACCOUNT_READ,
        Permission.PORTFOLIO_READ,
        Permission.AUDIT_READ_LIMITED,
    }
)

_TRADING: Final[frozenset[Permission]] = frozenset(
    {
        Permission.PAPER_TRADE,
        Permission.POSITION_MANAGE,
    }
)

_PORTFOLIO_LIFECYCLE: Final[frozenset[Permission]] = frozenset(
    {
        Permission.PORTFOLIO_RESET,
        Permission.PORTFOLIO_REFRESH,
    }
)

_ADMINISTRATION: Final[frozenset[Permission]] = frozenset(
    {
        Permission.MEMBERSHIP_READ,
        Permission.MEMBERSHIP_INVITE_TRADER_VIEWER,
        Permission.MEMBERSHIP_MANAGE_TRADER_VIEWER,
        Permission.AUDIT_READ_FULL,
        Permission.AUDIT_READ_OWN,
    }
)

_PORTFOLIO_FINANCIAL_ADMIN: Final[frozenset[Permission]] = frozenset(
    {
        Permission.MANUAL_TOP_UP,
        Permission.FEE_POLICY_MANAGE,
    }
)

_OWNER_ONLY: Final[frozenset[Permission]] = frozenset(
    {
        Permission.MEMBERSHIP_MANAGE_ADMINS,
        Permission.OWNERSHIP_TRANSFER,
        Permission.ACCOUNT_DELETE,
    }
)

ROLE_PERMISSIONS: Final[Mapping[AccountRole, frozenset[Permission]]] = (
    MappingProxyType(
        {
            AccountRole.OWNER: frozenset(
                _ALL_READ | _TRADING | _PORTFOLIO_LIFECYCLE
                | _PORTFOLIO_FINANCIAL_ADMIN
                | _ADMINISTRATION | _OWNER_ONLY
            ),
            AccountRole.ADMIN: frozenset(
                _ALL_READ | _TRADING | _PORTFOLIO_LIFECYCLE
                | _PORTFOLIO_FINANCIAL_ADMIN
                | _ADMINISTRATION
            ),
            AccountRole.TRADER: frozenset(
                _ALL_READ
                | _TRADING
                | {Permission.AUDIT_READ_OWN}
            ),
            AccountRole.VIEWER: _ALL_READ,
        }
    )
)


def _normalize_role(value: object) -> AccountRole | None:
    if isinstance(value, AccountRole):
        return value

    if type(value) is not str:
        return None

    try:
        return AccountRole(value)
    except ValueError:
        return None


def _normalize_permission(value: object) -> Permission | None:
    if isinstance(value, Permission):
        return value

    if type(value) is not str:
        return None

    try:
        return Permission(value)
    except ValueError:
        return None


def is_allowed(role: AccountRole | str, permission: Permission | str) -> bool:
    """Return False for unknown roles, permissions, and invalid input types."""
    normalized_role = _normalize_role(role)
    normalized_permission = _normalize_permission(permission)

    if normalized_role is None or normalized_permission is None:
        return False

    return normalized_permission in ROLE_PERMISSIONS[normalized_role]


def require_permission(
    role: AccountRole | str,
    permission: Permission | str,
) -> None:
    """Raise PermissionDenied unless the requested permission is granted."""
    normalized_role = _normalize_role(role)
    normalized_permission = _normalize_permission(permission)

    if (
        normalized_role is None
        or normalized_permission is None
        or not is_allowed(normalized_role, normalized_permission)
    ):
        raise PermissionDenied("The requested action is not permitted.")

