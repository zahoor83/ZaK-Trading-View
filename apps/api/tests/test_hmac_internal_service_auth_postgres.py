from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.models.internal_service_replay import InternalServiceRequestNonce
from app.security.hmac_internal_service_auth import (
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SERVICE_ID,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HMACInternalServiceAuthenticator,
)


SERVICE_ID_PREFIX = "pg-hmac-it-"
KEY_ID = "integration-test-v1"
SECRET = b"postgres-integration-hmac-secret-material-32-bytes-minimum"


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
                        "current_setting('port'), "
                        "current_setting('server_version_num')"
                    )
                )
            ).one()

            assert identity[0] == "zak_oidc_test_app"
            assert identity[1] == "zak_oidc_test"
            assert identity[2] == "55432"
            assert identity[3] == "180006"

        yield factory

    finally:
        await engine.dispose()


def _build_signed_request(
    *,
    service_id: str,
    nonce: str,
    timestamp: str,
    body: bytes,
    path: bytes = b"/internal/v1/example",
    query: bytes = b"mode=paper",
    session_factory,
):
    target = path.decode("ascii")
    if query:
        target += "?" + query.decode("ascii")

    body_digest = hashlib.sha256(body).hexdigest()

    canonical = "\n".join(
        (
            "ZAK-INTERNAL-REQUEST-V1",
            service_id,
            KEY_ID,
            timestamp,
            nonce,
            "POST",
            target,
            body_digest,
        )
    ).encode("ascii")

    signature = hmac.new(
        SECRET,
        canonical,
        hashlib.sha256,
    ).hexdigest()

    headers = [
        (HEADER_SERVICE_ID.lower().encode(), service_id.encode()),
        (HEADER_KEY_ID.lower().encode(), KEY_ID.encode()),
        (HEADER_TIMESTAMP.lower().encode(), timestamp.encode()),
        (HEADER_NONCE.lower().encode(), nonce.encode()),
        (HEADER_SIGNATURE.lower().encode(), signature.encode()),
    ]

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    database_manager = SimpleNamespace(
        session_factory=lambda: session_factory
    )

    app = SimpleNamespace(
        state=SimpleNamespace(
            database_manager=database_manager
        )
    )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path.decode("ascii"),
        "raw_path": path,
        "query_string": query,
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
        "app": app,
    }

    return Request(scope, receive=receive)


@pytest.mark.asyncio
async def test_postgres_persists_nonce_and_rejects_replay(
    pg_sessions,
):

    service_id = SERVICE_ID_PREFIX + uuid.uuid4().hex
    nonce = secrets.token_urlsafe(32)
    timestamp = str(int(time.time()))
    body = b'{"symbol":"BTCUSDT","mode":"paper"}'

    authenticator = HMACInternalServiceAuthenticator(
        service_id=service_id,
        keys={KEY_ID: SECRET},
    )

    nonce_hash = hashlib.sha256(
        nonce.encode("ascii")
    ).hexdigest()

    try:
        first_request = _build_signed_request(
            service_id=service_id,
            nonce=nonce,
            timestamp=timestamp,
            body=body,
            session_factory=pg_sessions,
        )

        first_principal = await authenticator.authenticate(first_request)

        assert first_principal is not None
        assert first_principal.service_id == service_id

        # Verify persistence from a separate database session,
        # after the authenticator's transaction has committed.
        async with pg_sessions() as session:
            record = await session.scalar(
                select(InternalServiceRequestNonce).where(
                    InternalServiceRequestNonce.service_id == service_id,
                    InternalServiceRequestNonce.key_id == KEY_ID,
                    InternalServiceRequestNonce.nonce_hash == nonce_hash,
                )
            )

            assert record is not None
            assert record.service_id == service_id
            assert record.key_id == KEY_ID
            assert record.nonce_hash == nonce_hash
            assert record.signed_at.tzinfo is not None
            assert record.expires_at > record.signed_at

        replay_request = _build_signed_request(
            service_id=service_id,
            nonce=nonce,
            timestamp=timestamp,
            body=body,
            session_factory=pg_sessions,
        )

        replay_principal = await authenticator.authenticate(replay_request)

        assert replay_principal is None

        async with pg_sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(
                    InternalServiceRequestNonce
                ).where(
                    InternalServiceRequestNonce.service_id == service_id,
                    InternalServiceRequestNonce.key_id == KEY_ID,
                    InternalServiceRequestNonce.nonce_hash == nonce_hash,
                )
            )

            assert total == 1

    finally:
        async with pg_sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(InternalServiceRequestNonce).where(
                        InternalServiceRequestNonce.service_id == service_id
                    )
                )


