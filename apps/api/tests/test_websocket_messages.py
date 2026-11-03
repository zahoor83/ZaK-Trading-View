import unittest

from app.security.websocket_messages import (
    WebSocketMessageViolation,
    validate_public_websocket_message,
)


class PublicWebSocketMessageTests(unittest.TestCase):

    def test_allows_subscribe(self):
        result = validate_public_websocket_message(
            '{"method":"SUBSCRIBE","params":["btcusdt@trade"],"id":1}'
        )
        self.assertEqual(result.method, "SUBSCRIBE")
        self.assertEqual(result.params, ("btcusdt@trade",))
        self.assertEqual(result.request_id, 1)

    def test_allows_unsubscribe(self):
        result = validate_public_websocket_message(
            b'{"method":"UNSUBSCRIBE","params":["ethusdt@depth"],"id":"req-2"}'
        )
        self.assertEqual(result.method, "UNSUBSCRIBE")
        self.assertEqual(result.request_id, "req-2")

    def test_allows_list_subscriptions(self):
        result = validate_public_websocket_message(
            '{"method":"LIST_SUBSCRIPTIONS","id":3}'
        )
        self.assertEqual(result.params, ())

    def test_rejects_order_and_unknown_methods(self):
        methods = ["order.place", "order.cancel", "LOGIN", "PING", "UNKNOWN"]
        for method in methods:
            with self.subTest(method=method):
                payload = (
                    '{"method":"' + method +
                    '","params":["btcusdt@trade"],"id":1}'
                )
                with self.assertRaises(WebSocketMessageViolation):
                    validate_public_websocket_message(payload)

    def test_rejects_private_stream_names(self):
        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message(
                '{"method":"SUBSCRIBE","params":["listenKey123"],"id":1}'
            )

    def test_rejects_extra_fields(self):
        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message(
                '{"method":"SUBSCRIBE","params":["btcusdt@trade"],"id":1,"apiKey":"secret"}'
            )

    def test_rejects_duplicate_json_keys(self):
        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message(
                '{"method":"SUBSCRIBE","method":"order.place","params":["btcusdt@trade"],"id":1}'
            )

    def test_rejects_duplicate_stream_parameters(self):
        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message(
                '{"method":"SUBSCRIBE","params":["btcusdt@trade","btcusdt@trade"],"id":1}'
            )

    def test_rejects_invalid_ids(self):
        ids = [True, -1, 2_147_483_648, "", "x" * 37, [], {}]
        for request_id in ids:
            with self.subTest(request_id=request_id):
                payload = {
                    "method": "LIST_SUBSCRIPTIONS",
                    "id": request_id,
                }
                import json
                with self.assertRaises(WebSocketMessageViolation):
                    validate_public_websocket_message(json.dumps(payload))

    def test_rejects_malformed_and_non_object_messages(self):
        for payload in ("{", "[]", '"text"', "null", "123"):
            with self.subTest(payload=payload):
                with self.assertRaises(WebSocketMessageViolation):
                    validate_public_websocket_message(payload)

    def test_rejects_invalid_params(self):
        payloads = [
            '{"method":"SUBSCRIBE","params":[],"id":1}',
            '{"method":"SUBSCRIBE","params":"btcusdt@trade","id":1}',
            '{"method":"SUBSCRIBE","params":["BTCUSDT@trade"],"id":1}',
            '{"method":"SUBSCRIBE","params":["btcusdt@trade/ethusdt@trade"],"id":1}',
            '{"method":"LIST_SUBSCRIPTIONS","params":["btcusdt@trade"],"id":1}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(WebSocketMessageViolation):
                    validate_public_websocket_message(payload)

    def test_rejects_invalid_payload_types_and_oversized_messages(self):
        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message({"method": "LIST_SUBSCRIPTIONS"})

        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message(" " * (64 * 1024 + 1))

        with self.assertRaises(WebSocketMessageViolation):
            validate_public_websocket_message(b"\xff")


if __name__ == "__main__":
    unittest.main()
