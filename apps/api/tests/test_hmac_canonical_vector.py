from __future__ import annotations

import hashlib
import hmac

from app.security.hmac_internal_service_auth import (
    HMACInternalServiceAuthenticator,
)


def test_matches_the_shared_typescript_hmac_canonical_vector_exactly():
    secret = bytes(range(32))
    body = b'{"amount":12,"symbol":"BTCUSDT"}'
    body_digest = hashlib.sha256(body).hexdigest()

    target = "/v1/items/%2Fpart?term=a%20b&tag=one&tag=two"

    assert body_digest == (
        "167a94d35a6386ab481fc16e4d259c5ce35ff9431efb742d8c78c14029cfd719"
    )

    canonical = HMACInternalServiceAuthenticator._canonical_message(
        service_id="web-bff",
        key_id="key-2026-01",
        timestamp="1730000000",
        nonce="A" * 43,
        method="POST",
        target=target,
        body_digest=body_digest,
    )

    signature = hmac.new(
        secret,
        canonical,
        hashlib.sha256,
    ).hexdigest()

    assert signature == (
        "d155e4203b4e8ed1f193a57e2702248e812a5f6c50b3f7536f32161f568af775"
    )