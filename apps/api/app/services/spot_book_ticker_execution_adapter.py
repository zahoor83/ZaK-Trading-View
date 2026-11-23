"""Trusted Spot BookTicker adapter for paper-execution price validation."""

from __future__ import annotations

from typing import Literal

from app.market_data.service import (
    MarketDataService,
    SpotBookTickerExecutionContext,
)
from app.services.paper_execution_price_gate import (
    ExecutableQuote,
    ValidatedExecutionPrice,
    _validate_executable_price,
)


class SpotBookTickerExecutionAdapter:
    """Bridge verified service-owned Spot BBO context into the price gate.

    This adapter performs no network access, database operations,
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
        """Validate one Spot quote using the service-owned clock context."""
        validated, _context = self.validate_with_context(symbol, side=side)
        return validated

    def validate_with_context(
        self,
        symbol: str,
        *,
        side: Literal["buy", "sell"],
    ) -> tuple[ValidatedExecutionPrice, SpotBookTickerExecutionContext]:
        """Return price and the exact trusted metadata/clock context it used."""
        context = self._market_data.spot_book_ticker_execution_context(symbol)

        quote = ExecutableQuote(
            market_type="spot",
            symbol=context.symbol,
            source="spot_book_ticker",
            bid=context.bid,
            ask=context.ask,
            event_time=None,
            received_at=None,
            metadata_received_at=None,
            book_synchronized=None,
            sequence_contiguous=None,
            update_id=context.update_id,
            received_at_monotonic=context.received_at_monotonic,
            metadata_received_at_monotonic=(
                context.metadata_received_at_monotonic
            ),
        )

        validated = _validate_executable_price(
            quote,
            side=side,
            tick_size=context.tick_size,
            current_monotonic=context.current_monotonic,
        )
        return validated, context
