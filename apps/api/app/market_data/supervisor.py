"""Fail-closed lifecycle supervision for approved public market-data streams."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.market_data.config import MarketDataConfig
from app.market_data.freshness import (
    FreshnessResult,
    FreshnessState,
    MarketDataFreshnessValidator,
)
from app.security.public_websocket import connect_public_market_data_websocket
from app.security.websocket_egress import (
    ApprovedWebSocketRequest,
    validate_public_market_data_websocket_url,
)
from app.security.websocket_inbound import (
    InboundWebSocketViolation,
    ValidatedInboundMessage,
    validate_public_websocket_inbound_message,
)
from app.security.websocket_reconnect import (
    ReconnectPolicy,
    ReconnectState,
    WebSocketReconnectManager,
)


class MarketDataSupervisorError(RuntimeError):
    """Raised when a supervisor cannot maintain a trusted data boundary."""


@dataclass(frozen=True, slots=True)
class SupervisedMarketDataEvent:
    """A validated event with explicit trust and synchronization metadata."""

    message: ValidatedInboundMessage
    received_at_monotonic: float
    freshness: FreshnessResult | None
    book_synchronized: bool


EventCallback = Callable[[SupervisedMarketDataEvent], Any]


class MarketDataWebSocketSupervisor:
    """Supervise exactly one immutable, explicitly configured stream group."""

    _GROUPS = {
        "spot": ("spot", "spot_websocket_url", "trade"),
        "futures_depth": (
            "usdm_futures",
            "futures_depth_websocket_url",
            "depthUpdate",
        ),
        "futures_mark_price": (
            "usdm_futures",
            "futures_mark_price_websocket_url",
            "markPriceUpdate",
        ),
    }

    def __init__(
        self,
        config: MarketDataConfig,
        group: str,
        *,
        freshness_validator: MarketDataFreshnessValidator | None = None,
        on_event: EventCallback | None = None,
        on_depth_event: EventCallback | None = None,
        connector: Callable[[str], Any] | None = None,
        reconnect_policy: ReconnectPolicy | None = None,
        sleep=asyncio.sleep,
        random_source=None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, MarketDataConfig):
            raise TypeError("config must be a MarketDataConfig.")

        if group not in self._GROUPS:
            raise ValueError("Unsupported market-data stream group.")

        market, url_property, expected_event_type = self._GROUPS[group]
        streams_by_group = dict(config.configured_groups)

        if group not in streams_by_group:
            raise ValueError("The selected stream group is disabled.")

        self._group = group
        self._market = market
        self._expected_event_type = expected_event_type
        self._streams = tuple(
            f"{symbol.lower()}@trade"
            if group == "spot"
            else (
                f"{symbol.lower()}@depth"
                if group == "futures_depth"
                else f"{symbol.lower()}@markPrice"
            )
            for symbol in streams_by_group[group]
        )

        self._url = getattr(config, url_property)
        if not isinstance(self._url, str) or not self._url:
            raise MarketDataSupervisorError(
                "Configured WebSocket URL is unavailable."
            )

        self._approved_request = validate_public_market_data_websocket_url(
            self._url
        )

        if (
            self._approved_request.market != self._market
            or tuple(sorted(self._approved_request.streams))
            != tuple(sorted(self._streams))
        ):
            raise MarketDataSupervisorError(
                "Generated endpoint does not match the configured stream group."
            )

        if group != "futures_depth" and not isinstance(
            freshness_validator, MarketDataFreshnessValidator
        ):
            raise TypeError(
                "A freshness validator is required for trade and mark-price groups."
            )

        if on_event is not None and not callable(on_event):
            raise TypeError("on_event must be callable.")

        if on_depth_event is not None and not callable(on_depth_event):
            raise TypeError("on_depth_event must be callable.")

        if not callable(monotonic):
            raise TypeError("monotonic must be callable.")

        self._freshness_validator = freshness_validator
        self._on_event = on_event
        self._on_depth_event = on_depth_event
        self._monotonic = monotonic
        self._connector = connector or connect_public_market_data_websocket

        if not callable(self._connector):
            raise TypeError("connector must be callable.")

        async def approved_connector(streams: tuple[str, ...]):
            if tuple(sorted(streams)) != tuple(sorted(self._streams)):
                raise MarketDataSupervisorError(
                    "Reconnect requested a different stream set."
                )
            return await self._connector(self._url)

        manager_kwargs = {
            "sleep": sleep,
        }
        if random_source is not None:
            manager_kwargs["random_source"] = random_source

        self._manager = WebSocketReconnectManager(
            approved_connector,
            self._streams,
            reconnect_policy,
            **manager_kwargs,
        )
        self._stop_requested = False

    @property
    def group(self) -> str:
        return self._group

    @property
    def active_streams(self) -> tuple[str, ...]:
        return tuple(sorted(self._streams))

    @property
    def state(self) -> ReconnectState:
        return self._manager.state

    @property
    def stopped(self) -> bool:
        return self._stop_requested

    async def _invoke(
        self,
        callback: EventCallback | None,
        event: SupervisedMarketDataEvent,
    ) -> None:
        if callback is None:
            return

        result = callback(event)
        if inspect.isawaitable(result):
            await result

    async def _dispatch(
        self,
        raw_message: str | bytes,
        client,
        received_at_monotonic: float,
    ) -> None:
        """Revalidate and route one transport message; never publish raw data."""

        if not isinstance(
            getattr(client, "approved_request", None),
            ApprovedWebSocketRequest,
        ):
            raise MarketDataSupervisorError(
                "Connection does not expose an approved request."
            )

        request = client.approved_request
        if (
            request.market != self._market
            or tuple(sorted(request.streams)) != tuple(sorted(self._streams))
            or request != self._approved_request
        ):
            raise MarketDataSupervisorError(
                "Connection approval does not match this supervisor."
            )

        if (
            isinstance(received_at_monotonic, bool)
            or not isinstance(received_at_monotonic, (int, float))
            or not math.isfinite(received_at_monotonic)
            or received_at_monotonic < 0
        ):
            raise MarketDataSupervisorError("Invalid receive monotonic timestamp.")

        message = validate_public_websocket_inbound_message(
            raw_message,
            request,
        )

        if not isinstance(message, ValidatedInboundMessage):
            raise MarketDataSupervisorError(
                "Inbound validator returned an unexpected object."
            )

        if message.kind != "event":
            return

        if (
            message.market != self._market
            or message.stream not in self._streams
            or message.event_type != self._expected_event_type
        ):
            raise MarketDataSupervisorError(
                "Validated event does not match this supervisor's stream group."
            )

        if self._group == "futures_depth":
            # Depth events are structurally validated only. No snapshot
            # reconciliation is implemented here, so the book stays unsynced.
            event = SupervisedMarketDataEvent(
                message=message,
                received_at_monotonic=float(received_at_monotonic),
                freshness=None,
                book_synchronized=False,
            )
            await self._invoke(self._on_depth_event, event)
            return

        timestamp = message.payload.get("E")
        freshness = self._freshness_validator.validate_event(
            message.event_type,
            timestamp,
            received_at_monotonic=float(received_at_monotonic),
        )

        if freshness.state is not FreshnessState.FRESH:
            return

        event = SupervisedMarketDataEvent(
            message=message,
            received_at_monotonic=float(received_at_monotonic),
            freshness=freshness,
            book_synchronized=False,
        )
        await self._invoke(self._on_event, event)

    async def run(self) -> None:
        """Receive, validate, route and reconnect until stopped."""

        if self._stop_requested:
            return

        try:
            client = await self._manager.recover()

            while not self._stop_requested:
                try:
                    raw_message = await client.recv()
                except asyncio.CancelledError:
                    await asyncio.shield(self._manager.shutdown())
                    self._stop_requested = True
                    raise
                except Exception:
                    if self._stop_requested:
                        break

                    # Identity check must happen before recovery. Otherwise
                    # recover() could return the same dead ready client.
                    await self._manager.mark_disconnected(client)

                    if self._stop_requested:
                        break

                    client = await self._manager.recover()
                    continue

                received_at = self._monotonic()

                try:
                    await self._dispatch(raw_message, client, received_at)
                except (InboundWebSocketViolation, TypeError, ValueError) as exc:
                    await self._manager.mark_disconnected(client)
                    await self._manager.shutdown()
                    self._stop_requested = True
                    raise MarketDataSupervisorError(
                        "Untrusted inbound data rejected; supervisor stopped."
                    ) from exc
                except MarketDataSupervisorError:
                    await self._manager.mark_disconnected(client)
                    await self._manager.shutdown()
                    self._stop_requested = True
                    raise

        except asyncio.CancelledError:
            self._stop_requested = True
            await asyncio.shield(self._manager.shutdown())
            raise

        except Exception:
            # Preserve FAILED when recovery itself is exhausted.
            # Close an active client if processing or callbacks fail.
            if self._manager.client is not None:
                await asyncio.shield(self._manager.shutdown())
                self._stop_requested = True
            raise

    async def stop(self) -> None:
        """Idempotently stop recovery and close the active connection."""
        self._stop_requested = True
        await self._manager.shutdown()
