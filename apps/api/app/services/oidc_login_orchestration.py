from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

import httpx

from app.security.oidc_configuration import Auth0Settings
from app.security.oidc_token_verification import (
    OIDCJWKSCache,
    OIDCVerificationError,
    VerifiedOIDCIdentity,
    verify_id_token,
)
from app.services.oidc_auth_service import (
    IdentityProvisioningError,
    OIDCAuthenticationError,
    exchange_authorization_code,
    resolve_or_provision_user,
)
from app.services.oidc_transaction_service import (
    OIDCTransactionEncryptionSettings,
    OIDCTransactionError,
    consume_oidc_auth_transaction,
)
from app.services.account_service import ensure_default_account_with_initial_portfolios
from app.services.session_service import (
    SessionServiceError,
    create_user_session,
)


SESSION_ABSOLUTE_TTL = timedelta(hours=12)


class OIDCLoginError(Exception):
    """Generic failure while completing an OIDC login."""


@dataclass(frozen=True, slots=True)
class OIDCLoginResult:
    user_id: UUID
    email: str
    session_id: UUID
    raw_session_token: str
    expires_at: datetime


def _require_aware_datetime(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("now must be a timezone-aware datetime.")


async def complete_oidc_login(
    *,
    settings: Auth0Settings,
    metadata: dict[str, Any],
    encryption: OIDCTransactionEncryptionSettings,
    session_factory: Callable[[], Any],
    jwks_cache: OIDCJWKSCache,
    state: str,
    browser_binding: str,
    authorization_code: str,
    now: datetime | None = None,
) -> OIDCLoginResult:
    """Consume one OIDC transaction and establish one local session.

    Metadata must come from the validated Auth0 discovery document. Each
    database operation receives a fresh session. Provider tokens other than
    the ID token are never retained or returned.
    """
    current_time = now if now is not None else datetime.now(timezone.utc)
    _require_aware_datetime(current_time)

    if not callable(session_factory):
        raise OIDCLoginError("Authentication could not be completed.")
    if not isinstance(metadata, dict):
        raise OIDCLoginError("Authentication could not be completed.")
    if not isinstance(state, str) or not state:
        raise OIDCLoginError("Authentication could not be completed.")
    if not isinstance(browser_binding, str) or not browser_binding:
        raise OIDCLoginError("Authentication could not be completed.")
    if not isinstance(authorization_code, str) or not authorization_code:
        raise OIDCLoginError("Authentication could not be completed.")

    try:
        # Transaction service owns its transaction; this session is dedicated.
        async with session_factory() as transaction_session:
            consumed = await consume_oidc_auth_transaction(
                transaction_session,
                state=state,
                browser_binding=browser_binding,
                encryption=encryption,
                now=current_time,
            )

        id_token = await exchange_authorization_code(
            settings,
            metadata,
            authorization_code,
            consumed.code_verifier,
        )

        identity: VerifiedOIDCIdentity = await verify_id_token(
            id_token,
            settings,
            jwks_cache,
            consumed.nonce_hash,
            now_epoch=current_time.timestamp(),
        )

        # Provisioning manages its own fresh session and rejects unverified email.
        user_id, canonical_email = await resolve_or_provision_user(
            session_factory,
            identity,
        )
        if (
            not isinstance(canonical_email, str)
            or not canonical_email.strip()
        ):
            raise IdentityProvisioningError(
                "Canonical user email is unavailable."
            )

        # Provision the default account before issuing a browser session.
        # The account service owns its transaction and requires a fresh session.
        async with session_factory() as account_session:
            await ensure_default_account_with_initial_portfolios(
                account_session,
                owner_user_id=user_id,
            )

        # Session service owns its transaction and requires a fresh session.
        expires_at = current_time + SESSION_ABSOLUTE_TTL
        async with session_factory() as session:
            created = await create_user_session(
                session,
                user_id=user_id,
                expires_at=expires_at,
                now=current_time,
            )

        return OIDCLoginResult(
            user_id=user_id,
            email=canonical_email,
            session_id=created.session_id,
            raw_session_token=created.raw_token,
            expires_at=created.expires_at,
        )

    except (
        OIDCTransactionError,
        OIDCAuthenticationError,
        OIDCVerificationError,
        IdentityProvisioningError,
        SessionServiceError,
        httpx.HTTPError,
        ValueError,
        TypeError,
    ):
        raise OIDCLoginError(
            "Authentication could not be completed."
        ) from None
    except Exception:
        # Fail closed and never expose provider, database, or token details.
        raise OIDCLoginError(
            "Authentication could not be completed."
        ) from None
