"""Trusted USD-M Futures depth-book adapter for paper execution."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from app.market_data.service import (
    FuturesDepthExecutionContext,
    MarketDataService,
)
from app.market_data.symbol_metadata import METADATA_MAX_AGE_SECONDS
from app.market_data.futures_order_book import (
    FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS,
)
from app.services.paper_execution_price_gate import (
    ExecutableQuote,
    ValidatedExecutionPrice,
    _validate_executable_price,
)


class FuturesDepthExecutionAdapter:
    """Bridge service-verified Futures depth context into the price gate.

    This adapter performs no network I/O, database operations,
    portfolio mutations, or exchange order execution.
    """

    def __init__(self, market_data: MarketDataService) -> None:
        if not isinstance(market_data, MarketDataService):
            raise TypeError("market_data must be a MarketDataService.")
        self._market_data = market_data

    def validate(
        self,
        symbol: str,
        *,
        side: Literal["buy", "sell"],
    ) -> ValidatedExecutionPrice:
        validated, _context = self.validate_with_context(symbol, side=side)
        return validated

    def validate_with_context(
        self,
        symbol: str,
        *,
        side: Literal["buy", "sell"],
    ) -> tuple[ValidatedExecutionPrice, FuturesDepthExecutionContext]:
        context = self._market_data.futures_depth_execution_context(symbol)

        quote = ExecutableQuote(
            market_type="futures",
            symbol=context.symbol,
            source="futures_depth_book",
            bid=context.bid,
            ask=context.ask,
            event_time=context.event_time,
            received_at=context.received_at,
            metadata_received_at=context.metadata_received_at,
            book_synchronized=True,
            sequence_contiguous=True,
            update_id=None,
            received_at_monotonic=None,
            metadata_received_at_monotonic=None,
        )

        validated = _validate_executable_price(
            quote,
            side=side,
            tick_size=context.tick_size,
            current_monotonic=context.current_monotonic,
            now=context.now,
            max_quote_age=timedelta(
                seconds=FUTURES_ORDER_BOOK_MAX_EVENT_AGE_SECONDS
            ),
            max_metadata_age=timedelta(
                seconds=METADATA_MAX_AGE_SECONDS
            ),
        )
        return validated, context
