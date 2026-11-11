import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from app.security.public_http import PublicHttpError
from app.security.egress_policy import EgressPolicyViolation
from app.market_data.binance_spot_rest import (
    BinanceSpotRestClient,
    MarketDataResponseError,
    BASE_URL,
)


class BinanceSpotRestTests(unittest.TestCase):

    def setUp(self):
        self.client = BinanceSpotRestClient()

    def mock_response(self, payload, status=200):
        from app.security.public_http import PublicHttpResponse

        body = (
            payload if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )

        return PublicHttpResponse(
            status_code=status,
            headers=(("Content-Type", "application/json"),),
            body=body,
        )

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_server_time(self, fetch):
        fetch.return_value = self.mock_response({"serverTime": 123456})

        result = self.client.get_server_time()

        self.assertEqual(result["serverTime"], 123456)
        self.assertTrue(fetch.call_args.args[0].startswith(BASE_URL))

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_exchange_info(self, fetch):
        fetch.return_value = self.mock_response({
            "timezone": "UTC",
            "serverTime": 123456,
            "rateLimits": [],
            "exchangeFilters": [],
            "symbols": [{
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT"],
                "filters": [],
            }],
        })

        result = self.client.get_exchange_info("BTCUSDT")

        self.assertEqual(result["symbols"], [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "orderTypes": ["LIMIT"], "filters": []}])
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        self.assertEqual(query["symbol"], ["BTCUSDT"])

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_klines_query(self, fetch):
        fetch.return_value = self.mock_response([[1, "1", "2", "1", "1", "1", 2, "1", 1, "1", "1", "0"]])

        result = self.client.get_klines(
            "BTCUSDT", "1m", 1000, 2000, 100
        )

        self.assertEqual(result, [[1, "1", "2", "1", "1", "1", 2, "1", 1, "1", "1", "0"]])

        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)

        self.assertEqual(query["symbol"], ["BTCUSDT"])
        self.assertEqual(query["interval"], ["1m"])
        self.assertEqual(query["startTime"], ["1000"])
        self.assertEqual(query["endTime"], ["2000"])
        self.assertEqual(query["limit"], ["100"])

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_optional_time_bounds(self, fetch):
        fetch.return_value = self.mock_response([])

        self.client.get_klines("BTCUSDT", "1h")

        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)

        self.assertNotIn("startTime", query)
        self.assertNotIn("endTime", query)
        self.assertEqual(query["limit"], ["500"])

    def test_supported_intervals(self):
        self.assertIn("1M", self.client.get_supported_intervals())
        self.assertIn("1s", self.client.get_supported_intervals())

    def test_rejects_invalid_symbols(self):
        for symbol in ("btcusdt", "", "BTC/USDT", "BTC USDT", None, 123):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    self.client.get_klines(symbol, "1m")

    def test_rejects_invalid_intervals(self):
        for interval in ("1M ", "2m", "1w ", "", None):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self.client.get_klines("BTCUSDT", interval)

    def test_rejects_invalid_timestamps(self):
        for value in (-1, True, False, 1.5, "1000"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.client.get_klines(
                        "BTCUSDT", "1m", start_time=value
                    )

    def test_rejects_reversed_time_bounds(self):
        with self.assertRaises(ValueError):
            self.client.get_klines("BTCUSDT", "1m", 2000, 1000)

    def test_rejects_invalid_limits(self):
        for limit in (0, 1001, -1, True, False, 1.5, "100"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.client.get_klines(
                        "BTCUSDT", "1m", limit=limit
                    )

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_accepts_boundary_limits(self, fetch):
        fetch.return_value = self.mock_response([])

        for limit in (1, 1000):
            with self.subTest(limit=limit):
                self.client.get_klines(
                    "BTCUSDT", "1m", limit=limit
                )

        self.assertEqual(fetch.call_count, 2)

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_non_object_server_time(self, fetch):
        fetch.return_value = self.mock_response([])

        with self.assertRaises(MarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_invalid_server_timestamp(self, fetch):
        for value in (True, -1, "123", 1.5, None):
            fetch.return_value = self.mock_response(
                {"serverTime": value}
            )

            with self.subTest(value=value):
                with self.assertRaises(MarketDataResponseError):
                    self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_invalid_exchange_info(self, fetch):
        for payload in ([], {}, {"symbols": {}}, {"symbols": [1]}):
            fetch.return_value = self.mock_response(payload)

            with self.subTest(payload=payload):
                with self.assertRaises(MarketDataResponseError):
                    self.client.get_exchange_info()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_invalid_kline_response(self, fetch):
        for payload in ({}, [1], ["invalid"], None, [["short"]]):
            fetch.return_value = self.mock_response(payload)

            with self.subTest(payload=payload):
                with self.assertRaises(MarketDataResponseError):
                    self.client.get_klines("BTCUSDT", "1m")

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_invalid_utf8(self, fetch):
        fetch.return_value = self.mock_response(b"\xff")

        with self.assertRaises(MarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_malformed_json(self, fetch):
        fetch.return_value = self.mock_response(b'{"serverTime":')

        with self.assertRaises(MarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_non_finite_json_numbers(self, fetch):
        for body in (
            b'{"serverTime":NaN}',
            b'{"serverTime":Infinity}',
            b'{"serverTime":-Infinity}',
            b'{"serverTime":1e999}',
        ):
            fetch.return_value = self.mock_response(body)

            with self.subTest(body=body):
                with self.assertRaises(MarketDataResponseError):
                    self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_duplicate_json_keys(self, fetch):
        fetch.return_value = self.mock_response(
            b'{"serverTime":123,"serverTime":456}'
        )

        with self.assertRaises(MarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_rejects_unexpected_http_success_status(self, fetch):
        fetch.return_value = self.mock_response(
            {"serverTime": 123}, status=201
        )

        with self.assertRaises(MarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_preserves_provider_error(self, fetch):
        error = PublicHttpError(
            429,
            (("Retry-After", "3"),),
            b'{"code":-1003}',
        )
        fetch.side_effect = error

        with self.assertRaises(PublicHttpError) as context:
            self.client.get_server_time()

        self.assertIs(context.exception, error)
        self.assertEqual(context.exception.retry_after, "3")

    @patch("app.market_data.binance_spot_rest.fetch_public_market_data")
    def test_preserves_egress_error(self, fetch):
        error = EgressPolicyViolation("Blocked")
        fetch.side_effect = error

        with self.assertRaises(EgressPolicyViolation) as context:
            self.client.get_server_time()

        self.assertIs(context.exception, error)

    def test_rejects_invalid_client_configuration(self):
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    BinanceSpotRestClient(timeout=timeout)

        for size in (0, -1, 1.5, True):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    BinanceSpotRestClient(max_response_bytes=size)


if __name__ == "__main__":
    unittest.main()