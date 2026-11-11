"""Regression tests for the public market-data WebSocket egress policy."""

import unittest

from app.security.websocket_egress import (
    WebSocketEgressViolation,
    validate_public_market_data_websocket_url,
)


class PublicWebSocketEgressTests(unittest.TestCase):

    def test_allows_spot_raw_market_stream(self):
        result = validate_public_market_data_websocket_url(
            "wss://data-stream.binance.vision:443/ws/btcusdt@trade"
        )

        self.assertEqual(result.market, "spot")
        self.assertEqual(result.streams, ("btcusdt@trade",))

    def test_allows_spot_combined_streams(self):
        result = validate_public_market_data_websocket_url(
            "wss://data-stream.binance.vision/stream?streams=btcusdt@trade/ethusdt@trade"
        )

        self.assertEqual(len(result.streams), 2)

    def test_allows_usdm_public_raw_stream(self):
        result = validate_public_market_data_websocket_url(
            "wss://fstream.binance.com/public/ws/btcusdt@depth"
        )

        self.assertEqual(result.market, "usdm_futures")

    def test_allows_usdm_market_combined_stream(self):
        result = validate_public_market_data_websocket_url(
            "wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice"
        )

        self.assertEqual(
            result.streams,
            ("btcusdt@markPrice",),
        )

    def test_rejects_non_secure_scheme(self):
        with self.assertRaises(WebSocketEgressViolation):
            validate_public_market_data_websocket_url(
                "ws://data-stream.binance.vision/ws/btcusdt@trade"
            )

    def test_rejects_unapproved_hosts(self):
        urls = (
            "wss://example.com/ws/btcusdt@trade",
            "wss://fstream.binance.com.evil.example/public/ws/btcusdt@trade",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)

    def test_rejects_private_and_websocket_api_paths(self):
        urls = (
            "wss://fstream.binance.com/private/ws",
            "wss://fstream.binance.com/ws-fapi/v1",
            "wss://data-stream.binance.vision/ws/listenKey123",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)

    def test_rejects_authentication_query_parameters(self):
        urls = (
            "wss://fstream.binance.com/private/ws?listenKey=secret",
            "wss://fstream.binance.com/market/stream?apiKey=secret",
            "wss://data-stream.binance.vision/stream?streams=btcusdt@trade&signature=x",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)

    def test_rejects_nonstandard_ports_and_userinfo(self):
        urls = (
            "wss://data-stream.binance.vision:9443/ws/btcusdt@trade",
            "wss://user@fstream.binance.com/public/ws/btcusdt@depth",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)

    def test_rejects_invalid_or_ambiguous_streams(self):
        urls = (
            "wss://data-stream.binance.vision/ws/",
            "wss://data-stream.binance.vision/ws/BTCUSDT@trade",
            "wss://data-stream.binance.vision/stream?streams=",
            "wss://data-stream.binance.vision/stream?streams=btcusdt@trade&streams=ethusdt@trade",
            "wss://data-stream.binance.vision/stream?streams=btcusdt@trade//ethusdt@trade",
            "wss://data-stream.binance.vision/ws/btcusdt%40trade",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)

    def test_rejects_duplicate_streams(self):
        with self.assertRaises(WebSocketEgressViolation):
            validate_public_market_data_websocket_url(
                "wss://data-stream.binance.vision/stream?streams=btcusdt@trade/btcusdt@trade"
            )

    def test_rejects_control_characters_and_whitespace_in_raw_url(self):
        invalid_urls = (
            "wss://data-stream.binance.vision/ws/btcusdt@trade\n",
            "wss://data-stream.binance.vision/ws/btcusdt@trade\r",
            "wss://data-stream.binance.vision/ws/btcusdt@\ttrade",
            "\nwss://data-stream.binance.vision/ws/btcusdt@trade",
            "wss://data-stream.binance.vision/ws/btcusdt@trade ",
            "wss://data-stream.binance.vision/ws/btcusdt@trade\x7f",
            "wss://data-stream.binance.vision/ws/btcusdt@trade\x00",
            "wss://data-stream.binance.vision/ws/btcusdt@trade\x85",
            "wss://data-stream.binance.vision/ws/btcusdt@trade\u00a0",
        )

        for url in invalid_urls:
            with self.subTest(url=repr(url)):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)


    def test_rejects_futures_streams_on_wrong_endpoint_category(self):
        urls = (
            "wss://fstream.binance.com/public/ws/btcusdt@markPrice",
            "wss://fstream.binance.com/market/ws/btcusdt@depth",
            "wss://fstream.binance.com/public/stream?streams=btcusdt@depth/ethusdt@markPrice",
            "wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice/ethusdt@depth",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(WebSocketEgressViolation):
                    validate_public_market_data_websocket_url(url)



if __name__ == "__main__":
    unittest.main()