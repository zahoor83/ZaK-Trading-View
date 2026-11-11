"""Policy-enforced Binance public market-data WebSocket transport."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
import ssl
from dataclasses import replace
from typing import Any

from websockets.asyncio.client import connect

from app.security.websocket_egress import (
    ApprovedWebSocketRequest,
    validate_public_market_data_websocket_url,
)
from app.security.websocket_inbound import (
    InboundWebSocketViolation,
    validate_public_websocket_inbound_message,
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
MAX_DEFERRED_EVENTS = 16
MAX_DEFERRED_BYTES = 1_048_576

_SPOT_STREAM = re.compile(r"^[a-z0-9]{2,30}@trade$")
_FUTURES_STREAM = re.compile(r"^[a-z0-9]{2,30}@(depth|markPrice)$")


class PublicWebSocketTransportError(RuntimeError):
    """Raised when the public WebSocket transport fails."""


class PublicMarketDataWebSocket:
    """Stateful, fail-closed Binance public market-data WebSocket."""

    def __init__(
        self,
        connection: Any,
        approved_request: ApprovedWebSocketRequest,
    ) -> None:
        if not isinstance(approved_request, ApprovedWebSocketRequest):
            raise TypeError("An approved WebSocket request is required.")

        self._connection = connection
        self.approved_request = approved_request
        self._active_streams = set(approved_request.streams)
        self._pending: dict | None = None
        self._deferred_events: deque[tuple[str | bytes, int, str]] = deque()
        self._deferred_event_bytes = 0
        self._closed = False

        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    @property
    def active_streams(self) -> tuple[str, ...]:
        """Return a stable snapshot of active subscriptions."""
        return tuple(sorted(self._active_streams))

    @property
    def pending_request_count(self) -> int:
        """Return the number of pending control requests."""
        return 1 if self._pending is not None else 0

    def _validate_market_streams(self, streams: tuple[str, ...]) -> None:
        pattern = (
            _SPOT_STREAM
            if self.approved_request.market == "spot"
            else _FUTURES_STREAM
        )

        if any(pattern.fullmatch(stream) is None for stream in streams):
            raise ValueError("Stream is not supported for the approved market.")

    async def send(self, payload: str | bytes) -> None:
        """Send one correlated subscription-control request."""
        if self._closed:
            raise PublicWebSocketTransportError("WebSocket is closed.")

        approved = validate_public_websocket_message(payload)

        if approved.request_id is None:
            raise ValueError("A non-null request ID is required.")

        self._validate_market_streams(approved.params)

        if approved.method == "LIST_SUBSCRIPTIONS" and approved.params:
            raise ValueError("LIST_SUBSCRIPTIONS cannot contain parameters.")

        if approved.method != "LIST_SUBSCRIPTIONS" and not approved.params:
            raise ValueError("Subscription parameters are required.")

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

        send_error: Exception | None = None
        send_cancelled: asyncio.CancelledError | None = None

        async with self._send_lock:
            async with self._state_lock:
                if self._closed:
                    raise PublicWebSocketTransportError("WebSocket is closed.")

                if self._pending is not None:
                    raise PublicWebSocketTransportError(
                        "Another subscription request is still pending."
                    )

                if approved.method == "SUBSCRIBE":
                    if set(approved.params) & self._active_streams:
                        raise ValueError("A requested stream is already active.")

                elif approved.method == "UNSUBSCRIBE":
                    if not set(approved.params).issubset(self._active_streams):
                        raise ValueError("Cannot unsubscribe an inactive stream.")

                self._pending = {
                    "id": approved.request_id,
                    "method": approved.method,
                    "streams": approved.params,
                }

                try:
                    await self._connection.send(encoded)
                except asyncio.CancelledError as exc:
                    self._pending = None
                    send_cancelled = exc
                except Exception as exc:
                    self._pending = None
                    send_error = exc

        if send_cancelled is not None:
            try:
                await asyncio.shield(self.close())
            except Exception:
                pass
            raise send_cancelled

        if send_error is not None:
            await self._fail_closed(
                "Public WebSocket send failed.",
                send_error,
            )

    async def recv(self) -> str | bytes:
        # During a control request, defer valid market events until its response.
        if self._closed:
            raise PublicWebSocketTransportError("WebSocket is closed.")

        failure: tuple[str, Exception | None] | None = None
        async with self._receive_lock:
            if self._closed:
                raise PublicWebSocketTransportError("WebSocket is closed.")

            while True:
                async with self._state_lock:
                    if self._closed:
                        raise PublicWebSocketTransportError("WebSocket is closed.")
                    if self._pending is None and self._deferred_events:
                        payload, size, _stream = self._deferred_events.popleft()
                        self._deferred_event_bytes -= size
                        return payload

                try:
                    result = await self._connection.recv()
                except Exception as exc:
                    failure = ("Public WebSocket receive failed.", exc)
                    break

                continue_receiving = False
                async with self._state_lock:
                    if self._closed:
                        failure = ("WebSocket closed during receive.", None)
                    else:
                        pending = self._pending
                        validation_streams = set(self._active_streams)
                        if pending is not None and pending["method"] == "SUBSCRIBE":
                            validation_streams.update(pending["streams"])
                        current_request = replace(
                            self.approved_request,
                            streams=tuple(sorted(validation_streams)),
                        )
                        try:
                            validated = validate_public_websocket_inbound_message(
                                result, current_request
                            )
                        except (InboundWebSocketViolation, TypeError) as exc:
                            failure = ("Untrusted inbound WebSocket message.", exc)
                        else:
                            if validated.kind == "event":
                                pending = self._pending
                                if pending is None:
                                    return result
                                size = len(result) if isinstance(result, bytes) else len(result.encode("utf-8"))
                                if (
                                    len(self._deferred_events) >= MAX_DEFERRED_EVENTS
                                    or self._deferred_event_bytes + size > MAX_DEFERRED_BYTES
                                ):
                                    failure = ("Deferred WebSocket event queue limit exceeded.", None)
                                else:
                                    self._deferred_events.append((result, size, validated.stream))
                                    self._deferred_event_bytes += size
                                    continue_receiving = True

                            elif validated.kind in {"acknowledgement", "provider_error"}:
                                pending = self._pending
                                message_id = validated.payload.get("id")
                                if pending is None or message_id != pending["id"]:
                                    failure = ("Unsolicited or unknown WebSocket response ID.", None)
                                elif validated.kind == "provider_error":
                                    requested = set(pending["streams"])
                                    contradictory = (
                                        pending["method"] == "SUBSCRIBE"
                                        and any(stream in requested for _payload, _size, stream in self._deferred_events)
                                    )
                                    if contradictory:
                                        failure = ("Provider rejected a subscription after emitting its events.", None)
                                    else:
                                        self._pending = None
                                        return result
                                else:
                                    response_result = validated.payload.get("result")
                                    method_name = pending["method"]
                                    streams = set(pending["streams"])
                                    if method_name in {"SUBSCRIBE", "UNSUBSCRIBE"}:
                                        if response_result is not None:
                                            failure = ("Unexpected subscription acknowledgement result.", None)
                                        elif method_name == "SUBSCRIBE":
                                            if streams & self._active_streams:
                                                failure = ("Subscription state conflict.", None)
                                            else:
                                                self._active_streams.update(streams)
                                        else:
                                            if not streams.issubset(self._active_streams):
                                                failure = ("Unsubscription state conflict.", None)
                                            else:
                                                self._active_streams.difference_update(streams)
                                    elif method_name == "LIST_SUBSCRIPTIONS":
                                        if not isinstance(response_result, (list, tuple)):
                                            failure = ("LIST_SUBSCRIPTIONS returned an invalid result.", None)
                                        elif set(response_result) != self._active_streams:
                                            failure = ("Provider and local subscription states disagree.", None)
                                    if failure is None:
                                        self._pending = None
                                        return result
                            else:
                                failure = ("Unexpected inbound WebSocket message kind.", None)

                if failure is not None:
                    break
                if continue_receiving:
                    continue
                failure = ("WebSocket receive ended without a message.", None)
                break

        if failure is not None:
            await self._fail_closed(failure[0], failure[1])
        raise PublicWebSocketTransportError("WebSocket receive failed.")

    async def _fail_closed(
        self,
        message: str,
        cause: Exception | None,
    ) -> None:
        try:
            await self.close()
        except Exception:
            pass

        error = PublicWebSocketTransportError(message)
        if cause is not None:
            raise error from cause
        raise error

    async def close(self) -> None:
        """Close the underlying connection once and clear pending state."""
        async with self._close_lock:
            async with self._state_lock:
                if self._closed:
                    return

                self._closed = True
                self._pending = None
                self._deferred_events.clear()
                self._deferred_event_bytes = 0

            try:
                await self._connection.close()
            except Exception as exc:
                raise PublicWebSocketTransportError(
                    "Public WebSocket close failed."
                ) from exc

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

    pattern = (
        _SPOT_STREAM if approved.market == "spot" else _FUTURES_STREAM
    )
    if any(pattern.fullmatch(stream) is None for stream in approved.streams):
        raise ValueError("Initial stream is not supported for the approved market.")

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