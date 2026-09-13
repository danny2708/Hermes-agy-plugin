"""Unit tests for redaction of sensitive credentials in logs."""

import unittest
import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import agy_bridge


class TestRedaction(unittest.TestCase):
    def test_redact_bearer_token(self):
        text = "Request with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz and more"
        redacted = agy_bridge.redact_sensitive_content(text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", redacted)
        self.assertIn("Bearer [REDACTED_TOKEN]", redacted)

    def test_redact_openai_key(self):
        text = "Key is sk-1234567890abcdef1234567890abcdef for auth"
        redacted = agy_bridge.redact_sensitive_content(text)
        self.assertNotIn("1234567890abcdef1234567890abcdef", redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)

    def test_redact_google_key(self):
        text = "API Key: AIzaSyA1234567890123456789012345678901"
        redacted = agy_bridge.redact_sensitive_content(text)
        self.assertNotIn("AIzaSyA1234567890123456789012345678901", redacted)
        self.assertIn("[REDACTED_GOOGLE_KEY]", redacted)

    def test_redact_password_field(self):
        text = '{"username": "admin", "password": "supersecretpassword123"}'
        redacted = agy_bridge.redact_sensitive_content(text)
        self.assertNotIn("supersecretpassword123", redacted)
        self.assertIn('"password": "[REDACTED]"', redacted)

    def test_redact_private_key(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Yv+...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        redacted = agy_bridge.redact_sensitive_content(text)
        self.assertNotIn("MIIEowIBAAKCAQEA0Yv+", redacted)
        self.assertIn("[REDACTED_PRIVATE_KEY]", redacted)


if __name__ == "__main__":
    unittest.main()
