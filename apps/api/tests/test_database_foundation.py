from __future__ import annotations

import pytest

from app.database.config import DatabaseSettings, MigrationDatabaseSettings, _load_local_environment
from app.database.session import DatabaseManager


def test_database_is_disabled_when_url_is_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert DatabaseSettings.from_environment() is None

    manager = DatabaseManager.from_environment()
    assert manager.is_configured is False
    assert manager.is_initialized is False


def test_database_rejects_non_async_postgresql_driver(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:password@localhost:5432/zak_test",
    )

    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        DatabaseSettings.from_environment()


def test_database_rejects_missing_database_components(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://")

    with pytest.raises(ValueError):
        DatabaseSettings.from_environment()


def test_database_settings_do_not_expose_password_in_repr():
    settings = DatabaseSettings(
        url="postgresql+asyncpg://user:secret-password@localhost:5432/zak_test"
    )

    assert "secret-password" not in repr(settings)


def test_session_factory_fails_closed_before_initialization():
    manager = DatabaseManager(None)

    with pytest.raises(RuntimeError, match="unavailable"):
        manager.session_factory()


@pytest.mark.asyncio
async def test_uninitialized_database_ping_returns_false():
    manager = DatabaseManager(None)

    assert await manager.ping() is False


@pytest.mark.asyncio
async def test_engine_initialization_does_not_connect_to_postgres(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:password@127.0.0.1:1/zak_test",
    )

    manager = DatabaseManager.from_environment()
    manager.initialize()

    try:
        assert manager.is_configured is True
        assert manager.is_initialized is True
    finally:
        await manager.dispose()

    assert manager.is_initialized is False


@pytest.mark.asyncio
async def test_unconfigured_database_does_not_break_market_data_lifespan(monkeypatch):
    import httpx

    from app.asgi import create_app
    from app.market_data.config import MarketDataConfig

    monkeypatch.delenv("DATABASE_URL", raising=False)

    class FakeService:
        def __init__(self, config):
            self.started = False
            self.stopped = False

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    service_ref = []

    def service_factory(config):
        service = FakeService(config)
        service_ref.append(service)
        return service

    app = create_app(
        service_factory=service_factory,
        config_factory=MarketDataConfig,
    )

    async with app.router.lifespan_context(app):
        manager = app.state.database_manager
        assert manager.is_configured is False
        assert manager.is_initialized is False
        assert app.state.oidc_runtime.available is False

        transport = httpx.ASGITransport(app=app)
        assert manager.is_configured is False
        assert manager.is_initialized is False

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/health/database")

        assert response.status_code == 503
        assert service_ref[0].started is True
        assert service_ref[0].stopped is False

    assert service_ref[0].stopped is True
    assert app.state.database_manager is None
    assert app.state.oidc_runtime is None
    assert app.state.market_data_service is None
    assert app.state.market_data_service is None


def test_migration_database_settings_do_not_fall_back_to_application_url(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app_user:app_password@localhost:5432/zak_test",
    )
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)

    assert MigrationDatabaseSettings.from_environment() is None


def test_migration_database_rejects_same_application_role(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://shared_user:app_password@localhost:5432/zak_test",
    )
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://shared_user:migration_password@localhost:5432/zak_test",
    )

    with pytest.raises(ValueError, match="must differ"):
        MigrationDatabaseSettings.from_environment()


def test_migration_database_accepts_separate_role(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://app_user:app_password@localhost:5432/zak_test",
    )
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://migration_user:migration_password@localhost:5432/zak_test",
    )

    settings = MigrationDatabaseSettings.from_environment()

    assert settings is not None
    assert "migration_password" not in repr(settings)


def test_migration_database_rejects_non_async_postgresql_driver(monkeypatch):
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql://migration_user:password@localhost:5432/zak_test",
    )

    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        MigrationDatabaseSettings.from_environment()


def test_local_environment_loader_reads_development_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=development\n"
        "ZAK_DOTENV_TEST_VALUE=loaded-from-file\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ZAK_DOTENV_TEST_VALUE", raising=False)

    _load_local_environment(env_file)

    assert __import__("os").environ["ZAK_DOTENV_TEST_VALUE"] == "loaded-from-file"


def test_local_environment_loader_does_not_override_process_environment(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=development\n"
        "ZAK_DOTENV_TEST_VALUE=file-value\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ZAK_DOTENV_TEST_VALUE", "process-value")

    _load_local_environment(env_file)

    assert __import__("os").environ["ZAK_DOTENV_TEST_VALUE"] == "process-value"


def test_local_environment_loader_skips_production(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=production\n"
        "ZAK_DOTENV_TEST_VALUE=must-not-load\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ZAK_DOTENV_TEST_VALUE", raising=False)

    _load_local_environment(env_file)

    assert "ZAK_DOTENV_TEST_VALUE" not in __import__("os").environ


def test_migration_database_requires_application_url_for_role_comparison(
    monkeypatch,
):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://migration_user:password@localhost:5432/zak_test",
    )

    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        MigrationDatabaseSettings.from_environment()
