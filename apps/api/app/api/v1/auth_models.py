"""Strict request and response models for internal authentication APIs."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InternalAuthModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )

    @field_validator("*")
    @classmethod
    def require_aware_datetimes(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Datetime must include timezone information.")
        return value


class OIDCTransactionCreateRequest(InternalAuthModel):
    request_id: str = Field(min_length=36, max_length=36)

    @field_validator("request_id")
    @classmethod
    def require_canonical_uuid_v4(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("request_id must be a canonical UUID v4.") from None

        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("request_id must be a canonical UUID v4.")

        return value


class BrowserBindingCookie(InternalAuthModel):
    name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)
    max_age_seconds: int = Field(ge=1, le=300)


class OIDCTransactionCreateResponse(InternalAuthModel):
    authorization_url: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=512)
    browser_binding_cookie: BrowserBindingCookie
    expires_at: datetime


class OIDCCallbackRequest(InternalAuthModel):
    state: str = Field(min_length=1, max_length=512)
    authorization_code: str = Field(min_length=1, max_length=8192)
    browser_binding: str = Field(min_length=1, max_length=512)


class InternalUserIdentity(InternalAuthModel):
    id: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=320)


class InternalSessionData(InternalAuthModel):
    token: str = Field(min_length=1, max_length=512)
    expires_at: datetime


class OIDCCallbackResponse(InternalAuthModel):
    session: InternalSessionData
    user: InternalUserIdentity


class SessionValidateRequest(InternalAuthModel):
    session_token: str = Field(min_length=1, max_length=512)


class InternalSessionMetadata(InternalAuthModel):
    expires_at: datetime


class SessionValidateResponse(InternalAuthModel):
    authenticated: bool
    user: InternalUserIdentity
    session: InternalSessionMetadata


class SessionRevokeRequest(InternalAuthModel):
    session_token: str = Field(min_length=1, max_length=512)


class SessionRevokeResponse(InternalAuthModel):
    revoked: bool