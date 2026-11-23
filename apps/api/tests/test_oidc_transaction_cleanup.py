from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.models.oidc import (
    OIDCAuthTransaction,
    OIDCIdempotencyRecord,
)
from app.services.oidc_transaction_cleanup import (
    cleanup_expired_oidc_idempotency_records,
    cleanup_expired_oidc_transactions,
    delete_unconsumed_oidc_transaction,
    run_oidc_transaction_cleanup,
)


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
async def test_cleanup_uses_bounded_batch_and_retention_predicates():
    session = FakeSession(rowcount=7)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)

    deleted = await cleanup_expired_oidc_transactions(
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
    assert "consumed_at IS NULL" in compiled
    assert "expires_at" in compiled
    assert "consumed_at" in compiled


@pytest.mark.asyncio
async def test_idempotency_cleanup_uses_bounded_expiry_predicate():
    session = FakeSession(rowcount=5)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)

    deleted = await cleanup_expired_oidc_idempotency_records(
        factory_for(session),
        now=now,
        batch_size=100,
    )

    assert deleted == 5
    assert session.committed is True

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "LIMIT 100" in compiled
    assert "oidc_idempotency_records.expires_at <= " in compiled
    assert "DELETE FROM oidc_idempotency_records" in compiled


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_idempotency_cleanup_scopes_query_to_service_id():
    session = FakeSession(rowcount=2)
    service_id = "oidc-unit-service-a"

    deleted = await cleanup_expired_oidc_idempotency_records(
        factory_for(session),
        batch_size=40,
        service_id=service_id,
    )

    assert deleted == 2

    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "LIMIT 40" in compiled
    assert "service_id = 'oidc-unit-service-a'" in compiled


@pytest.mark.asyncio
async def test_idempotency_cleanup_rejects_invalid_service_id():
    for service_id in ("", "   ", 123, True):
        with pytest.raises(ValueError, match="service_id"):
            await cleanup_expired_oidc_idempotency_records(
                factory_for(FakeSession()),
                service_id=service_id,
            )


@pytest.mark.asyncio
async def test_idempotency_cleanup_defaults_to_database_clock():
    session = FakeSession(rowcount=2)

    deleted = await cleanup_expired_oidc_idempotency_records(
        factory_for(session),
        batch_size=100,
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
    assert "LIMIT 100" in compiled
    assert "DELETE FROM oidc_idempotency_records" in compiled
    assert "service_id =" not in compiled


async def test_idempotency_cleanup_rejects_unbounded_batch_size():
    with pytest.raises(ValueError):
        await cleanup_expired_oidc_idempotency_records(
            factory_for(FakeSession()),
            batch_size=1001,
        )


@pytest.mark.asyncio
async def test_idempotency_cleanup_rejects_naive_datetime():
    with pytest.raises(ValueError):
        await cleanup_expired_oidc_idempotency_records(
            factory_for(FakeSession()),
            now=datetime(2026, 11, 12, 12, 0),
        )


@pytest.mark.asyncio
async def test_cleanup_rejects_unbounded_batch_size():
    with pytest.raises(ValueError):
        await cleanup_expired_oidc_transactions(
            factory_for(FakeSession()),
            batch_size=1001,
        )


@pytest.mark.asyncio
async def test_cleanup_rejects_naive_datetime():
    with pytest.raises(ValueError):
        await cleanup_expired_oidc_transactions(
            factory_for(FakeSession()),
            now=datetime(2026, 11, 12, 12, 0),
        )


@pytest.mark.asyncio
async def test_failure_cleanup_deletes_only_matching_unconsumed_row():
    session = FakeSession(rowcount=1)
    transaction_id = uuid4()

    deleted = await delete_unconsumed_oidc_transaction(
        factory_for(session),
        transaction_id,
    )

    assert deleted is True
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert str(transaction_id) in compiled
    assert "consumed_at IS NULL" in compiled


@pytest.mark.asyncio
async def test_failure_cleanup_returns_false_if_row_not_deleted():
    deleted = await delete_unconsumed_oidc_transaction(
        factory_for(FakeSession(rowcount=0)),
        uuid4(),
    )
    assert deleted is False


@pytest.mark.asyncio
async def test_failure_cleanup_rejects_non_uuid():
    with pytest.raises(TypeError):
        await delete_unconsumed_oidc_transaction(
            factory_for(FakeSession()),
            "not-a-uuid",
        )


@pytest.mark.asyncio
async def test_cleanup_worker_can_be_cancelled_cleanly():
    started = asyncio.Event()
    waiting = asyncio.Event()

    async def fake_cleanup(*args, **kwargs):
        started.set()

    async def fake_sleep(seconds):
        waiting.set()
        await asyncio.Future()

    from app.services import oidc_transaction_cleanup as module

    original = module.cleanup_expired_oidc_transactions
    original_idempotency = module.cleanup_expired_oidc_idempotency_records
    module.cleanup_expired_oidc_transactions = fake_cleanup
    module.cleanup_expired_oidc_idempotency_records = fake_cleanup
    try:
        task = asyncio.create_task(
            run_oidc_transaction_cleanup(
                factory_for(FakeSession()),
                sleep=fake_sleep,
            )
        )
        await started.wait()
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        module.cleanup_expired_oidc_transactions = original
        module.cleanup_expired_oidc_idempotency_records = original_idempotency
