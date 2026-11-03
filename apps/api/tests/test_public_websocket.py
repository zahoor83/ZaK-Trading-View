
import json
import ssl
import unittest
from unittest.mock import AsyncMock, patch

from app.security.public_websocket import (
    MAX_MESSAGE_SIZE,
    MAX_QUEUE_SIZE,
    CLOSE_TIMEOUT,
    OPEN_TIMEOUT,
    PING_INTERVAL,
    PING_TIMEOUT,
    PublicMarketDataWebSocket,
    PublicWebSocketTransportError,
    connect_public_market_data_websocket,
)


VALID_URL = (
    "wss://data-stream.binance.vision/ws/btcusdt@trade"
)


class PublicWebSocketClientTests(unittest.IsolatedAsyncioTestCase):

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
        self.assertEqual(
            tls_context.verify_mode,
            ssl.CERT_REQUIRED,
        )

        self.assertEqual(client.approved_request.market, "spot")

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

        client = PublicMarketDataWebSocket(
            fake_connection,
            None,
        )

        await client.send(
            '{ "id": 1, "params": ["btcusdt@trade"], '
            '"method": "SUBSCRIBE" }'
        )

        sent = fake_connection.send.await_args.args[0]

        self.assertEqual(
            json.loads(sent),
            {
                "method": "SUBSCRIBE",
                "params": ["btcusdt@trade"],
                "id": 1,
            },
        )

    async def test_order_method_is_rejected_before_send(self):
        fake_connection = AsyncMock()
        client = PublicMarketDataWebSocket(fake_connection, None)

        with self.assertRaises(ValueError):
            await client.send(
                '{"method":"order.place","params":[],"id":1}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_extra_fields_are_rejected_before_send(self):
        fake_connection = AsyncMock()
        client = PublicMarketDataWebSocket(fake_connection, None)

        with self.assertRaises(ValueError):
            await client.send(
                '{"method":"SUBSCRIBE","params":["btcusdt@trade"],'
                '"id":1,"apiKey":"secret"}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_close_is_idempotent(self):
        fake_connection = AsyncMock()
        client = PublicMarketDataWebSocket(fake_connection, None)

        await client.close()
        await client.close()

        fake_connection.close.assert_awaited_once()

    async def test_send_after_close_is_rejected(self):
        fake_connection = AsyncMock()
        client = PublicMarketDataWebSocket(fake_connection, None)

        await client.close()

        with self.assertRaises(PublicWebSocketTransportError):
            await client.send(
                '{"method":"SUBSCRIBE","params":["btcusdt@trade"],"id":1}'
            )

        fake_connection.send.assert_not_awaited()

    async def test_connection_failure_is_wrapped(self):
        with patch(
            "app.security.public_websocket.connect",
            new=AsyncMock(side_effect=OSError("simulated failure")),
        ):
            with self.assertRaises(PublicWebSocketTransportError):
                await connect_public_market_data_websocket(VALID_URL)

    async def test_recv_delegates_to_connection(self):
        fake_connection = AsyncMock()
        fake_connection.recv.return_value = '{"result": null, "id": 1}'

        client = PublicMarketDataWebSocket(fake_connection, None)

        result = await client.recv()

        self.assertEqual(result, '{"result": null, "id": 1}')
        fake_connection.recv.assert_awaited_once()

    async def test_recv_after_close_is_rejected(self):
        fake_connection = AsyncMock()
        client = PublicMarketDataWebSocket(fake_connection, None)

        await client.close()

        with self.assertRaises(PublicWebSocketTransportError):
            await client.recv()

        fake_connection.recv.assert_not_awaited()

    async def test_context_manager_closes_connection(self):
        fake_connection = AsyncMock()
        client = PublicMarketDataWebSocket(fake_connection, None)

        async with client as active_client:
            self.assertIs(active_client, client)

        fake_connection.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
