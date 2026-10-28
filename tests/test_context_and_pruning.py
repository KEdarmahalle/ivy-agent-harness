"""Tests for Context Optimization, Rolling Observation Masking, and Bilateral Trimming."""

from __future__ import annotations

import json
import unittest

from harness.context.pruning import (
    apply_observation_masking,
    mask_observation_payload,
    trim_tool_output,
)
from harness.declarative_graphs import DeclarativeNodeConfig
from harness.prompts import build_cacheable_system_prompt_prefix


class ContextAndPruningTests(unittest.TestCase):
    # ==================== 1. Bilateral Trimming Tests ====================

    def test_trim_tool_output_short(self) -> None:
        short_text = "pytest ran 5 tests: 5 passed in 0.2s"
        trimmed = trim_tool_output(short_text, max_chars=4000)
        self.assertEqual(trimmed, short_text)

    def test_trim_tool_output_long_multiline(self) -> None:
        lines = [f"Line {i}: test log event with metadata" for i in range(200)]
        full_text = "\n".join(lines)
        trimmed = trim_tool_output(full_text, max_chars=1000, head_lines=10, tail_lines=15)

        self.assertIn("Line 0:", trimmed)
        self.assertIn("Line 9:", trimmed)
        self.assertIn("lines omitted by sandbox policy", trimmed)
        self.assertIn("Line 199:", trimmed)
        self.assertTrue(len(trimmed) < len(full_text))

    # ==================== 2. Rolling Observation Masking Tests ====================

    def test_mask_observation_payload(self) -> None:
        raw_obs = {
            "tool": "file.read_text",
            "file": "harness/sdk.py",
            "ok": True,
            "content": "A" * 5000,
        }
        masked = mask_observation_payload(raw_obs)
        self.assertEqual(masked["tool"], "file.read_text")
        self.assertIn("[OBSERVATION_MASKED]", masked)
        self.assertNotIn("content", masked)

    def test_apply_observation_masking_to_react_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are Ivy."},
            {"role": "user", "content": "Fix the bug"},
            {
                "role": "user",
                "content": "Tool result(s):\n"
                + json.dumps({"tool": "file.search_text", "ok": True, "matches": ["A" * 1000]})
                + "\n\nSynthesize findings.",
            },
            {"role": "assistant", "content": "I found the file. Now reading it."},
            {
                "role": "user",
                "content": "Tool result(s):\n"
                + json.dumps({"tool": "file.read_text", "ok": True, "content": "B" * 2000})
                + "\n\nSynthesize findings.",
            },
            {"role": "assistant", "content": "I read the file. Now running tests."},
            {
                "role": "user",
                "content": "Tool result(s):\n"
                + json.dumps({"tool": "process.run", "ok": True, "stdout": "All tests pass"})
                + "\n\nSynthesize findings.",
            },
        ]

        # Keep last 1 tool observation intact; mask earlier ones
        optimized = apply_observation_masking(messages, keep_recent=1)

        # Message 2 (search_text) should be masked
        self.assertIn("[OBSERVATION_MASKED]", optimized[2]["content"])
        self.assertNotIn("A" * 500, optimized[2]["content"])

        # Message 4 (read_text) should also be masked
        self.assertIn("[OBSERVATION_MASKED]", optimized[4]["content"])
        self.assertNotIn("B" * 500, optimized[4]["content"])

        # Message 6 (process.run) is the most recent, so it must stay unmasked!
        self.assertIn("All tests pass", optimized[6]["content"])

    # ==================== 3. Declarative State Projections Tests ====================

    def test_declarative_channel_state_projection(self) -> None:
        node_cfg = DeclarativeNodeConfig.from_dict("test_node", {
            "name": "test_node",
            "inputs": ["target_metric", "baseline_score"],
            "outputs": {"result": "processed_metric"},
        })

        self.assertEqual(node_cfg.inputs, ["target_metric", "baseline_score"])
        self.assertEqual(node_cfg.outputs, {"result": "processed_metric"})

    # ==================== 4. Cacheable Prefix Tests ====================

    def test_build_cacheable_system_prompt_prefix(self) -> None:
        prefix1 = build_cacheable_system_prompt_prefix("Base prompt", {"tool1": {}}, "Harness ctx")
        prefix2 = build_cacheable_system_prompt_prefix("Base prompt", {"tool1": {}}, "Harness ctx")
        self.assertEqual(prefix1, prefix2)
        self.assertIn("Base prompt", prefix1)
        self.assertIn("ENGINEERING GUIDELINES", prefix1)


if __name__ == "__main__":
    unittest.main()
