"""Server-side authorization policy for paper trading operations."""

from __future__ import annotations

from types import MappingProxyType


class PaperAuthorizationError(PermissionError):
    """Raised when a membership role cannot perform an operation."""


_ROLE_PERMISSIONS = MappingProxyType({
    "owner": frozenset({"read", "trade", "manual_top_up", "reset"}),
    "admin": frozenset({"read", "trade", "manual_top_up", "reset"}),
    "trader": frozenset({"read", "trade"}),
    "viewer": frozenset({"read"}),
})


def require_paper_permission(role: str, operation: str) -> None:
    """Require an explicitly allowed operation for an active membership role.

    Membership status and account/user status must be verified separately
    by the database-backed authorization layer before calling this policy.
    """
    if not isinstance(role, str) or not isinstance(operation, str):
        raise PaperAuthorizationError("Invalid paper-trading authorization input.")

    permissions = _ROLE_PERMISSIONS.get(role)

    if permissions is None:
        raise PaperAuthorizationError("Unknown or unsupported membership role.")

    if operation not in {"read", "trade", "manual_top_up", "reset"}:
        raise PaperAuthorizationError("Unknown or unsupported paper operation.")

    if operation not in permissions:
        raise PaperAuthorizationError(
            "Membership role is not permitted to perform this operation."
        )
