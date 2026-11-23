from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.services.oidc_idempotency_service as idempotency_service

from app.models.oidc import (
    OIDCAuthTransaction,
    OIDCIdempotencyRecord,
)

from app.services.oidc_idempotency_service import (
    OIDCIdempotencyConflictError,
    create_or_replay_oidc_transaction,
)

from app.services.oidc_transaction_cleanup import (
    cleanup_expired_oidc_idempotency_records,
)

from app.services.oidc_transaction_service import (
    OIDCTransactionEncryptionSettings,
    create_oidc_auth_transaction_in_transaction,
)


def _validated_test_url() -> str:
    raw = os.environ.get("DATABASE_URL", "")

    if not raw:
        pytest.fail("DATABASE_URL is not configured.")

    parsed = make_url(raw)

    expected = {
        "drivername": "postgresql+asyncpg",
        "host": "127.0.0.1",
        "port": 55432,
        "database": "zak_oidc_test",
        "username": "zak_oidc_test_app",
    }

    actual = {
        "drivername": parsed.drivername,
        "host": parsed.host,
        "port": parsed.port,
        "database": parsed.database,
        "username": parsed.username,
    }

    if actual != expected:
        pytest.fail(
            "Refusing database connection: target is not the approved "
            "isolated PostgreSQL test database."
        )

    return raw


@pytest.fixture
async def pg_sessions():
    engine = create_async_engine(
        _validated_test_url(),
        poolclass=NullPool,
        pool_pre_ping=True,
    )

    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT current_user, current_database(), "
                        "current_setting('transaction_isolation')"
                    )
                )
            ).one()

            assert identity[0] == "zak_oidc_test_app"
            assert identity[1] == "zak_oidc_test"
            assert identity[2] == "read committed"

        yield factory

    finally:
        await engine.dispose()


@pytest.fixture
def encryption():
    return OIDCTransactionEncryptionSettings(
        keys={
            "integration-test-v1": b"Z" * 32,
        },
        active_key_id="integration-test-v1",
    )


def _new_identity():
    service_id = "pg-it-" + uuid.uuid4().hex
    request_id = str(uuid.uuid4())

    return service_id, request_id


def _url_builder(transaction):
    return (
        "https://issuer.example/authorize?state="
        + transaction.state
    )


async def _invoke(
    factory,
    service_id,
    request_id,
    encryption,
    builder=_url_builder,
):
    async with factory() as session:
        return await create_or_replay_oidc_transaction(
            session,
            service_id=service_id,
            request_id=request_id,
            encryption=encryption,
            authorization_url_builder=builder,
        )


async def _cleanup_service(factory, service_id):
    """
    Cleanup restricted to this test's unique service ID.
    No TRUNCATE and no broad database cleanup.
    """

    async with factory() as session:
        async with session.begin():

            transaction_ids = list(
                (
                    await session.scalars(
                        select(
                            OIDCIdempotencyRecord.transaction_id
                        ).where(
                            OIDCIdempotencyRecord.service_id == service_id
                        )
                    )
                ).all()
            )

            await session.execute(
                delete(OIDCIdempotencyRecord).where(
                    OIDCIdempotencyRecord.service_id == service_id
                )
            )

            valid_ids = [
                item for item in transaction_ids
                if item is not None
            ]

            if valid_ids:
                await session.execute(
                    delete(OIDCAuthTransaction).where(
                        OIDCAuthTransaction.id.in_(valid_ids)
                    )
                )


@pytest.mark.asyncio
async def test_postgres_create_persists_encrypted_response_and_replays(
    pg_sessions,
    encryption,
):
    service_id, request_id = _new_identity()

    try:
        first = await _invoke(
            pg_sessions,
            service_id,
            request_id,
            encryption,
        )

        replay = await _invoke(
            pg_sessions,
            service_id,
            request_id,
            encryption,
        )

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.response == first.response

        async with pg_sessions() as session:
            record = await session.scalar(
                select(OIDCIdempotencyRecord).where(
                    OIDCIdempotencyRecord.service_id == service_id
                )
            )

            assert record is not None
            assert record.status == "completed"
            assert record.transaction_id is not None
            assert len(record.response_nonce) == 12
            assert len(record.response_ciphertext) > 16
            assert (
                record.encryption_key_id
                == "integration-test-v1"
            )

            transaction = await session.get(
                OIDCAuthTransaction,
                record.transaction_id,
            )

            assert transaction is not None

            plaintext = json.dumps(
                first.response,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

            assert plaintext not in record.response_ciphertext

    finally:
        await _cleanup_service(pg_sessions, service_id)


@pytest.mark.asyncio
async def test_postgres_idempotency_is_scoped_to_service_id(
    pg_sessions,
    encryption,
):
    request_id = str(uuid.uuid4())

    service_a = "pg-it-a-" + uuid.uuid4().hex
    service_b = "pg-it-b-" + uuid.uuid4().hex

    try:
        first = await _invoke(
            pg_sessions,
            service_a,
            request_id,
            encryption,
        )

        second = await _invoke(
            pg_sessions,
            service_b,
            request_id,
            encryption,
        )

        assert first.replayed is False
        assert second.replayed is False
        assert first.response != second.response

        async with pg_sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(OIDCIdempotencyRecord)
                .where(
                    OIDCIdempotencyRecord.service_id.in_(
                        [service_a, service_b]
                    )
                )
            )

            assert count == 2

    finally:
        await _cleanup_service(pg_sessions, service_a)
        await _cleanup_service(pg_sessions, service_b)


