import unittest
import urllib.request
from unittest.mock import MagicMock, patch

from app.security.egress_policy import EgressPolicyViolation
from app.security.public_http import (
    PublicHttpResponse,
    _DenyRedirects,
    fetch_public_market_data,
)


class PublicHttpTransportTests(unittest.TestCase):
    URL = "https://api.binance.com/api/v3/time"

    def make_opener(self, body=b'{"serverTime":123}', status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = status
        response.read.return_value = body
        response.headers.items.return_value = [
            ("Content-Type", "application/json")
        ]

        opener = MagicMock()
        opener.open.return_value = response
        return opener, response

    def test_sends_only_validated_get_request(self):
        opener, _ = self.make_opener()

        with patch(
            "app.security.public_http._build_opener",
            return_value=opener,
        ):
            result = fetch_public_market_data(
                self.URL,
                headers={"Accept": "application/json"},
            )

        self.assertIsInstance(result, PublicHttpResponse)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.body, b'{"serverTime":123}')

        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, self.URL)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10.0)

    def test_rejects_unapproved_url_before_transport_creation(self):
        with patch(
            "app.security.public_http._build_opener"
        ) as build_opener:
            with self.assertRaises(EgressPolicyViolation):
                fetch_public_market_data(
                    "https://example.com/collect"
                )

        build_opener.assert_not_called()

    def test_rejects_redirects(self):
        handler = _DenyRedirects()
        request = urllib.request.Request(self.URL)

        with self.assertRaises(EgressPolicyViolation):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.com/redirect",
            )

    def test_rejects_oversized_response(self):
        opener, _ = self.make_opener(body=b"123456789")

        with patch(
            "app.security.public_http._build_opener",
            return_value=opener,
        ):
            with self.assertRaises(EgressPolicyViolation):
                fetch_public_market_data(
                    self.URL,
                    max_response_bytes=8,
                )

    def test_rejects_invalid_timeout(self):
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(EgressPolicyViolation):
                    fetch_public_market_data(
                        self.URL,
                        timeout=timeout,
                    )

    def test_rejects_invalid_response_size_limit(self):
        for limit in (0, -1, 1.5, True):
            with self.subTest(limit=limit):
                with self.assertRaises(EgressPolicyViolation):
                    fetch_public_market_data(
                        self.URL,
                        max_response_bytes=limit,
                    )

    def test_rejects_non_success_status(self):
        opener, _ = self.make_opener(status=404)

        with patch(
            "app.security.public_http._build_opener",
            return_value=opener,
        ):
            with self.assertRaises(EgressPolicyViolation):
                fetch_public_market_data(self.URL)


if __name__ == "__main__":
    unittest.main()
