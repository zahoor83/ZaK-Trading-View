"""ASGI entry point for the ZaK Trading View API.

The legacy app.main module is intentionally left untouched.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Callable

from app.security.internal_service_auth import InternalServiceAuthenticator
from app.security.internal_service_auth_factory import (
    load_internal_authentication_factory,
)

from fastapi import FastAPI

from app.api.v1.database_health import router as database_health_router
from app.api.v1.internal_auth import router as internal_auth_router
from app.api.v1.internal_auth_callbacks import router as internal_auth_callbacks_router
from app.api.v1.market_data import router as market_data_router
from app.api.v1.paper_trading import router as paper_trading_router
from app.database.session import DatabaseManager
from app.market_data.config import MarketDataConfig
from app.market_data.environment import market_data_config_from_environment
from app.market_data.service import MarketDataService
from app.security.oidc_runtime import create_oidc_runtime
from app.security.local_shared_secret_auth import (
    create_local_internal_authentication,
)
from app.services.oidc_transaction_cleanup import run_oidc_transaction_cleanup
from app.services.hmac_nonce_cleanup import run_hmac_nonce_cleanup


def create_app(
    *,
    service_factory: Callable[[MarketDataConfig], MarketDataService] = MarketDataService,
    config_factory: Callable[[], MarketDataConfig] = market_data_config_from_environment,
    internal_authentication: tuple[
        InternalServiceAuthenticator, frozenset[str]
    ] | None = None,
) -> FastAPI:
    """Create an isolated ASGI application with managed service lifecycle.

    Production requires an explicitly injected server-side authenticator.
    The local shared-secret adapter remains development-only.
    """

    app_env = os.environ.get("APP_ENV", "").strip().lower()
    auth_mode = os.environ.get("OIDC_INTERNAL_AUTH_MODE", "").strip()

    factory_spec = os.environ.get("OIDC_INTERNAL_AUTH_FACTORY", "").strip()

    if app_env not in {"development", "production"}:
        raise RuntimeError(
            "APP_ENV must be explicitly set to development or production."
        )

    if app_env == "production":
        if auth_mode != "injected":
            raise RuntimeError(
                "Production requires OIDC_INTERNAL_AUTH_MODE=injected."
            )

        if internal_authentication is not None and factory_spec:
            raise RuntimeError(
                "Configure either direct authentication injection or a factory, not both."
            )

        if internal_authentication is None:
            if not factory_spec:
                raise RuntimeError(
                    "Production internal service authentication adapter is not injected."
                )
            internal_authentication = load_internal_authentication_factory(
                factory_spec
            )

        if (
            not isinstance(internal_authentication, tuple)
            or len(internal_authentication) != 2
        ):
            raise RuntimeError(
                "Production internal service authentication configuration is invalid."
            )

        internal_authenticator, internal_allowed_ids = internal_authentication

        if (
            internal_authenticator is None
            or not callable(getattr(internal_authenticator, "authenticate", None))
            or not isinstance(internal_allowed_ids, frozenset)
            or not internal_allowed_ids
            or any(
                not isinstance(service_id, str)
                or not service_id
                or service_id != service_id.strip()
                or len(service_id) > 128
                or any(ord(character) < 33 or ord(character) == 127
                       for character in service_id)
                for service_id in internal_allowed_ids
            )
        ):
            raise RuntimeError(
                "Production internal service authentication configuration is invalid."
            )

    else:
        if factory_spec:
            raise RuntimeError(
                "Production authentication factory is only accepted in production."
            )
        if internal_authentication is not None:
            raise RuntimeError(
                "Injected production authentication is only accepted in production."
            )

        internal_authenticator, internal_allowed_ids = (
            create_local_internal_authentication()
        )
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if internal_authenticator is not None:
            application.state.internal_service_authenticator = internal_authenticator
            application.state.internal_service_allowed_ids = internal_allowed_ids
        database_manager = DatabaseManager.from_environment()
        application.state.database_manager = database_manager

        service = None
        service_started = False
        oidc_cleanup_task = None
        hmac_nonce_cleanup_task = None

        try:
            database_manager.initialize()
            application.state.oidc_runtime = create_oidc_runtime(
                database_manager
            )

            if database_manager.is_initialized:
                oidc_cleanup_task = asyncio.create_task(
                    run_oidc_transaction_cleanup(
                        database_manager.session_factory(),
                        interval_seconds=60,
                        batch_size=100,
                    ),
                    name="oidc-transaction-cleanup",
                )

                hmac_nonce_cleanup_task = asyncio.create_task(
                    run_hmac_nonce_cleanup(
                        database_manager.session_factory(),
                        interval_seconds=60,
                        batch_size=100,
                    ),
                    name="hmac-nonce-cleanup",
                )

            config = config_factory()
            service = service_factory(config)
            application.state.market_data_service = service

            try:
                await service.start()
            except BaseException:
                try:
                    await service.stop()
                except BaseException:
                    pass
                service = None
                raise

            service_started = True
            yield

        finally:
            try:
                if service is not None and service_started:
                    await service.stop()
            finally:
                cleanup_tasks = tuple(
                    task
                    for task in (
                        oidc_cleanup_task,
                        hmac_nonce_cleanup_task,
                    )
                    if task is not None
                )

                for task in cleanup_tasks:
                    task.cancel()

                for task in cleanup_tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                await database_manager.dispose()
                application.state.database_manager = None
                application.state.oidc_runtime = None
                application.state.market_data_service = None

    application = FastAPI(
        title="ZaK Trading View API",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.include_router(market_data_router)
    application.include_router(paper_trading_router)
    application.include_router(database_health_router)
    application.include_router(internal_auth_router)
    application.include_router(internal_auth_callbacks_router)
    return application


app = create_app()