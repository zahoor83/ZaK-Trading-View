"""Independent, fail-closed REST bootstrap for public market data."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping

from app.market_data.binance_spot_rest import BinanceSpotRestClient
from app.market_data.binance_usdm_futures_rest import BinanceUsdMFuturesRestClient
from app.market_data.config import MarketDataConfig
from app.market_data.freshness import (
    ExchangeClock,
    FreshnessState,
    MarketDataFreshnessValidator,
)
from app.market_data.supervisor import MarketDataWebSocketSupervisor
from app.market_data.service_models import (
    MarketDataGroup,
    MarketDataPipelineStatus,
    MarketDataServiceHealth,
    MarketDataServiceState,
    PipelineState,
)
from app.market_data.symbol_metadata import (
    SymbolMetadataSnapshot,
    parse_spot_exchange_info,
    parse_usdm_futures_exchange_info,
)


@dataclass(frozen=True, slots=True)
class MarketResources:
    """Verified REST resources shared by pipelines belonging to one market."""

    market: str
    clock: ExchangeClock
    metadata: SymbolMetadataSnapshot
    freshness_validator: MarketDataFreshnessValidator
    server_time_provider: Callable[[], dict]
    metadata_refresh_provider: Callable[[], Awaitable[SymbolMetadataSnapshot]]


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """Immutable outcome of independent Spot and Futures bootstraps."""

    resources: Mapping[str, MarketResources]
    errors: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resources", MappingProxyType(dict(self.resources))
        )
        object.__setattr__(
            self, "errors", MappingProxyType(dict(self.errors))
        )


def _freeze_payload(value):
    """Recursively freeze JSON-compatible market-data payloads."""

    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Payload mapping keys must be strings.")
            frozen[key] = _freeze_payload(item)
        return MappingProxyType(frozen)

    if isinstance(value, (list, tuple)):
        return tuple(_freeze_payload(item) for item in value)

    if value is None or type(value) in (str, bool, int):
        return value

    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite payload numbers are prohibited.")
        return value

    raise TypeError("Unsupported value in market-data payload.")


@dataclass(frozen=True, slots=True)
class CachedMarketDataEvent:
    """Immutable latest validated event for one pipeline and symbol."""

    group: str
    symbol: str
    event_type: str
    event_timestamp_ms: int
    received_at_monotonic: float
    freshness: object | None
    book_synchronized: bool
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


class MarketDataService:
    """Orchestrate independent public Spot and USD-M Futures market data."""

    def __init__(
        self,
        config: MarketDataConfig,
        *,
        spot_client_factory: Callable[[], BinanceSpotRestClient] | None = None,
        futures_client_factory: Callable[[], BinanceUsdMFuturesRestClient] | None = None,
        clock_factory: Callable[[], ExchangeClock] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        supervisor_factory: Callable[..., MarketDataWebSocketSupervisor] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_sync_interval_seconds: float = 30.0,
        clock_retry_interval_seconds: float = 5.0,
        metadata_refresh_interval_seconds: float = 180.0,
        metadata_retry_interval_seconds: float = 15.0,
    ) -> None:
        if not isinstance(config, MarketDataConfig):
            raise TypeError("config must be a MarketDataConfig.")

        if not callable(monotonic):
            raise TypeError("monotonic must be callable.")

        if not callable(sleep):
            raise TypeError("sleep must be callable.")

        if (
            not isinstance(clock_sync_interval_seconds, (int, float))
            or isinstance(clock_sync_interval_seconds, bool)
            or not math.isfinite(clock_sync_interval_seconds)
            or clock_sync_interval_seconds <= 0
        ):
            raise ValueError("clock_sync_interval_seconds must be finite and positive.")

        if (
            not isinstance(clock_retry_interval_seconds, (int, float))
            or isinstance(clock_retry_interval_seconds, bool)
            or not math.isfinite(clock_retry_interval_seconds)
            or clock_retry_interval_seconds <= 0
        ):
            raise ValueError("clock_retry_interval_seconds must be finite and positive.")

        for name, value in (
            ("metadata_refresh_interval_seconds", metadata_refresh_interval_seconds),
            ("metadata_retry_interval_seconds", metadata_retry_interval_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive.")

        self._config = config
        self._spot_client_factory = (
            spot_client_factory or BinanceSpotRestClient
        )
        self._futures_client_factory = (
            futures_client_factory or BinanceUsdMFuturesRestClient
        )
        self._clock_factory = clock_factory or ExchangeClock
        self._monotonic = monotonic
        self._sleep = sleep
        self._clock_sync_interval_seconds = float(clock_sync_interval_seconds)
        self._clock_retry_interval_seconds = float(clock_retry_interval_seconds)
        self._metadata_refresh_interval_seconds = float(metadata_refresh_interval_seconds)
        self._metadata_retry_interval_seconds = float(metadata_retry_interval_seconds)
        self._supervisor_factory = (
            supervisor_factory
            if supervisor_factory is not None
            else MarketDataWebSocketSupervisor
        )

        for factory in (
            self._spot_client_factory,
            self._futures_client_factory,
            self._clock_factory,
            self._supervisor_factory,
        ):
            if not callable(factory):
                raise TypeError("Client and clock factories must be callable.")

        self._resources: dict[str, MarketResources] = {}
        self._errors: dict[str, str] = {}
        self._report: BootstrapReport | None = None
        self._bootstrap_lock = asyncio.Lock()

        self._lifecycle_lock = asyncio.Lock()
        self._service_state = MarketDataServiceState.DISABLED
        self._pipeline_states: dict[str, PipelineState] = {}
        self._pipeline_errors: dict[str, str] = {}
        self._supervisors: dict[str, MarketDataWebSocketSupervisor] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._clock_tasks: dict[str, asyncio.Task] = {}
        self._metadata_tasks: dict[str, asyncio.Task] = {}
        self._shutdown_task: asyncio.Task | None = None
        self._latest_events: dict[tuple[str, str], CachedMarketDataEvent] = {}

    @property
    def report(self) -> BootstrapReport | None:
        return self._report

    async def _fetch_spot_metadata(
        self, client: BinanceSpotRestClient
    ) -> SymbolMetadataSnapshot:
        """Fetch and fully validate one complete Spot metadata snapshot."""

        snapshots = []
        for symbol in self._config.spot_symbols:
            payload = await asyncio.to_thread(client.get_exchange_info, symbol)
            fetched_at = self._monotonic()
            snapshot = await asyncio.to_thread(
                parse_spot_exchange_info,
                payload,
                fetched_at_monotonic=fetched_at,
            )
            snapshot.require_fresh(self._monotonic())
            snapshot.require_symbol(symbol, now_monotonic=self._monotonic())
            if hasattr(snapshot, "symbols") and set(snapshot.symbols) != {symbol}:
                raise RuntimeError(
                    f"Spot metadata response did not match requested symbol: {symbol}"
                )
            snapshots.append(snapshot)

        if len(snapshots) == 1:
            metadata = snapshots[0]
        else:
            combined_symbols = {}
            for snapshot in snapshots:
                for symbol, record in snapshot.symbols.items():
                    if symbol in combined_symbols:
                        raise RuntimeError(f"Duplicate Spot metadata symbol: {symbol}")
                    combined_symbols[symbol] = record
            if set(combined_symbols) != set(self._config.spot_symbols):
                raise RuntimeError(
                    "Combined Spot metadata does not match configured symbols."
                )
            metadata = SymbolMetadataSnapshot(
                market="spot",
                server_time_ms=min(s.server_time_ms for s in snapshots),
                fetched_at_monotonic=min(s.fetched_at_monotonic for s in snapshots),
                max_age_seconds=min(s.max_age_seconds for s in snapshots),
                symbols=MappingProxyType(combined_symbols),
                assets=frozenset().union(*(s.assets for s in snapshots)),
            )

        metadata.require_fresh(self._monotonic())
        for symbol in self._config.spot_symbols:
            metadata.require_symbol(symbol, now_monotonic=self._monotonic())
        return metadata

    async def _fetch_futures_metadata(
        self, client: BinanceUsdMFuturesRestClient
    ) -> SymbolMetadataSnapshot:
        """Fetch and fully validate one complete USD-M Futures snapshot."""

        payload = await asyncio.to_thread(client.get_exchange_info)
        metadata = await asyncio.to_thread(
            parse_usdm_futures_exchange_info,
            payload,
            fetched_at_monotonic=self._monotonic(),
        )
        metadata.require_fresh(self._monotonic())
        configured_symbols = tuple(dict.fromkeys(
            self._config.futures_depth_symbols
            + self._config.futures_mark_price_symbols
        ))
        for symbol in configured_symbols:
            metadata.require_symbol(symbol, now_monotonic=self._monotonic())
        return metadata

    async def _bootstrap_spot(self) -> MarketResources:
        client = self._spot_client_factory()
        clock = self._clock_factory()

        if not isinstance(client, BinanceSpotRestClient):
            raise TypeError("Spot client factory returned an invalid client.")

        if not isinstance(clock, ExchangeClock):
            raise TypeError("Clock factory returned an invalid ExchangeClock.")

        await asyncio.to_thread(clock.synchronize, client.get_server_time)
        metadata = await self._fetch_spot_metadata(client)

        async def refresh_metadata() -> SymbolMetadataSnapshot:
            return await self._fetch_spot_metadata(client)

        if not clock.is_synchronized:
            raise RuntimeError("Spot exchange clock expired during bootstrap.")

        return MarketResources(
            market="spot",
            clock=clock,
            metadata=metadata,
            freshness_validator=MarketDataFreshnessValidator(
                clock,
                monotonic=self._monotonic,
            ),
            server_time_provider=client.get_server_time,
            metadata_refresh_provider=refresh_metadata,
        )

    async def _bootstrap_futures(self) -> MarketResources:
        client = self._futures_client_factory()
        clock = self._clock_factory()

        if not isinstance(client, BinanceUsdMFuturesRestClient):
            raise TypeError("Futures client factory returned an invalid client.")

        if not isinstance(clock, ExchangeClock):
            raise TypeError("Clock factory returned an invalid ExchangeClock.")

        await asyncio.to_thread(clock.synchronize, client.get_server_time)

        metadata = await self._fetch_futures_metadata(client)

        async def refresh_metadata() -> SymbolMetadataSnapshot:
            return await self._fetch_futures_metadata(client)

        if not clock.is_synchronized:
            raise RuntimeError("Futures exchange clock expired during bootstrap.")

        return MarketResources(
            market="usdm_futures",
            clock=clock,
            metadata=metadata,
            freshness_validator=MarketDataFreshnessValidator(
                clock,
                monotonic=self._monotonic,
            ),
            server_time_provider=client.get_server_time,
            metadata_refresh_provider=refresh_metadata,
        )

    async def bootstrap(self) -> BootstrapReport:
        """Bootstrap enabled markets independently; retry failed markets."""

        async with self._bootstrap_lock:
            jobs = []

            if self._config.spot_symbols and "spot" not in self._resources:
                jobs.append(("spot", self._bootstrap_spot))

            futures_enabled = bool(
                self._config.futures_depth_symbols
                or self._config.futures_mark_price_symbols
            )
            if futures_enabled and "usdm_futures" not in self._resources:
                jobs.append(("usdm_futures", self._bootstrap_futures))

            if not jobs:
                if self._report is None:
                    self._report = BootstrapReport(
                        self._resources,
                        self._errors,
                    )
                return self._report

            results = await asyncio.gather(
                *(factory() for _, factory in jobs),
                return_exceptions=True,
            )

            for (market, _), result in zip(jobs, results):
                if isinstance(result, asyncio.CancelledError):
                    raise result

                if isinstance(result, Exception):
                    self._errors[market] = type(result).__name__
                    continue

                if isinstance(result, BaseException):
                    raise result

                self._resources[market] = result
                self._errors.pop(market, None)

            self._report = BootstrapReport(
                self._resources,
                self._errors,
            )
            return self._report

    @property
    def event_cache_size(self) -> int:
        return len(self._latest_events)

    def _configured_symbols_for_group(self, group: str) -> tuple[str, ...]:
        groups = {
            "spot": self._config.spot_symbols,
            "futures_depth": self._config.futures_depth_symbols,
            "futures_mark_price": self._config.futures_mark_price_symbols,
        }
        if group not in groups:
            raise ValueError("Unknown market-data group.")
        return groups[group]

    def latest_event(
        self,
        group: str,
        symbol: str,
    ) -> CachedMarketDataEvent | None:
        """Return the immutable latest event for a configured symbol."""

        configured = self._configured_symbols_for_group(group)
        if symbol not in configured:
            raise ValueError("Symbol is not configured for this pipeline.")

        self._refresh_cached_event_freshness(group, symbol)
        return self._latest_events.get((group, symbol))

    def _market_for_group(self, group: str) -> str:
        if group == "spot":
            return "spot"
        if group in ("futures_depth", "futures_mark_price"):
            return "usdm_futures"
        raise ValueError("Unknown market-data group.")

    @staticmethod
    def _safe_freshness_reason(result) -> str:
        reason = getattr(result, "reason", None)
        if (
            isinstance(reason, str)
            and len(reason) <= 64
            and reason.isascii()
            and reason.isidentifier()
        ):
            return reason
        return "freshness_validation_failed"

    def _refresh_cached_event_freshness(
        self,
        group: str,
        symbol: str,
    ) -> bool:
        """Revalidate a cached non-depth event; return whether it was removed."""

        cache_key = (group, symbol)
        cached = self._latest_events.get(cache_key)
        if cached is None:
            return False

        # Depth events require both a synchronized exchange clock and
        # a synchronized order book; timestamp freshness is not applicable.
        if group == "futures_depth":
            resources = self._resources.get("usdm_futures")
            clock = (
                None
                if resources is None
                else getattr(resources, "clock", None)
            )

            if (
                clock is None
                or getattr(clock, "is_synchronized", False) is not True
            ):
                self._latest_events.pop(cache_key, None)
                self._pipeline_states[group] = PipelineState.UNSYNCED
                self._pipeline_errors[group] = "ExchangeClockUnsynchronized"
                self._refresh_service_state()
                return True

            return False

        try:
            market = self._market_for_group(group)
            resources = self._resources.get(market)
            validator = (
                None if resources is None
                else getattr(resources, "freshness_validator", None)
            )
            if validator is None:
                self._latest_events.pop(cache_key, None)
                self._pipeline_states[group] = PipelineState.FAILED
                self._pipeline_errors[group] = "FreshnessResourcesUnavailable"
                self._refresh_service_state()
                return True

            result = validator.validate_event(
                cached.event_type,
                cached.event_timestamp_ms,
                received_at_monotonic=cached.received_at_monotonic,
            )
            state = getattr(result, "state", None)
            state = getattr(state, "value", state)

            if state == FreshnessState.FRESH.value:
                # A revalidation may refresh metadata, but it cannot recover a
                # pipeline already marked stale/unsynced/failed. Only a new
                # supervised event can establish readiness again.
                self._latest_events[cache_key] = replace(
                    cached,
                    freshness=result,
                )
                return False

            self._latest_events.pop(cache_key, None)
            reason = self._safe_freshness_reason(result)

            if state == FreshnessState.STALE.value:
                self._pipeline_states[group] = PipelineState.STALE
                self._pipeline_errors[group] = reason
            elif state == FreshnessState.CLOCK_UNSYNCED.value:
                self._pipeline_states[group] = PipelineState.UNSYNCED
                self._pipeline_errors[group] = reason
            else:
                self._pipeline_states[group] = PipelineState.FAILED
                self._pipeline_errors[group] = reason

            self._refresh_service_state()
            return True

        except Exception as exc:
            self._latest_events.pop(cache_key, None)
            self._pipeline_states[group] = PipelineState.FAILED
            self._pipeline_errors[group] = type(exc).__name__
            self._refresh_service_state()
            return True

    def refresh_freshness(self) -> int:
        """Revalidate all cached non-depth events and return removals count."""

        removed = 0
        for group, symbol in tuple(self._latest_events):
            if self._refresh_cached_event_freshness(group, symbol):
                removed += 1
        return removed

    def clear_event_cache(self) -> None:
        """Remove all cached market-data events."""

        self._latest_events.clear()

    @property
    def state(self) -> MarketDataServiceState:
        return self._service_state

    @property
    def pipeline_states(self) -> Mapping[str, PipelineState]:
        return MappingProxyType(dict(self._pipeline_states))

    @property
    def pipeline_errors(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._pipeline_errors))

    @property
    def supervisors(self) -> Mapping[str, MarketDataWebSocketSupervisor]:
        return MappingProxyType(dict(self._supervisors))


    def _verified_active_streams(
        self,
        group: str,
        supervisor,
    ) -> tuple[str, ...]:
        """Return streams only after the reconnect manager verified them."""

        if supervisor is None:
            return ()

        try:
            supervisor_state = getattr(supervisor, "state", None)
            state_value = getattr(supervisor_state, "value", supervisor_state)
            if state_value != "SUBSCRIPTIONS_READY":
                return ()

            streams = getattr(supervisor, "active_streams", None)
            if not isinstance(streams, tuple):
                return ()

            symbols = self._configured_symbols_for_group(group)
            suffix = {
                "spot": "@trade",
                "futures_depth": "@depth",
                "futures_mark_price": "@markPrice",
            }[group]
            expected = tuple(sorted(
                f"{symbol.lower()}{suffix}" for symbol in symbols
            ))
            normalized = tuple(sorted(streams))

            if normalized != expected:
                return ()

            return normalized
        except Exception:
            # Health reporting must never turn an unknown supervisor state
            # into a positive subscription claim.
            return ()

    def health_snapshot(self) -> MarketDataServiceHealth:
        """Build an immutable, fail-closed snapshot of all enabled pipelines."""

        now = self._monotonic()
        self.refresh_freshness()
        statuses = []

        group_models = {
            "spot": MarketDataGroup.SPOT,
            "futures_depth": MarketDataGroup.FUTURES_DEPTH,
            "futures_mark_price": MarketDataGroup.FUTURES_MARK_PRICE,
        }

        terminal_states = {
            PipelineState.FAILED,
            PipelineState.STOPPING,
            PipelineState.STOPPED,
            PipelineState.DISABLED,
        }

        for group, market in self._enabled_groups():
            symbols = self._configured_symbols_for_group(group)
            current_state = self._pipeline_states.get(
                group,
                PipelineState.BOOTSTRAPPING,
            )
            resources = self._resources.get(market)
            supervisor = self._supervisors.get(group)

            clock_synchronized = False
            metadata_fresh = False
            health_error = self._pipeline_errors.get(group)

            if resources is not None:
                try:
                    clock_synchronized = (
                        getattr(resources.clock, "is_synchronized", False) is True
                    )
                except Exception as exc:
                    health_error = health_error or type(exc).__name__

                try:
                    require_fresh = getattr(resources.metadata, "require_fresh", None)
                    if callable(require_fresh):
                        result = require_fresh(now_monotonic=now)
                        metadata_fresh = result is not False
                except Exception as exc:
                    health_error = health_error or type(exc).__name__

            active_streams = self._verified_active_streams(group, supervisor)

            group_events = tuple(
                event
                for (event_group, _), event in self._latest_events.items()
                if event_group == group
            )
            latest = max(
                group_events,
                key=lambda event: event.received_at_monotonic,
                default=None,
            )

            last_event_type = None
            last_event_timestamp_ms = None
            last_event_received_at = None
            last_freshness_state = None
            book_synchronized = False

            if latest is not None:
                last_event_type = latest.event_type
                last_event_timestamp_ms = latest.event_timestamp_ms
                last_event_received_at = latest.received_at_monotonic
                book_synchronized = latest.book_synchronized

                if group != "futures_depth":
                    freshness_state = getattr(
                        latest.freshness,
                        "state",
                        None,
                    )
                    freshness_value = getattr(
                        freshness_state,
                        "value",
                        freshness_state,
                    )
                    try:
                        last_freshness_state = FreshnessState(freshness_value)
                    except (TypeError, ValueError):
                        last_freshness_state = None

            effective_state = current_state

            # Do not recover terminal or previously degraded states merely
            # because a read-only health snapshot happens to run.
            if current_state not in terminal_states:
                if (
                    current_state is PipelineState.BOOTSTRAPPING
                    and supervisor is None
                ):
                    effective_state = PipelineState.BOOTSTRAPPING
                elif not clock_synchronized:
                    effective_state = PipelineState.UNSYNCED
                elif not metadata_fresh:
                    effective_state = PipelineState.STALE
                elif current_state in (PipelineState.STALE, PipelineState.UNSYNCED):
                    effective_state = current_state
                elif group == "futures_depth":
                    if latest is None or not book_synchronized:
                        effective_state = PipelineState.UNSYNCED
                    elif not active_streams:
                        effective_state = PipelineState.SUBSCRIBING
                elif latest is None:
                    effective_state = PipelineState.SUBSCRIBING
                elif last_freshness_state is not FreshnessState.FRESH:
                    effective_state = PipelineState.STALE
                elif not active_streams:
                    effective_state = PipelineState.SUBSCRIBING
                elif current_state is PipelineState.READY:
                    effective_state = PipelineState.READY

            if effective_state != current_state:
                self._pipeline_states[group] = effective_state

            if health_error is None:
                if resources is None and current_state != PipelineState.BOOTSTRAPPING:
                    health_error = self._errors.get(market)
                elif not clock_synchronized and resources is not None:
                    health_error = "ExchangeClockUnsynchronized"
                elif not metadata_fresh and resources is not None:
                    health_error = "MetadataStale"

            statuses.append(
                MarketDataPipelineStatus(
                    group=group_models[group],
                    state=effective_state,
                    symbols=tuple(symbols),
                    active_streams=active_streams,
                    clock_synchronized=clock_synchronized,
                    metadata_fresh=metadata_fresh,
                    book_synchronized=book_synchronized,
                    last_event_type=last_event_type,
                    last_event_timestamp_ms=last_event_timestamp_ms,
                    last_event_received_at_monotonic=last_event_received_at,
                    last_freshness_state=last_freshness_state,
                    last_error=health_error,
                    updated_at_monotonic=now,
                )
            )

        self._refresh_service_state()

        return MarketDataServiceHealth(
            state=self._service_state,
            generated_at_monotonic=now,
            pipelines=tuple(statuses),
        )

    def _enabled_groups(self) -> tuple[tuple[str, str], ...]:
        groups = []

        if self._config.spot_symbols:
            groups.append(("spot", "spot"))

        if self._config.futures_depth_symbols:
            groups.append(("futures_depth", "usdm_futures"))

        if self._config.futures_mark_price_symbols:
            groups.append(("futures_mark_price", "usdm_futures"))

        return tuple(groups)

    def _refresh_service_state(self) -> None:
        if not self._enabled_groups():
            self._service_state = MarketDataServiceState.DISABLED
            return

        states = tuple(self._pipeline_states.values())

        if not states:
            if self._service_state not in (
                MarketDataServiceState.STOPPING,
                MarketDataServiceState.STOPPED,
                MarketDataServiceState.FAILED,
            ):
                self._service_state = MarketDataServiceState.STARTING
            return

        if all(state is PipelineState.STOPPED for state in states):
            if self._service_state is not MarketDataServiceState.FAILED:
                self._service_state = MarketDataServiceState.STOPPED
        elif any(state is PipelineState.STOPPING for state in states):
            self._service_state = MarketDataServiceState.STOPPING
        elif all(state is PipelineState.FAILED for state in states):
            self._service_state = MarketDataServiceState.FAILED
        elif any(
            state in (
                PipelineState.FAILED,
                PipelineState.UNSYNCED,
                PipelineState.STALE,
                PipelineState.STOPPED,
            )
            for state in states
        ):
            self._service_state = MarketDataServiceState.DEGRADED
        else:
            self._service_state = MarketDataServiceState.RUNNING

    async def _record_supervised_event(self, group: str, event) -> bool:
        """Validate and cache one supervised event without trusting callbacks."""

        expected = {
            "spot": ("spot", "trade"),
            "futures_depth": ("usdm_futures", "depthUpdate"),
            "futures_mark_price": ("usdm_futures", "markPriceUpdate"),
        }

        if group not in expected:
            raise RuntimeError("Unknown market-data pipeline.")

        expected_market, expected_event_type = expected[group]
        configured_symbols = self._configured_symbols_for_group(group)

        message = getattr(event, "message", None)
        if message is None:
            raise RuntimeError("Supervisor supplied an invalid event.")

        if getattr(message, "kind", None) != "event":
            raise RuntimeError("Only validated event messages can be cached.")

        if getattr(message, "market", None) != expected_market:
            raise RuntimeError("Event market does not match its pipeline.")

        if getattr(message, "event_type", None) != expected_event_type:
            raise RuntimeError("Event type does not match its pipeline.")

        symbol = getattr(message, "symbol", None)
        if not isinstance(symbol, str) or symbol not in configured_symbols:
            raise RuntimeError("Event symbol is not configured for this pipeline.")

        expected_stream = {
            "spot": f"{symbol.lower()}@trade",
            "futures_depth": f"{symbol.lower()}@depth",
            "futures_mark_price": f"{symbol.lower()}@markPrice",
        }[group]

        if getattr(message, "stream", None) != expected_stream:
            raise RuntimeError("Event stream does not match its symbol.")

        payload = getattr(message, "payload", None)
        if not isinstance(payload, Mapping):
            raise RuntimeError("Supervisor event payload is invalid.")

        payload_symbol = payload.get("s")
        if payload_symbol != symbol:
            raise RuntimeError("Payload symbol does not match validated symbol.")

        timestamp = payload.get("E")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= 0
        ):
            raise RuntimeError("Supervisor event timestamp is invalid.")

        received_at = getattr(event, "received_at_monotonic", None)
        if (
            isinstance(received_at, bool)
            or not isinstance(received_at, (int, float))
            or not math.isfinite(received_at)
            or received_at < 0
        ):
            raise RuntimeError("Invalid event receive timestamp.")

        if group == "futures_depth":
            if (
                getattr(event, "freshness", None) is not None
                or getattr(event, "book_synchronized", True) is not False
            ):
                raise RuntimeError(
                    "Depth event cannot claim freshness or book synchronization."
                )
            freshness = None
            book_synchronized = False

        else:
            freshness = getattr(event, "freshness", None)
            freshness_state = getattr(freshness, "state", None)

            if (
                getattr(freshness_state, "value", freshness_state) != "FRESH"
                or getattr(freshness, "event_type", None) != expected_event_type
                or getattr(freshness, "event_timestamp_ms", None) != timestamp
                or getattr(event, "book_synchronized", True) is not False
            ):
                raise RuntimeError(
                    "Only matching fresh, non-book events can be cached."
                )

            book_synchronized = False

        cache_key = (group, symbol)
        previous = self._latest_events.get(cache_key)

        if previous is not None:
            if timestamp < previous.event_timestamp_ms:
                return False

            if (
                timestamp == previous.event_timestamp_ms
                and received_at < previous.received_at_monotonic
            ):
                return False

        cached = CachedMarketDataEvent(
            group=group,
            symbol=symbol,
            event_type=expected_event_type,
            event_timestamp_ms=timestamp,
            received_at_monotonic=float(received_at),
            freshness=freshness,
            book_synchronized=book_synchronized,
            payload=payload,
        )

        self._latest_events[cache_key] = cached

        if group == "futures_depth":
            self._pipeline_states[group] = PipelineState.UNSYNCED
        else:
            self._pipeline_states[group] = PipelineState.READY

        self._pipeline_errors.pop(group, None)
        self._refresh_service_state()
        return True
    def _make_event_callback(self, group: str):
        async def callback(event):
            await self._record_supervised_event(group, event)
        return callback

    async def _run_supervisor(self, group: str, supervisor) -> None:
        try:
            await supervisor.run()

            if self._service_state not in (
                MarketDataServiceState.STOPPING,
                MarketDataServiceState.STOPPED,
            ):
                self._pipeline_states[group] = PipelineState.FAILED
                self._pipeline_errors[group] = "UnexpectedSupervisorExit"

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            self._pipeline_states[group] = PipelineState.FAILED
            self._pipeline_errors[group] = type(exc).__name__

        finally:
            self._refresh_service_state()

    def _invalidate_market_events(self, market: str) -> None:
        """Invalidate cached events and degrade active pipelines for one market."""

        terminal_states = {
            PipelineState.FAILED,
            PipelineState.STOPPING,
            PipelineState.STOPPED,
            PipelineState.DISABLED,
        }

        affected_groups = {
            group
            for group, group_market in self._enabled_groups()
            if group_market == market
        }

        for cache_key in tuple(self._latest_events):
            if cache_key[0] in affected_groups:
                self._latest_events.pop(cache_key, None)

        for group in affected_groups:
            current = self._pipeline_states.get(group)
            if current is None or current in terminal_states:
                continue

            self._pipeline_states[group] = PipelineState.UNSYNCED
            self._pipeline_errors[group] = "ExchangeClockUnsynchronized"

        self._refresh_service_state()

    async def _maintain_exchange_clock(self, market: str, resources: MarketResources) -> None:
        """Periodically resynchronize one market clock without restarting streams."""

        delay = self._clock_sync_interval_seconds

        while True:
            await self._sleep(delay)

            try:
                await asyncio.to_thread(
                    resources.clock.synchronize,
                    resources.server_time_provider,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._invalidate_market_events(market)
                delay = self._clock_retry_interval_seconds
            else:
                delay = self._clock_sync_interval_seconds

    def _start_clock_maintenance_tasks(self, report: BootstrapReport) -> None:
        """Start one maintenance task for each market with a live supervisor."""

        active_markets = {
            self._market_for_group(group)
            for group in self._supervisors
        }

        for market in sorted(active_markets):
            if market in self._clock_tasks:
                continue

            resources = report.resources.get(market)
            if resources is None:
                continue

            if (
                not callable(getattr(resources, "server_time_provider", None))
                or not callable(getattr(resources.clock, "synchronize", None))
            ):
                continue

            self._clock_tasks[market] = asyncio.create_task(
                self._maintain_exchange_clock(market, resources),
                name=f"market-clock-{market}",
            )

    async def _maintain_metadata(self, market: str) -> None:
        """Refresh one market's complete metadata snapshot independently."""

        delay = self._metadata_refresh_interval_seconds
        while True:
            await self._sleep(delay)
            current = self._resources.get(market)
            if current is None:
                return
            try:
                provider = current.metadata_refresh_provider
                metadata = await provider()
                metadata.require_fresh(self._monotonic())
                configured = (
                    self._config.spot_symbols
                    if market == "spot"
                    else tuple(dict.fromkeys(
                        self._config.futures_depth_symbols
                        + self._config.futures_mark_price_symbols
                    ))
                )
                for symbol in configured:
                    metadata.require_symbol(symbol, now_monotonic=self._monotonic())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                async with self._bootstrap_lock:
                    self._errors[market] = type(exc).__name__
                    self._report = BootstrapReport(self._resources, self._errors)
                delay = self._metadata_retry_interval_seconds
                continue

            async with self._bootstrap_lock:
                # Do not overwrite a newer resource installed by another lifecycle.
                if self._resources.get(market) is current:
                    self._resources[market] = replace(current, metadata=metadata)
                    self._errors.pop(market, None)
                    self._report = BootstrapReport(self._resources, self._errors)
            delay = self._metadata_refresh_interval_seconds

    def _start_metadata_maintenance_tasks(self, report: BootstrapReport) -> None:
        """Start one independent metadata refresh task per active market."""

        active_markets = {self._market_for_group(group) for group in self._supervisors}
        for market in sorted(active_markets):
            if market in self._metadata_tasks:
                continue
            resources = report.resources.get(market)
            if resources is None or not callable(
                getattr(resources, "metadata_refresh_provider", None)
            ):
                continue
            self._metadata_tasks[market] = asyncio.create_task(
                self._maintain_metadata(market),
                name=f"market-metadata-{market}",
            )

    async def start(self) -> BootstrapReport:
        """Bootstrap resources and start each enabled supervisor independently."""

        async with self._lifecycle_lock:
            if self._service_state is MarketDataServiceState.STOPPING:
                raise RuntimeError("Service is currently stopping.")

            if any(
                not task.done()
                for task in (
                    *self._tasks.values(),
                    *self._clock_tasks.values(),
                    *self._metadata_tasks.values(),
                )
            ):
                if self._report is None:
                    raise RuntimeError("Active service has no bootstrap report.")
                return self._report

            self._service_state = MarketDataServiceState.STARTING

            try:
                report = await self.bootstrap()
            except asyncio.CancelledError:
                self._service_state = MarketDataServiceState.STOPPED
                raise
            except Exception:
                self._service_state = MarketDataServiceState.FAILED
                raise

            enabled = self._enabled_groups()
            if not enabled:
                self._service_state = MarketDataServiceState.DISABLED
                return report

            self._supervisors = {}
            self._tasks = {}
            self._clock_tasks = {}
            self._metadata_tasks = {}
            self._pipeline_states = {}
            self._pipeline_errors = {}

            for group, market in enabled:
                resources = report.resources.get(market)

                if resources is None:
                    self._pipeline_states[group] = PipelineState.FAILED
                    self._pipeline_errors[group] = (
                        report.errors.get(market, "BootstrapUnavailable")
                    )
                    continue

                try:
                    kwargs = {
                        "freshness_validator": (
                            None
                            if group == "futures_depth"
                            else resources.freshness_validator
                        ),
                        "on_event": (
                            self._make_event_callback(group)
                            if group != "futures_depth"
                            else None
                        ),
                        "on_depth_event": (
                            self._make_event_callback(group)
                            if group == "futures_depth"
                            else None
                        ),
                        "monotonic": self._monotonic,
                    }

                    supervisor = self._supervisor_factory(
                        self._config,
                        group,
                        **kwargs,
                    )

                    if (
                        not callable(getattr(supervisor, "run", None))
                        or not callable(getattr(supervisor, "stop", None))
                    ):
                        raise TypeError(
                            "Supervisor factory returned an invalid object."
                        )

                    self._supervisors[group] = supervisor
                    self._pipeline_states[group] = PipelineState.SUBSCRIBING

                    self._tasks[group] = asyncio.create_task(
                        self._run_supervisor(group, supervisor),
                        name=f"market-data-{group}",
                    )

                except Exception as exc:
                    self._pipeline_states[group] = PipelineState.FAILED
                    self._pipeline_errors[group] = type(exc).__name__

            self._start_clock_maintenance_tasks(report)
            self._start_metadata_maintenance_tasks(report)
            self._refresh_service_state()
            return report

    async def _stop_impl(self) -> None:
        async with self._lifecycle_lock:
            if self._service_state in (
                MarketDataServiceState.DISABLED,
                MarketDataServiceState.STOPPED,
            ):
                return

            self._service_state = MarketDataServiceState.STOPPING

            supervisors = tuple(self._supervisors.values())
            tasks = tuple(self._tasks.values())
            clock_tasks = tuple(self._clock_tasks.values())
            metadata_tasks = tuple(self._metadata_tasks.values())

            stop_results = await asyncio.gather(
                *(supervisor.stop() for supervisor in supervisors),
                return_exceptions=True,
            )

            all_tasks = (*tasks, *clock_tasks, *metadata_tasks)
            for task in all_tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(*all_tasks, return_exceptions=True)
            self._tasks.clear()
            self._clock_tasks.clear()
            self._metadata_tasks.clear()

            self.clear_event_cache()

            cleanup_failed = any(
                isinstance(result, BaseException)
                for result in stop_results
            )

            for group in self._pipeline_states:
                self._pipeline_states[group] = PipelineState.STOPPED

            if cleanup_failed:
                self._service_state = MarketDataServiceState.FAILED
                self._pipeline_errors["shutdown"] = "SupervisorShutdownError"
            else:
                self._service_state = MarketDataServiceState.STOPPED

    async def stop(self) -> None:
        """Idempotent shutdown; shield cleanup from caller cancellation."""

        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = asyncio.create_task(self._stop_impl())

        await asyncio.shield(self._shutdown_task)
