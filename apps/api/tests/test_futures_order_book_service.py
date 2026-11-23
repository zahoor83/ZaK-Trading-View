from types import SimpleNamespace

import pytest

from app.market_data.config import MarketDataConfig
from app.market_data.service import MarketDataService
from app.market_data.service_models import PipelineState


def make_depth_event():
    payload = {
        "e": "depthUpdate",
        "E": 1800000000001,
        "T": 1800000000001,
        "s": "BTCUSDT",
        "U": 99,
        "u": 101,
        "pu": 98,
        "b": [["99", "1"]],
        "a": [["101", "1"]],
    }
    return SimpleNamespace(
        message=SimpleNamespace(
            kind="event",
            market="usdm_futures",
            event_type="depthUpdate",
            symbol="BTCUSDT",
            stream="btcusdt@depth",
            payload=payload,
        ),
        received_at_monotonic=100.0,
        freshness=None,
        book_synchronized=False,
    )


@pytest.mark.asyncio
async def test_depth_event_is_reconciled_with_rest_snapshot():
    calls = []

    def snapshot_provider(symbol):
        calls.append(symbol)
        return {
            "lastUpdateId": 100,
            "E": 1800000000000,
            "T": 1800000000000,
            "bids": [["99", "2"]],
            "asks": [["101", "3"]],
        }

    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
    )
    service._resources["usdm_futures"] = SimpleNamespace(
        depth_snapshot_provider=snapshot_provider,
        clock=SimpleNamespace(is_synchronized=True),
    )

    accepted = await service._record_supervised_event(
        "futures_depth",
        make_depth_event(),
    )
    assert accepted is True

    task = service._futures_snapshot_tasks["BTCUSDT"]
    await task

    book = service._futures_order_books["BTCUSDT"].book
    assert calls == ["BTCUSDT"]
    assert book is not None
    assert book.last_update_id == 101
    assert book.book_synchronized is True
    assert book.sequence_contiguous is True
    assert service._pipeline_states["futures_depth"] is PipelineState.SUBSCRIBING


@pytest.mark.asyncio
async def test_futures_depth_disconnect_invalidates_synchronized_book():
    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
    )
    service._resources["usdm_futures"] = SimpleNamespace(
        depth_snapshot_provider=lambda symbol: {
            "lastUpdateId": 100,
            "E": 1800000000000,
            "T": 1800000000000,
            "bids": [["99", "2"]],
            "asks": [["101", "3"]],
        },
        clock=SimpleNamespace(is_synchronized=True),
    )

    await service._record_supervised_event("futures_depth", make_depth_event())
    await service._futures_snapshot_tasks["BTCUSDT"]

    synchronizer = service._futures_order_books["BTCUSDT"]
    assert synchronizer.book is not None

    await service._make_connection_lost_callback("futures_depth")()

    assert synchronizer.book is None
    assert service._pipeline_states["futures_depth"] is PipelineState.UNSYNCED
    assert service._pipeline_errors["futures_depth"] == "WebSocketConnectionLost"



@pytest.mark.asyncio
async def test_snapshot_provider_failure_retries_only_after_next_depth_event():
    calls = []

    def snapshot_provider(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise RuntimeError("temporary snapshot failure")
        return {
            "lastUpdateId": 100,
            "E": 1800000000000,
            "T": 1800000000000,
            "bids": [["99", "2"]],
            "asks": [["101", "3"]],
        }

    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
    )
    service._resources["usdm_futures"] = SimpleNamespace(
        depth_snapshot_provider=snapshot_provider,
        clock=SimpleNamespace(is_synchronized=True),
    )

    await service._record_supervised_event("futures_depth", make_depth_event())
    first_task = service._futures_snapshot_tasks["BTCUSDT"]
    await first_task

    assert calls == ["BTCUSDT"]
    assert "BTCUSDT" not in service._futures_snapshot_tasks
    assert service._futures_order_books["BTCUSDT"].book is None
    assert service._pipeline_states["futures_depth"] is PipelineState.UNSYNCED

    next_event = make_depth_event()
    next_event.message.payload.update({"U": 102, "u": 103, "pu": 101})
    next_event.message.payload["b"] = [["99", "4"]]

    await service._record_supervised_event("futures_depth", next_event)
    second_task = service._futures_snapshot_tasks["BTCUSDT"]
    await second_task

    book = service._futures_order_books["BTCUSDT"].book
    assert calls == ["BTCUSDT", "BTCUSDT"]
    assert book is not None
    assert book.last_update_id == 103
    assert book.book_synchronized is True
    assert book.sequence_contiguous is True


