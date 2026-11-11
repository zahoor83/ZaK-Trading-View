import asyncio
from collections import deque
from types import MappingProxyType
from unittest.mock import patch

import pytest

from app.market_data.config import MarketDataConfig
from app.market_data.freshness import (
    ExchangeClock,
    FreshnessState,
    MarketDataFreshnessValidator,
)
from app.market_data.supervisor import (
    MarketDataSupervisorError,
    MarketDataWebSocketSupervisor,
)
from app.security.websocket_egress import validate_public_market_data_websocket_url
from app.security.websocket_inbound import (
    InboundWebSocketViolation,
    ValidatedInboundMessage,
)
from app.security.websocket_reconnect import (
    ReconnectError,
    ReconnectPolicy,
    ReconnectState,
)


class FakeWebSocket:
    def __init__(self, approved_request, events=()):
        self.approved_request = approved_request
        self.active_streams = tuple(sorted(approved_request.streams))
        self.events = deque(events)
        self.control_response = None
        self.sent = []
        self.closed = False

    async def send(self, payload):
        import json

        request = json.loads(payload)
        self.sent.append(request)
        self.control_response = {
            "result": list(self.active_streams),
            "id": request["id"],
        }

    async def recv(self):
        if self.control_response is not None:
            result = self.control_response
            self.control_response = None
            import json
            return json.dumps(result)

        if not self.events:
            raise OSError("No more fake events.")

        item = self.events.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


def make_freshness_validator():
    clock = ExchangeClock(
        wall_clock_ms=lambda: 1_000_000,
        monotonic=lambda: 10.0,
    )
    clock.synchronize(lambda: {"serverTime": 1_000_000})
    return MarketDataFreshnessValidator(
        clock,
        monotonic=lambda: 10.0,
    )


def make_message(
    *,
    market="spot",
    stream="btcusdt@trade",
    event_type="trade",
    timestamp=1_000_000,
):
    return ValidatedInboundMessage(
        kind="event",
        market=market,
        stream=stream,
        event_type=event_type,
        symbol="BTCUSDT",
        payload=MappingProxyType({"E": timestamp}),
    )


def make_supervisor(
    *,
    group="spot",
    callback=None,
    depth_callback=None,
    freshness=None,
    connector=None,
    policy=None,
):
    config = (
        MarketDataConfig(spot_symbols=("BTCUSDT",))
        if group == "spot"
        else (
            MarketDataConfig(futures_depth_symbols=("BTCUSDT",))
            if group == "futures_depth"
            else MarketDataConfig(futures_mark_price_symbols=("BTCUSDT",))
        )
    )

    return MarketDataWebSocketSupervisor(
        config,
        group,
        freshness_validator=freshness or (
            None if group == "futures_depth" else make_freshness_validator()
        ),
        on_event=callback,
        on_depth_event=depth_callback,
        connector=connector,
        reconnect_policy=policy,
        sleep=lambda _delay: asyncio.sleep(0),
        random_source=lambda: 0.5,
        monotonic=lambda: 10.0,
    )


@pytest.mark.asyncio
async def test_fresh_trade_is_delivered_only_as_validated_event():
    received = []
    supervisor = make_supervisor(callback=received.append)
    request = validate_public_market_data_websocket_url(
        "wss://data-stream.binance.vision/stream?streams=btcusdt@trade"
    )
    client = FakeWebSocket(request)

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(),
    ):
        await supervisor._dispatch("raw-wire-message", client, 10.0)

    assert len(received) == 1
    assert isinstance(received[0].message, ValidatedInboundMessage)
    assert received[0].freshness.state is FreshnessState.FRESH
    assert received[0].message.payload["E"] == 1_000_000


@pytest.mark.asyncio
async def test_stale_trade_is_not_delivered():
    received = []
    supervisor = make_supervisor(callback=received.append)
    request = validate_public_market_data_websocket_url(
        "wss://data-stream.binance.vision/stream?streams=btcusdt@trade"
    )
    client = FakeWebSocket(request)

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(timestamp=989_999),
    ):
        await supervisor._dispatch("raw-wire-message", client, 10.0)

    assert received == []


