from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.security.hmac_internal_service_auth import (
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SERVICE_ID,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HMACInternalServiceAuthenticator,
)


SERVICE_ID = "web-bff"
KEY_ID = "test-key-01"
SECRET = b"test-only-secret-material-at-least-32-bytes"
NONCE = "A" * 43


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.session.committed = True
        return False


class FakeSession:
    def __init__(self, claimed_id="nonce-row-id", fail=False):
        self.claimed_id = claimed_id
        self.fail = fail
        self.committed = False
        self.executed = False
        self.parameters = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement, parameters):
        self.executed = True
        self.parameters = parameters
        if self.fail:
            raise RuntimeError("database unavailable")
        return FakeResult(self.claimed_id)


class FakeDatabaseManager:
    def __init__(self, session):
        self.session = session

    def session_factory(self):
        return lambda: self.session


def build_request(
    *,
    body=b'{"hello":"world"}',
    signed_body=None,
    path=b"/internal/v1/example",
    query=b"mode=paper",
    headers=None,
    database_session=None,
    timestamp=None,
    nonce=NONCE,
    service_id=SERVICE_ID,
    key_id=KEY_ID,
    signing_secret=SECRET,
    method="POST",
    signed_path=None,
    signed_query=None,
):
    timestamp_text = (
        str(int(time.time())) if timestamp is None else str(timestamp)
    )

    canonical_path = path if signed_path is None else signed_path
    canonical_query = query if signed_query is None else signed_query

    target = canonical_path.decode("ascii") + (
        "?" + canonical_query.decode("ascii") if canonical_query else ""
    )
    digest = hashlib.sha256(
        body if signed_body is None else signed_body
    ).hexdigest()

    message = "\n".join(
        (
            "ZAK-INTERNAL-REQUEST-V1",
            service_id,
            key_id,
            timestamp_text,
            nonce,
            method,
            target,
            digest,
        )
    ).encode("ascii")

    signature = hmac.new(
        signing_secret, message, hashlib.sha256
    ).hexdigest()

    request_headers = [
        (HEADER_SERVICE_ID.lower().encode(), service_id.encode()),
        (HEADER_KEY_ID.lower().encode(), key_id.encode()),
        (HEADER_TIMESTAMP.lower().encode(), timestamp_text.encode()),
        (HEADER_NONCE.lower().encode(), nonce.encode()),
        (HEADER_SIGNATURE.lower().encode(), signature.encode()),
    ]

    if headers is not None:
        request_headers = headers

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    session = database_session or FakeSession()
    app = SimpleNamespace(
        state=SimpleNamespace(
            database_manager=FakeDatabaseManager(session)
        )
    )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path.decode("ascii"),
        "raw_path": path,
        "query_string": query,
        "headers": request_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
        "app": app,
    }

    return Request(scope, receive=receive), session

def make_authenticator():
    return HMACInternalServiceAuthenticator(
        service_id=SERVICE_ID,
        keys={KEY_ID: SECRET},
    )


@pytest.mark.asyncio
async def test_valid_signature_claims_nonce_and_commits_transaction():
    request, session = build_request()

    principal = await make_authenticator().authenticate(request)

    assert principal is not None
    assert principal.service_id == SERVICE_ID
    assert session.executed is True
    assert session.committed is True
    assert session.parameters["service_id"] == SERVICE_ID
    assert len(session.parameters["nonce_hash"]) == 64


@pytest.mark.asyncio
async def test_duplicate_nonce_is_rejected():
    request, session = build_request(
        database_session=FakeSession(claimed_id=None)
    )

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_tampered_body_is_rejected_before_database_claim():
    request, session = build_request(
        body=b'{"hello":"attacker"}',
        signed_body=b'{"hello":"world"}',
    )

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_tampered_path_is_rejected_before_database_claim():
    request, session = build_request()
    request.scope["raw_path"] = b"/internal/v1/other"

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_before_database_claim():
    request, session = build_request()
    request.scope["headers"] = [
        (name, b"0" * 64 if name == HEADER_SIGNATURE.lower().encode()
         else value)
        for name, value in request.scope["headers"]
    ]

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False



