from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

import app.security.oidc_http as oidc_http

from app.security.oidc_http import (
    OIDCRequestTimeout,
    OIDCResponseTooLarge,
    OIDCUnsupportedContentEncoding,
    read_bounded_response,
)


TIMEOUT = httpx.Timeout(5.0)


@pytest.mark.asyncio
async def test_reads_response_within_configured_limit():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        body = await read_bounded_response(
            client,
            "GET",
            "https://provider.example/test",
            max_response_bytes=1024,
            timeout=TIMEOUT,
        )

    assert b'"status":"ok"' in body


@pytest.mark.asyncio
async def test_rejects_oversized_body_even_when_content_length_underreports():
    oversized = b"x" * 1025

    def handler(request):
        return httpx.Response(
            200,
            content=oversized,
            headers={"content-length": "1"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OIDCResponseTooLarge):
            await read_bounded_response(
                client,
                "GET",
                "https://provider.example/test",
                max_response_bytes=1024,
                timeout=TIMEOUT,
            )


@pytest.mark.asyncio
async def test_rejects_oversized_decompressed_response():
    original = b"x" * 4097
    compressed = gzip.compress(original)

    def handler(request):
        return httpx.Response(
            200,
            content=compressed,
            headers={
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OIDCUnsupportedContentEncoding):
            await read_bounded_response(
                client,
                "GET",
                "https://provider.example/test",
                max_response_bytes=1024,
                timeout=TIMEOUT,
            )


@pytest.mark.asyncio
async def test_rejects_client_with_environment_proxy_inheritance():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"ok")
        )
    ) as client:
        assert client._trust_env is True

        with pytest.raises(ValueError, match="trust_env=False"):
            await read_bounded_response(
                client,
                "GET",
                "https://provider.example/test",
                max_response_bytes=1024,
                timeout=TIMEOUT,
            )


@pytest.mark.asyncio
async def test_rejects_redirect_without_following_it():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await read_bounded_response(
                client,
                "GET",
                "https://provider.example/test",
                max_response_bytes=1024,
                timeout=TIMEOUT,
            )

    assert len(requests) == 1
    assert str(requests[0].url) == "https://provider.example/test"


@pytest.mark.asyncio
async def test_rejects_invalid_response_size_limits():
    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"ok")
        ),
    ) as client:
        for invalid_limit in (0, -1, True, 1.5, "1024"):
            with pytest.raises(ValueError, match="size limit"):
                await read_bounded_response(
                    client,
                    "GET",
                    "https://provider.example/test",
                    max_response_bytes=invalid_limit,
                    timeout=TIMEOUT,
                )

@pytest.mark.asyncio
async def test_requests_identity_content_encoding():
    captured = {}

    def handler(request):
        captured["encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(200, content=b"ok")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await read_bounded_response(
            client,
            "GET",
            "https://provider.example/test",
            max_response_bytes=1024,
            timeout=TIMEOUT,
            headers={"Accept": "application/json"},
        )

    assert result == b"ok"
    assert captured["encoding"] == "identity"


@pytest.mark.asyncio
async def test_total_deadline_rejects_slow_response():
    async def handler(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=b"too late")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OIDCRequestTimeout):
            await read_bounded_response(
                client,
                "GET",
                "https://provider.example/test",
                max_response_bytes=1024,
                timeout=TIMEOUT,
                total_timeout_seconds=0.01,
            )


@pytest.mark.asyncio
async def test_rejects_invalid_total_deadline():
    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"ok")
        ),
    ) as client:
        for invalid_deadline in (0, -1, True, float("inf"), float("nan"), "5"):
            with pytest.raises(ValueError, match="total request deadline"):
                await read_bounded_response(
                    client,
                    "GET",
                    "https://provider.example/test",
                    max_response_bytes=1024,
                    timeout=TIMEOUT,
                    total_timeout_seconds=invalid_deadline,
                )

def test_production_client_factory_enforces_transport_policy(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_async_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(oidc_http.httpx, "AsyncClient", fake_async_client)

    result = oidc_http.create_oidc_http_client(timeout=TIMEOUT)

    assert result is sentinel
    assert captured == {
        "timeout": TIMEOUT,
        "verify": True,
        "trust_env": False,
        "follow_redirects": False,
        "proxy": None,
        "mounts": None,
    }
    assert "transport" not in captured

