"""Bounded, environment-proxy-isolated OIDC HTTP transport."""

from __future__ import annotations

import asyncio
import math

import httpx


class OIDCResponseTooLarge(ValueError):
    """Provider response exceeded the configured byte limit."""


def create_oidc_http_client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Create the only supported production OIDC HTTP client.

    TLS verification is explicit. Environment proxies, explicit proxies,
    custom mounts, redirects and caller-provided transports are excluded.
    """
    return httpx.AsyncClient(
        timeout=timeout,
        verify=True,
        trust_env=False,
        follow_redirects=False,
        proxy=None,
        mounts=None,
    )


class OIDCUnsupportedContentEncoding(ValueError):
    """Provider returned a response requiring content decompression."""


class OIDCRequestTimeout(ValueError):
    """The complete OIDC HTTP operation exceeded its deadline."""


async def read_bounded_response(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_response_bytes: int,
    timeout: httpx.Timeout,
    total_timeout_seconds: float = 10.0,
    data: dict | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Read a raw, bounded HTTP response without following redirects.

    Compression is disabled at the request boundary and non-identity
    response encodings are rejected before reading the response body.
    """
    if (
        not isinstance(max_response_bytes, int)
        or isinstance(max_response_bytes, bool)
        or max_response_bytes <= 0
    ):
        raise ValueError("Invalid OIDC response size limit.")

    if (
        isinstance(total_timeout_seconds, bool)
        or not isinstance(total_timeout_seconds, (int, float))
        or not math.isfinite(total_timeout_seconds)
        or total_timeout_seconds <= 0
    ):
        raise ValueError("Invalid OIDC total request deadline.")

    # HTTPX 0.28.1 exposes this as an internal client attribute.
    # The trusted-client boundary is hardened separately in Micro-Batch 1D-B.
    if getattr(client, "_trust_env", None) is not False:
        raise ValueError("OIDC client must use trust_env=False.")

    request_headers = dict(headers or {})

    # Prevent caller-provided header casing from overriding this policy.
    for key in list(request_headers):
        if key.casefold() == "accept-encoding":
            del request_headers[key]

    request_headers["Accept-Encoding"] = "identity"

    body = bytearray()

    try:
        async with asyncio.timeout(total_timeout_seconds):
            async with client.stream(
                method,
                url,
                data=data,
                headers=request_headers,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()

                content_encoding = response.headers.get(
                    "content-encoding", "identity"
                ).strip().lower()

                if content_encoding not in ("", "identity"):
                    raise OIDCUnsupportedContentEncoding(
                        "OIDC provider response content encoding is unsupported."
                    )

                declared_length = response.headers.get("content-length")

                if declared_length is not None:
                    if (
                        not declared_length.isascii()
                        or not declared_length.isdigit()
                    ):
                        raise ValueError(
                            "Invalid OIDC response Content-Length."
                        )

                    if int(declared_length) > max_response_bytes:
                        raise OIDCResponseTooLarge(
                            "OIDC provider response exceeded size limit."
                        )

                # Non-identity encodings are rejected above, so this
                # iterator cannot invoke a compression decoder.
                # aiter_bytes() also supports preloaded MockTransport responses.
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_response_bytes:
                        raise OIDCResponseTooLarge(
                            "OIDC provider response exceeded size limit."
                        )
                    body.extend(chunk)

    except TimeoutError:
        raise OIDCRequestTimeout(
            "OIDC HTTP operation exceeded its total deadline."
        ) from None

    return bytes(body)
