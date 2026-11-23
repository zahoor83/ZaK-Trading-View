"""Production HMAC authentication for private BFF-to-API requests."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Mapping

from fastapi import Request
from sqlalchemy import text

from app.security.internal_service_auth import InternalServicePrincipal


PROTOCOL_VERSION = "ZAK-INTERNAL-REQUEST-V1"

HEADER_SERVICE_ID = "X-Zak-Internal-Service-Id"
HEADER_KEY_ID = "X-Zak-Internal-Key-Id"
HEADER_TIMESTAMP = "X-Zak-Internal-Timestamp"
HEADER_NONCE = "X-Zak-Internal-Nonce"
HEADER_SIGNATURE = "X-Zak-Internal-Signature"

_REQUIRED_HEADERS = (
    HEADER_SERVICE_ID,
    HEADER_KEY_ID,
    HEADER_TIMESTAMP,
    HEADER_NONCE,
    HEADER_SIGNATURE,
)

_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TIMESTAMP_RE = re.compile(r"^(0|[1-9][0-9]{0,11})$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SIGNATURE_RE = re.compile(r"^[a-f0-9]{64}$")


class HMACConfigurationError(ValueError):
    """Raised when trusted HMAC configuration is invalid."""


class HMACInternalServiceAuthenticator:
    """Verify signed requests and atomically claim their replay nonce."""

    def __init__(
        self,
        *,
        service_id: str,
        keys: Mapping[str, bytes],
        timestamp_window_seconds: int = 300,
        nonce_retention_seconds: int = 60,
    ) -> None:
        if not isinstance(service_id, str) or not _SERVICE_ID_RE.fullmatch(service_id):
            raise HMACConfigurationError("Invalid internal service ID.")

        if not isinstance(keys, Mapping) or not keys:
            raise HMACConfigurationError("HMAC key ring is empty.")

        validated_keys: dict[str, bytes] = {}
        for key_id, secret in keys.items():
            if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
                raise HMACConfigurationError("Invalid HMAC key ID.")
            if not isinstance(secret, bytes) or len(secret) < 32:
                raise HMACConfigurationError("HMAC keys must contain at least 32 bytes.")
            validated_keys[key_id] = secret

        if (
            isinstance(timestamp_window_seconds, bool)
            or not isinstance(timestamp_window_seconds, int)
            or not 30 <= timestamp_window_seconds <= 900
        ):
            raise HMACConfigurationError("Invalid timestamp window.")

        if (
            isinstance(nonce_retention_seconds, bool)
            or not isinstance(nonce_retention_seconds, int)
            or not 30 <= nonce_retention_seconds <= 600
        ):
            raise HMACConfigurationError("Invalid nonce retention.")

        self._service_id = service_id
        self._keys = dict(validated_keys)
        self._timestamp_window_seconds = timestamp_window_seconds
        self._nonce_retention_seconds = nonce_retention_seconds

    @staticmethod
    def _single_header(request: Request, name: str) -> str | None:
        values = request.headers.getlist(name)
        if len(values) != 1:
            return None
        value = values[0]
        if not value or "\r" in value or "\n" in value:
            return None
        return value

    @staticmethod
    def _canonical_target(request: Request) -> str | None:
        raw_path = request.scope.get("raw_path")
        query = request.scope.get("query_string", b"")

        if not isinstance(raw_path, bytes) or not isinstance(query, bytes):
            return None

        try:
            path_text = raw_path.decode("ascii")
            query_text = query.decode("ascii")
        except UnicodeDecodeError:
            return None

        if (
            not path_text.startswith("/")
            or "\n" in path_text
            or "\r" in path_text
            or "#" in path_text
            or "\n" in query_text
            or "\r" in query_text
            or "#" in query_text
        ):
            return None

        return path_text + (("?" + query_text) if query_text else "")

    @staticmethod
    def _canonical_message(
        *,
        service_id: str,
        key_id: str,
        timestamp: str,
        nonce: str,
        method: str,
        target: str,
        body_digest: str,
    ) -> bytes:
        fields = (
            PROTOCOL_VERSION,
            service_id,
            key_id,
            timestamp,
            nonce,
            method,
            target,
            body_digest,
        )
        return "\n".join(fields).encode("ascii")

    async def authenticate(
        self,
        request: Request,
    ) -> InternalServicePrincipal | None:
        values = {
            name: self._single_header(request, name)
            for name in _REQUIRED_HEADERS
        }
        if any(value is None for value in values.values()):
            return None

        service_id = values[HEADER_SERVICE_ID]
        key_id = values[HEADER_KEY_ID]
        timestamp_text = values[HEADER_TIMESTAMP]
        nonce = values[HEADER_NONCE]
        supplied_signature = values[HEADER_SIGNATURE]

        assert service_id is not None
        assert key_id is not None
        assert timestamp_text is not None
        assert nonce is not None
        assert supplied_signature is not None

        if service_id != self._service_id:
            return None
        if not _SERVICE_ID_RE.fullmatch(service_id):
            return None
        if not _KEY_ID_RE.fullmatch(key_id):
            return None
        if not _TIMESTAMP_RE.fullmatch(timestamp_text):
            return None
        if not _NONCE_RE.fullmatch(nonce):
            return None
        if not _SIGNATURE_RE.fullmatch(supplied_signature):
            return None

        secret = self._keys.get(key_id)
        if secret is None:
            return None

        signed_at_epoch = int(timestamp_text)
        now_epoch = int(time.time())

        if abs(now_epoch - signed_at_epoch) > self._timestamp_window_seconds:
            return None

        method = request.method
        if not method or not method.isascii() or not method.isalpha():
            return None
        method = method.upper()

        target = self._canonical_target(request)
        if target is None:
            return None

        body = await request.body()
        body_digest = hashlib.sha256(body).hexdigest()

        message = self._canonical_message(
            service_id=service_id,
            key_id=key_id,
            timestamp=timestamp_text,
            nonce=nonce,
            method=method,
            target=target,
            body_digest=body_digest,
        )

        expected_signature = hmac.new(
            secret,
            message,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, supplied_signature):
            return None

        database_manager = getattr(request.app.state, "database_manager", None)
        if database_manager is None:
            raise RuntimeError("Internal authentication database is unavailable.")

        session_factory = database_manager.session_factory()
        signed_at = datetime.fromtimestamp(signed_at_epoch, tz=timezone.utc)
        expires_at = signed_at + timedelta(
            seconds=(
                self._timestamp_window_seconds
                + self._nonce_retention_seconds
            )
        )
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()

        statement = text(
            """
            INSERT INTO internal_service_request_nonces
                (service_id, key_id, nonce_hash, signed_at, expires_at)
            VALUES
                (:service_id, :key_id, :nonce_hash, :signed_at, :expires_at)
            ON CONFLICT ON CONSTRAINT uq_internal_service_nonce_identity
            DO NOTHING
            RETURNING id
            """
        )

        async with session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    statement,
                    {
                        "service_id": service_id,
                        "key_id": key_id,
                        "nonce_hash": nonce_hash,
                        "signed_at": signed_at,
                        "expires_at": expires_at,
                    },
                )
                claimed_id = result.scalar_one_or_none()

        # The transaction has committed successfully before authentication
        # can return a principal. A conflict means the nonce was replayed.
        if claimed_id is None:
            return None

        return InternalServicePrincipal(service_id)