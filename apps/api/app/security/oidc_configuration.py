"""Strict Auth0 OIDC configuration and provider metadata retrieval."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from app.security.oidc_http import (
    create_oidc_http_client,
    read_bounded_response,
)


_DISCOVERY_MAX_RESPONSE_BYTES = 64 * 1024
_JWKS_MAX_RESPONSE_BYTES = 256 * 1024


class OIDCConfigurationError(ValueError):
    """Raised when OIDC configuration or provider metadata is unsafe."""


@dataclass(frozen=True, slots=True)
class Auth0Settings:
    domain: str
    client_id: str
    client_secret: str
    callback_url: str
    app_public_url: str

    @property
    def issuer(self) -> str:
        return f"https://{self.domain}/"

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}.well-known/openid-configuration"

    @property
    def allowed_signing_algorithms(self) -> tuple[str, ...]:
        return ("RS256",)

    @property
    def token_endpoint_auth_method(self) -> str:
        return "client_secret_post"

    @property
    def scopes(self) -> tuple[str, ...]:
        return ("openid", "email", "profile")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "Auth0Settings":
        source = os.environ if environ is None else environ

        required = (
            "AUTH0_DOMAIN",
            "AUTH0_CLIENT_ID",
            "AUTH0_CLIENT_SECRET",
            "AUTH0_CALLBACK_URL",
            "APP_PUBLIC_URL",
        )

        values: dict[str, str] = {}

        for name in required:
            value = source.get(name, "").strip()
            if not value:
                raise OIDCConfigurationError(
                    f"Required authentication configuration is missing: {name}"
                )
            values[name] = value

        return cls(
            domain=values["AUTH0_DOMAIN"],
            client_id=values["AUTH0_CLIENT_ID"],
            client_secret=values["AUTH0_CLIENT_SECRET"],
            callback_url=values["AUTH0_CALLBACK_URL"],
            app_public_url=values["APP_PUBLIC_URL"],
        ).validated()

    def validated(self) -> "Auth0Settings":
        domain = self.domain.strip()

        if (
            domain != domain.lower()
            or not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
                domain,
            )
        ):
            raise OIDCConfigurationError(
                "AUTH0_DOMAIN must be a lowercase DNS hostname without a scheme or path."
            )

        for name, value in (
            ("AUTH0_CLIENT_ID", self.client_id),
            ("AUTH0_CLIENT_SECRET", self.client_secret),
        ):
            if not isinstance(value, str) or not value.strip():
                raise OIDCConfigurationError(f"{name} must not be empty.")

        public = _parse_absolute_url(
            self.app_public_url,
            "APP_PUBLIC_URL",
            allow_local_http=True,
        )

        if public.path not in ("", "/") or public.query:
            raise OIDCConfigurationError(
                "APP_PUBLIC_URL must contain only the application origin."
            )

        callback = _parse_absolute_url(
            self.callback_url,
            "AUTH0_CALLBACK_URL",
            allow_local_http=True,
        )

        if (
            callback.scheme != public.scheme
            or callback.hostname != public.hostname
            or callback.port != public.port
        ):
            raise OIDCConfigurationError(
                "AUTH0_CALLBACK_URL must use the same origin as APP_PUBLIC_URL."
            )

        if callback.path in ("", "/"):
            raise OIDCConfigurationError(
                "AUTH0_CALLBACK_URL must include a dedicated callback path."
            )

        return Auth0Settings(
            domain=domain,
            client_id=self.client_id.strip(),
            client_secret=self.client_secret,
            callback_url=self.callback_url,
            app_public_url=self.app_public_url,
        )


def _parse_absolute_url(
    value: str,
    field_name: str,
    *,
    allow_local_http: bool,
):
    if not isinstance(value, str) or not value:
        raise OIDCConfigurationError(f"{field_name} must be an absolute URL.")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise OIDCConfigurationError(
            f"{field_name} is malformed."
        ) from None

    hostname = parsed.hostname

    if not hostname:
        raise OIDCConfigurationError(f"{field_name} must include a hostname.")

    local_http = (
        allow_local_http
        and parsed.scheme == "http"
        and hostname in {"localhost", "127.0.0.1", "::1"}
    )

    if parsed.scheme != "https" and not local_http:
        raise OIDCConfigurationError(
            f"{field_name} must use HTTPS outside local development."
        )

    if parsed.username is not None or parsed.password is not None:
        raise OIDCConfigurationError(
            f"{field_name} must not contain embedded credentials."
        )

    if parsed.fragment:
        raise OIDCConfigurationError(
            f"{field_name} must not contain a URL fragment."
        )

    if parsed.query:
        raise OIDCConfigurationError(
            f"{field_name} must not contain a query string."
        )

    # Access port above deliberately validates malformed port syntax.
    _ = port
    return parsed


def validate_provider_metadata(
    settings: Auth0Settings,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    settings = settings.validated()

    if not isinstance(metadata, Mapping):
        raise OIDCConfigurationError("OIDC discovery response must be an object.")

    if metadata.get("issuer") != settings.issuer:
        raise OIDCConfigurationError("OIDC issuer does not match configured issuer.")

    endpoint_names = (
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
    )

    for name in endpoint_names:
        value = metadata.get(name)

        if not isinstance(value, str):
            raise OIDCConfigurationError(
                f"OIDC discovery metadata is missing {name}."
            )

        parsed = _parse_absolute_url(
            value,
            name,
            allow_local_http=False,
        )

        if parsed.hostname != settings.domain:
            raise OIDCConfigurationError(
                f"{name} must use the configured Auth0 domain."
            )

        if parsed.port not in (None, 443):
            raise OIDCConfigurationError(
                f"{name} must use HTTPS port 443."
            )

    required_capabilities = {
        "response_types_supported": "code",
        "code_challenge_methods_supported": "S256",
        "token_endpoint_auth_methods_supported": "client_secret_post",
        "id_token_signing_alg_values_supported": "RS256",
    }

    for field, required_value in required_capabilities.items():
        advertised = metadata.get(field)

        if (
            not isinstance(advertised, list)
            or required_value not in advertised
        ):
            raise OIDCConfigurationError(
                f"OIDC provider does not advertise required capability: {field}={required_value}"
            )

    return dict(metadata)


async def _get_json(
    url: str,
    *,
    max_response_bytes: int,
) -> dict[str, Any]:
    async def request_json(active_client: httpx.AsyncClient):
        response_body = await read_bounded_response(
            active_client,
            "GET",
            url,
            max_response_bytes=max_response_bytes,
            timeout=httpx.Timeout(5.0),
            total_timeout_seconds=8.0,
        )

        try:
            data = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise OIDCConfigurationError(
                "OIDC provider returned invalid JSON."
            ) from None

        if not isinstance(data, dict):
            raise OIDCConfigurationError(
                "OIDC provider JSON response must be an object."
            )

        return data

    try:
        async with create_oidc_http_client(
            timeout=httpx.Timeout(5.0),
        ) as active_client:
            return await request_json(active_client)

    except OIDCConfigurationError:
        raise
    except (httpx.HTTPError, ValueError):
        raise OIDCConfigurationError(
            "OIDC provider metadata request failed."
        ) from None


async def fetch_provider_metadata(
    settings: Auth0Settings,
) -> dict[str, Any]:
    settings = settings.validated()
    metadata = await _get_json(
        settings.discovery_url,
        max_response_bytes=_DISCOVERY_MAX_RESPONSE_BYTES,
    )

    return validate_provider_metadata(settings, metadata)


def validate_jwks(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise OIDCConfigurationError("JWKS response must be an object.")

    keys = data.get("keys")

    if not isinstance(keys, list):
        raise OIDCConfigurationError("JWKS response has no valid keys array.")

    accepted: list[dict[str, Any]] = []
    seen_kids: set[str] = set()

    for key in keys:
        if not isinstance(key, dict):
            raise OIDCConfigurationError("JWKS contains a malformed key.")

        # Encryption keys and unsupported signing algorithms are not accepted.
        if key.get("use") not in (None, "sig"):
            continue

        if key.get("kty") != "RSA":
            continue

        if key.get("alg") not in (None, "RS256"):
            continue

        kid = key.get("kid")

        if not isinstance(kid, str) or not kid:
            raise OIDCConfigurationError(
                "An RSA signing key is missing its key identifier."
            )

        if kid in seen_kids:
            raise OIDCConfigurationError(
                "JWKS contains duplicate key identifiers."
            )

        seen_kids.add(kid)
        accepted.append(key)

    if not accepted:
        raise OIDCConfigurationError(
            "JWKS contains no acceptable RSA RS256 signing keys."
        )

    return {"keys": accepted}


async def fetch_provider_jwks(
    settings: Auth0Settings,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    validated_metadata = validate_provider_metadata(settings, metadata)
    data = await _get_json(
        validated_metadata["jwks_uri"],
        max_response_bytes=_JWKS_MAX_RESPONSE_BYTES,
    )

    return validate_jwks(data)