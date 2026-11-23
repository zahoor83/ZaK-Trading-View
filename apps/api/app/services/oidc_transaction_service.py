"""Secure server-side OIDC authorization transaction lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oidc import OIDCAuthTransaction


OIDC_TRANSACTION_TTL = timedelta(minutes=5)
OIDC_TRANSACTION_COOKIE_PREFIX = "__Host-zak_oidc_"
AES_GCM_NONCE_BYTES = 12
AES_GCM_KEY_BYTES = 32

_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_BASE64URL_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class OIDCTransactionError(Exception):
    """Base OIDC transaction error."""


class OIDCTransactionConfigurationError(OIDCTransactionError):
    """Encryption configuration is missing or invalid."""


class OIDCTransactionOwnershipError(OIDCTransactionError):
    """The service requires a fresh database session."""


class OIDCTransactionRejectedError(OIDCTransactionError):
    """The authorization transaction is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class OIDCTransactionEncryptionSettings:
    """Encryption keyring. Key material is deliberately excluded from repr."""

    keys: Mapping[str, bytes] = field(repr=False)
    active_key_id: str

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "OIDCTransactionEncryptionSettings":
        values = os.environ if environ is None else environ

        raw_keys = values.get("OIDC_TRANSACTION_ENCRYPTION_KEYS")
        active_key_id = values.get("OIDC_TRANSACTION_ACTIVE_KEY_ID")

        if not isinstance(raw_keys, str) or not raw_keys:
            raise OIDCTransactionConfigurationError(
                "OIDC transaction encryption keyring is not configured."
            )

        if not isinstance(active_key_id, str) or not active_key_id:
            raise OIDCTransactionConfigurationError(
                "OIDC transaction active key ID is not configured."
            )

        try:
            encoded_keys = json.loads(raw_keys)
        except (TypeError, ValueError):
            raise OIDCTransactionConfigurationError(
                "OIDC transaction keyring configuration is invalid."
            ) from None

        if not isinstance(encoded_keys, dict) or not encoded_keys:
            raise OIDCTransactionConfigurationError(
                "OIDC transaction keyring must be a non-empty object."
            )

        decoded: dict[str, bytes] = {}

        for key_id, encoded in encoded_keys.items():
            if (
                not isinstance(key_id, str)
                or not _KEY_ID_PATTERN.fullmatch(key_id)
                or not isinstance(encoded, str)
                or not _BASE64URL_KEY_PATTERN.fullmatch(encoded)
            ):
                raise OIDCTransactionConfigurationError(
                    "OIDC transaction keyring contains an invalid entry."
                )

            try:
                key = base64.b64decode(
                    encoded + "=",
                    altchars=b"-_",
                    validate=True,
                )
            except (ValueError, TypeError):
                raise OIDCTransactionConfigurationError(
                    "OIDC transaction keyring contains invalid key material."
                ) from None

            if len(key) != AES_GCM_KEY_BYTES:
                raise OIDCTransactionConfigurationError(
                    "Every OIDC transaction encryption key must be 32 bytes."
                )

            decoded[key_id] = key

        settings = cls(
            keys=MappingProxyType(decoded),
            active_key_id=active_key_id,
        )
        return settings.validated()

    def validated(self) -> "OIDCTransactionEncryptionSettings":
        if not isinstance(self.keys, Mapping) or not self.keys:
            raise OIDCTransactionConfigurationError(
                "OIDC transaction keyring is empty."
            )

        if (
            not isinstance(self.active_key_id, str)
            or not _KEY_ID_PATTERN.fullmatch(self.active_key_id)
            or self.active_key_id not in self.keys
        ):
            raise OIDCTransactionConfigurationError(
                "OIDC transaction active key ID is unavailable."
            )

        for key_id, key in self.keys.items():
            if (
                not isinstance(key_id, str)
                or not _KEY_ID_PATTERN.fullmatch(key_id)
                or not isinstance(key, bytes)
                or len(key) != AES_GCM_KEY_BYTES
            ):
                raise OIDCTransactionConfigurationError(
                    "OIDC transaction keyring contains invalid key material."
                )

        return self