@pytest.mark.asyncio
async def test_snapshot_bridge_failure_does_not_start_automatic_retry():
    calls = []

    def snapshot_provider(symbol):
        calls.append(symbol)
        return {
            "lastUpdateId": 1000,
            "E": 1800000000000,
            "T": 1800000000000,
            "bids": [["99", "2"]],
            "asks": [["101", "3"]],
        }

    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
    )
    service._resources["usdm_futures"] = SimpleNamespace(
        depth_snapshot_provider=snapshot_provider,
        clock=SimpleNamespace(is_synchronized=True),
    )

    await service._record_supervised_event("futures_depth", make_depth_event())
    task = service._futures_snapshot_tasks["BTCUSDT"]
    await task

    assert calls == ["BTCUSDT"]
    assert "BTCUSDT" not in service._futures_snapshot_tasks
    assert service._futures_order_books["BTCUSDT"].book is None
    assert service._pipeline_states["futures_depth"] is PipelineState.UNSYNCED

    # A failed reconciliation must not spin up another REST request by itself.
    await __import__("asyncio").sleep(0)
    assert calls == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_multiple_futures_symbols_synchronize_independently():
    calls = []

    def snapshot_provider(symbol):
        calls.append(symbol)
        return {
            "lastUpdateId": 100,
            "E": 1800000000000,
            "T": 1800000000000,
            "bids": [["99", "2"]],
            "asks": [["101", "3"]],
        }

    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT", "ETHUSDT")),
    )
    service._resources["usdm_futures"] = SimpleNamespace(
        depth_snapshot_provider=snapshot_provider,
        clock=SimpleNamespace(is_synchronized=True),
    )

    btc_event = make_depth_event()
    eth_event = make_depth_event()
    eth_event.message.symbol = "ETHUSDT"
    eth_event.message.stream = "ethusdt@depth"
    eth_event.message.payload["s"] = "ETHUSDT"

    await service._record_supervised_event("futures_depth", btc_event)
    await service._record_supervised_event("futures_depth", eth_event)

    btc_task = service._futures_snapshot_tasks["BTCUSDT"]
    eth_task = service._futures_snapshot_tasks["ETHUSDT"]

    import asyncio
    await asyncio.gather(btc_task, eth_task)

    assert sorted(calls) == ["BTCUSDT", "ETHUSDT"]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        book = service._futures_order_books[symbol].book
        assert book is not None
        assert book.symbol == symbol
        assert book.book_synchronized is True
        assert book.sequence_contiguous is True


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_snapshot_without_republishing_book():
    import asyncio
    import threading

    provider_started = threading.Event()
    provider_release = threading.Event()

    def snapshot_provider(symbol):
        provider_started.set()
        provider_release.wait(timeout=5)
        return {
            "lastUpdateId": 100,
            "E": 1800000000000,
            "T": 1800000000000,
            "bids": [["99", "2"]],
            "asks": [["101", "3"]],
        }

    service = MarketDataService(
        MarketDataConfig(futures_depth_symbols=("BTCUSDT",)),
    )
    service._resources["usdm_futures"] = SimpleNamespace(
        depth_snapshot_provider=snapshot_provider,
        clock=SimpleNamespace(is_synchronized=True),
    )

    try:
        await service._record_supervised_event("futures_depth", make_depth_event())
        task = service._futures_snapshot_tasks["BTCUSDT"]

        started = await asyncio.to_thread(provider_started.wait, 2)
        assert started is True

        await service._make_connection_lost_callback("futures_depth")()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert service._futures_order_books["BTCUSDT"].book is None
        assert "BTCUSDT" not in service._futures_snapshot_tasks
        assert service._pipeline_states["futures_depth"] is PipelineState.UNSYNCED
        assert service._pipeline_errors["futures_depth"] == "WebSocketConnectionLost"
    finally:
        provider_release.set()