@pytest.mark.asyncio
async def test_postgres_concurrent_identical_requests_create_one_record(
    pg_sessions,
    encryption,
    monkeypatch,
):
    service_id, request_id = _new_identity()

    original_create = create_oidc_auth_transaction_in_transaction

    both_created = asyncio.Event()
    arrival_count = 0

    async def synchronized_create(
        session,
        *,
        encryption,
        now=None,
    ):
        nonlocal arrival_count

        result = await original_create(
            session,
            encryption=encryption,
            now=now,
        )

        arrival_count += 1

        if arrival_count == 2:
            both_created.set()

        await asyncio.wait_for(
            both_created.wait(),
            timeout=10,
        )

        return result

    monkeypatch.setattr(
        idempotency_service,
        "create_oidc_auth_transaction_in_transaction",
        synchronized_create,
    )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                _invoke(
                    pg_sessions,
                    service_id,
                    request_id,
                    encryption,
                ),
                _invoke(
                    pg_sessions,
                    service_id,
                    request_id,
                    encryption,
                ),
            ),
            timeout=30,
        )

        assert arrival_count == 2

        assert sorted(
            result.replayed for result in results
        ) == [False, True]

        assert results[0].response == results[1].response

        async with pg_sessions() as session:
            records = list(
                (
                    await session.scalars(
                        select(OIDCIdempotencyRecord).where(
                            OIDCIdempotencyRecord.service_id
                            == service_id
                        )
                    )
                ).all()
            )

            assert len(records) == 1
            assert records[0].transaction_id is not None

            transactions = list(
                (
                    await session.scalars(
                        select(OIDCAuthTransaction).where(
                            OIDCAuthTransaction.id
                            == records[0].transaction_id
                        )
                    )
                ).all()
            )

            assert len(transactions) == 1

    finally:
        await _cleanup_service(pg_sessions, service_id)


@pytest.mark.asyncio
async def test_postgres_consumed_transaction_cannot_be_replayed(
    pg_sessions,
    encryption,
):
    service_id, request_id = _new_identity()

    try:
        await _invoke(
            pg_sessions,
            service_id,
            request_id,
            encryption,
        )

        async with pg_sessions() as session:
            async with session.begin():
                record = await session.scalar(
                    select(OIDCIdempotencyRecord).where(
                        OIDCIdempotencyRecord.service_id
                        == service_id
                    )
                )

                assert record is not None

                transaction = await session.get(
                    OIDCAuthTransaction,
                    record.transaction_id,
                )

                assert transaction is not None

                transaction.consumed_at = func.current_timestamp()

        with pytest.raises(OIDCIdempotencyConflictError):
            await _invoke(
                pg_sessions,
                service_id,
                request_id,
                encryption,
            )

    finally:
        await _cleanup_service(pg_sessions, service_id)


@pytest.mark.asyncio
async def test_postgres_expired_transaction_cannot_be_replayed(
    pg_sessions,
    encryption,
):
    service_id, request_id = _new_identity()

    try:
        await _invoke(
            pg_sessions,
            service_id,
            request_id,
            encryption,
        )

        async with pg_sessions() as session:
            async with session.begin():
                record = await session.scalar(
                    select(OIDCIdempotencyRecord).where(
                        OIDCIdempotencyRecord.service_id
                        == service_id
                    )
                )

                assert record is not None

                transaction = await session.get(
                    OIDCAuthTransaction,
                    record.transaction_id,
                )

                assert transaction is not None

                transaction.created_at = (
                    func.current_timestamp()
                    - text("interval '2 seconds'")
                )
                transaction.expires_at = (
                    func.current_timestamp()
                    - text("interval '1 second'")
                )

        with pytest.raises(OIDCIdempotencyConflictError):
            await _invoke(
                pg_sessions,
                service_id,
                request_id,
                encryption,
            )

        async with pg_sessions() as session:
            record = await session.scalar(
                select(OIDCIdempotencyRecord).where(
                    OIDCIdempotencyRecord.service_id
                    == service_id
                )
            )

            assert record is not None

    finally:
        await _cleanup_service(pg_sessions, service_id)


