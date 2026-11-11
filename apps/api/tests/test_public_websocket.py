import asyncio
import json
import ssl
import unittest
from unittest.mock import AsyncMock, patch

from app.security.public_websocket import (
    MAX_MESSAGE_SIZE,
    MAX_QUEUE_SIZE,
    MAX_DEFERRED_EVENTS,
    MAX_DEFERRED_BYTES,
    CLOSE_TIMEOUT,
    OPEN_TIMEOUT,
    PING_INTERVAL,
    PING_TIMEOUT,
    PublicMarketDataWebSocket,
    PublicWebSocketTransportError,
    connect_public_market_data_websocket,
)
from app.security.websocket_egress import (
    validate_public_market_data_websocket_url,
)


VALID_URL = "wss://data-stream.binance.vision/ws/btcusdt@trade"
SPOT_REQUEST = validate_public_market_data_websocket_url(VALID_URL)


class PublicWebSocketClientTests(unittest.IsolatedAsyncioTestCase):

    def make_client(self, fake_connection):
        return PublicMarketDataWebSocket(fake_connection, SPOT_REQUEST)

    async def test_connection_uses_restricted_configuration(self):
        fake_connection = AsyncMock()

        with patch(
            "app.security.public_websocket.connect",
            new=AsyncMock(return_value=fake_connection),
        ) as mock_connect:
            client = await connect_public_market_data_websocket(VALID_URL)

        mock_connect.assert_awaited_once()
        args, kwargs = mock_connect.call_args

        self.assertEqual(args[0], VALID_URL)
        self.assertIsNone(kwargs["proxy"])
        self.assertIsNone(kwargs["compression"])
        self.assertEqual(kwargs["open_timeout"], OPEN_TIMEOUT)
        self.assertEqual(kwargs["close_timeout"], CLOSE_TIMEOUT)
        self.assertEqual(kwargs["ping_interval"], PING_INTERVAL)
        self.assertEqual(kwargs["ping_timeout"], PING_TIMEOUT)
        self.assertEqual(kwargs["max_size"], MAX_MESSAGE_SIZE)
        self.assertEqual(kwargs["max_queue"], MAX_QUEUE_SIZE)

        tls_context = kwargs["ssl"]
        self.assertIsInstance(tls_context, ssl.SSLContext)
        self.assertTrue(tls_context.check_hostname)
        self.assertEqual(tls_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(client.active_streams, ("btcusdt@trade",))

    async def test_unsupported_initial_spot_stream_never_connects(self):
        invalid_url = "wss://data-stream.binance.vision/ws/btcusdt@depth"

        with patch(
            "app.security.public_websocket.connect",
            new_callable=AsyncMock,
        ) as connect_mock:
            with self.assertRaises(ValueError):
                await connect_public_market_data_websocket(invalid_url)

        connect_mock.assert_not_awaited()

    async def test_send_cancellation_closes_transport_and_clears_pending(self):
        fake_connection = AsyncMock()
        entered = asyncio.Event()

        async def blocked_send(payload):
            entered.set()
            await asyncio.Event().wait()

        fake_connection.send.side_effect = blocked_send
        client = self.make_client(fake_connection)

        task = asyncio.create_task(
            client.send(
                '{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":91}'
            )
        )

        await entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(client.pending_request_count, 0)
        self.assertTrue(client._closed)
        fake_connection.close.assert_awaited_once()

    async def test_invalid_url_never_attempts_connection(self):
        with patch(
            "app.security.public_websocket.connect",
            new=AsyncMock(),
        ) as mock_connect:
            with self.assertRaises(ValueError):
                await connect_public_market_data_websocket(
                    "wss://example.com/ws/btcusdt@trade"
                )

        mock_connect.assert_not_awaited()

    async def test_valid_subscription_is_canonicalized(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        await client.send(
            '{ "id": 1, "params": ["ethusdt@trade"], '
            '"method": "SUBSCRIBE" }'
        )

        sent = fake_connection.send.await_args.args[0]
        self.assertEqual(
            json.loads(sent),
            {
                "method": "SUBSCRIBE",
                "params": ["ethusdt@trade"],
                "id": 1,
            },
        )
        self.assertEqual(client.pending_request_count, 1)

    async def test_order_method_is_rejected_before_send(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        with self.assertRaises(ValueError):
            await client.send(
                '{"method":"order.place","params":[],"id":1}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_extra_fields_are_rejected_before_send(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        with self.assertRaises(ValueError):
            await client.send(
                '{"method":"SUBSCRIBE","params":["ethusdt@trade"],'
                '"id":1,"apiKey":"secret"}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_null_request_id_is_rejected(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        with self.assertRaises(ValueError):
            await client.send(
                '{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":null}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_duplicate_pending_request_is_rejected(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":1}'
        )

        with self.assertRaises(PublicWebSocketTransportError):
            await client.send(
                '{"method":"SUBSCRIBE","params":["bnbusdt@trade"],"id":2}'
            )

        self.assertEqual(fake_connection.send.await_count, 1)

    async def test_close_is_idempotent(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        await client.close()
        await client.close()

        fake_connection.close.assert_awaited_once()

    async def test_send_after_close_is_rejected(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        await client.close()

        with self.assertRaises(PublicWebSocketTransportError):
            await client.send(
                '{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":1}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_connection_failure_is_wrapped(self):
        with patch(
            "app.security.public_websocket.connect",
            new=AsyncMock(side_effect=OSError("simulated failure")),
        ):
            with self.assertRaises(PublicWebSocketTransportError):
                await connect_public_market_data_websocket(VALID_URL)

    async def test_list_acknowledgement_is_correlated(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = (
            '{"result":["btcusdt@trade"],"id":1}'
        )
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"LIST_SUBSCRIPTIONS","params":[],"id":1}'
        )

        result = await client.recv()

        self.assertEqual(result, '{"result":["btcusdt@trade"],"id":1}')
        self.assertEqual(client.pending_request_count, 0)
        self.assertEqual(client.active_streams, ("btcusdt@trade",))

    async def test_subscribe_acknowledgement_activates_stream(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = '{"result":null,"id":7}'
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}'
        )
        await client.recv()

        self.assertEqual(
            client.active_streams,
            ("btcusdt@trade", "ethusdt@trade"),
        )
        self.assertEqual(client.pending_request_count, 0)

    async def test_unsubscribe_acknowledgement_removes_stream(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = '{"result":null,"id":8}'
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"UNSUBSCRIBE","params":["btcusdt@trade"],"id":8}'
        )
        await client.recv()

        self.assertEqual(client.active_streams, ())
        self.assertEqual(client.pending_request_count, 0)

    async def test_unknown_acknowledgement_fails_closed(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = '{"result":null,"id":999}'
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"LIST_SUBSCRIPTIONS","params":[],"id":1}'
        )

        with self.assertRaises(PublicWebSocketTransportError):
            await client.recv()

        self.assertTrue(client._closed)
        fake_connection.close.assert_awaited_once()

    async def test_list_state_mismatch_fails_closed(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = (
            '{"result":["ethusdt@trade"],"id":1}'
        )
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"LIST_SUBSCRIPTIONS","params":[],"id":1}'
        )

        with self.assertRaises(PublicWebSocketTransportError):
            await client.recv()

        self.assertTrue(client._closed)

    async def test_provider_error_resolves_pending_without_state_change(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = (
            '{"code":-1,"msg":"Rejected","id":3}'
        )
        client = self.make_client(fake_connection)

        await client.send(
            '{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":3}'
        )
        result = await client.recv()

        self.assertEqual(result, '{"code":-1,"msg":"Rejected","id":3}')
        self.assertEqual(client.active_streams, ("btcusdt@trade",))
        self.assertEqual(client.pending_request_count, 0)

    async def test_recv_after_close_is_rejected(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        await client.close()

        with self.assertRaises(PublicWebSocketTransportError):
            await client.recv()

        fake_connection.recv.assert_not_awaited()

    async def test_context_manager_closes_connection(self):
        fake_connection = AsyncMock()
        client = self.make_client(fake_connection)

        async with client as active_client:
            self.assertIs(active_client, client)

        fake_connection.close.assert_awaited_once()


    @staticmethod
    def make_trade_event(stream="ethusdt@trade", trade_id=1):
        symbol = stream.split("@", 1)[0].upper()
        return json.dumps({
            "stream": stream,
            "data": {
                "e": "trade", "E": 1700000000000, "s": symbol,
                "t": trade_id, "p": "2000.00", "q": "0.1",
                "T": 1700000000000, "m": False, "M": True,
            },
        }, separators=(",", ":"))

    async def test_event_before_subscribe_ack_is_deferred_and_delivered(self):
        event = self.make_trade_event()
        ack = '{"result":null,"id":7}'
        fake = AsyncMock()
        fake.recv.side_effect = [event, ack]
        client = self.make_client(fake)
        await client.send('{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}')
        self.assertEqual(await client.recv(), ack)
        self.assertEqual(client.active_streams, ("btcusdt@trade", "ethusdt@trade"))
        self.assertEqual(await client.recv(), event)
        self.assertEqual(fake.recv.await_count, 2)

    async def test_multiple_events_before_ack_preserve_fifo_order(self):
        first = self.make_trade_event(trade_id=11)
        second = self.make_trade_event(trade_id=12)
        ack = '{"result":null,"id":7}'
        fake = AsyncMock()
        fake.recv.side_effect = [first, second, ack]
        client = self.make_client(fake)
        await client.send('{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}')
        self.assertEqual(await client.recv(), ack)
        self.assertEqual(await client.recv(), first)
        self.assertEqual(await client.recv(), second)

    async def test_event_before_unsubscribe_ack_is_preserved(self):
        event = self.make_trade_event(stream="btcusdt@trade", trade_id=21)
        ack = '{"result":null,"id":8}'
        fake = AsyncMock()
        fake.recv.side_effect = [event, ack]
        client = self.make_client(fake)
        await client.send('{"method":"UNSUBSCRIBE","params":["btcusdt@trade"],"id":8}')
        self.assertEqual(await client.recv(), ack)
        self.assertEqual(client.active_streams, ())
        self.assertEqual(await client.recv(), event)

    async def test_deferred_event_count_overflow_fails_closed(self):
        event = self.make_trade_event()
        fake = AsyncMock()
        fake.recv.side_effect = [event] * (MAX_DEFERRED_EVENTS + 1)
        client = self.make_client(fake)
        await client.send('{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}')
        with self.assertRaises(PublicWebSocketTransportError):
            await client.recv()
        self.assertTrue(client._closed)
        self.assertEqual(len(client._deferred_events), 0)
        self.assertEqual(client._deferred_event_bytes, 0)
        fake.close.assert_awaited_once()

    async def test_deferred_event_byte_overflow_fails_closed(self):
        event = self.make_trade_event()
        fake = AsyncMock()
        fake.recv.return_value = event
        client = self.make_client(fake)
        await client.send('{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}')
        with patch(
            "app.security.public_websocket.MAX_DEFERRED_BYTES",
            len(event.encode("utf-8")) - 1,
        ):
            with self.assertRaises(PublicWebSocketTransportError):
                await client.recv()
        self.assertTrue(client._closed)
        self.assertEqual(len(client._deferred_events), 0)
        self.assertEqual(client._deferred_event_bytes, 0)

    async def test_rejected_subscribe_after_new_stream_event_fails_closed(self):
        event = self.make_trade_event()
        error = '{"code":-1,"msg":"Rejected","id":7}'
        fake = AsyncMock()
        fake.recv.side_effect = [event, error]
        client = self.make_client(fake)
        await client.send('{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}')
        with self.assertRaises(PublicWebSocketTransportError):
            await client.recv()
        self.assertTrue(client._closed)
        self.assertEqual(client.active_streams, ("btcusdt@trade",))

    async def test_provider_error_preserves_events_from_existing_streams(self):
        event = self.make_trade_event(stream="btcusdt@trade", trade_id=31)
        error = '{"code":-1,"msg":"Rejected","id":7}'
        fake = AsyncMock()
        fake.recv.side_effect = [event, error]
        client = self.make_client(fake)
        await client.send('{"method":"SUBSCRIBE","params":["ethusdt@trade"],"id":7}')
        self.assertEqual(await client.recv(), error)
        self.assertEqual(await client.recv(), event)
        self.assertEqual(client.active_streams, ("btcusdt@trade",))
        self.assertFalse(client._closed)

    async def test_deferred_queue_limits_are_bounded(self):
        self.assertEqual(MAX_DEFERRED_EVENTS, 16)
        self.assertEqual(MAX_DEFERRED_BYTES, 1_048_576)


if __name__ == "__main__":
    unittest.main(verbosity=2)