"""Explicit local-development shared-secret adapter for the Next.js BFF."""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

from fastapi import Request

from app.security.internal_service_auth import InternalServicePrincipal


HEADER_NAME = "X-Zak-Internal-Service-Secret"
LOCAL_SERVICE_ID = "web-bff"
LOCAL_AUTH_MODE = "local_shared_secret"
MIN_SECRET_LENGTH = 32


class LocalSharedSecretAuthenticator:
    """Authenticate the single configured BFF using a local-only secret."""

    def __init__(self, secret: str) -> None:
        if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
            raise ValueError("Local internal service secret is invalid.")
        self._secret = secret

    async def authenticate(
        self,
        request: Request,
    ) -> InternalServicePrincipal | None:
        supplied = request.headers.get(HEADER_NAME)
        if not isinstance(supplied, str):
            return None

        try:
            supplied_bytes = supplied.encode("utf-8", errors="strict")
            expected_bytes = self._secret.encode("utf-8", errors="strict")
        except UnicodeError:
            return None

        if not hmac.compare_digest(supplied_bytes, expected_bytes):
            return None

        return InternalServicePrincipal(service_id=LOCAL_SERVICE_ID)


def create_local_internal_authentication(
    environ: Mapping[str, str] | None = None,
) -> tuple[LocalSharedSecretAuthenticator | None, frozenset[str]]:
    """Load explicitly enabled local authentication; otherwise fail closed."""

    source = os.environ if environ is None else environ
    mode = source.get("OIDC_INTERNAL_AUTH_MODE", "").strip()
    app_env = source.get("APP_ENV", "").strip().lower()

    if not mode:
        return None, frozenset()

    if mode != LOCAL_AUTH_MODE:
        raise RuntimeError("Unsupported internal authentication mode.")

    if app_env != "development":
        raise RuntimeError(
            "Local shared-secret authentication is permitted only in development."
        )

    secret = source.get("OIDC_INTERNAL_SERVICE_SECRET", "")
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            "Local internal service authentication is not configured safely."
        )

    return (
        LocalSharedSecretAuthenticator(secret),
        frozenset({LOCAL_SERVICE_ID}),
    )