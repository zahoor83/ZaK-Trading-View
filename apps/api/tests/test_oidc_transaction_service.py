from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models.oidc import OIDCAuthTransaction
from app.services.oidc_transaction_service import (
    OIDC_TRANSACTION_TTL,
    OIDCTransactionEncryptionSettings,
    OIDCTransactionOwnershipError,
    OIDCTransactionConfigurationError,
    OIDCTransactionRejectedError,
    create_oidc_auth_transaction,
    consume_oidc_auth_transaction,
)


NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)


def encryption_settings():
    return OIDCTransactionEncryptionSettings(
        keys={"test-v1": b"K" * 32},
        active_key_id="test-v1",
    )


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.transaction_active = True
        self.session.begin_count += 1
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.session.transaction_active = False

        if exc_type is None:
            self.session.committed = True
        else:
            self.session.rolled_back = True

        return False


class FakeSession:
    def __init__(self, row=None, already_active=False, fail_on_flush=False):
        self.row = row
        self.transaction_active = already_active
        self.fail_on_flush = fail_on_flush
        self.begin_count = 0
        self.execute_count = 0
        self.flush_count = 0
        self.committed = False
        self.rolled_back = False
        self.added = []
        self.statement = None

    def in_transaction(self):
        return self.transaction_active

    def begin(self):
        return FakeTransaction(self)

    def add(self, instance):
        self.added.append(instance)

    async def execute(self, statement):
        self.execute_count += 1
        self.statement = statement
        return FakeResult(self.row)

    async def flush(self):
        self.flush_count += 1
        if self.fail_on_flush:
            raise RuntimeError("Simulated flush failure.")


@pytest.mark.asyncio
async def test_create_transaction_persists_only_hashes_and_ciphertext():
    session = FakeSession()

    result = await create_oidc_auth_transaction(
        session,
        encryption=encryption_settings(),
        now=NOW,
    )

    row = next(x for x in session.added if isinstance(x, OIDCAuthTransaction))

    assert result.transaction_id == row.id
    assert row.state_hash != result.state
    assert row.nonce_hash != result.nonce
    assert row.browser_binding_hash != result.browser_binding_cookie_value

    assert len(row.state_hash) == 64
    assert len(row.nonce_hash) == 64
    assert len(row.browser_binding_hash) == 64

    assert row.code_verifier_nonce != b""
    assert len(row.code_verifier_nonce) == 12
    assert len(row.code_verifier_ciphertext) > 16
    assert result.code_challenge != row.code_verifier_ciphertext.decode(
        "latin1", errors="ignore"
    )

    assert row.encryption_key_id == "test-v1"
    assert row.expires_at - row.created_at == OIDC_TRANSACTION_TTL
    assert row.consumed_at is None
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_created_transaction_has_rfc7636_s256_challenge():
    result = await create_oidc_auth_transaction(
        FakeSession(),
        encryption=encryption_settings(),
        now=NOW,
    )

    assert len(result.code_challenge) == 43
    assert result.state
    assert result.nonce
    assert result.browser_binding_cookie_value
    assert result.browser_binding_cookie_name.startswith("__Host-zak_oidc_")


