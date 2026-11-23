"""Fail-closed Binance USD-M Futures local order-book synchronization."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable


_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS = 5.0


class FuturesOrderBookError(ValueError):
    """Raised when a Futures order book cannot be trusted."""


@dataclass(frozen=True, slots=True)
class FuturesOrderBookView:
    symbol: str
    last_update_id: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    event_time_ms: int
    transaction_time_ms: int
    received_at_monotonic: float
    book_synchronized: bool = True
    sequence_contiguous: bool = True


class FuturesOrderBookSynchronizer:
    """Reconcile buffered USD-M Futures depth events with a REST snapshot.

    This class does not perform network I/O. The caller must buffer WebSocket
    events while fetching a REST snapshot, then call apply_snapshot().
    """

    def __init__(
        self,
        symbol: str,
        *,
        max_buffered_events: int = 10_000,
        max_event_age_seconds: float = FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", symbol):
            raise ValueError("Invalid Futures symbol.")
        if (
            isinstance(max_buffered_events, bool)
            or not isinstance(max_buffered_events, int)
            or max_buffered_events < 1
        ):
            raise ValueError("max_buffered_events must be a positive integer.")
        if (
            isinstance(max_event_age_seconds, bool)
            or not isinstance(max_event_age_seconds, (int, float))
            or not math.isfinite(max_event_age_seconds)
            or max_event_age_seconds <= 0
        ):
            raise ValueError("max_event_age_seconds must be finite and positive.")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable.")

        self.symbol = symbol
        self._max_buffered_events = max_buffered_events
        self._max_event_age_seconds = float(max_event_age_seconds)
        self._monotonic = monotonic
        self._buffer: list[tuple[dict, float]] = []
        self._book: FuturesOrderBookView | None = None
        self._reason: str | None = "AwaitingSnapshot"

    @property
    def synchronized(self) -> bool:
        return self._book is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def book(self) -> FuturesOrderBookView | None:
        if self._book is None:
            return None
        if not self._is_recent(self._book.received_at_monotonic):
            self.invalidate("BookStale")
            return None
        return self._book

    def _is_recent(self, received_at: float) -> bool:
        now = self._monotonic()
        return (
            isinstance(now, (int, float))
            and not isinstance(now, bool)
            and math.isfinite(now)
            and now >= received_at
            and now - received_at <= self._max_event_age_seconds
        )

    @staticmethod
    def _integer(value, name: str, *, positive: bool = True) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or (value <= 0 if positive else value < 0)
        ):
            raise FuturesOrderBookError(f"Invalid {name}.")
        return value

    @staticmethod
    def _levels(value, name: str) -> dict[Decimal, Decimal]:
        if not isinstance(value, (list, tuple)):
            raise FuturesOrderBookError(f"Invalid {name} levels.")

        result: dict[Decimal, Decimal] = {}
        for level in value:
            if (
                not isinstance(level, (list, tuple))
                or len(level) != 2
                or not all(isinstance(item, str) for item in level)
            ):
                raise FuturesOrderBookError(f"Invalid {name} level.")

            price_text, quantity_text = level
            if (
                not _DECIMAL_PATTERN.fullmatch(price_text)
                or not _DECIMAL_PATTERN.fullmatch(quantity_text)
            ):
                raise FuturesOrderBookError(f"Invalid {name} decimal.")

            try:
                price = Decimal(price_text)
                quantity = Decimal(quantity_text)
            except InvalidOperation as exc:
                raise FuturesOrderBookError(f"Invalid {name} decimal.") from exc

            if (
                not price.is_finite()
                or not quantity.is_finite()
                or price <= 0
                or quantity < 0
                or price in result
            ):
                raise FuturesOrderBookError(f"Invalid or duplicate {name} price.")

            result[price] = quantity

        return result

    def _parse_event(self, payload: Mapping) -> tuple[dict, float]:
        if not isinstance(payload, Mapping) or payload.get("s") != self.symbol:
            raise FuturesOrderBookError("Depth event symbol mismatch.")

        event = dict(payload)
        self._integer(event.get("U"), "first update ID")
        self._integer(event.get("u"), "final update ID")
        self._integer(event.get("pu"), "previous final update ID", positive=False)
        self._integer(event.get("E"), "event timestamp", positive=False)
        self._integer(event.get("T"), "transaction timestamp", positive=False)

        if event["U"] > event["u"] or event["pu"] > event["u"]:
            raise FuturesOrderBookError("Invalid Futures depth update range.")

        event["_bids"] = self._levels(event.get("b"), "bid")
        event["_asks"] = self._levels(event.get("a"), "ask")

        received_at = self._monotonic()
        if (
            isinstance(received_at, bool)
            or not isinstance(received_at, (int, float))
            or not math.isfinite(received_at)
            or received_at < 0
        ):
            raise FuturesOrderBookError("Invalid event receive timestamp.")

        return event, float(received_at)

    def invalidate(self, reason: str = "Invalidated") -> None:
        self._book = None
        self._buffer.clear()
        self._reason = reason

    def ingest_event(self, payload: Mapping) -> bool:
        """Buffer an event or apply it to an already synchronized book.

        Returns True only when an event is accepted. Any sequence failure
        invalidates the book and preserves the incoming event for a new snapshot.
        """
        try:
            event, received_at = self._parse_event(payload)
        except Exception:
            # A malformed event makes an already-published sequence untrustworthy.
            self.invalidate("InvalidDepthEvent")
            raise

        if self._book is None:
            self._append_buffer(event, received_at)
            return True

        if not self._is_recent(received_at):
            self.invalidate("DepthEventStale")
            return False

        current = self._book.last_update_id

        if event["u"] <= current:
            self.invalidate("StaleOrDuplicateDepthEvent")
            return False

        if event["pu"] != current:
            self.invalidate("SequenceGap")
            self._append_buffer(event, received_at)
            return False

        bids = dict(self._book.bids)
        asks = dict(self._book.asks)
        self._apply_levels(bids, event["_bids"])
        self._apply_levels(asks, event["_asks"])

        if not self._valid_book(bids, asks):
            self.invalidate("InvalidUpdatedBook")
            return False

        self._book = self._make_view(
            event["u"], bids, asks, event["E"], event["T"], received_at
        )
        self._reason = None
        return True

    def _append_buffer(self, event: dict, received_at: float) -> None:
        if len(self._buffer) >= self._max_buffered_events:
            self.invalidate("EventBufferOverflow")
            raise FuturesOrderBookError("Futures depth event buffer overflow.")
        self._buffer.append((event, received_at))

    @staticmethod
    def _apply_levels(book: dict, changes: Mapping[Decimal, Decimal]) -> None:
        for price, quantity in changes.items():
            if quantity == 0:
                book.pop(price, None)
            else:
                book[price] = quantity

    @staticmethod
    def _valid_book(bids: Mapping, asks: Mapping) -> bool:
        return bool(bids and asks) and max(bids) < min(asks)

    def _make_view(
        self,
        update_id: int,
        bids: Mapping,
        asks: Mapping,
        event_time: int,
        transaction_time: int,
        received_at: float,
    ) -> FuturesOrderBookView:
        return FuturesOrderBookView(
            symbol=self.symbol,
            last_update_id=update_id,
            bids=tuple(sorted(bids.items(), reverse=True)),
            asks=tuple(sorted(asks.items())),
            event_time_ms=event_time,
            transaction_time_ms=transaction_time,
            received_at_monotonic=received_at,
        )

    def apply_snapshot(self, snapshot: Mapping) -> FuturesOrderBookView:
        """Apply a validated REST snapshot and reconcile buffered depth events."""
        if not isinstance(snapshot, Mapping):
            self.invalidate("InvalidSnapshot")
            raise FuturesOrderBookError("Invalid Futures depth snapshot.")

        try:
            last_id = self._integer(snapshot.get("lastUpdateId"), "snapshot update ID")
            bids = self._levels(snapshot.get("bids"), "snapshot bid")
            asks = self._levels(snapshot.get("asks"), "snapshot ask")
            event_time = self._integer(snapshot.get("E"), "snapshot event timestamp", positive=False)
            transaction_time = self._integer(snapshot.get("T"), "snapshot transaction timestamp", positive=False)

            if not self._valid_book(bids, asks):
                raise FuturesOrderBookError("Crossed or empty Futures snapshot.")

            buffered = list(self._buffer)
            self._buffer.clear()

            # Ignore buffered events already covered by the REST snapshot.
            candidates = [
                (event, received)
                for event, received in buffered
                if event["u"] >= last_id
            ]

            # Binance local-book rule: after dropping events already covered
            # by the snapshot, the FIRST remaining event must bridge last_id.
            # Searching later events could silently skip a sequence gap.
            if not candidates:
                raise FuturesOrderBookError("Snapshot has no buffered sequence bridge.")

            first_event, _ = candidates[0]
            if not (first_event["U"] <= last_id <= first_event["u"]):
                raise FuturesOrderBookError("First buffered event does not bridge snapshot.")

            current_id = last_id
            last_event_time = event_time
            last_transaction_time = transaction_time
            last_received = self._monotonic()

            for index, (event, received_at) in enumerate(candidates):
                if not self._is_recent(received_at):
                    raise FuturesOrderBookError("Buffered depth event is stale.")
                if index > 0 and event["pu"] != current_id:
                    raise FuturesOrderBookError("Buffered Futures sequence gap.")

                self._apply_levels(bids, event["_bids"])
                self._apply_levels(asks, event["_asks"])

                if not self._valid_book(bids, asks):
                    raise FuturesOrderBookError("Updated Futures book is invalid.")

                current_id = event["u"]
                last_event_time = event["E"]
                last_transaction_time = event["T"]
                last_received = received_at

            view = self._make_view(
                current_id,
                bids,
                asks,
                last_event_time,
                last_transaction_time,
                last_received,
            )
            self._book = view
            self._reason = None
            return view

        except Exception:
            self.invalidate("SnapshotReconciliationFailed")
            raise
