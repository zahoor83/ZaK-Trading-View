from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.asgi as asgi


class FakeDatabaseManager:
    def __init__(self, initialized=True, events=None):
        self.is_initialized = initialized
        self.is_configured = initialized
        self.events = events if events is not None else []
        self.initialized = False
        self.disposed = False

    def initialize(self):
        self.initialized = True
        self.events.append("database_initialize")

    def session_factory(self):
        return object()

    async def dispose(self):
        self.events.append("database_dispose")
        self.disposed = True
        self.is_initialized = False


class FakeMarketDataService:
    def __init__(self, config, events, fail_start=False, fail_stop=False):
        self.events = events
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    async def start(self):
        self.events.append("service_start")
        if self.fail_start:
            raise RuntimeError("controlled startup failure")

    async def stop(self):
        self.events.append("service_stop")
        if self.fail_stop:
            raise RuntimeError("controlled shutdown failure")


def install_fakes(monkeypatch, manager, events, *, fail_start=False, fail_stop=False):
    monkeypatch.setattr(
        asgi.DatabaseManager,
        "from_environment",
        lambda: manager,
    )

    monkeypatch.setattr(
        asgi,
        "create_oidc_runtime",
        lambda database_manager: SimpleNamespace(available=True),
    )

    def service_factory(config):
        return FakeMarketDataService(
            config,
            events,
            fail_start=fail_start,
            fail_stop=fail_stop,
        )

    return asgi.create_app(
        service_factory=service_factory,
        config_factory=lambda: None,
    )


def install_cleanup_worker(monkeypatch, events):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_worker(session_factory, **kwargs):
        events.append("cleanup_started")
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("cleanup_cancelled")
            raise
        finally:
            events.append("cleanup_stopped")
            stopped.set()

    monkeypatch.setattr(
        asgi,
        "run_oidc_transaction_cleanup",
        fake_worker,
    )

    return started, stopped


@pytest.mark.asyncio
async def test_configured_database_starts_and_stops_cleanup_worker(monkeypatch):
    events = []
    manager = FakeDatabaseManager(events=events)
    app = install_fakes(monkeypatch, manager, events)
    started, stopped = install_cleanup_worker(monkeypatch, events)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=1)

        assert manager.initialized is True
        assert events.count("cleanup_started") == 1
        assert manager.disposed is False

    assert stopped.is_set()
    assert manager.disposed is True
    assert events.index("cleanup_cancelled") < events.index("database_dispose")
    assert events.index("cleanup_stopped") < events.index("database_dispose")


@pytest.mark.asyncio
async def test_unconfigured_database_does_not_start_cleanup_worker(monkeypatch):
    events = []
    manager = FakeDatabaseManager(initialized=False, events=events)
    app = install_fakes(monkeypatch, manager, events)

    async def unexpected_worker(*args, **kwargs):
        events.append("unexpected_cleanup_start")

    monkeypatch.setattr(
        asgi,
        "run_oidc_transaction_cleanup",
        unexpected_worker,
    )

    async with app.router.lifespan_context(app):
        assert "unexpected_cleanup_start" not in events

    assert manager.disposed is True
    assert "unexpected_cleanup_start" not in events


@pytest.mark.asyncio
async def test_startup_failure_stops_cleanup_worker_before_database_dispose(monkeypatch):
    events = []
    manager = FakeDatabaseManager(events=events)
    app = install_fakes(
        monkeypatch,
        manager,
        events,
        fail_start=True,
    )

    install_cleanup_worker(monkeypatch, events)

    created_tasks = []
    original_create_task = asyncio.create_task

    def recording_create_task(coro, *, name=None, context=None):
        task = original_create_task(coro, name=name, context=context)
        if name == "oidc-transaction-cleanup":
            created_tasks.append(task)
        return task

    monkeypatch.setattr(asgi.asyncio, "create_task", recording_create_task)

    with pytest.raises(RuntimeError, match="controlled startup failure"):
        async with app.router.lifespan_context(app):
            pass

    assert len(created_tasks) == 1
    cleanup_task = created_tasks[0]

    assert cleanup_task.done()
    assert cleanup_task.cancelled()
    assert manager.disposed is True
    assert events.index("database_dispose") >= 0


@pytest.mark.asyncio
async def test_service_stop_failure_still_stops_worker_and_disposes_database(monkeypatch):
    events = []
    manager = FakeDatabaseManager(events=events)
    app = install_fakes(
        monkeypatch,
        manager,
        events,
        fail_stop=True,
    )
    started, stopped = install_cleanup_worker(monkeypatch, events)

    with pytest.raises(RuntimeError, match="controlled shutdown failure"):
        async with app.router.lifespan_context(app):
            await started.wait()

    assert stopped.is_set()
    assert manager.disposed is True
    assert events.index("cleanup_stopped") < events.index("database_dispose")

