import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import oidc_idempotency_service as service
from app.services.oidc_transaction_service import (
    OIDCTransactionCreationResult,
    OIDCTransactionEncryptionSettings,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
SERVICE_ID = "test-bff"
REQUEST_ID = str(uuid.uuid4())
TRANSACTION_ID = uuid.uuid4()
RECORD_ID = uuid.uuid4()


class FakeBegin:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.active = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.session.active = False
        self.session.committed = exc_type is None
        self.session.rolled_back = exc_type is not None
        return False


class FakeResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, scalar_values=(), execute_values=()):
        self.active = False
        self.committed = False
        self.rolled_back = False
        self.scalar_values = list(scalar_values)
        self.execute_values = list(execute_values)
        self.statements = []
        self.flush_count = 0

    def in_transaction(self):
        return self.active

    def begin(self):
        return FakeBegin(self)

    async def scalar(self, statement):
        self.statements.append(statement)
        if not self.scalar_values:
            raise AssertionError("Unexpected session.scalar call")
        return self.scalar_values.pop(0)

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.execute_values:
            return FakeResult()
        value = self.execute_values.pop(0)
        return value if hasattr(value, "scalar_one_or_none") else FakeResult(value)

    async def flush(self):
        self.flush_count += 1


def make_encryption():
    return OIDCTransactionEncryptionSettings(
        keys={"test-key": b"x" * 32},
        active_key_id="test-key",
    )


def make_transaction():
    return OIDCTransactionCreationResult(
        transaction_id=TRANSACTION_ID,
        state="test-state",
        nonce="test-nonce",
        code_challenge="test-challenge",
        browser_binding_cookie_name="__Host-zak_oidc_test",
        browser_binding_cookie_value="test-cookie-secret",
        expires_at=NOW + timedelta(minutes=5),
    )


def make_record(*, fingerprint=None, expires_at=None, transaction_id=TRANSACTION_ID):
    return type(
        "FakeRecord",
        (),
        {
            "id": RECORD_ID,
            "service_id": SERVICE_ID,
            "idempotency_key_hash": service.hash_idempotency_key(
                SERVICE_ID, REQUEST_ID
            ),
            "request_fingerprint": fingerprint or service.fingerprint_request(
                service.IDEMPOTENCY_OPERATION, {"version": 1}
            ),
            "transaction_id": transaction_id,
            "response_nonce": b"n" * 12,
            "response_ciphertext": b"c" * 64,
            "encryption_key_id": "test-key",
            "status": "completed",
            "expires_at": expires_at or NOW + timedelta(hours=24),
        },
    )()


def make_db_transaction(*, consumed_at=None, expires_at=None):
    return type(
        "FakeOIDCTransaction",
        (),
        {
            "id": TRANSACTION_ID,
            "created_at": NOW - timedelta(seconds=1),
            "expires_at": expires_at or NOW + timedelta(minutes=5),
            "consumed_at": consumed_at,
        },
    )()


def patch_creation(monkeypatch):
    async def fake_create(session, *, encryption, now):
        assert session.in_transaction()
        return make_transaction()

    monkeypatch.setattr(
        service,
        "create_oidc_auth_transaction_in_transaction",
        fake_create,
    )
    monkeypatch.setattr(
        service,
        "encrypt_idempotency_response",
        lambda payload, **kwargs: (b"n" * 12, b"c" * 64, "test-key"),
    )


