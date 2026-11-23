"""Lazy, fail-closed OIDC runtime composition."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Callable

from app.security.oidc_configuration import (
    Auth0Settings,
    OIDCConfigurationError,
    fetch_provider_jwks,
    fetch_provider_metadata,
)
from app.security.oidc_token_verification import OIDCJWKSCache
from app.services.oidc_transaction_service import (
    OIDCTransactionConfigurationError,
    OIDCTransactionEncryptionSettings,
)


class OIDCRuntimeUnavailableError(RuntimeError):
    """Generic error raised when OIDC runtime services are unavailable."""


class OIDCRuntime:
    """Owns validated OIDC settings and lazy provider metadata/JWKS access."""

    def __init__(
        self,
        *,
        settings: Auth0Settings | None = None,
        encryption: OIDCTransactionEncryptionSettings | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._encryption = encryption
        self._session_factory = session_factory
        self._metadata: dict[str, Any] | None = None
        self._metadata_lock = asyncio.Lock()

        self._available = (
            settings is not None
            and encryption is not None
            and callable(session_factory)
        )

        self._jwks_cache: OIDCJWKSCache | None = None

        if self._available:
            self._jwks_cache = OIDCJWKSCache(self._fetch_jwks)

    @classmethod
    def unavailable(cls) -> "OIDCRuntime":
        return cls()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def encryption(self) -> OIDCTransactionEncryptionSettings:
        if not self._available or self._encryption is None:
            raise OIDCRuntimeUnavailableError(
                "OIDC authentication is unavailable."
            )
        return self._encryption

    @property
    def settings(self) -> Auth0Settings:
        if not self._available or self._settings is None:
            raise OIDCRuntimeUnavailableError(
                "OIDC authentication is unavailable."
            )
        return self._settings

    @property
    def session_factory(self) -> Callable[[], Any]:
        if not self._available or self._session_factory is None:
            raise OIDCRuntimeUnavailableError(
                "OIDC authentication is unavailable."
            )
        return self._session_factory

    @property
    def jwks_cache(self) -> OIDCJWKSCache:
        if not self._available or self._jwks_cache is None:
            raise OIDCRuntimeUnavailableError(
                "OIDC authentication is unavailable."
            )
        return self._jwks_cache

    async def get_metadata(self) -> dict[str, Any]:
        if not self._available or self._settings is None:
            raise OIDCRuntimeUnavailableError(
                "OIDC authentication is unavailable."
            )

        if self._metadata is not None:
            return self._metadata

        async with self._metadata_lock:
            if self._metadata is not None:
                return self._metadata

            try:
                metadata = await fetch_provider_metadata(self._settings)
            except Exception:
                raise OIDCRuntimeUnavailableError(
                    "OIDC provider metadata is unavailable."
                ) from None

            if not isinstance(metadata, dict):
                raise OIDCRuntimeUnavailableError(
                    "OIDC provider metadata is unavailable."
                )

            self._metadata = metadata
            return self._metadata

    async def _fetch_jwks(self) -> Mapping[str, Any]:
        if not self._available or self._settings is None:
            raise OIDCRuntimeUnavailableError(
                "OIDC authentication is unavailable."
            )

        metadata = await self.get_metadata()

        return await fetch_provider_jwks(
            self._settings,
            metadata,
        )

    def __repr__(self) -> str:
        return f"OIDCRuntime(available={self._available})"


def create_oidc_runtime(
    database_manager: Any,
    environ: Mapping[str, str] | None = None,
) -> OIDCRuntime:
    """Build OIDC services without network access or database connectivity checks."""

    if database_manager is None:
        return OIDCRuntime.unavailable()

    if not getattr(database_manager, "is_initialized", False):
        return OIDCRuntime.unavailable()

    try:
        session_factory = database_manager.session_factory()

        if not callable(session_factory):
            return OIDCRuntime.unavailable()

        settings = Auth0Settings.from_environment(environ)
        encryption = OIDCTransactionEncryptionSettings.from_environment(
            environ
        )

        return OIDCRuntime(
            settings=settings,
            encryption=encryption,
            session_factory=session_factory,
        )

    except (
        OIDCConfigurationError,
        OIDCTransactionConfigurationError,
        RuntimeError,
        ValueError,
        TypeError,
    ):
        return OIDCRuntime.unavailable()