import ssl
import unittest
import urllib.request
from unittest.mock import patch

from app.security.public_http import _DenyRedirects, _build_opener


class PublicHttpConfigurationTests(unittest.TestCase):

    def test_environment_proxy_inheritance_is_disabled(self):
        captured = {}

        def capture_handlers(*handlers):
            captured["handlers"] = handlers
            return object()

        with patch(
            "urllib.request.build_opener",
            side_effect=capture_handlers,
        ):
            _build_opener()

        proxy_handlers = [
            handler for handler in captured["handlers"]
            if isinstance(handler, urllib.request.ProxyHandler)
        ]

        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})

    def test_tls_verification_and_redirect_handler_are_configured(self):
        captured = {}

        def capture_handlers(*handlers):
            captured["handlers"] = handlers
            return object()

        with patch(
            "urllib.request.build_opener",
            side_effect=capture_handlers,
        ):
            _build_opener()

        handlers = captured["handlers"]

        tls_handlers = [
            handler for handler in handlers
            if isinstance(handler, urllib.request.HTTPSHandler)
        ]

        redirect_handlers = [
            handler for handler in handlers
            if isinstance(handler, _DenyRedirects)
        ]

        self.assertEqual(len(tls_handlers), 1)
        self.assertEqual(len(redirect_handlers), 1)

        context = tls_handlers[0]._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


if __name__ == "__main__":
    unittest.main()