@pytest.mark.asyncio
async def test_unsynchronized_exchange_clock_prevents_delivery():
    received = []
    supervisor = make_supervisor(
        callback=received.append,
        freshness=MarketDataFreshnessValidator(
            ExchangeClock(
                wall_clock_ms=lambda: 1_000_000,
                monotonic=lambda: 10.0,
            ),
            monotonic=lambda: 10.0,
        ),
    )
    request = validate_public_market_data_websocket_url(
        "wss://data-stream.binance.vision/stream?streams=btcusdt@trade"
    )
    client = FakeWebSocket(request)

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(),
    ):
        await supervisor._dispatch("raw-wire-message", client, 10.0)

    assert received == []


@pytest.mark.asyncio
async def test_depth_callback_explicitly_receives_unsynchronized_book():
    received = []
    supervisor = make_supervisor(
        group="futures_depth",
        depth_callback=received.append,
    )
    request = validate_public_market_data_websocket_url(
        "wss://fstream.binance.com/public/stream?streams=btcusdt@depth"
    )
    client = FakeWebSocket(request)
    depth_message = make_message(
        market="usdm_futures",
        stream="btcusdt@depth",
        event_type="depthUpdate",
    )

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=depth_message,
    ):
        await supervisor._dispatch("raw-wire-message", client, 10.0)

    assert len(received) == 1
    assert received[0].freshness is None
    assert received[0].book_synchronized is False


@pytest.mark.asyncio
async def test_depth_is_not_published_without_explicit_depth_callback():
    supervisor = make_supervisor(group="futures_depth")
    request = validate_public_market_data_websocket_url(
        "wss://fstream.binance.com/public/stream?streams=btcusdt@depth"
    )
    client = FakeWebSocket(request)

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(
            market="usdm_futures",
            stream="btcusdt@depth",
            event_type="depthUpdate",
        ),
    ):
        await supervisor._dispatch("raw-wire-message", client, 10.0)


@pytest.mark.asyncio
async def test_inbound_violation_is_not_silently_published():
    received = []
    supervisor = make_supervisor(callback=received.append)
    request = validate_public_market_data_websocket_url(
        "wss://data-stream.binance.vision/stream?streams=btcusdt@trade"
    )
    client = FakeWebSocket(request)

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        side_effect=InboundWebSocketViolation("invalid"),
    ):
        with pytest.raises(InboundWebSocketViolation):
            await supervisor._dispatch("invalid-wire-message", client, 10.0)

    assert received == []


@pytest.mark.asyncio
async def test_connection_approval_mismatch_is_rejected():
    received = []
    supervisor = make_supervisor(callback=received.append)
    wrong_request = validate_public_market_data_websocket_url(
        "wss://data-stream.binance.vision/stream?streams=ethusdt@trade"
    )
    client = FakeWebSocket(wrong_request)

    with pytest.raises(MarketDataSupervisorError):
        await supervisor._dispatch("raw-wire-message", client, 10.0)

    assert received == []


@pytest.mark.asyncio
async def test_run_reconnects_after_disconnect_and_stops_cleanly():
    config = MarketDataConfig(spot_symbols=("BTCUSDT",))
    request = validate_public_market_data_websocket_url(
        config.spot_websocket_url
    )
    first = FakeWebSocket(request, events=[OSError("simulated disconnect")])
    second = FakeWebSocket(request, events=["raw-wire-message"])
    clients = deque([first, second])
    urls = []
    delivered = []

    async def connector(url):
        urls.append(url)
        return clients.popleft()

    supervisor = MarketDataWebSocketSupervisor(
        config,
        "spot",
        freshness_validator=make_freshness_validator(),
        connector=connector,
        sleep=lambda _delay: asyncio.sleep(0),
        random_source=lambda: 0.5,
        monotonic=lambda: 10.0,
    )

    async def callback(event):
        delivered.append(event)
        await supervisor.stop()

    supervisor._on_event = callback

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(),
    ):
        await supervisor.run()

    assert len(urls) == 2
    assert urls == [config.spot_websocket_url, config.spot_websocket_url]
    assert first.closed is True
    assert len(delivered) == 1
    assert supervisor.stopped is True
    assert supervisor.state is ReconnectState.STOPPED


