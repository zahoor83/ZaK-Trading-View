"""Managed asynchronous database engine and request sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.config import DatabaseSettings


class DatabaseManager:
    """Owns one API-process database engine and its session factory."""

    def __init__(self, settings: DatabaseSettings | None) -> None:
        self._settings = settings
        self._engine = None
        self._session_factory = None

    @classmethod
    def from_environment(cls) -> DatabaseManager:
        return cls(DatabaseSettings.from_environment())

    @property
    def is_configured(self) -> bool:
        return self._settings is not None

    @property
    def is_initialized(self) -> bool:
        return self._engine is not None

    def initialize(self) -> None:
        """Create the engine without opening a network connection."""
        if self._settings is None:
            return

        if self._engine is not None:
            return

        self._engine = create_async_engine(
            self._settings.url,
            pool_pre_ping=True,
            echo=False,
        )

        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def ping(self) -> bool:
        """Verify actual database connectivity."""
        if self._engine is None:
            return False

        async with self._engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return result.scalar_one() == 1

    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database session factory is unavailable.")

        return self._session_factory

    async def dispose(self) -> None:
        """Release the engine and all associated pooled connections."""
        if self._engine is not None:
            engine = self._engine
            self._engine = None
            self._session_factory = None
            await engine.dispose()


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for request-scoped database sessions."""
    manager = getattr(request.app.state, "database_manager", None)

    if manager is None or not manager.is_initialized:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service is unavailable.",
            headers={"Cache-Control": "no-store"},
        )

    factory = manager.session_factory()

    async with factory() as session:
        yield session