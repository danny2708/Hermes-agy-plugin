"""Unit tests for StreamToolCallFilter state machine."""

import unittest
import sys
import os

# Add plugin root to sys.path portably
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import agy_bridge


class TestStreamToolCallFilter(unittest.TestCase):
    def test_plain_text(self):
        f = agy_bridge.StreamToolCallFilter(allow_tool_calls=True)
        t, r, tc, done = f.feed("Hello world! How are you?")
        self.assertEqual(t, "Hello world! How are you?")
        self.assertEqual(r, "")
        self.assertEqual(tc, [])

    def test_thinking_extraction(self):
        f = agy_bridge.StreamToolCallFilter(allow_tool_calls=True)
        t1, r1, _, _ = f.feed("Before <think>Analyzing problem")
        t2, r2, _, _ = f.feed(" deeply</think>After thought.")
        
        self.assertEqual(t1, "Before ")
        self.assertEqual(r1, "Analyzing problem")
        self.assertEqual(r2, " deeply")
        self.assertEqual(t2, "After thought.")

    def test_tool_call_streaming(self):
        f = agy_bridge.StreamToolCallFilter(allow_tool_calls=True)
        xml = (
            "I will run dir.\n"
            "<tool_call>\n"
            "<name>terminal</name>\n"
            "<arguments>{\"command\": \"dir\"}</arguments>\n"
            "</tool_call>"
        )
        
        all_text = ""
        all_tool_chunks = []
        for ch in xml:
            t, r, tcs, _ = f.feed(ch)
            all_text += t
            all_tool_chunks.extend(tcs)

        self.assertIn("I will run dir.", all_text)
        self.assertTrue(len(all_tool_chunks) > 0)
        
        # Check first chunk has name and function
        first_chunk = all_tool_chunks[0]
        self.assertEqual(first_chunk["function"]["name"], "terminal")
        
        # Check joined arguments
        arg_chunks = [c["function"]["arguments"] for c in all_tool_chunks if "arguments" in c.get("function", {})]
        full_args = "".join(arg_chunks)
        self.assertEqual(full_args, '{"command": "dir"}')

    def test_partial_tag_buffering(self):
        f = agy_bridge.StreamToolCallFilter(allow_tool_calls=True)
        t1, _, _, _ = f.feed("Testing <th")
        self.assertEqual(t1, "Testing ")
        t2, r2, _, _ = f.feed("ink>my thought</think>done")
        self.assertEqual(r2, "my thought")
        self.assertEqual(t2, "done")


if __name__ == "__main__":
    unittest.main()
