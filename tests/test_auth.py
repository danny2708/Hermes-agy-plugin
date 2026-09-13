"""Unit tests for HTTP bearer token verification."""

import unittest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "G:", "Work", "Side-Projects", "Hermes-agy-plugin")))
import agy_bridge


class DummyHandler:
    def __init__(self, headers):
        self.headers = headers

    _is_authorized = agy_bridge.OpenAIBaseHandler._is_authorized


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

        h3 = DummyHandler({"Authorization": "Bearer local-agy"})
        self.assertTrue(h3._is_authorized())

    def test_auth_unauthorized(self):
        h1 = DummyHandler({})
        self.assertFalse(h1._is_authorized())

        h2 = DummyHandler({"Authorization": "Bearer wrong-token-xyz"})
        self.assertFalse(h2._is_authorized())


if __name__ == "__main__":
    unittest.main()
