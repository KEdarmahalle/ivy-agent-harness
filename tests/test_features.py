from __future__ import annotations

import unittest

from harness.features import FeatureRegistry


class FeatureRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = FeatureRegistry()
        self.config: dict = {
            "chat": {
                "streaming": {"enabled": True},
                "agent_loop": {"max_tokens": 4096, "auto_approve_tools": False},
                "ui": {"tool_display": "compact"},
            }
        }

    def test_default_features_registered(self) -> None:
        features = self.registry.list_features()
        self.assertGreaterEqual(len(features), 10)
        keys = [f.key for f in features]
        self.assertIn("streaming.enabled", keys)
        self.assertIn("agent_loop.max_tokens", keys)
        self.assertIn("agent_loop.auto_repair_truncated_tools", keys)

    def test_get_and_set_boolean_feature(self) -> None:
        self.assertTrue(self.registry.get_value("streaming.enabled", self.config))
        self.registry.set_value("streaming.enabled", False, self.config)
        self.assertFalse(self.registry.get_value("streaming.enabled", self.config))
        self.assertFalse(self.config["chat"]["streaming"]["enabled"])

    def test_toggle_feature(self) -> None:
        self.assertFalse(self.registry.get_value("agent_loop.auto_approve_tools", self.config))
        new_state = self.registry.toggle("agent_loop.auto_approve_tools", self.config)
        self.assertTrue(new_state)
        self.assertTrue(self.registry.get_value("agent_loop.auto_approve_tools", self.config))

    def test_set_integer_feature_with_type_casting(self) -> None:
        self.registry.set_value("agent_loop.max_tokens", "8192", self.config)
        self.assertEqual(self.registry.get_value("agent_loop.max_tokens", self.config), 8192)

    def test_options_validation(self) -> None:
        self.registry.set_value("ui.tool_display", "verbose", self.config)
        self.assertEqual(self.registry.get_value("ui.tool_display", self.config), "verbose")
        with self.assertRaises(ValueError):
            self.registry.set_value("ui.tool_display", "invalid-style", self.config)

    def test_snapshot_structure(self) -> None:
        snap = self.registry.snapshot(self.config)
        self.assertIsInstance(snap, list)
        self.assertTrue(any(f["key"] == "streaming.enabled" for f in snap))
        for item in snap:
            self.assertIn("key", item)
            self.assertIn("name", item)
            self.assertIn("description", item)
            self.assertIn("category", item)
            self.assertIn("value", item)


if __name__ == "__main__":
    unittest.main()