@pytest.mark.asyncio
async def test_consume_returns_verifier_and_nonce_hash_once():
    created = await create_oidc_auth_transaction(
        FakeSession(),
        encryption=encryption_settings(),
        now=NOW,
    )
    stored_session = FakeSession()
    created_again = await create_oidc_auth_transaction(
        stored_session,
        encryption=encryption_settings(),
        now=NOW,
    )
    row = stored_session.added[0]

    session = FakeSession(row=row)
    consumed = await consume_oidc_auth_transaction(
        session,
        state=created_again.state,
        browser_binding=created_again.browser_binding_cookie_value,
        encryption=encryption_settings(),
        now=NOW + timedelta(seconds=1),
    )

    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(consumed.code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")

    assert challenge == created_again.code_challenge
    assert consumed.nonce_hash == row.nonce_hash
    assert row.consumed_at == NOW + timedelta(seconds=1)
    assert session.committed is True
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_replayed_transaction_is_rejected():
    created_session = FakeSession()
    created = await create_oidc_auth_transaction(
        created_session,
        encryption=encryption_settings(),
        now=NOW,
    )
    row = created_session.added[0]

    row.consumed_at = NOW + timedelta(seconds=1)

    session = FakeSession(row=row)

    with pytest.raises(OIDCTransactionRejectedError):
        await consume_oidc_auth_transaction(
            session,
            state=created.state,
            browser_binding=created.browser_binding_cookie_value,
            encryption=encryption_settings(),
            now=NOW + timedelta(seconds=2),
        )

    assert session.execute_count == 1
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_wrong_browser_binding_is_rejected_without_consuming():
    created_session = FakeSession()
    created = await create_oidc_auth_transaction(
        created_session,
        encryption=encryption_settings(),
        now=NOW,
    )
    row = created_session.added[0]

    session = FakeSession(row=row)

    with pytest.raises(OIDCTransactionRejectedError):
        await consume_oidc_auth_transaction(
            session,
            state=created.state,
            browser_binding="B" * 43,
            encryption=encryption_settings(),
            now=NOW + timedelta(seconds=1),
        )

    assert row.consumed_at is None
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_expired_transaction_is_rejected():
    created_session = FakeSession()
    created = await create_oidc_auth_transaction(
        created_session,
        encryption=encryption_settings(),
        now=NOW,
    )
    row = created_session.added[0]

    session = FakeSession(row=row)

    with pytest.raises(OIDCTransactionRejectedError):
        await consume_oidc_auth_transaction(
            session,
            state=created.state,
            browser_binding=created.browser_binding_cookie_value,
            encryption=encryption_settings(),
            now=row.expires_at,
        )

    assert row.consumed_at is None
    assert session.rolled_back is True


@pytest.mark.asyncio
async def test_unknown_state_is_rejected():
    session = FakeSession(row=None)

    with pytest.raises(OIDCTransactionRejectedError):
        await consume_oidc_auth_transaction(
            session,
            state="S" * 43,
            browser_binding="B" * 43,
            encryption=encryption_settings(),
            now=NOW,
        )

    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_invalid_token_input_is_rejected_before_database_query():
    session = FakeSession()

    with pytest.raises(OIDCTransactionRejectedError):
        await consume_oidc_auth_transaction(
            session,
            state="short",
            browser_binding="B" * 43,
            encryption=encryption_settings(),
            now=NOW,
        )

    assert session.execute_count == 0
    assert session.begin_count == 0


@pytest.mark.asyncio
async def test_active_database_transaction_is_rejected():
    session = FakeSession(already_active=True)

    with pytest.raises(OIDCTransactionOwnershipError):
        await create_oidc_auth_transaction(
            session,
            encryption=encryption_settings(),
            now=NOW,
        )

    assert session.begin_count == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_flush_failure_rolls_back_transaction_creation():
    session = FakeSession(fail_on_flush=True)

    with pytest.raises(RuntimeError):
        await create_oidc_auth_transaction(
            session,
            encryption=encryption_settings(),
            now=NOW,
        )

    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_two_tabs_create_independent_transactions():
    first_session = FakeSession()
    second_session = FakeSession()

    first = await create_oidc_auth_transaction(
        first_session,
        encryption=encryption_settings(),
        now=NOW,
    )
    second = await create_oidc_auth_transaction(
        second_session,
        encryption=encryption_settings(),
        now=NOW,
    )

    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.browser_binding_cookie_name != second.browser_binding_cookie_name
    assert first.transaction_id != second.transaction_id


@pytest.mark.asyncio
async def test_old_key_can_decrypt_transaction_after_key_rotation():
    old_settings = OIDCTransactionEncryptionSettings(
        keys={"old-v1": b"O" * 32},
        active_key_id="old-v1",
    )
    rotated_settings = OIDCTransactionEncryptionSettings(
        keys={
            "old-v1": b"O" * 32,
            "new-v2": b"N" * 32,
        },
        active_key_id="new-v2",
    )

    created_session = FakeSession()
    created = await create_oidc_auth_transaction(
        created_session,
        encryption=old_settings,
        now=NOW,
    )
    row = created_session.added[0]

    assert row.encryption_key_id == "old-v1"

    consumed = await consume_oidc_auth_transaction(
        FakeSession(row=row),
        state=created.state,
        browser_binding=created.browser_binding_cookie_value,
        encryption=rotated_settings,
        now=NOW + timedelta(seconds=1),
    )

    assert consumed.transaction_id == created.transaction_id


@pytest.mark.asyncio
async def test_tampered_ciphertext_is_burned_and_rejected():
    created_session = FakeSession()
    created = await create_oidc_auth_transaction(
        created_session,
        encryption=encryption_settings(),
        now=NOW,
    )
    row = created_session.added[0]

    tampered = bytearray(row.code_verifier_ciphertext)
    tampered[0] ^= 1
    row.code_verifier_ciphertext = bytes(tampered)

    session = FakeSession(row=row)

    with pytest.raises(OIDCTransactionRejectedError):
        await consume_oidc_auth_transaction(
            session,
            state=created.state,
            browser_binding=created.browser_binding_cookie_value,
            encryption=encryption_settings(),
            now=NOW + timedelta(seconds=1),
        )

    assert row.consumed_at == NOW + timedelta(seconds=1)
    assert session.committed is True


def test_invalid_encryption_key_length_rejected():
    with pytest.raises(OIDCTransactionConfigurationError):
        OIDCTransactionEncryptionSettings(
            keys={"bad": b"short"},
            active_key_id="bad",
        ).validated()


def test_active_key_must_exist():
    with pytest.raises(OIDCTransactionConfigurationError):
        OIDCTransactionEncryptionSettings(
            keys={"old": b"O" * 32},
            active_key_id="missing",
        ).validated()


def test_environment_keyring_loads_without_exposing_key_material():
    encoded = base64.urlsafe_b64encode(b"E" * 32).rstrip(b"=").decode("ascii")
    environment = {
        "OIDC_TRANSACTION_ENCRYPTION_KEYS": json.dumps(
            {"env-v1": encoded}
        ),
        "OIDC_TRANSACTION_ACTIVE_KEY_ID": "env-v1",
    }

    settings = OIDCTransactionEncryptionSettings.from_environment(environment)

    assert settings.active_key_id == "env-v1"
    assert settings.keys["env-v1"] == b"E" * 32
    assert "EEEE" not in repr(settings)


def test_environment_rejects_malformed_keyring():
    with pytest.raises(OIDCTransactionConfigurationError):
        OIDCTransactionEncryptionSettings.from_environment(
            {
                "OIDC_TRANSACTION_ENCRYPTION_KEYS": "not-json",
                "OIDC_TRANSACTION_ACTIVE_KEY_ID": "v1",
            }
        )


def test_model_has_expected_table_name():
    assert OIDCAuthTransaction.__tablename__ == "oidc_auth_transactions"
