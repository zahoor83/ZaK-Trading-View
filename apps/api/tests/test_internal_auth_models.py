from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.api.v1.auth_models import (
    BrowserBindingCookie,
    InternalSessionData,
    InternalSessionMetadata,
    OIDCTransactionCreateRequest,
    OIDCTransactionCreateResponse,
)


def transaction_response(expires_at):
    return OIDCTransactionCreateResponse(
        authorization_url="https://example.invalid/authorize",
        state="state",
        browser_binding_cookie=BrowserBindingCookie(
            name="__Host-zak_oidc_test",
            value="binding",
            max_age_seconds=300,
        ),
        expires_at=expires_at,
    )


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda value: transaction_response(value),
        lambda value: InternalSessionData(token="token", expires_at=value),
        lambda value: InternalSessionMetadata(expires_at=value),
    ],
)
def test_all_internal_expiry_fields_reject_naive_datetime(model_factory):
    with pytest.raises(ValidationError):
        model_factory(datetime(2026, 11, 11, 12, 0, 0))


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda value: transaction_response(value),
        lambda value: InternalSessionData(token="token", expires_at=value),
        lambda value: InternalSessionMetadata(expires_at=value),
    ],
)
def test_all_internal_expiry_fields_accept_aware_datetime(model_factory):
    result = model_factory(datetime.now(timezone.utc))
    assert result is not None


def test_transaction_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        OIDCTransactionCreateRequest(
            request_id="request-1",
            unexpected="value",
        )


def test_browser_cookie_enforces_maximum_lifetime():
    with pytest.raises(ValidationError):
        BrowserBindingCookie(
            name="__Host-zak_oidc_test",
            value="binding",
            max_age_seconds=301,
        )