@pytest.mark.asyncio
async def test_wrong_service_id_is_rejected_before_database_claim():
    request, session = build_request(service_id="another-service")

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_unknown_key_id_is_rejected_before_database_claim():
    request, session = build_request(key_id="unknown-key")

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_wrong_signing_secret_is_rejected_before_database_claim():
    request, session = build_request(
        signing_secret=b"wrong-test-secret-material-32-bytes-long!!"
    )

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_stale_timestamp_is_rejected_before_database_claim():
    request, session = build_request(
        timestamp=str(int(time.time()) - 301)
    )

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_future_timestamp_is_rejected_before_database_claim():
    request, session = build_request(
        timestamp=str(int(time.time()) + 301)
    )

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_malformed_timestamp_is_rejected_before_database_claim():
    request, session = build_request(timestamp="01")

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_malformed_nonce_is_rejected_before_database_claim():
    request, session = build_request(nonce="invalid-nonce")

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_missing_signature_header_is_rejected_before_database_claim():
    request, session = build_request()
    signature_header = HEADER_SIGNATURE.lower().encode()

    request.scope["headers"] = [
        (name, value)
        for name, value in request.scope["headers"]
        if name != signature_header
    ]

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_duplicate_signature_header_is_rejected_before_database_claim():
    request, session = build_request()
    signature_header = HEADER_SIGNATURE.lower().encode()

    original = next(
        (name, value)
        for name, value in request.scope["headers"]
        if name == signature_header
    )
    request.scope["headers"].append(original)

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_tampered_query_is_rejected_before_database_claim():
    request, session = build_request()
    request.scope["query_string"] = b"mode=live"

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.asyncio
async def test_database_failure_propagates_and_never_authenticates():
    request, session = build_request(
        database_session=FakeSession(fail=True)
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await make_authenticator().authenticate(request)

    assert session.executed is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_factory_rotation_accepts_overlap_and_rejects_retired_key(
    monkeypatch,
):
    import base64
    import json

    from app.security.hmac_internal_service_auth_factory import (
        build_internal_service_authentication,
    )

    previous_secret = b"previous-rotation-secret-material-32"
    active_secret = b"active-rotation-secret-material-32"

    previous_id = "key-previous-2026"
    active_id = "key-active-2026"

    encoded_keys = {
        previous_id: base64.urlsafe_b64encode(
            previous_secret
        ).decode("ascii").rstrip("="),
        active_id: base64.urlsafe_b64encode(
            active_secret
        ).decode("ascii").rstrip("="),
    }

    monkeypatch.setenv("OIDC_INTERNAL_SERVICE_ID", SERVICE_ID)
    monkeypatch.setenv(
        "OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID",
        active_id,
    )
    monkeypatch.setenv(
        "OIDC_INTERNAL_SERVICE_HMAC_KEYS",
        json.dumps(encoded_keys),
    )

    overlap_authenticator, allowlist = (
        build_internal_service_authentication()
    )

    assert allowlist == frozenset({SERVICE_ID})

    previous_request, previous_session = build_request(
        key_id=previous_id,
        signing_secret=previous_secret,
        nonce="A" * 43,
    )
    active_request, active_session = build_request(
        key_id=active_id,
        signing_secret=active_secret,
        nonce="B" * 43,
    )

    previous_principal = await overlap_authenticator.authenticate(
        previous_request
    )
    active_principal = await overlap_authenticator.authenticate(
        active_request
    )

    assert previous_principal is not None
    assert active_principal is not None
    assert previous_session.committed is True
    assert active_session.committed is True

    # Simulate retirement: only the new key remains configured.
    monkeypatch.setenv(
        "OIDC_INTERNAL_SERVICE_HMAC_KEYS",
        json.dumps({active_id: encoded_keys[active_id]}),
    )

    retired_authenticator, retired_allowlist = (
        build_internal_service_authentication()
    )

    assert retired_allowlist == frozenset({SERVICE_ID})

    retired_request, retired_session = build_request(
        key_id=previous_id,
        signing_secret=previous_secret,
        nonce="C" * 43,
    )

    retired_principal = await retired_authenticator.authenticate(
        retired_request
    )

    assert retired_principal is None
    assert retired_session.executed is False



@pytest.mark.parametrize("offset", [-300, 300])
@pytest.mark.asyncio
async def test_accepts_exact_timestamp_window_boundaries(monkeypatch, offset):
    from app.security import hmac_internal_service_auth as auth_module

    fixed_now = 1_800_000_000
    monkeypatch.setattr(auth_module.time, "time", lambda: fixed_now)

    request, session = build_request(
        timestamp=str(fixed_now + offset),
        nonce=("D" if offset < 0 else "E") * 43,
    )

    principal = await make_authenticator().authenticate(request)

    assert principal is not None
    assert principal.service_id == SERVICE_ID
    assert session.executed is True
    assert session.committed is True


@pytest.mark.parametrize(
    "header_name",
    [
        HEADER_SERVICE_ID,
        HEADER_KEY_ID,
        HEADER_TIMESTAMP,
        HEADER_NONCE,
        HEADER_SIGNATURE,
    ],
)
@pytest.mark.asyncio
async def test_duplicate_any_required_security_header_is_rejected(header_name):
    request, session = build_request()
    encoded_name = header_name.lower().encode("ascii")

    original = next(
        (name, value)
        for name, value in request.scope["headers"]
        if name == encoded_name
    )
    request.scope["headers"].append(original)

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


@pytest.mark.parametrize(
    "scope_field,invalid_value",
    [
        ("raw_path", b"/internal/\xff"),
        ("query_string", b"mode=\xff"),
    ],
)
@pytest.mark.asyncio
async def test_non_ascii_raw_target_is_rejected_before_database_claim(
    scope_field,
    invalid_value,
):
    request, session = build_request()
    request.scope[scope_field] = invalid_value

    principal = await make_authenticator().authenticate(request)

    assert principal is None
    assert session.executed is False


def test_authenticator_rejects_invalid_security_window_configuration():
    from app.security.hmac_internal_service_auth import (
        HMACConfigurationError,
    )

    invalid_timestamp_windows = [29, 901, True, 30.0]

    for value in invalid_timestamp_windows:
        with pytest.raises(HMACConfigurationError):
            HMACInternalServiceAuthenticator(
                service_id=SERVICE_ID,
                keys={KEY_ID: SECRET},
                timestamp_window_seconds=value,
            )

    invalid_nonce_retention = [29, 601, True, 30.0]

    for value in invalid_nonce_retention:
        with pytest.raises(HMACConfigurationError):
            HMACInternalServiceAuthenticator(
                service_id=SERVICE_ID,
                keys={KEY_ID: SECRET},
                nonce_retention_seconds=value,
            )
