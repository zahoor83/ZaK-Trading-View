import io
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.security.egress_policy import EgressPolicyViolation, validate_public_market_data_request
from app.security.rate_limit import RateLimitCoordinator
from app.security.rate_limit_store import SQLiteRateLimitStore
from app.security.public_http import (
    PublicHttpError,
    PublicHttpResponse,
    _DenyRedirects,
    fetch_public_market_data,
)


class PublicHttpTransportTests(unittest.TestCase):
    URL = "https://api.binance.com/api/v3/time"

    def setUp(self):
        from app.security import public_http as public_http_module
        self._coordinator_factory = public_http_module._get_default_rate_limit_coordinator
        self._temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self._temporary_directory.name) / "rate_limit.sqlite3"
        self.coordinator = RateLimitCoordinator(SQLiteRateLimitStore(database_path))
        self._default_coordinator_patch = patch(
            "app.security.public_http._get_default_rate_limit_coordinator",
            return_value=self.coordinator,
        )
        self._default_coordinator_patch.start()
        self.addCleanup(self._default_coordinator_patch.stop)
        self.addCleanup(self._temporary_directory.cleanup)

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

    def test_preserves_rate_limit_http_error_metadata(self):
        error = urllib.error.HTTPError(
            self.URL,
            429,
            "Too Many Requests",
            {"Retry-After": "3", "Content-Type": "application/json"},
            io.BytesIO(b'{"code":-1003,"msg":"Too many requests"}'),
        )

        opener = MagicMock()
        opener.open.side_effect = error

        with patch(
            "app.security.public_http._build_opener",
            return_value=opener,
        ):
            with self.assertRaises(PublicHttpError) as context:
                fetch_public_market_data(self.URL)

        result = context.exception
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.retry_after, "3")
        self.assertEqual(
            result.body,
            b'{"code":-1003,"msg":"Too many requests"}',
        )
        self.assertTrue(
            any(name.lower() == "content-type" for name, _ in result.headers)
        )
        self.assertIsInstance(result, EgressPolicyViolation)

    def test_rejects_oversized_http_error_body(self):
        error = urllib.error.HTTPError(
            self.URL,
            429,
            "Too Many Requests",
            {"Retry-After": "3"},
            io.BytesIO(b"123456789"),
        )

        opener = MagicMock()
        opener.open.side_effect = error

        with patch(
            "app.security.public_http._build_opener",
            return_value=opener,
        ):
            with self.assertRaises(EgressPolicyViolation):
                fetch_public_market_data(
                    self.URL,
                    max_response_bytes=8,
                )

    def test_rate_limit_guard_blocks_before_request_or_opener_creation(self):
        approved = validate_public_market_data_request("GET", self.URL)
        self.coordinator.record_response(
            approved, status_code=429, headers=(("Retry-After", "60"),)
        )

        with patch("app.security.public_http.urllib.request.Request") as request_factory:
            with patch("app.security.public_http._build_opener") as build_opener:
                with self.assertRaises(EgressPolicyViolation):
                    fetch_public_market_data(self.URL)
        request_factory.assert_not_called()
        build_opener.assert_not_called()

    def test_rate_limit_store_failure_blocks_before_dispatch(self):
        with patch.object(
            self.coordinator._store,
            "get_state",
            side_effect=OSError("simulated database read failure"),
        ):
            with patch("app.security.public_http.urllib.request.Request") as request_factory:
                with patch("app.security.public_http._build_opener") as build_opener:
                    with self.assertRaises(EgressPolicyViolation):
                        fetch_public_market_data(self.URL)
        request_factory.assert_not_called()
        build_opener.assert_not_called()

    def test_normal_response_object_429_persists_provider_cooldown(self):
        opener, response = self.make_opener(body=b'{"error":"limited"}', status=429)
        response.headers.items.return_value = [("Retry-After", "45")]
        with patch("app.security.public_http._build_opener", return_value=opener):
            with self.assertRaises(PublicHttpError):
                fetch_public_market_data(self.URL)

        approved = validate_public_market_data_request("GET", self.URL)
        state = self.coordinator._store.get_state("spot")
        self.assertIsNotNone(state)
        self.assertEqual(state.state, "cooldown")
        self.assertEqual(state.status_code, 429)
        with self.assertRaises(EgressPolicyViolation):
            fetch_public_market_data(self.URL)

    def test_http_error_418_with_missing_retry_after_persists_indefinite_block(self):
        error = urllib.error.HTTPError(
            self.URL, 418, "I'm a teapot", {"Content-Type": "application/json"},
            io.BytesIO(b'{"code":-1003}'),
        )
        opener = MagicMock()
        opener.open.side_effect = error
        with patch("app.security.public_http._build_opener", return_value=opener):
            with self.assertRaises(PublicHttpError):
                fetch_public_market_data(self.URL)

        state = self.coordinator._store.get_state("spot")
        self.assertIsNotNone(state)
        self.assertEqual(state.state, "indefinite")
        with self.assertRaises(EgressPolicyViolation):
            fetch_public_market_data(self.URL)

    def test_oversized_429_body_still_persists_rate_limit_before_rejection(self):
        opener, response = self.make_opener(body=b"123456789", status=429)
        response.headers.items.return_value = [("Retry-After", "30")]
        with patch("app.security.public_http._build_opener", return_value=opener):
            with self.assertRaises(EgressPolicyViolation):
                fetch_public_market_data(self.URL, max_response_bytes=8)

        state = self.coordinator._store.get_state("spot")
        self.assertIsNotNone(state)
        self.assertEqual(state.status_code, 429)

    def test_spot_and_futures_rate_limit_states_are_isolated(self):
        futures_url = "https://fapi.binance.com/fapi/v1/time"
        spot_approved = validate_public_market_data_request("GET", self.URL)
        futures_approved = validate_public_market_data_request("GET", futures_url)
        self.coordinator.record_response(
            spot_approved, status_code=429, headers=(("Retry-After", "60"),)
        )
        with patch("app.security.public_http._build_opener") as build_opener:
            opener, _ = self.make_opener()
            build_opener.return_value = opener
            result = fetch_public_market_data(futures_url)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(self.coordinator._store.get_state("usdm_futures"))
        self.assertIsNotNone(self.coordinator._store.get_state("spot"))

    def test_environment_database_path_must_be_absolute(self):
        with patch.dict("os.environ", {"ZAK_TV_RATE_LIMIT_DB_PATH": "relative.sqlite3"}):
            with patch("app.security.public_http._rate_limit_coordinator", None):
                with self.assertRaises(EgressPolicyViolation):
                    self._coordinator_factory()

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