@dataclass(frozen=True, slots=True)
class OIDCTransactionCreationResult:
    transaction_id: uuid.UUID
    state: str
    nonce: str
    code_challenge: str
    browser_binding_cookie_name: str
    browser_binding_cookie_value: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedOIDCTransaction:
    transaction_id: uuid.UUID
    code_verifier: str
    nonce_hash: str


def _require_aware_datetime(value: Any, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OIDCTransactionError(
            f"{name} must be a timezone-aware datetime."
        )


def _require_fresh_session(session: AsyncSession) -> None:
    if session.in_transaction():
        raise OIDCTransactionOwnershipError(
            "OIDC transaction operations require a fresh database session."
        )


def _validate_secret(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 512
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise OIDCTransactionRejectedError(
            f"{name} is invalid."
        )


def _domain_hash(purpose: str, value: str) -> str:
    _validate_secret(value, purpose)

    material = purpose.encode("ascii") + b"\x00" + value.encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _associated_data(transaction_id: uuid.UUID, key_id: str) -> bytes:
    return (
        b"zak-oidc-pkce-v1\x00"
        + transaction_id.bytes
        + b"\x00"
        + key_id.encode("ascii")
    )


def _encrypt_verifier(
    verifier: str,
    transaction_id: uuid.UUID,
    settings: OIDCTransactionEncryptionSettings,
) -> tuple[bytes, bytes]:
    key_id = settings.active_key_id
    key = settings.keys[key_id]
    nonce = secrets.token_bytes(AES_GCM_NONCE_BYTES)

    ciphertext = AESGCM(key).encrypt(
        nonce,
        verifier.encode("ascii"),
        _associated_data(transaction_id, key_id),
    )
    return nonce, ciphertext


def _decrypt_verifier(
    row: OIDCAuthTransaction,
    settings: OIDCTransactionEncryptionSettings,
) -> str:
    key_id = row.encryption_key_id
    key = settings.keys.get(key_id)

    if key is None:
        raise OIDCTransactionRejectedError(
            "OIDC transaction cannot be decrypted."
        )

    if (
        not isinstance(row.code_verifier_nonce, bytes)
        or len(row.code_verifier_nonce) != AES_GCM_NONCE_BYTES
        or not isinstance(row.code_verifier_ciphertext, bytes)
        or len(row.code_verifier_ciphertext) <= 16
    ):
        raise OIDCTransactionRejectedError(
            "OIDC transaction cannot be decrypted."
        )

    try:
        plaintext = AESGCM(key).decrypt(
            row.code_verifier_nonce,
            row.code_verifier_ciphertext,
            _associated_data(row.id, key_id),
        )
        verifier = plaintext.decode("ascii")
    except (InvalidTag, ValueError, UnicodeError):
        raise OIDCTransactionRejectedError(
            "OIDC transaction cannot be decrypted."
        ) from None

    if not 43 <= len(verifier) <= 128:
        raise OIDCTransactionRejectedError(
            "OIDC transaction verifier is invalid."
        )

    return verifier


async def create_oidc_auth_transaction_in_transaction(
    session: AsyncSession,
    *,
    encryption: OIDCTransactionEncryptionSettings,
    now: datetime | None = None,
) -> OIDCTransactionCreationResult:
    """Create a transaction inside the caller-owned database transaction."""

    if not session.in_transaction():
        raise OIDCTransactionOwnershipError(
            "A caller-owned database transaction is required."
        )

    encryption = encryption.validated()
    current_time = now if now is not None else datetime.now(timezone.utc)
    _require_aware_datetime(current_time, "now")

    transaction_id = uuid.uuid4()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    browser_binding = secrets.token_urlsafe(32)

    state_hash = _domain_hash("state", state)
    nonce_hash = _domain_hash("nonce", nonce)
    browser_hash = _domain_hash("browser-binding", browser_binding)

    code_challenge = _b64url(
        hashlib.sha256(verifier.encode("ascii")).digest()
    )

    verifier_nonce, verifier_ciphertext = _encrypt_verifier(
        verifier,
        transaction_id,
        encryption,
    )

    expires_at = current_time + OIDC_TRANSACTION_TTL

    transaction = OIDCAuthTransaction(
        id=transaction_id,
        state_hash=state_hash,
        nonce_hash=nonce_hash,
        browser_binding_hash=browser_hash,
        code_verifier_nonce=verifier_nonce,
        code_verifier_ciphertext=verifier_ciphertext,
        encryption_key_id=encryption.active_key_id,
        created_at=current_time,
        expires_at=expires_at,
    )

    session.add(transaction)
    await session.flush()

    return OIDCTransactionCreationResult(
        transaction_id=transaction_id,
        state=state,
        nonce=nonce,
        code_challenge=code_challenge,
        browser_binding_cookie_name=(
            OIDC_TRANSACTION_COOKIE_PREFIX + state_hash[:24]
        ),
        browser_binding_cookie_value=browser_binding,
        expires_at=expires_at,
    )


async def create_oidc_auth_transaction(
    session: AsyncSession,
    *,
    encryption: OIDCTransactionEncryptionSettings,
    now: datetime | None = None,
) -> OIDCTransactionCreationResult:
    """Create one independent, five-minute OIDC authorization transaction."""

    _require_fresh_session(session)

    async with session.begin():
        result = await create_oidc_auth_transaction_in_transaction(
            session,
            encryption=encryption,
            now=now,
        )

    return result

async def consume_oidc_auth_transaction(
    session: AsyncSession,
    *,
    state: str,
    browser_binding: str,
    encryption: OIDCTransactionEncryptionSettings,
    now: datetime | None = None,
) -> ConsumedOIDCTransaction:
    """Atomically consume a valid transaction exactly once."""

    encryption = encryption.validated()
    current_time = now if now is not None else datetime.now(timezone.utc)
    _require_aware_datetime(current_time, "now")
    _require_fresh_session(session)

    state_hash = _domain_hash("state", state)
    browser_hash = _domain_hash("browser-binding", browser_binding)

    result_value: ConsumedOIDCTransaction | None = None
    decryption_failed = False

    async with session.begin():
        result = await session.execute(
            select(OIDCAuthTransaction)
            .where(OIDCAuthTransaction.state_hash == state_hash)
            .with_for_update()
        )

        row = result.scalar_one_or_none()

        if row is None:
            raise OIDCTransactionRejectedError(
                "OIDC authorization transaction is invalid."
            )

        if row.consumed_at is not None:
            raise OIDCTransactionRejectedError(
                "OIDC authorization transaction is invalid."
            )

        _require_aware_datetime(row.created_at, "transaction.created_at")
        _require_aware_datetime(row.expires_at, "transaction.expires_at")

        if row.expires_at <= current_time:
            raise OIDCTransactionRejectedError(
                "OIDC authorization transaction is invalid."
            )

        if row.created_at > current_time:
            raise OIDCTransactionRejectedError(
                "OIDC authorization transaction is invalid."
            )

        if not hmac.compare_digest(row.browser_binding_hash, browser_hash):
            raise OIDCTransactionRejectedError(
                "OIDC authorization transaction is invalid."
            )

        if not isinstance(row.nonce_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", row.nonce_hash
        ):
            raise OIDCTransactionRejectedError(
                "OIDC authorization transaction is invalid."
            )

        # Consume before decryption. Even corrupted ciphertext cannot be retried.
        row.consumed_at = current_time
        await session.flush()

        try:
            verifier = _decrypt_verifier(row, encryption)
        except OIDCTransactionRejectedError:
            decryption_failed = True
        else:
            result_value = ConsumedOIDCTransaction(
                transaction_id=row.id,
                code_verifier=verifier,
                nonce_hash=row.nonce_hash,
            )

    if decryption_failed or result_value is None:
        raise OIDCTransactionRejectedError(
            "OIDC authorization transaction is invalid."
        )

    return result_value
