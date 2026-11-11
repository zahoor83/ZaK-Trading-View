"""Immutable health and lifecycle contracts for market-data orchestration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

from app.market_data.freshness import FreshnessState


_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,30}$")

_EXPECTED_EVENT_TYPES = {
    "spot": "trade",
    "futures_depth": "depthUpdate",
    "futures_mark_price": "markPriceUpdate",
}


class MarketDataGroup(str, Enum):
    SPOT = "spot"
    FUTURES_DEPTH = "futures_depth"
    FUTURES_MARK_PRICE = "futures_mark_price"


class PipelineState(str, Enum):
    DISABLED = "DISABLED"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    SUBSCRIBING = "SUBSCRIBING"
    READY = "READY"
    STALE = "STALE"
    UNSYNCED = "UNSYNCED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class MarketDataServiceState(str, Enum):
    DISABLED = "DISABLED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


def _validate_monotonic(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field} must be finite and non-negative.")

    return float(value)


def _expected_streams(
    group: MarketDataGroup,
    symbols: tuple[str, ...],
) -> tuple[str, ...]:
    suffix = {
        MarketDataGroup.SPOT: "@trade",
        MarketDataGroup.FUTURES_DEPTH: "@depth",
        MarketDataGroup.FUTURES_MARK_PRICE: "@markPrice",
    }[group]

    return tuple(sorted(f"{symbol.lower()}{suffix}" for symbol in symbols))


@dataclass(frozen=True, slots=True)
class MarketDataPipelineStatus:
    """Immutable health snapshot for one independently managed pipeline."""

    group: MarketDataGroup
    state: PipelineState
    symbols: tuple[str, ...]
    active_streams: tuple[str, ...] = ()
    clock_synchronized: bool = False
    metadata_fresh: bool = False
    book_synchronized: bool = False
    last_event_type: str | None = None
    last_event_timestamp_ms: int | None = None
    last_event_received_at_monotonic: float | None = None
    last_freshness_state: FreshnessState | None = None
    last_error: str | None = None
    updated_at_monotonic: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.group, MarketDataGroup):
            raise TypeError("group must be a MarketDataGroup.")

        if not isinstance(self.state, PipelineState):
            raise TypeError("state must be a PipelineState.")

        if not isinstance(self.symbols, tuple):
            raise TypeError("symbols must be a tuple.")

        if not isinstance(self.active_streams, tuple):
            raise TypeError("active_streams must be a tuple.")

        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("Duplicate configured symbols are prohibited.")

        for symbol in self.symbols:
            if (
                not isinstance(symbol, str)
                or not _SYMBOL_PATTERN.fullmatch(symbol)
            ):
                raise ValueError("Invalid configured symbol.")

        for stream in self.active_streams:
            if not isinstance(stream, str) or not stream:
                raise ValueError("Invalid active stream.")

        if len(self.active_streams) != len(set(self.active_streams)):
            raise ValueError("Duplicate active streams are prohibited.")

        if self.active_streams:
            expected = _expected_streams(self.group, self.symbols)
            if tuple(sorted(self.active_streams)) != expected:
                raise ValueError("Active streams do not match configured symbols.")

        if type(self.clock_synchronized) is not bool:
            raise TypeError("clock_synchronized must be boolean.")

        if type(self.metadata_fresh) is not bool:
            raise TypeError("metadata_fresh must be boolean.")

        if type(self.book_synchronized) is not bool:
            raise TypeError("book_synchronized must be boolean.")

        if (
            self.group != MarketDataGroup.FUTURES_DEPTH
            and self.book_synchronized
        ):
            raise ValueError("Only Futures Depth can have a synchronized book.")

        event_fields = (
            self.last_event_type,
            self.last_event_timestamp_ms,
            self.last_event_received_at_monotonic,
        )

        if any(value is not None for value in event_fields):
            if any(value is None for value in event_fields):
                raise ValueError("Event metadata must be supplied together.")

            expected_type = _EXPECTED_EVENT_TYPES[self.group]
            if self.last_event_type != expected_type:
                raise ValueError("Event type does not match the pipeline group.")

            if (
                isinstance(self.last_event_timestamp_ms, bool)
                or not isinstance(self.last_event_timestamp_ms, int)
                or self.last_event_timestamp_ms <= 0
            ):
                raise ValueError("Invalid event timestamp.")

            received = _validate_monotonic(
                self.last_event_received_at_monotonic,
                "last_event_received_at_monotonic",
            )
        else:
            received = None

        if self.group == MarketDataGroup.FUTURES_DEPTH:
            if self.last_freshness_state is not None:
                raise ValueError("Depth events do not use trade freshness validation.")
        elif self.last_freshness_state is not None and not isinstance(
            self.last_freshness_state, FreshnessState
        ):
            raise TypeError("Invalid freshness state.")

        if self.last_error is not None:
            if not isinstance(self.last_error, str) or not self.last_error.strip():
                raise ValueError("last_error must be a non-empty string.")

        updated = _validate_monotonic(
            self.updated_at_monotonic,
            "updated_at_monotonic",
        )

        if received is not None and received > updated:
            raise ValueError("Event receive time cannot exceed status update time.")

        if self.state == PipelineState.DISABLED:
            if self.symbols or self.active_streams:
                raise ValueError("Disabled pipelines cannot have configured streams.")

        elif not self.symbols:
            raise ValueError("Enabled pipeline states require configured symbols.")

        if self.state == PipelineState.READY:
            if not self.active_streams:
                raise ValueError("READY requires verified active subscriptions.")
            if not self.clock_synchronized:
                raise ValueError("READY requires a synchronized exchange clock.")

            if not self.metadata_fresh:
                raise ValueError("READY requires fresh symbol metadata.")

            if received is None:
                raise ValueError("READY requires a verified market-data event.")

            if self.group == MarketDataGroup.FUTURES_DEPTH:
                if not self.book_synchronized:
                    raise ValueError("Futures Depth READY requires a synchronized book.")
            else:
                if self.book_synchronized:
                    raise ValueError("Non-depth pipelines cannot claim book synchronization.")

                if self.last_freshness_state != FreshnessState.FRESH:
                    raise ValueError("READY requires a FRESH market-data event.")


@dataclass(frozen=True, slots=True)
class MarketDataServiceHealth:
    """Immutable aggregate health snapshot across independent pipelines."""

    state: MarketDataServiceState
    generated_at_monotonic: float
    pipelines: tuple[MarketDataPipelineStatus, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, MarketDataServiceState):
            raise TypeError("state must be a MarketDataServiceState.")

        _validate_monotonic(
            self.generated_at_monotonic,
            "generated_at_monotonic",
        )

        if not isinstance(self.pipelines, tuple):
            raise TypeError("pipelines must be a tuple.")

        if any(
            not isinstance(item, MarketDataPipelineStatus)
            for item in self.pipelines
        ):
            raise TypeError("pipelines must contain MarketDataPipelineStatus objects.")

        groups = tuple(item.group for item in self.pipelines)
        if len(groups) != len(set(groups)):
            raise ValueError("Duplicate pipeline groups are prohibited.")
