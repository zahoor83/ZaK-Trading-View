"""Fail-closed internal service authentication boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request, status


@dataclass(frozen=True, slots=True)
class InternalServicePrincipal:
    """Identity established by a trusted server-side authenticator."""

    service_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.service_id, str)
            or not self.service_id.strip()
            or len(self.service_id) > 128
        ):
            raise ValueError("Invalid internal service identity.")


class InternalServiceAuthenticator(Protocol):
    """Server-side adapter contract for authenticating an internal caller."""

    async def authenticate(
        self,
        request: Request,
    ) -> InternalServicePrincipal | None:
        ...


async def get_internal_service_principal(
    request: Request,
) -> InternalServicePrincipal:
    """Require a trusted authenticator and explicit immutable allowlist."""

    authenticator = getattr(
        request.app.state,
        "internal_service_authenticator",
        None,
    )

    if authenticator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal authentication is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    allowed_ids = getattr(
        request.app.state,
        "internal_service_allowed_ids",
        None,
    )

    if (
        not isinstance(allowed_ids, frozenset)
        or not allowed_ids
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 128
            for item in allowed_ids
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal authentication is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    try:
        principal = await authenticator.authenticate(request)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal authentication is unavailable.",
            headers={"Cache-Control": "no-store"},
        ) from None

    if not isinstance(principal, InternalServicePrincipal):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Internal service authentication required.",
            headers={"Cache-Control": "no-store"},
        )

    if principal.service_id not in allowed_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal service is not authorized.",
            headers={"Cache-Control": "no-store"},
        )

    return principal