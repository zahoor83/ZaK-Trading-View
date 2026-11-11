"""Regression tests for the public market-data egress policy."""

import unittest

from app.security.egress_policy import (
    ApprovedRequest,
    EgressPolicyViolation,
    validate_public_market_data_request,
)


class PublicMarketDataEgressPolicyTests(unittest.TestCase):

    def test_allows_approved_spot_public_endpoint(self):
        result = validate_public_market_data_request(
            "GET",
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m",
        )

        self.assertEqual(
            result,
            ApprovedRequest(
                "GET",
                "api.binance.com",
                "/api/v3/klines",
            ),
        )

    def test_allows_approved_futures_public_endpoint(self):
        result = validate_public_market_data_request(
            "GET",
            "https://fapi.binance.com/fapi/v1/exchangeInfo",
        )

        self.assertEqual(result.host, "fapi.binance.com")

    def test_allows_dedicated_spot_market_data_host(self):
        result = validate_public_market_data_request(
            "GET",
            "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m",
        )

        self.assertEqual(result.host, "data-api.binance.vision")
        self.assertEqual(result.path, "/api/v3/klines")

    def test_rejects_unapproved_path_on_dedicated_data_host(self):
        with self.assertRaises(EgressPolicyViolation):
            validate_public_market_data_request(
                "GET",
                "https://data-api.binance.vision/api/v3/order",
            )

    def test_rejects_lookalike_dedicated_data_host(self):
        with self.assertRaises(EgressPolicyViolation):
            validate_public_market_data_request(
                "GET",
                "https://data-api.binance.vision.evil.example/api/v3/klines",
            )

    def test_rejects_non_get_methods(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", ""):
            with self.subTest(method=method):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request(
                        method,
                        "https://api.binance.com/api/v3/time",
                    )

    def test_rejects_order_and_unknown_paths(self):
        paths = (
            "/api/v3/order",
            "/fapi/v1/order",
            "/api/v3/account",
            "/api/v3/openOrders",
            "/api/v3/unknown",
        )

        for path in paths:
            with self.subTest(path=path):
                host = (
                    "fapi.binance.com"
                    if path.startswith("/fapi")
                    else "api.binance.com"
                )

                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request(
                        "GET",
                        f"https://{host}{path}",
                    )

    def test_rejects_unapproved_hosts_and_lookalikes(self):
        urls = (
            "https://evil.example/api/v3/klines",
            "https://binance.com/api/v3/klines",
            "https://api.binance.com.evil.example/api/v3/klines",
            "http://api.binance.com/api/v3/klines",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request("GET", url)

    def test_rejects_nonstandard_ports_and_userinfo(self):
        urls = (
            "https://api.binance.com:8443/api/v3/time",
            "https://user@api.binance.com/api/v3/time",
            "https://user:pass@api.binance.com/api/v3/time",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request("GET", url)

    def test_rejects_credential_headers(self):
        for header in (
            "X-MBX-APIKEY",
            "Authorization",
            "Cookie",
            "Proxy-Authorization",
        ):
            with self.subTest(header=header):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request(
                        "GET",
                        "https://api.binance.com/api/v3/time",
                        headers={header: "redacted"},
                    )

    def test_rejects_signed_or_credential_query_parameters(self):
        for key in (
            "signature",
            "apiKey",
            "api_key",
            "secret",
            "recvWindow",
        ):
            with self.subTest(key=key):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request(
                        "GET",
                        f"https://api.binance.com/api/v3/time?{key}=redacted",
                    )

    def test_rejects_ambiguous_or_encoded_paths(self):
        urls = (
            "https://api.binance.com//api/v3/time",
            "https://api.binance.com/api%2fv3/time",
            "https://api.binance.com/api/v3\\time",
            "https://api.binance.com/api/v3/time#fragment",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request("GET", url)

    def test_rejects_control_characters_and_whitespace_in_raw_url(self):
        invalid_urls = (
            "https://api.binance.com/api/v3/time\n",
            "https://api.binance.com/api/v3/time\r",
            "https://api.binance.com/api/v3/\ttime",
            "\nhttps://api.binance.com/api/v3/time",
            "https://api.binance.com/api/v3/time ",
            "https://api.binance.com/api/v3/time\x7f",
            "https://api.binance.com/api/v3/time\x00",
            "https://api.binance.com/api/v3/time\x85",
            "https://api.binance.com/api/v3/time\u00a0",
        )

        for url in invalid_urls:
            with self.subTest(url=repr(url)):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request("GET", url)

    def test_rejects_invalid_inputs(self):
        invalid_inputs = (
            (None, "https://api.binance.com/api/v3/time"),
            ("GET", None),
            ("GET", ""),
            ("GET", "/api/v3/time"),
        )

        for method, url in invalid_inputs:
            with self.subTest(method=method, url=url):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request(method, url)


if __name__ == "__main__":
    unittest.main()