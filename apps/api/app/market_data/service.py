"""Independent, fail-closed REST bootstrap for public market data."""

from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping

from app.market_data.binance_spot_rest import BinanceSpotRestClient
from app.market_data.binance_usdm_futures_rest import BinanceUsdMFuturesRestClient
from app.market_data.futures_order_book import (
    FuturesOrderBookSynchronizer,
    FuturesOrderBookView,
    FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS,
)
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
    SpotBookTickerQuoteStatus,
    SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS,
)
from app.market_data.symbol_filters import get_price_tick_size
from app.market_data.symbol_metadata import (
    SymbolMetadataSnapshot,
    parse_spot_exchange_info,
    parse_usdm_futures_exchange_info,
)


@dataclass(frozen=True, slots=True)
class MarketResources:
    """Verified REST resources shared by pipelines belonging to one market."""

    market: str
    clock: ExchangeClock | None
    metadata: SymbolMetadataSnapshot
    freshness_validator: MarketDataFreshnessValidator | None
    server_time_provider: Callable[[], dict] | None
    metadata_refresh_provider: Callable[[], Awaitable[SymbolMetadataSnapshot]]
    depth_snapshot_provider: Callable[[str], dict] | None = None


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


@dataclass(frozen=True, slots=True)
class CachedSpotBookTicker:
    """Immutable Spot BBO snapshot without a fabricated provider timestamp."""

    symbol: str
    update_id: int
    received_at_monotonic: float
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_payload(self.payload))