@pytest.mark.asyncio
async def test_postgres_transaction_delete_preserves_reservation_with_null_fk(
    pg_sessions,
    encryption,
):
    service_id, request_id = _new_identity()

    try:
        await _invoke(
            pg_sessions,
            service_id,
            request_id,
            encryption,
        )

        async with pg_sessions() as session:
            async with session.begin():
                record = await session.scalar(
                    select(OIDCIdempotencyRecord).where(
                        OIDCIdempotencyRecord.service_id
                        == service_id
                    )
                )

                assert record is not None

                transaction_id = record.transaction_id

                await session.execute(
                    delete(OIDCAuthTransaction).where(
                        OIDCAuthTransaction.id == transaction_id
                    )
                )

        async with pg_sessions() as session:
            record = await session.scalar(
                select(OIDCIdempotencyRecord).where(
                    OIDCIdempotencyRecord.service_id
                    == service_id
                )
            )

            assert record is not None
            assert record.transaction_id is None

        with pytest.raises(OIDCIdempotencyConflictError):
            await _invoke(
                pg_sessions,
                service_id,
                request_id,
                encryption,
            )

    finally:
        await _cleanup_service(pg_sessions, service_id)


@pytest.mark.asyncio
async def test_postgres_builder_failure_rolls_back_transaction_and_reservation(
    pg_sessions,
    encryption,
):
    service_id, request_id = _new_identity()

    def failing_builder(transaction):
        raise RuntimeError("integration test builder failure")

    try:
        async with pg_sessions() as session:
            before_count = await session.scalar(
                select(func.count()).select_from(OIDCAuthTransaction)
            )

        with pytest.raises(
            RuntimeError,
            match="integration test builder failure",
        ):
            await _invoke(
                pg_sessions,
                service_id,
                request_id,
                encryption,
                builder=failing_builder,
            )

        async with pg_sessions() as session:
            record_count = await session.scalar(
                select(func.count())
                .select_from(OIDCIdempotencyRecord)
                .where(
                    OIDCIdempotencyRecord.service_id == service_id
                )
            )

            after_count = await session.scalar(
                select(func.count()).select_from(OIDCAuthTransaction)
            )

            assert record_count == 0
            assert after_count == before_count

    finally:
        await _cleanup_service(pg_sessions, service_id)


@pytest.mark.asyncio
async def test_postgres_expired_idempotency_cleanup_removes_reservation(
    pg_sessions,
    encryption,
):
    service_id, request_id = _new_identity()
    other_service_id, other_request_id = _new_identity()

    transaction_id = None

    try:
        await _invoke(
            pg_sessions,
            service_id,
            request_id,
            encryption,
        )
        await _invoke(
            pg_sessions,
            other_service_id,
            other_request_id,
            encryption,
        )

        async with pg_sessions() as session:
            async with session.begin():
                record = await session.scalar(
                    select(OIDCIdempotencyRecord).where(
                        OIDCIdempotencyRecord.service_id
                        == service_id
                    )
                )

                assert record is not None
                transaction_id = record.transaction_id

                await session.execute(
                    update(OIDCIdempotencyRecord)
                    .where(
                        OIDCIdempotencyRecord.service_id.in_(
                            [service_id, other_service_id]
                        )
                    )
                    .values(
                        created_at=(
                            func.current_timestamp()
                            - text("interval '2 days'")
                        ),
                        expires_at=(
                            func.current_timestamp()
                            - text("interval '1 second'")
                        ),
                    )
                )

        deleted = await cleanup_expired_oidc_idempotency_records(
            pg_sessions,
            batch_size=100,
            service_id=service_id,
        )

        assert deleted == 1

        async with pg_sessions() as session:
            remaining = await session.scalar(
                select(func.count())
                .select_from(OIDCIdempotencyRecord)
                .where(
                    OIDCIdempotencyRecord.service_id == service_id
                )
            )
            other_remaining = await session.scalar(
                select(func.count())
                .select_from(OIDCIdempotencyRecord)
                .where(
                    OIDCIdempotencyRecord.service_id == other_service_id
                )
            )

            assert remaining == 0
            assert other_remaining == 1

        # Cleanup worker deletes the reservation, not the transaction.
        # Remove only this test's remaining transaction.
        if transaction_id is not None:
            async with pg_sessions() as session:
                async with session.begin():
                    await session.execute(
                        delete(OIDCAuthTransaction).where(
                            OIDCAuthTransaction.id == transaction_id
                        )
                    )

    finally:
        await _cleanup_service(pg_sessions, service_id)
        await _cleanup_service(pg_sessions, other_service_id)
