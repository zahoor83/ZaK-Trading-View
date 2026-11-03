import unittest

from app.security.egress_policy import (
    EgressPolicyViolation,
    validate_public_market_data_request,
)


class TestEgressHeaderHardening(unittest.TestCase):
    URL = "https://api.binance.com/api/v3/time"

    def test_rejects_unapproved_headers(self):
        blocked_headers = [
            {"Host": "evil.example"},
            {"Authorization": "Bearer token"},
            {"Cookie": "session=value"},
            {"X-MBX-APIKEY": "secret"},
            {"X-Custom": "value"},
        ]

        for headers in blocked_headers:
            with self.subTest(headers=headers):
                with self.assertRaises(EgressPolicyViolation):
                    validate_public_market_data_request(
                        "GET", self.URL, headers
                    )

    def test_allows_only_safe_header_values(self):
        result = validate_public_market_data_request(
            "GET",
            self.URL,
            {
                "Accept": "application/json",
                "User-Agent": "ZaK-Trading-View",
            },
        )
        self.assertEqual(result.host, "api.binance.com")

    def test_rejects_non_string_header_values(self):
        with self.assertRaises(EgressPolicyViolation):
            validate_public_market_data_request(
                "GET", self.URL, {"Accept": 123}
            )

    def test_rejects_header_line_injection(self):
        with self.assertRaises(EgressPolicyViolation):
            validate_public_market_data_request(
                "GET", self.URL, {"Accept": "application/json\r\nHost: evil.example"}
            )

    def test_rejects_duplicate_case_insensitive_headers(self):
        with self.assertRaises(EgressPolicyViolation):
            validate_public_market_data_request(
                "GET",
                self.URL,
                {"Accept": "application/json", "accept": "text/plain"},
            )


if __name__ == "__main__":
    unittest.main()
