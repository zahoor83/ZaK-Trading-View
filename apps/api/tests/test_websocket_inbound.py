import json
import unittest
from types import SimpleNamespace

from app.security.websocket_inbound import (
    InboundWebSocketViolation,
    validate_public_websocket_inbound_message,
)


SPOT = SimpleNamespace(market="spot", streams=("btcusdt@trade",))
DEPTH = SimpleNamespace(market="usdm_futures", streams=("btcusdt@depth",))
MARK = SimpleNamespace(market="usdm_futures", streams=("btcusdt@markPrice",))


def trade_event():
    return {
        "e": "trade", "E": 1700000000000, "s": "BTCUSDT",
        "t": 123, "p": "50000.00", "q": "0.01",
        "T": 1700000000000, "m": False, "M": True,
        "b": 10, "a": 11,
    }


def depth_event():
    return {
        "e": "depthUpdate", "E": 1700000000000,
        "T": 1700000000000, "s": "BTCUSDT",
        "U": 100, "u": 101, "pu": 99,
        "b": [["50000.00", "0.1"]],
        "a": [["50001.00", "0.2"]],
    }


def mark_event():
    return {
        "e": "markPriceUpdate", "E": 1700000000000,
        "s": "BTCUSDT", "p": "50000.00",
        "i": "49999.00", "P": "50010.00",
        "r": "0.000100", "T": 1700000000000,
    }