@pytest.mark.asyncio
async def test_postgres_concurrent_same_nonce_accepts_exactly_one_request(
    pg_sessions,
):
    import asyncio

    service_id = SERVICE_ID_PREFIX + uuid.uuid4().hex
    nonce = secrets.token_urlsafe(32)
    timestamp = str(int(time.time()))
    body = b'{"symbol":"BTCUSDT","mode":"paper"}'

    authenticator = HMACInternalServiceAuthenticator(
        service_id=service_id,
        keys={KEY_ID: SECRET},
    )

    try:
        requests = [
            _build_signed_request(
                service_id=service_id,
                nonce=nonce,
                timestamp=timestamp,
                body=body,
                session_factory=pg_sessions,
            )
            for _ in range(2)
        ]

        results = await asyncio.gather(
            *(authenticator.authenticate(request) for request in requests)
        )

        accepted = [result for result in results if result is not None]
        rejected = [result for result in results if result is None]

        assert len(accepted) == 1
        assert accepted[0].service_id == service_id
        assert len(rejected) == 1

        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()

        async with pg_sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(
                    InternalServiceRequestNonce
                ).where(
                    InternalServiceRequestNonce.service_id == service_id,
                    InternalServiceRequestNonce.key_id == KEY_ID,
                    InternalServiceRequestNonce.nonce_hash == nonce_hash,
                )
            )

        assert total == 1

    finally:
        async with pg_sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(InternalServiceRequestNonce).where(
                        InternalServiceRequestNonce.service_id == service_id
                    )
                )


@pytest.mark.asyncio
async def test_postgres_cleanup_deletes_expired_nonce_and_preserves_unexpired(
    pg_sessions,
):
    from datetime import datetime, timedelta, timezone
    from app.services.hmac_nonce_cleanup import cleanup_expired_hmac_nonces

    service_id = SERVICE_ID_PREFIX + uuid.uuid4().hex
    other_service_id = SERVICE_ID_PREFIX + uuid.uuid4().hex
    now = datetime.now(timezone.utc)

    expired = InternalServiceRequestNonce(
        service_id=service_id,
        key_id=KEY_ID,
        nonce_hash=hashlib.sha256(b"expired-test-nonce").hexdigest(),
        signed_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        created_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(1971, 1, 1, tzinfo=timezone.utc),
    )

    other_expired = InternalServiceRequestNonce(
        service_id=other_service_id,
        key_id=KEY_ID,
        nonce_hash=hashlib.sha256(b"other-service-expired-nonce").hexdigest(),
        signed_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        created_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(1971, 1, 1, tzinfo=timezone.utc),
    )

    unexpired = InternalServiceRequestNonce(
        service_id=service_id,
        key_id=KEY_ID,
        nonce_hash=hashlib.sha256(b"unexpired-test-nonce").hexdigest(),
        signed_at=now,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    try:
        async with pg_sessions() as session:
            async with session.begin():
                session.add_all([expired, unexpired, other_expired])

        deleted = await cleanup_expired_hmac_nonces(
            pg_sessions,
            batch_size=1000,
            service_id=service_id,
        )

        assert deleted == 1

        async with pg_sessions() as session:
            expired_record = await session.scalar(
                select(InternalServiceRequestNonce).where(
                    InternalServiceRequestNonce.service_id == service_id,
                    InternalServiceRequestNonce.nonce_hash == expired.nonce_hash,
                )
            )
            unexpired_record = await session.scalar(
                select(InternalServiceRequestNonce).where(
                    InternalServiceRequestNonce.service_id == service_id,
                    InternalServiceRequestNonce.nonce_hash == unexpired.nonce_hash,
                )
            )
            other_expired_record = await session.scalar(
                select(InternalServiceRequestNonce).where(
                    InternalServiceRequestNonce.service_id == other_service_id,
                    InternalServiceRequestNonce.nonce_hash == other_expired.nonce_hash,
                )
            )

        assert expired_record is None
        assert unexpired_record is not None
        assert other_expired_record is not None

    finally:
        async with pg_sessions() as session:
            async with session.begin():
                await session.execute(
                    delete(InternalServiceRequestNonce).where(
                        InternalServiceRequestNonce.service_id.in_(
                            [service_id, other_service_id]
                        )
                    )
                )