def patch_decryption(monkeypatch):
    payload = {
        "authorization_url": "https://issuer.example/authorize?state=test",
        "state": "test-state",
        "browser_cookie_name": "__Host-zak_oidc_test",
        "browser_cookie_value": "test-cookie-secret",
        "max_age": 300,
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    monkeypatch.setattr(
        service,
        "decrypt_idempotency_response",
        lambda *args, **kwargs: payload,
    )
    return payload


def test_production_path_uses_database_clock(monkeypatch):
    patch_creation(monkeypatch)
    session = FakeSession(
        scalar_values=["read committed", NOW, None, NOW],
        execute_values=[FakeResult(RECORD_ID)],
    )

    result = asyncio.run(
        service.create_or_replay_oidc_transaction(
            session,
            service_id=SERVICE_ID,
            request_id=REQUEST_ID,
            encryption=make_encryption(),
            authorization_url_builder=lambda tx: (
                "https://issuer.example/authorize?state=" + tx.state
            ),
        )
    )

    assert result.replayed is False
    assert session.committed is True
    assert len(session.statements) == 5


def test_non_read_committed_isolation_fails_closed():
    session = FakeSession(scalar_values=["repeatable read"])

    with pytest.raises(service.OIDCIdempotencyPersistenceError):
        asyncio.run(
            service.create_or_replay_oidc_transaction(
                session,
                service_id=SERVICE_ID,
                request_id=REQUEST_ID,
                encryption=make_encryption(),
                authorization_url_builder=lambda tx: "https://issuer.example",
                now=NOW,
            )
        )

    assert session.rolled_back is True


def test_first_request_creates_transaction_and_idempotency_record(monkeypatch):
    patch_creation(monkeypatch)
    session = FakeSession(
        scalar_values=["read committed", None],
        execute_values=[FakeResult(RECORD_ID)],
    )

    result = asyncio.run(
        service.create_or_replay_oidc_transaction(
            session,
            service_id=SERVICE_ID,
            request_id=REQUEST_ID,
            encryption=make_encryption(),
            authorization_url_builder=lambda tx: (
                "https://issuer.example/authorize?state=" + tx.state
            ),
            now=NOW,
        )
    )

    assert result.replayed is False
    assert result.response["state"] == "test-state"
    assert result.response["browser_cookie_value"] == "test-cookie-secret"
    assert session.committed is True
    assert session.rolled_back is False
    assert len(session.statements) == 3


def test_existing_active_record_replays_original_response(monkeypatch):
    payload = patch_decryption(monkeypatch)
    record = make_record()
    transaction = make_db_transaction()
    session = FakeSession(scalar_values=["read committed", record, transaction])

    result = asyncio.run(
        service.create_or_replay_oidc_transaction(
            session,
            service_id=SERVICE_ID,
            request_id=REQUEST_ID,
            encryption=make_encryption(),
            authorization_url_builder=lambda tx: pytest.fail(
                "Builder must not run during replay"
            ),
            now=NOW,
        )
    )

    assert result.replayed is True
    assert result.response == payload
    assert session.committed is True


def test_fingerprint_mismatch_returns_conflict_and_rolls_back():
    record = make_record(fingerprint="0" * 64)
    session = FakeSession(scalar_values=["read committed", record])

    with pytest.raises(service.OIDCIdempotencyConflictError):
        asyncio.run(
            service.create_or_replay_oidc_transaction(
                session,
                service_id=SERVICE_ID,
                request_id=REQUEST_ID,
                encryption=make_encryption(),
                authorization_url_builder=lambda tx: "https://issuer.example",
                now=NOW,
            )
        )

    assert session.rolled_back is True


def test_consumed_transaction_cannot_be_replayed(monkeypatch):
    patch_decryption(monkeypatch)
    record = make_record()
    transaction = make_db_transaction(consumed_at=NOW)
    session = FakeSession(scalar_values=["read committed", record, transaction])

    with pytest.raises(service.OIDCIdempotencyConflictError):
        asyncio.run(
            service.create_or_replay_oidc_transaction(
                session,
                service_id=SERVICE_ID,
                request_id=REQUEST_ID,
                encryption=make_encryption(),
                authorization_url_builder=lambda tx: "https://issuer.example",
                now=NOW,
            )
        )

    assert session.rolled_back is True


def test_concurrent_insert_loser_deletes_provisional_and_replays_winner(
    monkeypatch,
):
    patch_creation(monkeypatch)
    payload = patch_decryption(monkeypatch)
    winner = make_record()
    transaction = make_db_transaction()
    session = FakeSession(
        scalar_values=["read committed", None, winner, transaction],
        execute_values=[FakeResult(None), FakeResult()],
    )

    result = asyncio.run(
        service.create_or_replay_oidc_transaction(
            session,
            service_id=SERVICE_ID,
            request_id=REQUEST_ID,
            encryption=make_encryption(),
            authorization_url_builder=lambda tx: (
                "https://issuer.example/authorize?state=" + tx.state
            ),
            now=NOW,
        )
    )

    assert result.replayed is True
    assert result.response == payload
    assert session.committed is True
    assert len(session.statements) == 6


def test_unexpired_reservation_with_missing_transaction_conflicts():
    record = make_record(transaction_id=None)
    session = FakeSession(scalar_values=["read committed", record])

    with pytest.raises(service.OIDCIdempotencyConflictError):
        asyncio.run(
            service.create_or_replay_oidc_transaction(
                session,
                service_id=SERVICE_ID,
                request_id=REQUEST_ID,
                encryption=make_encryption(),
                authorization_url_builder=lambda tx: "https://issuer.example",
                now=NOW,
            )
        )

    assert session.rolled_back is True


def test_fresh_session_is_required():
    session = FakeSession()
    session.active = True

    with pytest.raises(service.OIDCIdempotencyPersistenceError):
        asyncio.run(
            service.create_or_replay_oidc_transaction(
                session,
                service_id=SERVICE_ID,
                request_id=REQUEST_ID,
                encryption=make_encryption(),
                authorization_url_builder=lambda tx: "https://issuer.example",
                now=NOW,
            )
        )


def test_builder_failure_rolls_back_transaction(monkeypatch):
    patch_creation(monkeypatch)
    session = FakeSession(scalar_values=["read committed", None])

    with pytest.raises(RuntimeError, match="builder failure"):
        asyncio.run(
            service.create_or_replay_oidc_transaction(
                session,
                service_id=SERVICE_ID,
                request_id=REQUEST_ID,
                encryption=make_encryption(),
                authorization_url_builder=lambda tx: (
                    (_ for _ in ()).throw(RuntimeError("builder failure"))
                ),
                now=NOW,
            )
        )

    assert session.rolled_back is True