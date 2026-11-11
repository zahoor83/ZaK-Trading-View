"""ASGI entry point for the ZaK Trading View API.

The legacy app.main module is intentionally left untouched.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from app.api.v1.market_data import router as market_data_router
from app.market_data.config import MarketDataConfig
from app.market_data.environment import market_data_config_from_environment
from app.market_data.service import MarketDataService


def create_app(
    *,
    service_factory: Callable[[MarketDataConfig], MarketDataService] = MarketDataService,
    config_factory: Callable[[], MarketDataConfig] = market_data_config_from_environment,
) -> FastAPI:
    """Create an isolated ASGI application with managed service lifecycle."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
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
            application.state.market_data_service = None
            raise
        try:
            yield
        finally:
            await service.stop()
            application.state.market_data_service = None

    application = FastAPI(
        title="ZaK Trading View API",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.include_router(market_data_router)
    return application


app = create_app()
