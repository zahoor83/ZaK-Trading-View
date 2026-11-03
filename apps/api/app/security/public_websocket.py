
"""Policy-enforced Binance public market-data WebSocket transport."""

from __future__ import annotations

import json
import ssl
from typing import Any

from websockets.asyncio.client import connect

from app.security.websocket_egress import (
    ApprovedWebSocketRequest,
    validate_public_market_data_websocket_url,
)
from app.security.websocket_messages import (
    validate_public_websocket_message,
)


OPEN_TIMEOUT = 10.0
CLOSE_TIMEOUT = 5.0
PING_INTERVAL = 20.0
PING_TIMEOUT = 10.0
MAX_MESSAGE_SIZE = 1_048_576
MAX_QUEUE_SIZE = 16


class PublicWebSocketTransportError(RuntimeError):
    """Raised when the public WebSocket transport fails."""


class PublicMarketDataWebSocket:
    """Restricted wrapper that exposes only validated subscription messages."""

    def __init__(
        self,
        connection: Any,
        approved_request: ApprovedWebSocketRequest,
    ) -> None:
        self._connection = connection
        self.approved_request = approved_request
        self._closed = False

    async def send(self, payload: str | bytes) -> None:
        """Send only a validated public subscription-control message."""
        if self._closed:
            raise PublicWebSocketTransportError("WebSocket is closed.")

        approved = validate_public_websocket_message(payload)

        canonical_message = {
            "method": approved.method,
            "params": list(approved.params),
            "id": approved.request_id,
        }

        encoded = json.dumps(
            canonical_message,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

        await self._connection.send(encoded)

    async def recv(self) -> str | bytes:
        """Receive one server message."""
        if self._closed:
            raise PublicWebSocketTransportError("WebSocket is closed.")

        return await self._connection.recv()

    async def close(self) -> None:
        """Close the underlying connection once."""
        if not self._closed:
            self._closed = True
            await self._connection.close()

    async def __aenter__(self) -> PublicMarketDataWebSocket:
        if self._closed:
            raise PublicWebSocketTransportError("WebSocket is closed.")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()


async def connect_public_market_data_websocket(
    url: str,
) -> PublicMarketDataWebSocket:
    """Connect only to an approved Binance public market-data endpoint."""

    approved = validate_public_market_data_websocket_url(url)

    tls_context = ssl.create_default_context()

    try:
        connection = await connect(
            url,
            proxy=None,
            ssl=tls_context,
            compression=None,
            open_timeout=OPEN_TIMEOUT,
            close_timeout=CLOSE_TIMEOUT,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            max_size=MAX_MESSAGE_SIZE,
            max_queue=MAX_QUEUE_SIZE,
        )
    except Exception as exc:
        raise PublicWebSocketTransportError(
            "Public market-data WebSocket connection failed."
        ) from exc

    return PublicMarketDataWebSocket(connection, approved)
