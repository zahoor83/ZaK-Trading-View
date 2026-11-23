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
    def __init__(self, events, fail_start=False, fail_stop=False):
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


def install_app(monkeypatch, manager, events, *, fail_start=False, fail_stop=False):
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
            events,
            fail_start=fail_start,
            fail_stop=fail_stop,
        )

    return asgi.create_app(
        service_factory=service_factory,
        config_factory=lambda: None,
    )


def install_workers(monkeypatch, events):
    states = {}

    def install(name, attribute):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def fake_worker(session_factory, **kwargs):
            events.append(f"{name}_started")
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                events.append(f"{name}_cancelled")
                raise
            finally:
                events.append(f"{name}_stopped")
                stopped.set()

        monkeypatch.setattr(asgi, attribute, fake_worker)
        states[name] = (started, stopped)

    install("oidc", "run_oidc_transaction_cleanup")
    install("hmac", "run_hmac_nonce_cleanup")
    return states


@pytest.mark.asyncio
async def test_configured_database_starts_and_stops_both_cleanup_workers(monkeypatch):
    events = []
    manager = FakeDatabaseManager(events=events)
    app = install_app(monkeypatch, manager, events)
    workers = install_workers(monkeypatch, events)

    async with app.router.lifespan_context(app):
        for started, _ in workers.values():
            await asyncio.wait_for(started.wait(), timeout=1)

        assert manager.initialized is True
        assert "oidc_started" in events
        assert "hmac_started" in events
        assert manager.disposed is False

    for _, stopped in workers.values():
        assert stopped.is_set()

    assert manager.disposed is True
    assert events.index("oidc_stopped") < events.index("database_dispose")
    assert events.index("hmac_stopped") < events.index("database_dispose")


@pytest.mark.asyncio
async def test_unconfigured_database_starts_neither_cleanup_worker(monkeypatch):
    events = []
    manager = FakeDatabaseManager(initialized=False, events=events)
    app = install_app(monkeypatch, manager, events)
    install_workers(monkeypatch, events)

    async with app.router.lifespan_context(app):
        assert "oidc_started" not in events
        assert "hmac_started" not in events

    assert manager.disposed is True
    assert "oidc_started" not in events
    assert "hmac_started" not in events


@pytest.mark.asyncio
async def test_startup_failure_cancels_both_workers_before_database_dispose(monkeypatch):
    events = []
    manager = FakeDatabaseManager(events=events)
    app = install_app(
        monkeypatch,
        manager,
        events,
        fail_start=True,
    )
    workers = install_workers(monkeypatch, events)

    created_tasks = []
    original_create_task = asyncio.create_task

    def recording_create_task(coro, *, name=None, context=None):
        task = original_create_task(coro, name=name, context=context)
        if name in {"oidc-transaction-cleanup", "hmac-nonce-cleanup"}:
            created_tasks.append((name, task))
        return task

    monkeypatch.setattr(asgi.asyncio, "create_task", recording_create_task)

    with pytest.raises(RuntimeError, match="controlled startup failure"):
        async with app.router.lifespan_context(app):
            pass

    assert len(created_tasks) == 2
    assert all(task.done() and task.cancelled() for _, task in created_tasks)
    assert manager.disposed is True

    # A task cancelled before its coroutine starts has no worker finally block.
    # Only workers that actually started must report their stopped event.
    for worker_name, (started, stopped) in workers.items():
        if started.is_set():
            assert stopped.is_set()
            assert f"{worker_name}_stopped" in events
            assert events.index(
                f"{worker_name}_stopped"
            ) < events.index("database_dispose")


@pytest.mark.asyncio
async def test_service_stop_failure_still_stops_both_workers_and_disposes_database(monkeypatch):
    events = []
    manager = FakeDatabaseManager(events=events)
    app = install_app(
        monkeypatch,
        manager,
        events,
        fail_stop=True,
    )
    workers = install_workers(monkeypatch, events)

    with pytest.raises(RuntimeError, match="controlled shutdown failure"):
        async with app.router.lifespan_context(app):
            for started, _ in workers.values():
                await asyncio.wait_for(started.wait(), timeout=1)

    assert all(stopped.is_set() for _, stopped in workers.values())
    assert manager.disposed is True
    assert events.index("oidc_stopped") < events.index("database_dispose")
    assert events.index("hmac_stopped") < events.index("database_dispose")