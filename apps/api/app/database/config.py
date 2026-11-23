"""Strict configuration for the asynchronous PostgreSQL connection."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from sqlalchemy.engine import make_url


def _load_local_environment(env_file: Path | None = None) -> None:
    """Load the repository .env only for local development.

    Existing process environment variables always take precedence.
    """
    path = env_file or Path(__file__).resolve().parents[4] / ".env"

    if not path.is_file():
        return

    file_values = dotenv_values(path)

    configured_environment = os.environ.get("APP_ENV")
    if configured_environment is None:
        configured_environment = file_values.get("APP_ENV")

    environment = (configured_environment or "development").strip().lower()

    if environment in {"development", "test"}:
        load_dotenv(dotenv_path=path, override=False)


# Do not inject local development configuration into pytest processes.
if "pytest" not in sys.modules:
    _load_local_environment()


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    # Prevent accidental disclosure of credentials through repr().
    url: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> DatabaseSettings | None:
        raw_url = os.environ.get("DATABASE_URL", "").strip()

        # Database infrastructure remains disabled until explicitly configured.
        if not raw_url:
            return None

        try:
            parsed = make_url(raw_url)
        except (TypeError, ValueError):
            raise ValueError("DATABASE_URL is not a valid SQLAlchemy URL.") from None

        if parsed.drivername != "postgresql+asyncpg":
            raise ValueError(
                "DATABASE_URL must use the postgresql+asyncpg driver."
            )

        if not parsed.host or not parsed.database or not parsed.username:
            raise ValueError(
                "DATABASE_URL must specify a host, database and username."
            )

        return cls(url=raw_url)

@dataclass(frozen=True, slots=True)
class MigrationDatabaseSettings:
    """Dedicated database configuration for Alembic migrations."""

    url: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> MigrationDatabaseSettings | None:
        raw_url = os.environ.get("MIGRATION_DATABASE_URL", "").strip()

        if not raw_url:
            return None

        try:
            parsed = make_url(raw_url)
        except (TypeError, ValueError):
            raise ValueError(
                "MIGRATION_DATABASE_URL is not a valid SQLAlchemy URL."
            ) from None

        if parsed.drivername != "postgresql+asyncpg":
            raise ValueError(
                "MIGRATION_DATABASE_URL must use the postgresql+asyncpg driver."
            )

        if not parsed.host or not parsed.database or not parsed.username:
            raise ValueError(
                "MIGRATION_DATABASE_URL must specify a host, database and username."
            )

        application_settings = DatabaseSettings.from_environment()
        if application_settings is None:
            raise ValueError(
                "DATABASE_URL is required to validate migration role separation."
            )

        application_parsed = make_url(application_settings.url)

        if application_parsed.username == parsed.username:
            raise ValueError(
                "Migration database role must differ from the application database role."
            )

        return cls(url=raw_url)

