"""Unit tests for working memory preservation, multimodal images, and thought rendering."""

import base64
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import agy_bridge


class TestPromptMemory(unittest.TestCase):
    def test_multimodal_base64_image_extracted(self):
        sample_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{sample_png_b64}"}}
            ]
        }
        rendered = agy_bridge._render_agy_message(msg)
        self.assertIn("What is in this image?", rendered)
        self.assertIn("[Attached Image File:", rendered)
        self.assertIn("Please inspect and analyze the image at:", rendered)
        self.assertTrue(rendered.endswith(".png\n") or ".png" in rendered)

    def test_multimodal_file_url_extracted(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Check this screenshot"},
                {"type": "image_url", "image_url": {"url": "file:///C:/path/to/screenshot.png"}}
            ]
        }
        rendered = agy_bridge._render_agy_message(msg)
        self.assertIn("Check this screenshot", rendered)
        self.assertIn("[Attached Image File:", rendered)
        self.assertIn("screenshot.png", rendered)

    def test_assistant_thought_preserved(self):
        msg = {
            "role": "assistant",
            "reasoning_content": "We should check the process list first.",
            "tool_calls": [
                {
                    "id": "call_123",
                    "function": {"name": "terminal", "arguments": '{"command": "tasklist"}'}
                }
            ]
        }
        rendered = agy_bridge._render_agy_message(msg)
        self.assertIn("<think>\nWe should check the process list first.\n</think>", rendered)
        self.assertIn("<name>terminal</name>", rendered)

    def test_large_working_memory_not_truncated(self):
        # Generate 15 tool rounds totaling ~60KB (previously would be wiped by 10KB limit)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Fix the discord chat issue."},
        ]
        for i in range(10):
            messages.append({
                "role": "assistant",
                "tool_calls": [{"id": f"call_{i}", "function": {"name": "search_files", "arguments": f'{{"step": {i}}}'}}]
            })
            messages.append({
                "role": "tool",
                "name": "search_files",
                "tool_call_id": f"call_{i}",
                "content": f"Results for step {i}: " + ("x" * 4000)  # 4KB per tool result
            })

        prompt = agy_bridge.format_messages_for_agy(messages)
        # Verify first step is still present in working memory
        self.assertIn("Results for step 0", prompt)
        # Verify last step is still present
        self.assertIn("Results for step 9", prompt)
        # Ensure no accidental truncation marker was needed
        self.assertNotIn("[... older intermediate tool output omitted", prompt)


if __name__ == "__main__":
    unittest.main()
