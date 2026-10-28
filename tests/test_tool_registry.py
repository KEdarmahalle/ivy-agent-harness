from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.registries.tool_registry import ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def test_catalog_exposes_configured_builtin_tools(self) -> None:
        registry = ToolRegistry.from_config(
            {
                "tools": {
                    "builtins": {
                        "enabled": ["file.list", "file.write_text", "sandbox.inspect"],
                        "definitions": {
                            "file.write_text": {
                                "origin": "workspace-local",
                                "description": "Configured write tool",
                            }
                        },
                    },
                    "allowlist": ["file.list", "sandbox.inspect", "tool.catalog"],
                    "sandbox": {"workspace_roots": ["."], "writable_roots": ["."]},
                }
            }
        )
        catalog = registry.catalog()
        self.assertIn("file.list", catalog)
        self.assertNotIn("process.run", catalog)
        self.assertIn("sandbox.inspect", catalog)
        self.assertEqual(catalog["file.write_text"]["origin"], "workspace-local")
        self.assertEqual(catalog["file.write_text"]["description"], "Configured write tool")
        self.assertTrue(catalog["file.write_text"]["mutates_state"])
        self.assertFalse(catalog["file.write_text"]["allowed"])

    def test_unknown_configured_builtin_tool_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            ToolRegistry.from_config({"tools": {"builtins": {"enabled": ["missing.tool"]}}})

    def test_file_read_write_and_replace_obey_writable_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry.from_config(
                {
                    "tools": {
                        "allowlist": ["file.write_text", "file.read_text", "file.replace_text"],
                        "sandbox": {
                            "workspace_roots": [str(root)],
                            "writable_roots": [str(root)],
                        },
                    }
                }
            )
            write = registry.invoke(
                "file.write_text",
                root=str(root),
                file="notes/example.txt",
                content="hello codex",
                create_parent_dirs=True,
            )
            self.assertEqual(write["file"], "notes/example.txt")
            read = registry.invoke("file.read_text", root=str(root), file="notes/example.txt")
            self.assertEqual(read["content"], "hello codex")
            replaced = registry.invoke(
                "file.replace_text",
                root=str(root),
                file="notes/example.txt",
                old="codex",
                new="harness",
            )
            self.assertEqual(replaced["replacements"], 1)
            self.assertEqual((root / "notes" / "example.txt").read_text(encoding="utf-8"), "hello harness")

    def test_file_write_blocks_path_outside_writable_root(self) -> None:
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as denied:
            registry = ToolRegistry.from_config(
                {
                    "tools": {
                        "allowlist": ["file.write_text"],
                        "sandbox": {
                            "workspace_roots": [allowed],
                            "writable_roots": [allowed],
                        },
                    }
                }
            )
            with self.assertRaises(PermissionError):
                registry.invoke(
                    "file.write_text",
                    root=denied,
                    file="blocked.txt",
                    content="no",
                )

    def test_process_run_uses_sandbox_cwd_and_output_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ToolRegistry.from_config(
                {
                    "tools": {
                        "allowlist": ["process.run"],
                        "sandbox": {
                            "workspace_roots": [str(root)],
                            "default_timeout_seconds": 10,
                            "max_output_chars": 20,
                        },
                    }
                }
            )
            result = registry.invoke(
                "process.run",
                cwd=str(root),
                command="python -c \"print('abcdefghijklmnopqrstuvwxyz')\"",
            )
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(len(result["output_excerpt"]), 20)

    def test_web_fetch_requires_network_enabled(self) -> None:
        registry = ToolRegistry.from_config(
            {"tools": {"allowlist": ["web.fetch_url"], "sandbox": {"network_enabled": False}}}
        )
        with self.assertRaises(PermissionError):
            registry.invoke("web.fetch_url", url="https://example.com")

    def test_sandbox_run_python_executes_code(self) -> None:
        registry = ToolRegistry.from_config(
            {"tools": {"allowlist": ["sandbox.run_python"], "sandbox": {"workspace_roots": ["."]}}}
        )
        result = registry.invoke("sandbox.run_python", code="print(2 + 2)")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("4", result["stdout"])

    def test_to_openai_tools_conversion(self) -> None:
        registry = ToolRegistry.from_config(
            {"tools": {"allowlist": ["file.read_text", "sandbox.run_python"]}}
        )
        tools = registry.to_openai_tools()
        self.assertEqual(len(tools), 2)
        tool_names = [t["function"]["name"] for t in tools]
        self.assertIn("file.read_text", tool_names)
        self.assertIn("sandbox.run_python", tool_names)

        read_tool = next(t for t in tools if t["function"]["name"] == "file.read_text")
        params = read_tool["function"]["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertIn("file", params["properties"])
        self.assertIn("file", params["required"])


if __name__ == "__main__":
    unittest.main()
