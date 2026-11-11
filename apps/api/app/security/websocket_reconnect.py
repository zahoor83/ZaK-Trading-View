"""Bounded, fail-closed recovery for approved Binance market-data WebSockets."""

from __future__ import annotations

import asyncio
import json
import math
import random
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol


class ReconnectError(RuntimeError):
    """Raised when market-data WebSocket recovery cannot be trusted."""


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"Non-standard JSON constant is prohibited: {value}")


class ReconnectState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    VERIFYING = "VERIFYING"
    SUBSCRIPTIONS_READY = "SUBSCRIPTIONS_READY"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 15.0
    jitter_fraction: float = 0.2

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be an integer between 1 and 100.")

        for name, value in (
            ("base_delay_seconds", self.base_delay_seconds),
            ("max_delay_seconds", self.max_delay_seconds),
            ("jitter_fraction", self.jitter_fraction),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite number.")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{name} must be a finite number.")

        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive.")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds.")
        if not 0 <= self.jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between zero and one.")


class RecoverableWebSocket(Protocol):
    @property
    def active_streams(self) -> tuple[str, ...]: ...

    async def send(self, payload: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connector = Callable[
    [tuple[str, ...]],
    Awaitable[RecoverableWebSocket],
]


class WebSocketReconnectManager:
    """Reconnect an approved stream set and verify provider subscriptions.

    The connector is responsible for constructing a new connection using
    only the supplied approved streams and the appropriate approved Binance
    endpoint. This manager never accepts a URL from an external caller.

    SUBSCRIPTIONS_READY means subscription state was reconciled. It does
    not mean a Futures order book is synchronized or market data is fresh.
    """

    def __init__(
        self,
        connector: Connector,
        active_streams: tuple[str, ...],
        policy: ReconnectPolicy | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if not callable(connector):
            raise TypeError("connector must be callable.")

        if not isinstance(active_streams, tuple) or not active_streams:
            raise ValueError("A non-empty approved stream tuple is required.")

        if any(not isinstance(item, str) or not item for item in active_streams):
            raise ValueError("Invalid active stream.")

        if len(set(active_streams)) != len(active_streams):
            raise ValueError("Duplicate active streams are prohibited.")

        if not callable(sleep) or not callable(random_source):
            raise TypeError("sleep and random_source must be callable.")

        self._connector = connector
        self._active_streams = tuple(sorted(active_streams))
        self._policy = policy if policy is not None else ReconnectPolicy()
        self._sleep = sleep
        self._random_source = random_source

        self._state = ReconnectState.DISCONNECTED
        self._client: RecoverableWebSocket | None = None
        self._recovery_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._recovery_task = None
        self._stopped = False
        self._request_id = 0
        self._last_error: str | None = None

    @property
    def state(self) -> ReconnectState:
        return self._state

    @property
    def active_streams(self) -> tuple[str, ...]:
        return self._active_streams

    @property
    def client(self) -> RecoverableWebSocket | None:
        if self._state != ReconnectState.SUBSCRIPTIONS_READY:
            return None
        return self._client

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _next_request_id(self) -> int:
        self._request_id += 1
        if self._request_id > 2_147_483_647:
            self._request_id = 1
        return self._request_id

    def _delay_for_attempt(self, attempt: int) -> float:
        base = min(
            self._policy.max_delay_seconds,
            self._policy.base_delay_seconds * (2 ** (attempt - 1)),
        )

        sample = self._random_source()
        if (
            isinstance(sample, bool)
            or not isinstance(sample, (int, float))
            or not math.isfinite(sample)
            or not 0 <= sample <= 1
        ):
            raise ReconnectError("Jitter source returned an invalid value.")

        spread = self._policy.jitter_fraction
        multiplier = (1 - spread) + (2 * spread * sample)
        return min(self._policy.max_delay_seconds, base * multiplier)

    async def _close_quietly(self, client: RecoverableWebSocket) -> None:
        try:
            await client.close()
        except Exception:
            pass

    async def _verify_subscriptions(
        self,
        client: RecoverableWebSocket,
    ) -> None:
        local_streams = tuple(sorted(client.active_streams))

        if local_streams != self._active_streams:
            raise ReconnectError(
                "New connection was not initialized with the approved stream set."
            )

        request_id = self._next_request_id()
        request = json.dumps(
            {
                "method": "LIST_SUBSCRIPTIONS",
                "params": [],
                "id": request_id,
            },
            separators=(",", ":"),
        )

        await client.send(request)
        response = await client.recv()

        if isinstance(response, bytes):
            response = response.decode("utf-8", errors="strict")

        if not isinstance(response, str):
            raise ReconnectError("Provider response has an invalid type.")

        try:
            message = json.loads(
                response,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ReconnectError("Provider response is malformed.") from exc

        if not isinstance(message, dict):
            raise ReconnectError("Provider response must be an object.")

        if set(message) != {"result", "id"}:
            raise ReconnectError("Unexpected subscription verification response.")

        if type(message["id"]) is not int or message["id"] != request_id:
            raise ReconnectError("Subscription verification ID mismatch.")

        result = message["result"]
        if not isinstance(result, list):
            raise ReconnectError("Subscription verification result must be a list.")

        if any(not isinstance(item, str) for item in result):
            raise ReconnectError("Provider returned an invalid stream name.")

        if len(result) != len(set(result)):
            raise ReconnectError("Provider returned duplicate subscriptions.")

        if tuple(sorted(result)) != self._active_streams:
            raise ReconnectError("Provider subscription state does not match.")

    async def mark_disconnected(
        self,
        disconnected_client: RecoverableWebSocket,
    ) -> None:
        """Invalidate the current client only for its own disconnect event."""
        async with self._recovery_lock:
            if self._stopped or disconnected_client is not self._client:
                return

            self._client = None
            self._state = ReconnectState.DISCONNECTED
            await self._close_quietly(disconnected_client)

    async def recover(self) -> RecoverableWebSocket:
        """Reconnect with bounded backoff and reconcile subscriptions."""
        async with self._recovery_lock:
            current_task = asyncio.current_task()
            if current_task is None:
                raise ReconnectError("Recovery requires an asyncio task.")
            self._recovery_task = current_task
            try:
                if self._stopped:
                    raise ReconnectError("Reconnect manager is stopped.")

                if (
                    self._state == ReconnectState.SUBSCRIPTIONS_READY
                    and self._client is not None
                ):
                    return self._client

                self._state = ReconnectState.RECONNECTING
                self._last_error = None

                for attempt in range(1, self._policy.max_attempts + 1):
                    if self._stopped:
                        self._state = ReconnectState.STOPPED
                        raise ReconnectError("Reconnect manager is stopped.")

                    candidate: RecoverableWebSocket | None = None

                    try:
                        if attempt > 1:
                            await self._sleep(self._delay_for_attempt(attempt - 1))

                        if self._stopped:
                            self._state = ReconnectState.STOPPED
                            raise ReconnectError("Reconnect manager is stopped.")

                        candidate = await self._connector(self._active_streams)

                        if self._stopped:
                            await self._close_quietly(candidate)
                            self._state = ReconnectState.STOPPED
                            raise ReconnectError("Reconnect manager is stopped.")

                        self._state = ReconnectState.VERIFYING
                        await self._verify_subscriptions(candidate)

                        if self._stopped:
                            await self._close_quietly(candidate)
                            self._state = ReconnectState.STOPPED
                            raise ReconnectError("Reconnect manager is stopped.")

                        self._client = candidate
                        self._state = ReconnectState.SUBSCRIPTIONS_READY
                        return candidate

                    except asyncio.CancelledError:
                        if candidate is not None:
                            await self._close_quietly(candidate)
                        if not self._stopped:
                            self._state = ReconnectState.DISCONNECTED
                        raise

                    except Exception as exc:
                        if candidate is not None:
                            await self._close_quietly(candidate)

                        self._client = None
                        self._last_error = type(exc).__name__

                        if self._stopped:
                            self._state = ReconnectState.STOPPED
                            raise ReconnectError("Reconnect manager is stopped.") from exc

                        self._state = ReconnectState.RECONNECTING

                self._state = ReconnectState.FAILED
                raise ReconnectError(
                    "Maximum WebSocket recovery attempts exhausted."
                )

            finally:
                if self._recovery_task is current_task:
                    self._recovery_task = None
    async def shutdown(self) -> None:
        """Stop recovery, await cancellation cleanup, and close the client."""
        async with self._shutdown_lock:
            self._stopped = True
            self._state = ReconnectState.STOPPED

            recovery_task = self._recovery_task
            current_task = asyncio.current_task()

            if recovery_task is not None and recovery_task is not current_task:
                if not recovery_task.done():
                    recovery_task.cancel()

                try:
                    await recovery_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Recovery failure is expected during shutdown.
                    pass

            previous = self._client
            self._client = None
            self._state = ReconnectState.STOPPED

            if previous is not None:
                await self._close_quietly(previous)
