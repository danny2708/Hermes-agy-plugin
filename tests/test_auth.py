"""Unit tests for HTTP bearer token verification."""

import unittest
import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import agy_bridge


class DummyHandler:
    def __init__(self, headers):
        self.headers = headers

    _is_authorized = agy_bridge.OpenAIBaseHandler._is_authorized
    _get_trusted_origin = agy_bridge.OpenAIBaseHandler._get_trusted_origin


class TestAuth(unittest.TestCase):
    def test_token_generated(self):
        token = agy_bridge.get_or_create_auth_token()
        self.assertTrue(len(token) >= 16)
        self.assertTrue(token.startswith("agy-"))

    def test_auth_authorized(self):
        token = agy_bridge.get_or_create_auth_token()
        h1 = DummyHandler({"Authorization": f"Bearer {token}"})
        self.assertTrue(h1._is_authorized())

        h2 = DummyHandler({"X-API-Key": token})
        self.assertTrue(h2._is_authorized())

    def test_auth_unauthorized(self):
        h1 = DummyHandler({})
        self.assertFalse(h1._is_authorized())

        h2 = DummyHandler({"Authorization": "Bearer wrong-token-xyz"})
        self.assertFalse(h2._is_authorized())

        # Strict security: 'local-agy' backdoor MUST be rejected
        h3 = DummyHandler({"Authorization": "Bearer local-agy"})
        self.assertFalse(h3._is_authorized())

    def test_trusted_origin_validation(self):
        # Missing origin (standard server-to-server or CLI request)
        h_none = DummyHandler({})
        self.assertIsNone(h_none._get_trusted_origin())

        # Trusted origins: local loopback
        h_lh = DummyHandler({"Origin": "http://localhost:3000"})
        self.assertEqual(h_lh._get_trusted_origin(), "http://localhost:3000")

        h_ip = DummyHandler({"Origin": "http://127.0.0.1:8080"})
        self.assertEqual(h_ip._get_trusted_origin(), "http://127.0.0.1:8080")

        h_vsc = DummyHandler({"Origin": "vscode-webview://webview-panel"})
        self.assertEqual(h_vsc._get_trusted_origin(), "vscode-webview://webview-panel")

        # Untrusted origins: remote/external domains
        h_evil = DummyHandler({"Origin": "https://evil.com"})
        self.assertIsNone(h_evil._get_trusted_origin())

        h_attacker = DummyHandler({"Origin": "http://attacker.org:8765"})
        self.assertIsNone(h_attacker._get_trusted_origin())


if __name__ == "__main__":
    unittest.main()
