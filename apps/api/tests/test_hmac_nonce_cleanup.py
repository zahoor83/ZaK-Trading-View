from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.models.internal_service_replay import InternalServiceRequestNonce
from app.services import hmac_nonce_cleanup as module


class FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeSession:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount
        self.statement = None
        self.committed = False

    def begin(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.committed = exc_type is None
        return False

    async def execute(self, statement):
        self.statement = statement
        return FakeResult(self.rowcount)


def factory_for(session):
    @asynccontextmanager
    async def make_session():
        yield session

    return make_session


@pytest.mark.asyncio
async def test_cleanup_deletes_only_expired_rows_in_bounded_batch():
    session = FakeSession(rowcount=7)
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    deleted = await module.cleanup_expired_hmac_nonces(
        factory_for(session),
        now=now,
        batch_size=100,
    )

    assert deleted == 7
    assert session.committed is True

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "LIMIT 100" in compiled
    assert "internal_service_request_nonces.expires_at <=" in compiled
    assert "DELETE FROM internal_service_request_nonces" in compiled


@pytest.mark.asyncio
async def test_cleanup_defaults_to_database_clock():
    session = FakeSession(rowcount=2)

    deleted = await module.cleanup_expired_hmac_nonces(
        factory_for(session),
        batch_size=25,
    )

    assert deleted == 2
    assert session.committed is True

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "CURRENT_TIMESTAMP" in compiled
    assert "LIMIT 25" in compiled


@pytest.mark.asyncio
async def test_cleanup_rejects_unbounded_or_invalid_batch_size():
    for batch_size in (0, 1001):
        with pytest.raises(ValueError):
            await module.cleanup_expired_hmac_nonces(
                factory_for(FakeSession()),
                batch_size=batch_size,
            )

    for batch_size in (True, 1.5, "10"):
        with pytest.raises(TypeError):
            await module.cleanup_expired_hmac_nonces(
                factory_for(FakeSession()),
                batch_size=batch_size,
            )


@pytest.mark.asyncio
async def test_cleanup_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        await module.cleanup_expired_hmac_nonces(
            factory_for(FakeSession()),
            now=datetime(2026, 11, 15, 12, 0),
        )


@pytest.mark.asyncio
async def test_cleanup_worker_can_be_cancelled_cleanly(monkeypatch):
    started = asyncio.Event()
    waiting = asyncio.Event()

    async def fake_cleanup(*args, **kwargs):
        started.set()

    async def fake_sleep(seconds):
        assert seconds == 60
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(module, "cleanup_expired_hmac_nonces", fake_cleanup)

    task = asyncio.create_task(
        module.run_hmac_nonce_cleanup(
            factory_for(FakeSession()),
            sleep=fake_sleep,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(waiting.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cleanup_worker_survives_transient_failure(monkeypatch):
    calls = []
    waiting = asyncio.Event()

    async def fake_cleanup(*args, **kwargs):
        calls.append("attempt")
        if len(calls) == 1:
            raise RuntimeError("controlled database failure")

    async def fake_sleep(seconds):
        if len(calls) >= 2:
            waiting.set()
            await asyncio.Future()

    monkeypatch.setattr(module, "cleanup_expired_hmac_nonces", fake_cleanup)

    task = asyncio.create_task(
        module.run_hmac_nonce_cleanup(
            factory_for(FakeSession()),
            sleep=fake_sleep,
        )
    )

    await asyncio.wait_for(waiting.wait(), timeout=1)

    assert len(calls) >= 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_worker_rejects_invalid_interval():
    for interval in (0, 0.5, True, "60"):
        with pytest.raises(ValueError):
            await module.run_hmac_nonce_cleanup(
                factory_for(FakeSession()),
                interval_seconds=interval,
            )



@pytest.mark.asyncio
async def test_cleanup_database_failure_rolls_back_transaction():
    class FailingSession(FakeSession):
        async def execute(self, statement):
            self.statement = statement
            raise RuntimeError("controlled cleanup database failure")

    session = FailingSession()

    with pytest.raises(RuntimeError, match="controlled cleanup database failure"):
        await module.cleanup_expired_hmac_nonces(
            factory_for(session),
            batch_size=10,
        )

    assert session.committed is False
