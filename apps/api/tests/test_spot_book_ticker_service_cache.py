import asyncio
from types import SimpleNamespace

import pytest

from app.market_data.config import MarketDataConfig
from app.market_data.service import MarketDataService
from app.market_data.service_models import PipelineState


def make_event(
    *,
    update_id=10,
    received_at=50.0,
    symbol="BTCUSDT",
    stream=None,
    payload=None,
    event_type=None,
    freshness=None,
    book_synchronized=False,
):
    if payload is None:
        payload = {
            "u": update_id,
            "s": symbol,
            "b": "60000.10",
            "B": "0.250",
            "a": "60000.20",
            "A": "0.400",
        }
    return SimpleNamespace(
        message=SimpleNamespace(
            kind="event",
            market="spot",
            stream=stream or f"{symbol.lower()}@bookTicker",
            event_type=event_type,
            symbol=symbol,
            payload=payload,
        ),
        received_at_monotonic=received_at,
        freshness=freshness,
        book_synchronized=book_synchronized,
    )


def make_service():
    return MarketDataService(
        MarketDataConfig(spot_book_ticker_symbols=("BTCUSDT",)),
        monotonic=lambda: 100.0,
    )


@pytest.mark.asyncio
async def test_book_ticker_is_cached_without_provider_timestamp():
    service = make_service()
    event = make_event()

    assert await service._record_supervised_event("spot_book_ticker", event)
    quote = service.latest_spot_book_ticker("BTCUSDT")

    assert quote is not None
    assert quote.update_id == 10
    assert quote.received_at_monotonic == 50.0
    assert quote.payload["b"] == "60000.10"
    assert "E" not in quote.payload
    assert service.quote_cache_size == 1
    assert service.pipeline_states["spot_book_ticker"] is PipelineState.SUBSCRIBING
    assert service.pipeline_errors.get("spot_book_ticker") is None


@pytest.mark.asyncio
async def test_duplicate_or_backwards_update_id_cannot_overwrite_quote():
    service = make_service()

    assert await service._record_supervised_event(
        "spot_book_ticker", make_event(update_id=20, received_at=60.0)
    )
    assert not await service._record_supervised_event(
        "spot_book_ticker", make_event(update_id=20, received_at=61.0)
    )
    assert not await service._record_supervised_event(
        "spot_book_ticker", make_event(update_id=19, received_at=62.0)
    )

    quote = service.latest_spot_book_ticker("BTCUSDT")
    assert quote.update_id == 20
    assert quote.received_at_monotonic == 60.0


@pytest.mark.asyncio
async def test_update_id_need_not_be_consecutive():
    service = make_service()

    assert await service._record_supervised_event(
        "spot_book_ticker", make_event(update_id=20)
    )
    assert await service._record_supervised_event(
        "spot_book_ticker", make_event(update_id=27, received_at=51.0)
    )
    assert service.latest_spot_book_ticker("BTCUSDT").update_id == 27


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"u": 1, "s": "BTCUSDT", "b": "0", "B": "1", "a": "2", "A": "1"},
        {"u": 1, "s": "BTCUSDT", "b": "3", "B": "1", "a": "2", "A": "1"},
        {"u": 1, "s": "BTCUSDT", "b": "NaN", "B": "1", "a": "2", "A": "1"},
        {"u": 1, "s": "BTCUSDT", "b": "1e3", "B": "1", "a": "2", "A": "1"},
        {"u": 1, "s": "BTCUSDT", "b": "1", "B": "0", "a": "2", "A": "1"},
        {"u": 1, "s": "BTCUSDT", "b": "1", "B": "1", "a": "2", "A": "1", "E": 123},
    ],
)
async def test_invalid_bbo_payload_is_rejected(payload):
    service = make_service()
    with pytest.raises(RuntimeError):
        await service._record_supervised_event(
            "spot_book_ticker", make_event(payload=payload)
        )
    assert service.quote_cache_size == 0


@pytest.mark.asyncio
async def test_invalid_event_claims_and_future_receive_time_are_rejected():
    service = make_service()

    with pytest.raises(RuntimeError, match="event type"):
        await service._record_supervised_event(
            "spot_book_ticker", make_event(event_type="bookTicker")
        )

    with pytest.raises(RuntimeError, match="receive monotonic"):
        await service._record_supervised_event(
            "spot_book_ticker", make_event(received_at=101.0)
        )

    with pytest.raises(RuntimeError, match="freshness"):
        await service._record_supervised_event(
            "spot_book_ticker", make_event(freshness=object())
        )

    assert service.quote_cache_size == 0


@pytest.mark.asyncio
async def test_connection_loss_invalidates_only_spot_book_ticker_cache():
    service = make_service()
    service._pipeline_states["spot_book_ticker"] = PipelineState.SUBSCRIBING

    assert await service._record_supervised_event(
        "spot_book_ticker", make_event()
    )
    callback = service._make_connection_lost_callback("spot_book_ticker")
    await callback()

    assert service.latest_spot_book_ticker("BTCUSDT") is None
    assert service.quote_cache_size == 0
    assert service.pipeline_states["spot_book_ticker"] is PipelineState.UNSYNCED
    assert service.pipeline_errors["spot_book_ticker"] == "WebSocketConnectionLost"


@pytest.mark.asyncio
async def test_connection_loss_callback_does_not_clear_spot_trade_cache():
    service = make_service()
    service._latest_events[("spot", "BTCUSDT")] = object()
    service._spot_book_ticker_quotes[("spot_book_ticker", "BTCUSDT")] = (
        await _cached_quote(service)
    )

    await service._make_connection_lost_callback("spot_book_ticker")()

    assert ("spot", "BTCUSDT") in service._latest_events
    assert service.quote_cache_size == 0


async def _cached_quote(service):
    from app.market_data.service import CachedSpotBookTicker
    return CachedSpotBookTicker(
        symbol="BTCUSDT",
        update_id=10,
        received_at_monotonic=50.0,
        payload={
            "u": 10,
            "s": "BTCUSDT",
            "b": "1",
            "B": "1",
            "a": "2",
            "A": "1",
        },
    )


def test_quote_accessor_rejects_unconfigured_symbol():
    service = make_service()
    with pytest.raises(ValueError, match="not configured"):
        service.latest_spot_book_ticker("ETHUSDT")


def test_clear_event_cache_clears_both_cache_types():
    service = make_service()
    service._latest_events[("spot", "BTCUSDT")] = object()
    service._spot_book_ticker_quotes[("spot_book_ticker", "BTCUSDT")] = (
        asyncio.run(_cached_quote(service))
    )

    service.clear_event_cache()

    assert service.event_cache_size == 0
    assert service.quote_cache_size == 0
