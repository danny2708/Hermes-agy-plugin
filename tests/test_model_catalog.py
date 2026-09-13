"""Unit tests for model indexing and resolution."""

import unittest
import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import agy_bridge


class TestModelCatalog(unittest.TestCase):
    def test_build_dynamic_model_index(self):
        sample_entries = [
            ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
            ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
            ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
            ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
        ]
        cat, slug_map, beff, bdef = agy_bridge.build_dynamic_model_index(sample_entries)
        
        # Verify base model IDs
        cat_ids = [m["id"] for m in cat]
        self.assertIn("gemini-3.8-flash", cat_ids)
        self.assertIn("claude-sonnet-4.6", cat_ids)
        self.assertIn("gpt-oss-120b", cat_ids)
        
        # Verify slug mapping covers both dot and dash variants
        self.assertEqual(slug_map["claude-sonnet-4-6"], "claude-sonnet-4-6")
        self.assertEqual(slug_map["claude-sonnet-4.6"], "claude-sonnet-4-6")
        
        # Verify effort mappings
        self.assertEqual(beff["gemini-3.8-flash"]["high"], "gemini-3.8-flash-high")
        self.assertEqual(beff["gemini-3.8-flash"]["medium"], "gemini-3.8-flash-medium")
        self.assertEqual(beff["gemini-3.8-flash"]["low"], "gemini-3.8-flash-low")
        self.assertEqual(bdef["gemini-3.8-flash"], "gemini-3.8-flash-high")

    def test_build_agy_command_security(self):
        cmd = agy_bridge.build_agy_command("gemini-3.8-flash-high")
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--input-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--output-format", cmd)


if __name__ == "__main__":
    unittest.main()