@pytest.mark.asyncio
async def test_reconnect_exhaustion_propagates_failure():
    async def connector(_url):
        raise OSError("provider unavailable")

    supervisor = make_supervisor(
        connector=connector,
        policy=ReconnectPolicy(max_attempts=1),
    )

    with pytest.raises(ReconnectError):
        await supervisor.run()

    assert supervisor.state is ReconnectState.FAILED


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    supervisor = make_supervisor()
    await supervisor.stop()
    await supervisor.stop()

    assert supervisor.stopped is True
    assert supervisor.state is ReconnectState.STOPPED


def test_disabled_group_cannot_be_supervised():
    with pytest.raises(ValueError):
        MarketDataWebSocketSupervisor(
            MarketDataConfig(),
            "spot",
            freshness_validator=make_freshness_validator(),
        )


def test_unknown_group_is_rejected():
    with pytest.raises(ValueError):
        MarketDataWebSocketSupervisor(
            MarketDataConfig(),
            "unknown",
        )


@pytest.mark.asyncio
async def test_callback_failure_closes_active_connection():
    async def failing_callback(_event):
        raise RuntimeError("simulated callback failure")

    config = MarketDataConfig(spot_symbols=("BTCUSDT",))
    request = validate_public_market_data_websocket_url(
        config.spot_websocket_url
    )
    client = FakeWebSocket(request, events=["raw-wire-message"])

    async def connector(_url):
        return client

    supervisor = make_supervisor(
        callback=failing_callback,
        connector=connector,
    )

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(),
    ):
        with pytest.raises(RuntimeError, match="simulated callback failure"):
            await supervisor.run()

    assert client.closed is True
    assert supervisor.stopped is True
    assert supervisor.state is ReconnectState.STOPPED


@pytest.mark.asyncio
async def test_invalid_inbound_data_closes_connection_during_run():
    config = MarketDataConfig(spot_symbols=("BTCUSDT",))
    request = validate_public_market_data_websocket_url(
        config.spot_websocket_url
    )
    client = FakeWebSocket(request, events=["invalid-wire-message"])

    async def connector(_url):
        return client

    supervisor = make_supervisor(connector=connector)

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        side_effect=InboundWebSocketViolation("invalid inbound message"),
    ):
        with pytest.raises(
            MarketDataSupervisorError,
            match="Untrusted inbound data rejected",
        ):
            await supervisor.run()

    assert client.closed is True
    assert supervisor.stopped is True
    assert supervisor.state is ReconnectState.STOPPED

@pytest.mark.asyncio
async def test_cancellation_during_callback_closes_active_connection():
    import asyncio

    callback_started = asyncio.Event()

    async def blocking_callback(_event):
        callback_started.set()
        await asyncio.Event().wait()

    config = MarketDataConfig(spot_symbols=("BTCUSDT",))
    request = validate_public_market_data_websocket_url(
        config.spot_websocket_url
    )
    client = FakeWebSocket(request, events=["raw-wire-message"])

    async def connector(_url):
        return client

    supervisor = make_supervisor(
        callback=blocking_callback,
        connector=connector,
    )

    with patch(
        "app.market_data.supervisor.validate_public_websocket_inbound_message",
        return_value=make_message(),
    ):
        task = asyncio.create_task(supervisor.run())

        await asyncio.wait_for(callback_started.wait(), timeout=2)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    assert client.closed is True
    assert supervisor.stopped is True
    assert supervisor.state is ReconnectState.STOPPED