class InboundWebSocketTests(unittest.TestCase):

    def test_accepts_spot_trade(self):
        result = validate_public_websocket_inbound_message(
            json.dumps(trade_event()), SPOT
        )
        self.assertEqual(result.kind, "event")
        self.assertEqual(result.event_type, "trade")
        self.assertEqual(result.symbol, "BTCUSDT")

    def test_accepts_combined_spot_trade(self):
        payload = {"stream": "btcusdt@trade", "data": trade_event()}
        result = validate_public_websocket_inbound_message(
            json.dumps(payload), SPOT
        )
        self.assertEqual(result.stream, "btcusdt@trade")

    def test_accepts_futures_depth(self):
        result = validate_public_websocket_inbound_message(
            json.dumps(depth_event()), DEPTH
        )
        self.assertEqual(result.event_type, "depthUpdate")

    def test_accepts_futures_mark_price(self):
        result = validate_public_websocket_inbound_message(
            json.dumps(mark_event()), MARK
        )
        self.assertEqual(result.event_type, "markPriceUpdate")

    def test_accepts_live_binance_mark_price_payload(self):
        event = mark_event()
        event["ap"] = "73757.60000000"
        event["st"] = 1

        result = validate_public_websocket_inbound_message(
            json.dumps({"stream": "btcusdt@markPrice", "data": event}),
            MARK,
        )

        self.assertEqual(result.kind, "event")
        self.assertEqual(result.event_type, "markPriceUpdate")
        self.assertEqual(result.symbol, "BTCUSDT")

    def test_rejects_invalid_mark_price_ap(self):
        for value in ("NaN", "Infinity", "-1", "0", 123):
            with self.subTest(value=value):
                event = mark_event()
                event["ap"] = value
                with self.assertRaises(InboundWebSocketViolation):
                    validate_public_websocket_inbound_message(
                        json.dumps(event), MARK
                    )

    def test_rejects_invalid_mark_price_st(self):
        for value in (True, False, "1", -1, 1.5):
            with self.subTest(value=value):
                event = mark_event()
                event["st"] = value
                with self.assertRaises(InboundWebSocketViolation):
                    validate_public_websocket_inbound_message(
                        json.dumps(event), MARK
                    )

    def test_rejects_unknown_mark_price_field(self):
        event = mark_event()
        event["unexpected"] = 1
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                json.dumps(event), MARK
            )

    def test_accepts_mark_price_without_optional_fields(self):
        result = validate_public_websocket_inbound_message(
            json.dumps(mark_event()), MARK
        )
        self.assertEqual(result.kind, "event")

    def test_accepts_subscription_acknowledgement(self):
        result = validate_public_websocket_inbound_message(
            '{"result":null,"id":1}', SPOT
        )
        self.assertEqual(result.kind, "acknowledgement")

    def test_accepts_list_subscriptions_response(self):
        result = validate_public_websocket_inbound_message(
            '{"result":["btcusdt@trade"],"id":2}', SPOT
        )
        self.assertEqual(result.kind, "acknowledgement")

    def test_accepts_provider_error(self):
        result = validate_public_websocket_inbound_message(
            '{"code":-1121,"msg":"Invalid symbol.","id":3}', SPOT
        )
        self.assertEqual(result.kind, "provider_error")

    def test_rejects_duplicate_json_keys(self):
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                '{"e":"trade","e":"order","s":"BTCUSDT"}', SPOT
            )

    def test_rejects_unknown_event(self):
        event = trade_event()
        event["e"] = "unknown"
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), SPOT)

    def test_rejects_symbol_mismatch(self):
        event = trade_event()
        event["s"] = "ETHUSDT"
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), SPOT)

    def test_rejects_unapproved_combined_stream(self):
        payload = {"stream": "ethusdt@trade", "data": trade_event()}
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(payload), SPOT)

    def test_rejects_unexpected_event_field(self):
        event = trade_event()
        event["apiKey"] = "secret"
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), SPOT)

    def test_rejects_non_decimal_price(self):
        event = trade_event()
        event["p"] = "NaN"
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), SPOT)

    def test_rejects_boolean_timestamp(self):
        event = trade_event()
        event["E"] = True
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), SPOT)

    def test_rejects_invalid_trade_flags(self):
        event = trade_event()
        event["m"] = 1
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), SPOT)

    def test_rejects_invalid_depth_range(self):
        event = depth_event()
        event["U"] = 200
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), DEPTH)

    def test_rejects_invalid_depth_level(self):
        event = depth_event()
        event["b"] = [["50000.00", "-1"]]
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), DEPTH)

    def test_rejects_invalid_mark_price(self):
        event = mark_event()
        event["r"] = "Infinity"
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(json.dumps(event), MARK)

    def test_rejects_ack_with_unapproved_stream(self):
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                '{"result":["ethusdt@trade"],"id":1}', SPOT
            )

    def test_rejects_malformed_and_non_object_payloads(self):
        for payload in ("{", "[]", "null", "NaN"):
            with self.subTest(payload=payload):
                with self.assertRaises(InboundWebSocketViolation):
                    validate_public_websocket_inbound_message(payload, SPOT)

    def test_rejects_invalid_utf8(self):
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(b"\xff", SPOT)


    def test_accepts_acknowledgement_with_empty_active_streams(self):
        empty = SimpleNamespace(market="spot", streams=())
        result = validate_public_websocket_inbound_message(
            '{"result":null,"id":7}', empty
        )
        self.assertEqual(result.kind, "acknowledgement")

    def test_rejects_event_with_empty_active_streams(self):
        empty = SimpleNamespace(market="spot", streams=())
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                json.dumps(trade_event()), empty
            )

    def test_rejects_list_response_for_inactive_supported_stream(self):
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                '{"result":["ethusdt@trade"],"id":8}', SPOT
            )

    def test_accepts_empty_list_response_with_empty_active_streams(self):
        empty = SimpleNamespace(market="spot", streams=())
        result = validate_public_websocket_inbound_message(
            '{"result":[],"id":8}', empty
        )
        self.assertEqual(result.kind, "acknowledgement")

    def test_rejects_unsupported_market_stream_in_list_response(self):
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                '{"result":["btcusdt@depth"],"id":8}', SPOT
            )

    def test_raw_futures_events_disambiguate_by_event_type(self):
        both = SimpleNamespace(
            market="usdm_futures",
            streams=("btcusdt@depth", "btcusdt@markPrice"),
        )
        depth = validate_public_websocket_inbound_message(
            json.dumps(depth_event()), both
        )
        mark = validate_public_websocket_inbound_message(
            json.dumps(mark_event()), both
        )
        self.assertEqual(depth.stream, "btcusdt@depth")
        self.assertEqual(mark.stream, "btcusdt@markPrice")
    def test_rejects_invalid_context(self):
        invalid = SimpleNamespace(market="spot", streams=[])
        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                json.dumps(trade_event()), invalid
            )


    def test_accepts_negative_funding_rate(self):
        event = mark_event()
        event["r"] = "-0.000100"

        result = validate_public_websocket_inbound_message(
            json.dumps(event), MARK
        )
        self.assertEqual(result.kind, "event")

    def test_rejects_negative_mark_price(self):
        event = mark_event()
        event["p"] = "-50000.00"

        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                json.dumps(event), MARK
            )

    def test_rejects_negative_depth_quantity(self):
        event = depth_event()
        event["b"] = [["50000.00", "-0.1"]]

        with self.assertRaises(InboundWebSocketViolation):
            validate_public_websocket_inbound_message(
                json.dumps(event), DEPTH
            )

    def test_validated_event_is_deeply_immutable(self):
        result = validate_public_websocket_inbound_message(
            json.dumps(depth_event()), DEPTH
        )

        with self.assertRaises(TypeError):
            result.payload["s"] = "ETHUSDT"

        with self.assertRaises(TypeError):
            result.payload["b"][0][0] = "1"

        with self.assertRaises(AttributeError):
            result.payload["b"].append(["1", "1"])

    def test_acknowledgement_result_is_immutable(self):
        result = validate_public_websocket_inbound_message(
            '{"result":["btcusdt@trade"],"id":2}', SPOT
        )

        with self.assertRaises(TypeError):
            result.payload["id"] = 3

        with self.assertRaises(AttributeError):
            result.payload["result"].append("ethusdt@trade")

    def test_provider_error_payload_is_immutable(self):
        result = validate_public_websocket_inbound_message(
            '{"code":-1121,"msg":"Invalid symbol.","id":3}', SPOT
        )

        with self.assertRaises(TypeError):
            result.payload["msg"] = "changed"

if __name__ == "__main__":
    unittest.main(verbosity=2)
