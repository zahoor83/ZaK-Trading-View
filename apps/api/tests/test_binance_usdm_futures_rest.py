import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app.security.public_http import PublicHttpError
from app.security.egress_policy import EgressPolicyViolation
from app.market_data.binance_usdm_futures_rest import (
    BinanceUsdMFuturesRestClient,
    FuturesMarketDataResponseError,
    BASE_URL,
)


class BinanceUsdMFuturesRestTests(unittest.TestCase):

    def setUp(self):
        self.client = BinanceUsdMFuturesRestClient()

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

    def exchange_fixture(self):
        return {
            "timezone": "UTC",
            "serverTime": 123456,
            "rateLimits": [],
            "exchangeFilters": [],
            "assets": [{
                "asset": "USDT",
                "marginAvailable": True,
                "autoAssetExchange": "",
            }],
            "symbols": [{
                "symbol": "BTCUSDT",
                "pair": "BTCUSDT",
                "contractType": "PERPETUAL",
                "deliveryDate": 4133404800000,
                "onboardDate": 1569398400000,
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "orderTypes": ["LIMIT", "MARKET"],
                "filters": [],
            }],
        }

    def valid_kline(self):
        return [1, "1", "2", "1", "1", "1", 2, "1", 1, "1", "1", "0"]

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_server_time(self, fetch):
        fetch.return_value = self.mock_response({"serverTime": 123456})
        result = self.client.get_server_time()
        self.assertEqual(result["serverTime"], 123456)
        self.assertTrue(fetch.call_args.args[0].startswith(BASE_URL))

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_exchange_info(self, fetch):
        fetch.return_value = self.mock_response(self.exchange_fixture())
        result = self.client.get_exchange_info()
        self.assertEqual(result["symbols"][0]["contractType"], "PERPETUAL")
        self.assertEqual(result["assets"][0]["asset"], "USDT")
        self.assertEqual(
            urlsplit(fetch.call_args.args[0]).path,
            "/fapi/v1/exchangeInfo",
        )

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_klines_query(self, fetch):
        row = self.valid_kline()
        fetch.return_value = self.mock_response([row])

        result = self.client.get_klines("BTCUSDT", "1m", 1000, 2000, 100)

        self.assertEqual(result, [row])
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        self.assertEqual(query["symbol"], ["BTCUSDT"])
        self.assertEqual(query["interval"], ["1m"])
        self.assertEqual(query["startTime"], ["1000"])
        self.assertEqual(query["endTime"], ["2000"])
        self.assertEqual(query["limit"], ["100"])

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_default_limit_and_optional_bounds(self, fetch):
        fetch.return_value = self.mock_response([])
        self.client.get_klines("BTCUSDT", "1h")

        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        self.assertEqual(query["limit"], ["500"])
        self.assertNotIn("startTime", query)
        self.assertNotIn("endTime", query)

    def test_supported_intervals(self):
        intervals = self.client.get_supported_intervals()
        self.assertIn("1m", intervals)
        self.assertIn("1M", intervals)
        self.assertNotIn("1s", intervals)

    def test_rejects_invalid_symbols(self):
        for symbol in ("btcusdt", "", "BTC/USDT", "BTC USDT", None, 123):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    self.client.get_klines(symbol, "1m")

    def test_rejects_invalid_intervals(self):
        for interval in ("1s", "2m", "1M ", "", None):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self.client.get_klines("BTCUSDT", interval)

    def test_rejects_invalid_timestamps(self):
        for value in (-1, True, False, 1.5, "1000"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.client.get_klines("BTCUSDT", "1m", start_time=value)

    def test_rejects_reversed_time_bounds(self):
        with self.assertRaises(ValueError):
            self.client.get_klines("BTCUSDT", "1m", 2000, 1000)

    def test_rejects_invalid_limits(self):
        for limit in (0, 1501, -1, True, False, 1.5, "100"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.client.get_klines("BTCUSDT", "1m", limit=limit)

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_accepts_boundary_limits(self, fetch):
        fetch.return_value = self.mock_response([])
        for limit in (1, 1500):
            with self.subTest(limit=limit):
                self.client.get_klines("BTCUSDT", "1m", limit=limit)
        self.assertEqual(fetch.call_count, 2)

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_invalid_server_time(self, fetch):
        for value in (True, -1, "123", 1.5, None):
            fetch.return_value = self.mock_response({"serverTime": value})
            with self.subTest(value=value):
                with self.assertRaises(FuturesMarketDataResponseError):
                    self.client.get_server_time()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_invalid_exchange_info(self, fetch):
        invalid_payloads = (
            [],
            {},
            {"timezone": "UTC"},
            {**self.exchange_fixture(), "assets": {}},
            {**self.exchange_fixture(), "symbols": {}},
            {
                **self.exchange_fixture(),
                "symbols": [{**self.exchange_fixture()["symbols"][0],
                             "contractType": ""}],
            },
            {
                **self.exchange_fixture(),
                "symbols": [{**self.exchange_fixture()["symbols"][0],
                             "pricePrecision": True}],
            },
            {
                **self.exchange_fixture(),
                "assets": [{**self.exchange_fixture()["assets"][0],
                            "marginAvailable": "true"}],
            },
        )

        for payload in invalid_payloads:
            fetch.return_value = self.mock_response(payload)
            with self.subTest(payload=payload):
                with self.assertRaises(FuturesMarketDataResponseError):
                    self.client.get_exchange_info()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_invalid_kline_response(self, fetch):
        valid = self.valid_kline()
        invalid_payloads = (
            {},
            [1],
            ["invalid"],
            None,
            [["short"]],
            [[True, *valid[1:]]],
            [[valid[0], 1, *valid[2:]]],
            [[*valid[:6], True, *valid[7:]]],
        )

        for payload in invalid_payloads:
            fetch.return_value = self.mock_response(payload)
            with self.subTest(payload=payload):
                with self.assertRaises(FuturesMarketDataResponseError):
                    self.client.get_klines("BTCUSDT", "1m")

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_invalid_utf8(self, fetch):
        fetch.return_value = self.mock_response(b"\xff")
        with self.assertRaises(FuturesMarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_malformed_json(self, fetch):
        fetch.return_value = self.mock_response(b'{"serverTime":')
        with self.assertRaises(FuturesMarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_non_finite_json_numbers(self, fetch):
        for body in (
            b'{"serverTime":NaN}',
            b'{"serverTime":Infinity}',
            b'{"serverTime":-Infinity}',
            b'{"serverTime":1e999}',
        ):
            fetch.return_value = self.mock_response(body)
            with self.subTest(body=body):
                with self.assertRaises(FuturesMarketDataResponseError):
                    self.client.get_server_time()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_duplicate_json_keys(self, fetch):
        fetch.return_value = self.mock_response(
            b'{"serverTime":123,"serverTime":456}'
        )
        with self.assertRaises(FuturesMarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_rejects_unexpected_http_status(self, fetch):
        fetch.return_value = self.mock_response(
            {"serverTime": 123}, status=201
        )
        with self.assertRaises(FuturesMarketDataResponseError):
            self.client.get_server_time()

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_preserves_provider_error(self, fetch):
        error = PublicHttpError(429, (("Retry-After", "3"),), b'{"code":-1003}')
        fetch.side_effect = error

        with self.assertRaises(PublicHttpError) as context:
            self.client.get_server_time()

        self.assertIs(context.exception, error)
        self.assertEqual(context.exception.retry_after, "3")

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
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
                    BinanceUsdMFuturesRestClient(timeout=timeout)

        for size in (0, -1, 1.5, True):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    BinanceUsdMFuturesRestClient(
                        max_response_bytes=size
                    )


    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_exchange_info_accepts_unicode_and_delivery_contract_symbols(self, fetch):
        payload = self.exchange_fixture()
        payload["symbols"] = [
            {**payload["symbols"][0], "symbol": "币安人生USDT", "pair": "币安人生USDT", "baseAsset": "币安人生"},
            {**payload["symbols"][0], "symbol": "BTCUSDT_261225", "pair": "BTCUSDT"},
        ]
        fetch.return_value = self.mock_response(payload)
        result = self.client.get_exchange_info()
        self.assertEqual(result["symbols"][0]["symbol"], "币安人生USDT")
        self.assertEqual(result["symbols"][1]["symbol"], "BTCUSDT_261225")

    @patch("app.market_data.binance_usdm_futures_rest.fetch_public_market_data")
    def test_exchange_info_rejects_malformed_provider_symbols(self, fetch):
        for symbol in ("BTCUSDT_", "BTCUSDT_26122X", "BTC_USDT_261225", "BTC/USDT", "BTC USDT", "_261225"):
            payload = self.exchange_fixture()
            payload["symbols"][0]["symbol"] = symbol
            fetch.return_value = self.mock_response(payload)
            with self.subTest(symbol=symbol):
                with self.assertRaises(FuturesMarketDataResponseError):
                    self.client.get_exchange_info()


if __name__ == "__main__":
    unittest.main()