def _metadata_json_value(value: object) -> object:
    """Convert frozen exchange metadata into safe JSON-compatible values."""
    if isinstance(value, Mapping):
        return {
            key: _metadata_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_metadata_json_value(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise RuntimeError("Trusted symbol metadata contains a non-JSON value.")


@dataclass(frozen=True, slots=True)
class SpotBookTickerExecutionContext:
    """Immutable, service-verified Spot BBO and symbol metadata context."""

    symbol: str
    update_id: int
    bid: Decimal
    ask: Decimal
    received_at_monotonic: float
    metadata_received_at_monotonic: float
    tick_size: Decimal
    current_monotonic: float
    metadata_snapshot: SymbolMetadataSnapshot
    symbol_metadata_snapshot: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FuturesDepthExecutionContext:
    """Immutable service-verified USD-M Futures depth quote and clock context."""

    symbol: str
    last_update_id: int
    bid: Decimal
    ask: Decimal
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    event_time: datetime
    received_at: datetime
    metadata_received_at: datetime
    received_at_monotonic: float
    metadata_received_at_monotonic: float
    tick_size: Decimal
    current_monotonic: float
    now: datetime
    metadata_snapshot: SymbolMetadataSnapshot
    symbol_metadata_snapshot: Mapping[str, object]


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
        self._spot_book_ticker_quotes: dict[
            tuple[str, str], CachedSpotBookTicker
        ] = {}
        self._futures_order_books: dict[str, FuturesOrderBookSynchronizer] = {}
        self._futures_snapshot_tasks: dict[str, asyncio.Task] = {}

    @property
    def report(self) -> BootstrapReport | None:
        return self._report

    async def _fetch_spot_metadata(
        self, client: BinanceSpotRestClient
    ) -> SymbolMetadataSnapshot:
        """Fetch and fully validate one complete Spot metadata snapshot."""

        snapshots = []
        configured_symbols = tuple(dict.fromkeys(
            self._config.spot_symbols
            + self._config.spot_book_ticker_symbols
        ))

        if not configured_symbols:
            raise RuntimeError("Spot metadata requested without configured symbols.")

        for symbol in configured_symbols:
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
            if set(combined_symbols) != set(configured_symbols):
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
        for symbol in configured_symbols:
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

        if not isinstance(client, BinanceSpotRestClient):
            raise TypeError("Spot client factory returned an invalid client.")

        # Spot Trade uses exchange-clock freshness. BookTicker does not expose
        # a provider event timestamp, so a BookTicker-only bootstrap must not
        # create or synchronize an ExchangeClock.
        clock = None
        freshness_validator = None
        server_time_provider = None

        if self._config.spot_symbols:
            clock = self._clock_factory()
            if not isinstance(clock, ExchangeClock):
                raise TypeError("Clock factory returned an invalid ExchangeClock.")

            await asyncio.to_thread(clock.synchronize, client.get_server_time)

            if not clock.is_synchronized:
                raise RuntimeError("Spot exchange clock expired during bootstrap.")

            freshness_validator = MarketDataFreshnessValidator(
                clock,
                monotonic=self._monotonic,
            )
            server_time_provider = client.get_server_time

        metadata = await self._fetch_spot_metadata(client)

        async def refresh_metadata() -> SymbolMetadataSnapshot:
            return await self._fetch_spot_metadata(client)

        return MarketResources(
            market="spot",
            clock=clock,
            metadata=metadata,
            freshness_validator=freshness_validator,
            server_time_provider=server_time_provider,
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
            depth_snapshot_provider=client.get_depth_snapshot,
        )

    async def bootstrap(self) -> BootstrapReport:
        """Bootstrap enabled markets independently; retry failed markets."""

        async with self._bootstrap_lock:
            jobs = []

            spot_enabled = bool(
                self._config.spot_symbols
                or self._config.spot_book_ticker_symbols
            )
            if spot_enabled and "spot" not in self._resources:
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
            "spot_book_ticker": self._config.spot_book_ticker_symbols,
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
        """Return the immutable latest timestamped event for a symbol."""

        if group == "spot_book_ticker":
            raise ValueError("Use latest_spot_book_ticker for Spot BBO data.")

        configured = self._configured_symbols_for_group(group)
        if symbol not in configured:
            raise ValueError("Symbol is not configured for this pipeline.")

        self._refresh_cached_event_freshness(group, symbol)
        return self._latest_events.get((group, symbol))

    @property
    def quote_cache_size(self) -> int:
        """Number of cached Spot BookTicker quotes."""
        return len(self._spot_book_ticker_quotes)

    def latest_spot_book_ticker(
        self,
        symbol: str,
    ) -> CachedSpotBookTicker | None:
        """Return the latest validated BBO; this accessor does not assert freshness."""
        configured = self._configured_symbols_for_group("spot_book_ticker")
        if symbol not in configured:
            raise ValueError("Symbol is not configured for Spot BookTicker.")
        return self._spot_book_ticker_quotes.get(("spot_book_ticker", symbol))

    def spot_book_ticker_execution_context(
        self,
        symbol: str,
    ) -> SpotBookTickerExecutionContext:
        """Return a trusted, fresh Spot BBO context for paper execution.

        Does not call health_snapshot(), perform network I/O, or mutate
        pipeline state. All configured BookTicker symbols must be fresh.
        """
        group = "spot_book_ticker"
        configured = self._configured_symbols_for_group(group)

        if not isinstance(symbol, str) or symbol not in configured:
            raise RuntimeError("Symbol is not configured for Spot BookTicker.")

        now = self._monotonic()
        if (
            isinstance(now, bool)
            or type(now) not in (int, float)
            or not math.isfinite(now)
            or now < 0
        ):
            raise RuntimeError("Service monotonic clock is invalid.")

        pipeline_state = self._pipeline_states.get(group)
        if pipeline_state not in (PipelineState.SUBSCRIBING, PipelineState.READY):
            raise RuntimeError("Spot BookTicker pipeline is not execution-eligible.")

        resources = self._resources.get("spot")
        if resources is None:
            raise RuntimeError("Spot market resources are unavailable.")

        metadata = resources.metadata
        if (
            not isinstance(metadata, SymbolMetadataSnapshot)
            or metadata.market != "spot"
        ):
            raise RuntimeError("Trusted Spot metadata is unavailable.")

        metadata.require_fresh(now_monotonic=now)
        symbol_metadata = metadata.require_symbol(symbol, now_monotonic=now)

        supervisor = self._supervisors.get(group)
        active_streams = self._verified_active_streams(group, supervisor)
        expected_streams = tuple(sorted(
            f"{item.lower()}@bookTicker" for item in configured
        ))
        if active_streams != expected_streams:
            raise RuntimeError("Spot BookTicker subscriptions are not verified.")

        requested_quote = None
        required_fields = {"u", "s", "b", "B", "a", "A"}

        for configured_symbol in configured:
            cached = self._spot_book_ticker_quotes.get(
                (group, configured_symbol)
            )
            if not isinstance(cached, CachedSpotBookTicker):
                raise RuntimeError(
                    f"Spot BookTicker quote missing: {configured_symbol}"
                )

            if cached.symbol != configured_symbol:
                raise RuntimeError("Cached BookTicker symbol mismatch.")

            if type(cached.update_id) is not int or cached.update_id <= 0:
                raise RuntimeError("Cached BookTicker update ID is invalid.")

            received = cached.received_at_monotonic
            if (
                isinstance(received, bool)
                or type(received) not in (int, float)
                or not math.isfinite(received)
                or received < 0
            ):
                raise RuntimeError("Cached BookTicker receive time is invalid.")

            age = now - received
            if age < 0 or age > SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS:
                raise RuntimeError(
                    f"BookTicker quote is stale or future-dated: {configured_symbol}"
                )

            payload = cached.payload
            if not isinstance(payload, Mapping) or set(payload) != required_fields:
                raise RuntimeError("Cached BookTicker payload fields are invalid.")

            if payload.get("s") != configured_symbol:
                raise RuntimeError("Cached BookTicker payload symbol mismatch.")

            if payload.get("u") != cached.update_id:
                raise RuntimeError("Cached BookTicker payload update ID mismatch.")

            parsed = {}
            for field in ("b", "B", "a", "A"):
                raw = payload.get(field)
                if not isinstance(raw, str) or not raw or len(raw) > 64:
                    raise RuntimeError("Cached BookTicker decimal field is invalid.")
                try:
                    number = Decimal(raw)
                except (InvalidOperation, ValueError):
                    raise RuntimeError(
                        "Cached BookTicker decimal field is invalid."
                    ) from None
                if not number.is_finite() or number <= 0:
                    raise RuntimeError(
                        "Cached BookTicker decimal field must be positive."
                    )
                parsed[field] = number

            if parsed["B"] <= 0 or parsed["A"] <= 0:
                raise RuntimeError("BookTicker quantities must be positive.")

            if parsed["b"] > parsed["a"]:
                raise RuntimeError("Cached BookTicker bid exceeds ask.")

            if configured_symbol == symbol:
                requested_quote = (cached, parsed["b"], parsed["a"])

        if requested_quote is None:
            raise RuntimeError("Requested Spot BookTicker quote is unavailable.")

        metadata_time = metadata.fetched_at_monotonic
        if (
            isinstance(metadata_time, bool)
            or type(metadata_time) not in (int, float)
            or not math.isfinite(metadata_time)
            or metadata_time < 0
            or metadata_time > now
        ):
            raise RuntimeError("Spot metadata monotonic timestamp is invalid.")

        tick_size = get_price_tick_size(
            metadata,
            symbol,
            now_monotonic=now,
        )

        cached, bid, ask = requested_quote

        symbol_metadata_snapshot = {
            "schema_version": 1,
            "market": metadata.market,
            "symbol": symbol_metadata.symbol,
            "status": symbol_metadata.status,
            "base_asset": symbol_metadata.base_asset,
            "quote_asset": symbol_metadata.quote_asset,
            "order_types": list(symbol_metadata.order_types),
            "filter_types": list(symbol_metadata.filter_types),
            "price_precision": symbol_metadata.price_precision,
            "quantity_precision": symbol_metadata.quantity_precision,
            "filters": _metadata_json_value(symbol_metadata.filters),
            "metadata_server_time_ms": metadata.server_time_ms,
            "tick_size": format(tick_size, "f"),
        }

        return SpotBookTickerExecutionContext(
            symbol=symbol,
            update_id=cached.update_id,
            bid=bid,
            ask=ask,
            received_at_monotonic=float(cached.received_at_monotonic),
            metadata_received_at_monotonic=float(metadata_time),
            tick_size=tick_size,
            current_monotonic=float(now),
            metadata_snapshot=metadata,
            symbol_metadata_snapshot=symbol_metadata_snapshot,
        )


    def futures_depth_execution_context(
        self,
        symbol: str,
    ) -> FuturesDepthExecutionContext:
        """Return a trusted executable quote from a synchronized Futures book.

        Every configured Futures depth symbol must have a fresh, synchronized
        book, valid metadata and verified active subscription. This method
        performs no network I/O and does not call health_snapshot().
        """
        group = "futures_depth"
        configured = self._configured_symbols_for_group(group)

        if not isinstance(symbol, str) or symbol not in configured:
            raise RuntimeError("Symbol is not configured for Futures depth.")

        now_monotonic = self._monotonic()
        if (
            isinstance(now_monotonic, bool)
            or type(now_monotonic) not in (int, float)
            or not math.isfinite(now_monotonic)
            or now_monotonic < 0
        ):
            raise RuntimeError("Service monotonic clock is invalid.")

        if self._pipeline_states.get(group) not in (
            PipelineState.SUBSCRIBING,
            PipelineState.READY,
        ):
            raise RuntimeError("Futures depth pipeline is not execution-eligible.")

        resources = self._resources.get("usdm_futures")
        if resources is None or resources.market != "usdm_futures":
            raise RuntimeError("USD-M Futures resources are unavailable.")

        metadata = resources.metadata
        if (
            not isinstance(metadata, SymbolMetadataSnapshot)
            or metadata.market != "usdm_futures"
        ):
            raise RuntimeError("Trusted USD-M Futures metadata is unavailable.")

        metadata.require_fresh(now_monotonic=now_monotonic)
        metadata_time = metadata.fetched_at_monotonic
        if (
            isinstance(metadata_time, bool)
            or type(metadata_time) not in (int, float)
            or not math.isfinite(metadata_time)
            or metadata_time < 0
            or metadata_time > now_monotonic
        ):
            raise RuntimeError("Futures metadata monotonic timestamp is invalid.")

        clock = resources.clock
        if (
            clock is None
            or getattr(clock, "is_synchronized", False) is not True
            or not callable(getattr(clock, "now_exchange_ms", None))
        ):
            raise RuntimeError("Futures exchange clock is not synchronized.")

        supervisor = self._supervisors.get(group)
        active_streams = self._verified_active_streams(group, supervisor)
        expected_streams = tuple(sorted(
            f"{item.lower()}@depth" for item in configured
        ))
        if active_streams != expected_streams:
            raise RuntimeError("Futures depth subscriptions are not verified.")

        requested = None

        # Validate every configured symbol. A partial multi-symbol pipeline
        # must never be treated as execution-ready for just one symbol.
        for configured_symbol in configured:
            metadata_symbol = metadata.require_symbol(
                configured_symbol,
                now_monotonic=now_monotonic,
            )
            tick_size = get_price_tick_size(
                metadata,
                configured_symbol,
                now_monotonic=now_monotonic,
            )

            synchronizer = self._futures_order_books.get(configured_symbol)
            if (
                not isinstance(synchronizer, FuturesOrderBookSynchronizer)
                or synchronizer.synchronized is not True
            ):
                raise RuntimeError(
                    f"Futures order book is not synchronized: {configured_symbol}"
                )

            book = synchronizer.book
            if not isinstance(book, FuturesOrderBookView):
                raise RuntimeError(
                    f"Futures order book is unavailable or stale: {configured_symbol}"
                )

            if (
                book.symbol != configured_symbol
                or type(book.last_update_id) is not int
                or book.last_update_id <= 0
                or type(book.book_synchronized) is not bool
                or book.book_synchronized is not True
                or type(book.sequence_contiguous) is not bool
                or book.sequence_contiguous is not True
            ):
                raise RuntimeError(
                    f"Futures order book integrity check failed: {configured_symbol}"
                )

            if (
                type(book.event_time_ms) is not int
                or book.event_time_ms < 0
                or type(book.transaction_time_ms) is not int
                or book.transaction_time_ms < 0
            ):
                raise RuntimeError(
                    f"Futures order book timestamps are invalid: {configured_symbol}"
                )

            received_monotonic = book.received_at_monotonic
            if (
                isinstance(received_monotonic, bool)
                or type(received_monotonic) not in (int, float)
                or not math.isfinite(received_monotonic)
                or received_monotonic < 0
                or received_monotonic > now_monotonic
                or now_monotonic - received_monotonic
                    > FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS
            ):
                raise RuntimeError(
                    f"Futures order book is stale or future-dated: {configured_symbol}"
                )

            if (
                not isinstance(book.bids, tuple)
                or not isinstance(book.asks, tuple)
                or not book.bids
                or not book.asks
            ):
                raise RuntimeError(
                    f"Futures order book has no executable depth: {configured_symbol}"
                )

            def validate_depth_levels(levels, *, name, descending):
                if not isinstance(levels, tuple) or not levels:
                    raise RuntimeError(
                        f"Futures {name} depth is empty or malformed: {configured_symbol}"
                    )

                prices = []
                seen = set()

                for index, level in enumerate(levels):
                    if type(level) is not tuple or len(level) != 2:
                        raise RuntimeError(
                            f"Futures {name} level structure is invalid: {configured_symbol}"
                        )

                    price, quantity = level
                    if (
                        type(price) is not Decimal
                        or type(quantity) is not Decimal
                        or not price.is_finite()
                        or not quantity.is_finite()
                        or price <= 0
                        or quantity <= 0
                    ):
                        raise RuntimeError(
                            f"Futures {name} level values are invalid: {configured_symbol}"
                        )

                    if price in seen:
                        raise RuntimeError(
                            f"Futures {name} depth contains duplicate prices: {configured_symbol}"
                        )
                    seen.add(price)
                    prices.append(price)

                    # Preserve the adapter's established best-price tick error.
                    # The fill engine independently validates every consumed level.
                    if index > 0:
                        try:
                            if price % tick_size != 0:
                                raise RuntimeError(
                                    f"Futures {name} price is not tick aligned: {configured_symbol}"
                                )
                        except (InvalidOperation, ArithmeticError) as exc:
                            raise RuntimeError(
                                f"Futures {name} tick validation failed: {configured_symbol}"
                            ) from exc

                if prices != sorted(prices, reverse=descending):
                    raise RuntimeError(
                        f"Futures {name} levels are not correctly ordered: {configured_symbol}"
                    )

            validate_depth_levels(book.bids, name="bid", descending=True)
            validate_depth_levels(book.asks, name="ask", descending=False)

            bid, bid_quantity = book.bids[0]
            ask, ask_quantity = book.asks[0]
            if bid > ask:
                raise RuntimeError(
                    f"Futures best bid/ask is invalid: {configured_symbol}"
                )

            if configured_symbol == symbol:
                requested = (
                    book,
                    bid,
                    ask,
                    tick_size,
                    metadata_symbol,
                )

        if requested is None:
            raise RuntimeError("Requested Futures depth book is unavailable.")

        book, bid, ask, tick_size, symbol_metadata = requested

        # ExchangeClock maps monotonic receive time into Binance's time domain.
        # If a resynchronization makes an old monotonic point unconvertible,
        # fail closed; the next fresh depth event can establish a new quote.
        try:
            current_exchange_ms = clock.now_exchange_ms(
                at_monotonic=float(now_monotonic)
            )
            received_exchange_ms = clock.now_exchange_ms(
                at_monotonic=float(book.received_at_monotonic)
            )
        except Exception as exc:
            raise RuntimeError(
                "Futures exchange clock cannot verify quote timestamps."
            ) from exc

        if (
            type(current_exchange_ms) is not int
            or current_exchange_ms < 0
            or type(received_exchange_ms) is not int
            or received_exchange_ms < 0
            or type(metadata.server_time_ms) is not int
            or metadata.server_time_ms < 0
        ):
            raise RuntimeError("Futures exchange timestamp is invalid.")

        try:
            now = datetime.fromtimestamp(
                current_exchange_ms / 1000,
                tz=timezone.utc,
            )
            event_time = datetime.fromtimestamp(
                book.event_time_ms / 1000,
                tz=timezone.utc,
            )
            received_at = datetime.fromtimestamp(
                received_exchange_ms / 1000,
                tz=timezone.utc,
            )
            metadata_received_at = datetime.fromtimestamp(
                metadata.server_time_ms / 1000,
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise RuntimeError("Futures exchange timestamp is out of range.") from exc

        symbol_metadata_snapshot = {
            "schema_version": 1,
            "market": metadata.market,
            "symbol": symbol_metadata.symbol,
            "status": symbol_metadata.status,
            "base_asset": symbol_metadata.base_asset,
            "quote_asset": symbol_metadata.quote_asset,
            "pair": symbol_metadata.pair,
            "contract_type": symbol_metadata.contract_type,
            "margin_asset": symbol_metadata.margin_asset,
            "delivery_date": symbol_metadata.delivery_date,
            "onboard_date": symbol_metadata.onboard_date,
            "order_types": list(symbol_metadata.order_types),
            "filter_types": list(symbol_metadata.filter_types),
            "price_precision": symbol_metadata.price_precision,
            "quantity_precision": symbol_metadata.quantity_precision,
            "filters": _metadata_json_value(symbol_metadata.filters),
            "metadata_server_time_ms": metadata.server_time_ms,
            "tick_size": format(tick_size, "f"),
        }

        return FuturesDepthExecutionContext(
            symbol=symbol,
            last_update_id=book.last_update_id,
            bid=bid,
            ask=ask,
            bids=book.bids,
            asks=book.asks,
            event_time=event_time,
            received_at=received_at,
            metadata_received_at=metadata_received_at,
            received_at_monotonic=float(book.received_at_monotonic),
            metadata_received_at_monotonic=float(metadata_time),
            tick_size=tick_size,
            current_monotonic=float(now_monotonic),
            now=now,
            metadata_snapshot=metadata,
            symbol_metadata_snapshot=symbol_metadata_snapshot,
        )

    def _market_for_group(self, group: str) -> str:
        if group in ("spot", "spot_book_ticker"):
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

            synchronizer = self._futures_order_books.get(symbol)
            book = None if synchronizer is None else synchronizer.book
            if (
                clock is None
                or getattr(clock, "is_synchronized", False) is not True
            ):
                if synchronizer is not None:
                    synchronizer.invalidate("ExchangeClockUnsynchronized")
                self._latest_events.pop(cache_key, None)
                self._pipeline_states[group] = PipelineState.UNSYNCED
                self._pipeline_errors[group] = "ExchangeClockUnsynchronized"
                self._refresh_service_state()
                return True

            if book is None:
                # Raw depth cache and executable synchronized book are
                # separate contracts. Keep the validated raw event available.
                self._pipeline_states[group] = PipelineState.UNSYNCED
                self._pipeline_errors[group] = "FuturesOrderBookUnsynchronized"
                self._refresh_service_state()
                return False

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
        """Remove all cached events, Spot quotes and Futures order books."""

        self._latest_events.clear()
        self._spot_book_ticker_quotes.clear()
        self._invalidate_futures_order_books("EventCacheCleared")
        self._futures_order_books.clear()

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
                "spot_book_ticker": "@bookTicker",
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
            "spot_book_ticker": MarketDataGroup.SPOT_BOOK_TICKER,
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
                # Spot BookTicker has no provider event timestamp and does not
                # depend on ExchangeClock. False here means not applicable;
                # it must not degrade the BookTicker pipeline.
                if group != "spot_book_ticker":
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
            last_update_id = None
            last_event_received_at = None
            last_freshness_state = None
            book_synchronized = False

            book_ticker_statuses = ()
            book_ticker_missing_symbols = ()
            book_ticker_expired_symbols = ()

            if group == "spot_book_ticker":
                quote_by_symbol = {
                    symbol: self._spot_book_ticker_quotes.get((group, symbol))
                    for symbol in symbols
                }
                book_ticker_missing_symbols = tuple(
                    symbol for symbol, quote in quote_by_symbol.items() if quote is None
                )
                present_quotes = tuple(
                    quote for quote in quote_by_symbol.values() if quote is not None
                )
                book_ticker_statuses = tuple(
                    SpotBookTickerQuoteStatus(
                        symbol=quote.symbol,
                        update_id=quote.update_id,
                        received_at_monotonic=quote.received_at_monotonic,
                    )
                    for quote in present_quotes
                )
                latest_quote = max(
                    present_quotes,
                    key=lambda quote: quote.received_at_monotonic,
                    default=None,
                )
                if latest_quote is not None:
                    last_update_id = latest_quote.update_id
                    last_event_received_at = latest_quote.received_at_monotonic
                book_ticker_expired_symbols = tuple(
                    quote.symbol for quote in present_quotes
                    if (
                        now - quote.received_at_monotonic < 0
                        or now - quote.received_at_monotonic > SPOT_BOOK_TICKER_MAX_RECEIVE_AGE_SECONDS
                    )
                )

            if latest is not None:
                last_event_type = latest.event_type
                last_event_timestamp_ms = latest.event_timestamp_ms
                last_event_received_at = latest.received_at_monotonic
                if group != "futures_depth":
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

            if group == "futures_depth":
                books = {
                    symbol: self._futures_order_books.get(symbol)
                    for symbol in symbols
                }
                views = {
                    symbol: (None if sync is None else sync.book)
                    for symbol, sync in books.items()
                }
                book_synchronized = bool(symbols) and all(
                    view is not None
                    and view.book_synchronized is True
                    and view.sequence_contiguous is True
                    for view in views.values()
                )
                if len(symbols) == 1 and book_synchronized:
                    last_update_id = views[symbols[0]].last_update_id

            effective_state = current_state

            # Do not recover terminal or previously degraded states merely
            # because a read-only health snapshot happens to run.
            if current_state not in terminal_states:
                if group == "spot_book_ticker":
                    if current_state is PipelineState.BOOTSTRAPPING and supervisor is None:
                        effective_state = PipelineState.BOOTSTRAPPING
                    elif not metadata_fresh:
                        effective_state = PipelineState.STALE
                    elif current_state is PipelineState.UNSYNCED and not active_streams:
                        effective_state = PipelineState.UNSYNCED
                    elif not active_streams:
                        effective_state = PipelineState.SUBSCRIBING
                    elif book_ticker_missing_symbols:
                        effective_state = PipelineState.SUBSCRIBING
                    elif book_ticker_expired_symbols:
                        effective_state = PipelineState.STALE
                    else:
                        effective_state = PipelineState.READY

                elif (
                    current_state is PipelineState.BOOTSTRAPPING
                    and supervisor is None
                ):
                    effective_state = PipelineState.BOOTSTRAPPING
                elif not clock_synchronized:
                    effective_state = PipelineState.UNSYNCED
                elif not metadata_fresh:
                    effective_state = PipelineState.STALE
                elif group == "futures_depth":
                    if not book_synchronized:
                        effective_state = PipelineState.UNSYNCED
                    elif not active_streams:
                        effective_state = PipelineState.SUBSCRIBING
                    else:
                        effective_state = PipelineState.READY
                elif current_state in (PipelineState.STALE, PipelineState.UNSYNCED):
                    effective_state = current_state
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

            if group == "spot_book_ticker":
                if resources is None and current_state != PipelineState.BOOTSTRAPPING:
                    health_error = self._errors.get(market)
                elif resources is not None and not metadata_fresh:
                    health_error = "MetadataStale"
                elif not active_streams:
                    health_error = "WebSocketSubscriptionsNotReady"
                elif book_ticker_missing_symbols:
                    health_error = "BookTickerQuoteMissing"
                elif book_ticker_expired_symbols:
                    health_error = "BookTickerReceiveAgeExceeded"
                else:
                    health_error = None
            elif health_error is None:
                if resources is None and current_state != PipelineState.BOOTSTRAPPING:
                    health_error = self._errors.get(market)
                elif not clock_synchronized and resources is not None:
                    health_error = "ExchangeClockUnsynchronized"
                elif not metadata_fresh and resources is not None:
                    health_error = "MetadataStale"
                elif group == "futures_depth" and not book_synchronized:
                    health_error = "FuturesOrderBookUnsynchronized"
                elif group == "futures_depth" and not active_streams:
                    health_error = "WebSocketSubscriptionsNotReady"

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
                    last_update_id=last_update_id,
                    last_event_received_at_monotonic=last_event_received_at,
                    book_ticker_quotes=book_ticker_statuses,
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

        if self._config.spot_book_ticker_symbols:
            groups.append(("spot_book_ticker", "spot"))

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

    async def _record_spot_book_ticker_event(self, event) -> bool:
        """Validate and cache a timestamp-free Spot BBO event independently."""
        group = "spot_book_ticker"
        message = getattr(event, "message", None)
        if message is None or getattr(message, "kind", None) != "event":
            raise RuntimeError("Supervisor supplied an invalid BookTicker event.")
        if getattr(message, "market", None) != "spot":
            raise RuntimeError("BookTicker market does not match Spot.")
        if getattr(message, "event_type", None) is not None:
            raise RuntimeError("Spot BookTicker cannot claim a provider event type.")

        symbol = getattr(message, "symbol", None)
        if (
            not isinstance(symbol, str)
            or symbol not in self._configured_symbols_for_group(group)
        ):
            raise RuntimeError("BookTicker symbol is not configured.")
        if getattr(message, "stream", None) != f"{symbol.lower()}@bookTicker":
            raise RuntimeError("BookTicker stream does not match its symbol.")

        payload = getattr(message, "payload", None)
        required = {"u", "s", "b", "B", "a", "A"}
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise RuntimeError("BookTicker payload fields are invalid.")
        if payload.get("s") != symbol:
            raise RuntimeError("BookTicker payload symbol mismatch.")

        update_id = payload.get("u")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id <= 0:
            raise RuntimeError("BookTicker update ID is invalid.")

        prices = {}
        for field in ("b", "B", "a", "A"):
            value = payload.get(field)
            if not isinstance(value, str) or not value or len(value) > 64:
                raise RuntimeError("BookTicker decimal field is invalid.")
            try:
                number = Decimal(value)
            except (InvalidOperation, ValueError):
                raise RuntimeError("BookTicker decimal field is invalid.") from None
            if not number.is_finite() or number <= 0:
                raise RuntimeError("BookTicker decimal field must be positive.")
            prices[field] = number

        if prices["b"] > prices["a"]:
            raise RuntimeError("BookTicker bid exceeds ask.")

        received_at = getattr(event, "received_at_monotonic", None)
        if (
            isinstance(received_at, bool)
            or not isinstance(received_at, (int, float))
            or (isinstance(received_at, float) and not math.isfinite(received_at))
            or received_at < 0
            or received_at > self._monotonic()
        ):
            raise RuntimeError("Invalid BookTicker receive monotonic timestamp.")
        if getattr(event, "freshness", None) is not None:
            raise RuntimeError("BookTicker cannot claim trade freshness.")
        if getattr(event, "book_synchronized", True) is not False:
            raise RuntimeError("Spot BBO cannot claim synchronized depth.")

        cache_key = (group, symbol)
        previous = self._spot_book_ticker_quotes.get(cache_key)
        if previous is not None and update_id <= previous.update_id:
            return False

        self._spot_book_ticker_quotes[cache_key] = CachedSpotBookTicker(
            symbol=symbol,
            update_id=update_id,
            received_at_monotonic=float(received_at),
            payload=payload,
        )
        # A new validated event permits health_snapshot() to evaluate the
        # approved local monotonic receive-age policy for every symbol.
        self._pipeline_states[group] = PipelineState.SUBSCRIBING
        self._pipeline_errors.pop(group, None)
        self._refresh_service_state()
        return True

    async def _record_supervised_event(self, group: str, event) -> bool:
        """Validate and cache one supervised event without trusting callbacks."""

        if group == "spot_book_ticker":
            return await self._record_spot_book_ticker_event(event)

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
            synchronizer = self._futures_order_books.get(symbol)
            if synchronizer is None:
                synchronizer = FuturesOrderBookSynchronizer(
                    symbol,
                    monotonic=self._monotonic,
                )
                self._futures_order_books[symbol] = synchronizer

            try:
                synchronizer.ingest_event(payload)
            except Exception as exc:
                # The supervised raw event is independently validated and
                # already cached above. A local-book parsing/sequence failure
                # must invalidate executable depth, not reject raw cache data.
                synchronizer.invalidate(type(exc).__name__)
                self._pipeline_states[group] = PipelineState.UNSYNCED
                self._pipeline_errors[group] = type(exc).__name__
                self._refresh_service_state()
                return True

            if synchronizer.book is None:
                self._schedule_futures_depth_snapshot(symbol)
            self._pipeline_states[group] = PipelineState.UNSYNCED
        else:
            self._pipeline_states[group] = PipelineState.READY

        self._pipeline_errors.pop(group, None)
        self._refresh_service_state()
        return True
    def _invalidate_futures_order_books(self, reason: str) -> None:
        """Invalidate every local Futures book and cancel pending snapshots."""

        for synchronizer in self._futures_order_books.values():
            synchronizer.invalidate(reason)

        for task in tuple(self._futures_snapshot_tasks.values()):
            if not task.done():
                task.cancel()

    def _schedule_futures_depth_snapshot(self, symbol: str) -> None:
        """Schedule at most one REST snapshot request per symbol."""

        synchronizer = self._futures_order_books.get(symbol)
        resources = self._resources.get("usdm_futures")

        if synchronizer is None or synchronizer.book is not None:
            return

        if resources is None:
            return

        provider = getattr(resources, "depth_snapshot_provider", None)
        if not callable(provider):
            self._pipeline_states["futures_depth"] = PipelineState.FAILED
            self._pipeline_errors["futures_depth"] = "DepthSnapshotProviderUnavailable"
            self._refresh_service_state()
            return

        existing = self._futures_snapshot_tasks.get(symbol)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._synchronize_futures_depth_snapshot(symbol, provider),
            name=f"futures-depth-snapshot-{symbol.lower()}",
        )
        self._futures_snapshot_tasks[symbol] = task

    async def _synchronize_futures_depth_snapshot(self, symbol: str, provider) -> None:
        """Fetch a REST snapshot off-loop and reconcile buffered WebSocket deltas."""

        task = asyncio.current_task()

        try:
            snapshot = await asyncio.to_thread(provider, symbol)

            resources = self._resources.get("usdm_futures")
            if (
                resources is None
                or getattr(resources, "depth_snapshot_provider", None) is not provider
            ):
                return

            synchronizer = self._futures_order_books.get(symbol)
            if synchronizer is None:
                return

            view = synchronizer.apply_snapshot(snapshot)
            if view is None or synchronizer.book is None:
                raise RuntimeError("FuturesOrderBookSnapshotNotSynchronized")

            # Health evaluation may promote this to READY only after it
            # independently verifies all books, metadata, clock and streams.
            self._pipeline_states["futures_depth"] = PipelineState.SUBSCRIBING
            self._pipeline_errors.pop("futures_depth", None)
            self._refresh_service_state()

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            self._pipeline_states["futures_depth"] = PipelineState.UNSYNCED
            self._pipeline_errors["futures_depth"] = type(exc).__name__
            self._refresh_service_state()

        finally:
            if self._futures_snapshot_tasks.get(symbol) is task:
                self._futures_snapshot_tasks.pop(symbol, None)

    def _make_event_callback(self, group: str):
        async def callback(event):
            await self._record_supervised_event(group, event)
        return callback

    def _make_connection_lost_callback(self, group: str):
        async def callback():
            if group == "spot_book_ticker":
                self._spot_book_ticker_quotes.clear()
                current = self._pipeline_states.get(group)
                if current not in (
                    PipelineState.FAILED,
                    PipelineState.STOPPING,
                    PipelineState.STOPPED,
                    PipelineState.DISABLED,
                ):
                    self._pipeline_states[group] = PipelineState.UNSYNCED
                    self._pipeline_errors[group] = "WebSocketConnectionLost"
                self._refresh_service_state()
            elif group == "futures_depth":
                for cache_key in tuple(self._latest_events):
                    if cache_key[0] == "futures_depth":
                        self._latest_events.pop(cache_key, None)
                self._invalidate_futures_order_books("WebSocketConnectionLost")
                current = self._pipeline_states.get(group)
                if current not in (
                    PipelineState.FAILED,
                    PipelineState.STOPPING,
                    PipelineState.STOPPED,
                    PipelineState.DISABLED,
                ):
                    self._pipeline_states[group] = PipelineState.UNSYNCED
                    self._pipeline_errors[group] = "WebSocketConnectionLost"
                self._refresh_service_state()
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
            if group_market == market and group != "spot_book_ticker"
        }

        for cache_key in tuple(self._latest_events):
            if cache_key[0] in affected_groups:
                self._latest_events.pop(cache_key, None)

        if market == "usdm_futures":
            self._invalidate_futures_order_books("FuturesMarketResourcesInvalidated")

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
                    tuple(dict.fromkeys(
                        self._config.spot_symbols
                        + self._config.spot_book_ticker_symbols
                    ))
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
                if market == "usdm_futures":
                    self._invalidate_futures_order_books("FuturesMetadataRefreshFailed")
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
                    *self._futures_snapshot_tasks.values(),
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
            self._futures_snapshot_tasks = {}
            self._futures_order_books = {}
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
                            if group in ("futures_depth", "spot_book_ticker")
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
                        "on_connection_lost": (
                            self._make_connection_lost_callback(group)
                            if group in ("spot_book_ticker", "futures_depth")
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
            futures_snapshot_tasks = tuple(self._futures_snapshot_tasks.values())

            stop_results = await asyncio.gather(
                *(supervisor.stop() for supervisor in supervisors),
                return_exceptions=True,
            )

            all_tasks = (*tasks, *clock_tasks, *metadata_tasks, *futures_snapshot_tasks)
            for task in all_tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(*all_tasks, return_exceptions=True)
            self._tasks.clear()
            self._clock_tasks.clear()
            self._metadata_tasks.clear()
            self._futures_snapshot_tasks.clear()

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
