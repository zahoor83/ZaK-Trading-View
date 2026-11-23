from __future__ import annotations

import json
import re

from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.identity import AuthIdentity, User
from app.security.oidc_configuration import Auth0Settings
from app.security.oidc_token_verification import VerifiedOIDCIdentity
from app.security.oidc_http import (
    create_oidc_http_client,
    read_bounded_response,
)


class OIDCAuthenticationError(Exception):
    """Generic OIDC authentication failure."""


class IdentityProvisioningError(Exception):
    """Identity could not be safely resolved or provisioned."""


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise IdentityProvisioningError("Invalid verified email.")

    normalized = email.strip().casefold()

    if not normalized or len(normalized) > 320:
        raise IdentityProvisioningError("Invalid verified email.")

    return normalized


def _validate_auth_endpoint(settings: Auth0Settings, endpoint: str) -> None:
    try:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError

        configured_host = settings.domain.lower()
        parsed = urlparse(endpoint)

        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() != configured_host
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.params
            or parsed.fragment
            or not parsed.path.startswith("/")
            or not parsed.path.strip("/")
        ):
            raise ValueError

    except Exception:
        raise OIDCAuthenticationError(
            "Invalid OIDC provider endpoint."
        ) from None


def build_authorization_url(
    settings: Auth0Settings,
    metadata: dict[str, Any],
    transaction: Any,
) -> str:
    endpoint = metadata.get("authorization_endpoint")

    if not isinstance(endpoint, str):
        raise OIDCAuthenticationError("Invalid OIDC authorization metadata.")

    _validate_auth_endpoint(settings, endpoint)

    required = (
        "state",
        "nonce",
        "code_challenge",
    )

    if any(
        not isinstance(getattr(transaction, field, None), str)
        or not getattr(transaction, field)
        for field in required
    ):
        raise OIDCAuthenticationError("Invalid OIDC transaction.")

    # Transaction state and nonce are generated with token_urlsafe(32).
    # The PKCE S256 challenge is a 43-character base64url value.
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", transaction.state)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", transaction.nonce)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", transaction.code_challenge)
    ):
        raise OIDCAuthenticationError("Invalid OIDC transaction.")

    if not isinstance(settings.callback_url, str) or not settings.callback_url:
        raise OIDCAuthenticationError("Invalid OIDC callback configuration.")

    query = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.callback_url,
        "scope": "openid email profile",
        "state": transaction.state,
        "nonce": transaction.nonce,
        "code_challenge": transaction.code_challenge,
        "code_challenge_method": "S256",
    }

    separator = "&" if "?" in endpoint else "?"
    return endpoint + separator + urlencode(query)


_TOKEN_MAX_RESPONSE_BYTES = 64 * 1024


async def exchange_authorization_code(
    settings: Auth0Settings,
    metadata: dict[str, Any],
    code: str,
    code_verifier: str,
) -> str:
    endpoint = metadata.get("token_endpoint")

    if not isinstance(endpoint, str):
        raise OIDCAuthenticationError("Invalid OIDC token metadata.")

    _validate_auth_endpoint(settings, endpoint)

    if (
        not isinstance(code, str)
        or not code
        or len(code) > 8192
        or not isinstance(code_verifier, str)
        or not 43 <= len(code_verifier) <= 128
        or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier)
    ):
        raise OIDCAuthenticationError("Invalid authorization response.")

    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "code": code,
        "redirect_uri": settings.callback_url,
        "code_verifier": code_verifier,
    }

    async def request_token(active_client: httpx.AsyncClient) -> bytes:
        return await read_bounded_response(
            active_client,
            "POST",
            endpoint,
            max_response_bytes=_TOKEN_MAX_RESPONSE_BYTES,
            timeout=httpx.Timeout(10.0),
            total_timeout_seconds=15.0,
            data=payload,
            headers={"Accept": "application/json"},
        )

    try:
        async with create_oidc_http_client(
            timeout=httpx.Timeout(10.0),
        ) as active_client:
            response_body = await request_token(active_client)

        body = json.loads(response_body)

        if not isinstance(body, dict):
            raise ValueError

        id_token = body.get("id_token")

        if not isinstance(id_token, str) or not id_token:
            raise ValueError

        return id_token

    except Exception:
        # Never expose provider response bodies, tokens, codes or secrets.
        raise OIDCAuthenticationError(
            "Authorization code exchange failed."
        ) from None

async def _find_eligible_identity(
    session: Any,
    issuer: str,
    subject: str,
) -> tuple[UUID, str] | None:
    statement = (
        select(User)
        .join(AuthIdentity, AuthIdentity.user_id == User.id)
        .where(
            AuthIdentity.issuer == issuer,
            AuthIdentity.subject == subject,
        )
    )

    result = await session.execute(statement)
    user = result.scalar_one_or_none()

    if user is None:
        return None

    if (
        user.status != "active"
        or user.email_verified is not True
        or not isinstance(user.email, str)
        or not user.email.strip()
    ):
        raise IdentityProvisioningError("Identity is not eligible.")

    return user.id, user.email


async def resolve_or_provision_user(
    session_factory: Any,
    identity: VerifiedOIDCIdentity,
) -> tuple[UUID, str]:
    if (
        not isinstance(identity.issuer, str)
        or not identity.issuer
        or not isinstance(identity.subject, str)
        or not identity.subject
        or len(identity.issuer) > 512
        or len(identity.subject) > 255
        or identity.email_verified is not True
    ):
        raise IdentityProvisioningError("Verified identity is not eligible.")

    normalized_email = normalize_email(identity.email)

    try:
        async with session_factory() as session:
            async with session.begin():
                existing_id = await _find_eligible_identity(
                    session,
                    identity.issuer,
                    identity.subject,
                )

                if existing_id is not None:
                    return existing_id

                email_statement = select(User.id).where(
                    User.email_normalized == normalized_email
                )
                email_result = await session.execute(email_statement)

                if email_result.scalar_one_or_none() is not None:
                    # Never automatically link identities by email.
                    raise IdentityProvisioningError(
                        "Identity requires separate account verification."
                    )

                user = User(
                    email=identity.email.strip(),
                    email_normalized=normalized_email,
                    email_verified=True,
                )
                session.add(user)
                await session.flush()

                auth_identity = AuthIdentity(
                    user_id=user.id,
                    issuer=identity.issuer,
                    subject=identity.subject,
                )
                session.add(auth_identity)
                await session.flush()

                created_user = (user.id, user.email)

        return created_user

    except IntegrityError:
        # A concurrent signup may have won a unique-constraint race.
        # Re-check the exact provider identity using a fresh session.
        try:
            async with session_factory() as recovery_session:
                existing_id = await _find_eligible_identity(
                    recovery_session,
                    identity.issuer,
                    identity.subject,
                )

            if existing_id is not None:
                return existing_id

        except IdentityProvisioningError:
            raise
        except Exception:
            raise IdentityProvisioningError(
                "Identity provisioning could not be completed."
            ) from None

        raise IdentityProvisioningError(
            "Identity provisioning could not be completed."
        ) from None
