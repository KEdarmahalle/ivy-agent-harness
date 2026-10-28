"""Adversarial stress-testing suite for Phase 1 (Native Tool Calling, Failover Chains, and Resilient Parsing)."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from harness.registries.mcp_registry import MCPRegistry, MCPServerConfig
from harness.registries.model_registry import ModelRegistry
from harness.registries.tool_registry import ToolRegistry


class Phase1AdversarialTests(unittest.TestCase):
    def test_multi_tool_native_calls_in_single_turn(self) -> None:
        """Adversarial test: Model returns 3 separate tool calls in one turn."""
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        tc1 = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="file.list", arguments='{"path": "."}'),
        )
        tc2 = SimpleNamespace(
            id="call_2",
            function=SimpleNamespace(name="file.read_text", arguments='{"file": "config.yaml"}'),
        )
        tc3 = SimpleNamespace(
            id="call_3",
            function=SimpleNamespace(name="environment.info", arguments='{}'),
        )
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Inspecting workspace with 3 tools",
                        tool_calls=[tc1, tc2, tc3],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )

        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=mock_response),
        ):
            res = registry.complete(
                "nim",
                "example",
                [{"role": "user", "content": "Scan workspace"}],
            )

        self.assertEqual(len(res.tool_calls), 3)
        self.assertEqual([t.name for t in res.tool_calls], ["file.list", "file.read_text", "environment.info"])
        self.assertEqual(res.tool_calls[0].arguments, {"path": "."})
        self.assertEqual(res.tool_calls[1].arguments, {"file": "config.yaml"})
        self.assertEqual(res.tool_calls[2].arguments, {})

    def test_malformed_json_arguments_graceful_recovery(self) -> None:
        """Adversarial test: Model hallucinates broken JSON string in arguments."""
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        broken_tc = SimpleNamespace(
            id="call_broken",
            function=SimpleNamespace(
                name="file.read_text",
                arguments='{"file": "test.txt", UNCLOSED_JSON...',
            ),
        )
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="", tool_calls=[broken_tc]),
                    finish_reason="tool_calls",
                )
            ]
        )

        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=mock_response),
        ):
            res = registry.complete(
                "nim",
                "example",
                [{"role": "user", "content": "Read file"}],
            )

        self.assertEqual(len(res.tool_calls), 1)
        self.assertEqual(res.tool_calls[0].name, "file.read_text")
        # Empty dict fallback, raw arguments preserved for debugging/auto-repair
        self.assertEqual(res.tool_calls[0].arguments, {})
        self.assertIn("UNCLOSED_JSON", res.tool_calls[0].raw_arguments)

    def test_mcp_tool_schema_conversion_and_call(self) -> None:
        """Adversarial test: Discovered MCP tools must convert to valid OpenAI schema alongside builtins."""
        mcp_reg = MCPRegistry(
            {
                "mock_server": MCPServerConfig(
                    name="mock_server",
                    transport="stdio",
                    command="python",
                    args=["-m", "harness.mcp_server"],
                )
            }
        )
        try:
            tool_reg = ToolRegistry(mcp=mcp_reg)
            openai_tools = tool_reg.to_openai_tools()
            tool_names = [t["function"]["name"] for t in openai_tools]

            # Verify MCP namespaced tools are present
            self.assertIn("mcp.mock_server.hash_text", tool_names)
            self.assertIn("mcp.mock_server.generate_uuid", tool_names)
            self.assertIn("mcp.mock_server.sqlite_query", tool_names)

            # Verify parameters structure of MCP tool
            hash_tool = next(t for t in openai_tools if t["function"]["name"] == "mcp.mock_server.hash_text")
            params = hash_tool["function"]["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("text", params["properties"])
        finally:
            mcp_reg.close_all()

    def test_full_chain_failover_to_deterministic_default(self) -> None:
        """Adversarial test: Primary and secondary cloud routes both fail; must fall back to deterministic default without crashing."""
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.flaky_primary": {
                        "provider": "nvidia_nim",
                        "model": "flaky-model-1",
                        "fallbacks": ["nim.flaky_secondary", "default"],
                        "retry_count": 0,
                    },
                    "nim.flaky_secondary": {
                        "provider": "nvidia_nim",
                        "model": "flaky-model-2",
                        "retry_count": 0,
                    },
                    "default": {
                        "provider": "fallback",
                        "model": "deterministic-local",
                    },
                }
            }
        )

        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", side_effect=RuntimeError("500 Internal Server Error")),
        ):
            res = registry.complete(
                "nim",
                "flaky_primary",
                [{"role": "user", "content": "Help me audit code"}],
            )

        self.assertEqual(res.route_used, "default")
        self.assertIn("Deterministic analysis for flaky_primary", res.content)


if __name__ == "__main__":
    unittest.main()
