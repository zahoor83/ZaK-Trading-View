"""Atomic persistence for idempotent OIDC transaction creation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oidc import OIDCAuthTransaction, OIDCIdempotencyRecord
from app.security.oidc_idempotency import (
    fingerprint_request,
    hash_idempotency_key,
)
from app.security.oidc_idempotency_encryption import (
    decrypt_idempotency_response,
    encrypt_idempotency_response,
)
from app.services.oidc_transaction_service import (
    OIDCTransactionCreationResult,
    OIDCTransactionEncryptionSettings,
    create_oidc_auth_transaction_in_transaction,
)


IDEMPOTENCY_RETENTION = timedelta(hours=24)
IDEMPOTENCY_OPERATION = "POST:/internal/v1/auth/oidc/transactions:v1"


class OIDCIdempotencyConflictError(Exception):
    """The idempotency key cannot safely replay or create this operation."""


class OIDCIdempotencyPersistenceError(Exception):
    """Idempotency persistence failed closed."""


@dataclass(frozen=True, slots=True)
class OIDCIdempotentTransactionResult:
    response: dict
    replayed: bool


def _require_aware(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OIDCIdempotencyPersistenceError(
            f"{name} must be timezone-aware."
        )


def _response_payload(
    authorization_url: str,
    transaction: OIDCTransactionCreationResult,
) -> dict:
    if not isinstance(authorization_url, str) or not authorization_url:
        raise OIDCIdempotencyPersistenceError(
            "Authorization response is invalid."
        )

    return {
        "authorization_url": authorization_url,
        "state": transaction.state,
        "browser_cookie_name": transaction.browser_binding_cookie_name,
        "browser_cookie_value": transaction.browser_binding_cookie_value,
        "max_age": 300,
        "expires_at": transaction.expires_at.isoformat(),
    }


def _validate_replay_payload(payload: dict) -> dict:
    expected = {
        "authorization_url",
        "state",
        "browser_cookie_name",
        "browser_cookie_value",
        "max_age",
        "expires_at",
    }

    if set(payload) != expected:
        raise OIDCIdempotencyPersistenceError(
            "Stored idempotency response is invalid."
        )

    if (
        not isinstance(payload["authorization_url"], str)
        or not payload["authorization_url"].startswith("https://")
        or not isinstance(payload["state"], str)
        or not isinstance(payload["browser_cookie_name"], str)
        or not payload["browser_cookie_name"].startswith(
            "__Host-zak_oidc_"
        )
        or not isinstance(payload["browser_cookie_value"], str)
        or payload["max_age"] != 300
        or not isinstance(payload["expires_at"], str)
    ):
        raise OIDCIdempotencyPersistenceError(
            "Stored idempotency response is invalid."
        )

    try:
        expiry = datetime.fromisoformat(payload["expires_at"])
        _require_aware(expiry, "Stored response expiry")
    except (ValueError, TypeError):
        raise OIDCIdempotencyPersistenceError(
            "Stored idempotency response is invalid."
        ) from None

    return payload


async def _replay_record(
    session: AsyncSession,
    record: OIDCIdempotencyRecord,
    *,
    service_id: str,
    key_hash: str,
    fingerprint: str,
    encryption: OIDCTransactionEncryptionSettings,
    now: datetime,
) -> dict:
    if record.expires_at <= now:
        raise OIDCIdempotencyConflictError(
            "Idempotency reservation has expired."
        )

    if record.request_fingerprint != fingerprint:
        raise OIDCIdempotencyConflictError(
            "Idempotency key was used for a different request."
        )

    if record.transaction_id is None:
        raise OIDCIdempotencyConflictError(
            "Original OIDC transaction is no longer available."
        )

    transaction = await session.scalar(
        select(OIDCAuthTransaction).where(
            OIDCAuthTransaction.id == record.transaction_id
        )
    )

    if transaction is None:
        raise OIDCIdempotencyConflictError(
            "Original OIDC transaction is no longer available."
        )

    _require_aware(transaction.created_at, "Transaction creation time")
    _require_aware(transaction.expires_at, "Transaction expiry")

    if (
        transaction.consumed_at is not None
        or transaction.created_at > now
        or transaction.expires_at <= now
    ):
        raise OIDCIdempotencyConflictError(
            "Original OIDC transaction is no longer active."
        )

    try:
        payload = decrypt_idempotency_response(
            record.response_nonce,
            record.response_ciphertext,
            key_id=record.encryption_key_id,
            record_id=record.id,
            service_id=service_id,
            key_hash=key_hash,
            fingerprint=fingerprint,
            transaction_id=record.transaction_id,
            settings=encryption,
        )
    except Exception:
        raise OIDCIdempotencyPersistenceError(
            "Stored idempotency response is unavailable."
        ) from None

    return _validate_replay_payload(payload)


async def create_or_replay_oidc_transaction(
    session: AsyncSession,
    *,
    service_id: str,
    request_id: str,
    encryption: OIDCTransactionEncryptionSettings,
    authorization_url_builder: Callable[
        [OIDCTransactionCreationResult], str
    ],
    now: datetime | None = None,
) -> OIDCIdempotentTransactionResult:
    """Atomically create an OIDC transaction or replay its original response."""

    if session.in_transaction():
        raise OIDCIdempotencyPersistenceError(
            "A fresh database session is required."
        )

    if now is not None:
        _require_aware(now, "Current time override")
    encryption = encryption.validated()

    key_hash = hash_idempotency_key(service_id, request_id)
    fingerprint = fingerprint_request(
        IDEMPOTENCY_OPERATION,
        {"version": 1},
    )

    async with session.begin():
        isolation_level = await session.scalar(
            select(func.current_setting("transaction_isolation"))
        )
        if isolation_level != "read committed":
            raise OIDCIdempotencyPersistenceError(
                "OIDC idempotency requires READ COMMITTED isolation."
            )

        current_time = now
        if current_time is None:
            current_time = await session.scalar(
                select(func.current_timestamp())
            )
        _require_aware(current_time, "Database current time")

        existing = await session.scalar(
            select(OIDCIdempotencyRecord)
            .where(
                OIDCIdempotencyRecord.service_id == service_id,
                OIDCIdempotencyRecord.idempotency_key_hash == key_hash,
            )
            .with_for_update()
        )

        # CURRENT_TIMESTAMP is fixed at transaction start. Refresh
        # wall-clock time after acquiring the reservation row lock.
        if now is None:
            current_time = await session.scalar(
                select(func.clock_timestamp())
            )
        _require_aware(current_time, "Database current time")

        if existing is not None:
            _require_aware(existing.expires_at, "Idempotency expiry")

            if existing.expires_at > current_time:
                response = await _replay_record(
                    session,
                    existing,
                    service_id=service_id,
                    key_hash=key_hash,
                    fingerprint=fingerprint,
                    encryption=encryption,
                    now=current_time,
                )
                return OIDCIdempotentTransactionResult(
                    response=response,
                    replayed=True,
                )

            await session.execute(
                delete(OIDCIdempotencyRecord).where(
                    OIDCIdempotencyRecord.id == existing.id
                )
            )
            await session.flush()

        transaction = await create_oidc_auth_transaction_in_transaction(
            session,
            encryption=encryption,
            now=current_time,
        )

        authorization_url = authorization_url_builder(transaction)
        payload = _response_payload(authorization_url, transaction)

        record_id = uuid.uuid4()
        nonce, ciphertext, key_id = encrypt_idempotency_response(
            payload,
            record_id=record_id,
            service_id=service_id,
            key_hash=key_hash,
            fingerprint=fingerprint,
            transaction_id=transaction.transaction_id,
            settings=encryption,
        )

        record_expiry = current_time + IDEMPOTENCY_RETENTION

        statement = (
            insert(OIDCIdempotencyRecord)
            .values(
                id=record_id,
                service_id=service_id,
                idempotency_key_hash=key_hash,
                request_fingerprint=fingerprint,
                transaction_id=transaction.transaction_id,
                response_nonce=nonce,
                response_ciphertext=ciphertext,
                encryption_key_id=key_id,
                status="completed",
                expires_at=record_expiry,
            )
            .on_conflict_do_nothing(
                constraint="uq_oidc_idempotency_service_key"
            )
            .returning(OIDCIdempotencyRecord.id)
        )

        inserted_id = (
            await session.execute(statement)
        ).scalar_one_or_none()

        if inserted_id is not None:
            return OIDCIdempotentTransactionResult(
                response=payload,
                replayed=False,
            )

        # Another request committed the same key while this request
        # was building its response. Remove this request's unused
        # transaction before loading the winning reservation.
        await session.execute(
            delete(OIDCAuthTransaction).where(
                OIDCAuthTransaction.id == transaction.transaction_id,
                OIDCAuthTransaction.consumed_at.is_(None),
            )
        )
        await session.flush()

        winner = await session.scalar(
            select(OIDCIdempotencyRecord)
            .where(
                OIDCIdempotencyRecord.service_id == service_id,
                OIDCIdempotencyRecord.idempotency_key_hash == key_hash,
            )
            .with_for_update()
        )

        if winner is None:
            raise OIDCIdempotencyPersistenceError(
                "Idempotency reservation could not be resolved."
            )

        # ON CONFLICT may wait for another transaction. Refresh the
        # database wall clock before validating the winning transaction.
        if now is None:
            current_time = await session.scalar(
                select(func.clock_timestamp())
            )
        _require_aware(current_time, "Database current time")

        response = await _replay_record(
            session,
            winner,
            service_id=service_id,
            key_hash=key_hash,
            fingerprint=fingerprint,
            encryption=encryption,
            now=current_time,
        )

        return OIDCIdempotentTransactionResult(
            response=response,
            replayed=True,
        )