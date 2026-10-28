from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.memory import MemoryStore, compact_history


class MemoryTests(unittest.TestCase):
    def test_memory_store_add_list_remove_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(directory)
            self.assertEqual(len(store.list_all()), 0)

            e1 = store.add("Use uv run for Python commands", category="workflow")
            self.assertEqual(e1.id, 1)
            self.assertEqual(e1.text, "Use uv run for Python commands")

            e2 = store.add("Prefer concise markdown output", category="style")
            self.assertEqual(e2.id, 2)
            self.assertEqual(len(store.list_all()), 2)

            # Reload from disk
            reloaded = MemoryStore(directory)
            self.assertEqual(len(reloaded.list_all()), 2)

            # Remove memory
            self.assertTrue(reloaded.remove(1))
            self.assertFalse(reloaded.remove(999))
            self.assertEqual(len(reloaded.list_all()), 1)

            # Clear
            reloaded.clear()
            self.assertEqual(len(reloaded.list_all()), 0)

    def test_memory_store_loads_workspace_guidelines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("Rule 1: Always check tests.", encoding="utf-8")
            (root / "CLAUDE.md").write_text("Rule 2: Keep functions small.", encoding="utf-8")

            store = MemoryStore(root)
            guidelines = store.load_project_guidelines()
            self.assertIn("Rule 1: Always check tests.", guidelines)
            self.assertIn("Rule 2: Keep functions small.", guidelines)

            rendered = store.render_system_memory_context()
            self.assertIn("# WORKSPACE INSTRUCTIONS", rendered)
            self.assertIn("Rule 1", rendered)

    def test_session_save_load_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(directory)
            messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

            store.save_session("session-alpha", messages)
            sessions = store.list_sessions()
            self.assertIn("session-alpha", sessions)

            loaded = store.load_session("session-alpha")
            self.assertEqual(loaded, messages)
            self.assertIsNone(store.load_session("missing-session"))

    def test_compact_history(self) -> None:
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Turn 1: explore project"},
            {"role": "assistant", "content": "Turn 1 reply: found 10 files"},
            {"role": "user", "content": "Turn 2: run tests"},
            {"role": "assistant", "content": "Turn 2 reply: all passed"},
            {"role": "user", "content": "Turn 3: refactor module"},
            {"role": "assistant", "content": "Turn 3 reply: done"},
            {"role": "user", "content": "Turn 4: final check"},
        ]
        compacted = compact_history(messages, keep_recent=2)
        self.assertLess(len(compacted), len(messages))
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("COMPACTED WORKING MEMORY", compacted[1]["content"])
        self.assertEqual(compacted[-1]["content"], "Turn 4: final check")


if __name__ == "__main__":
    unittest.main()
