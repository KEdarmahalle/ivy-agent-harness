"""Comprehensive Test Suite for Tier 2: Model Context Protocol (MCP) Server."""

import io
import json
import unittest
from unittest.mock import patch

from harness import cli
from harness.mcp_server import (
    handle_initialize,
    handle_prompts_get,
    handle_prompts_list,
    handle_resources_list,
    handle_resources_read,
    handle_tool_call,
    handle_tools_list,
)


class Tier2MCPServerTests(unittest.TestCase):
    def test_mcp_initialize(self) -> None:
        resp = handle_initialize(1)
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertIn("prompts", resp["result"]["capabilities"])
        self.assertIn("resources", resp["result"]["capabilities"])

    def test_mcp_tools_list(self) -> None:
        resp = handle_tools_list(2)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("ivy_agent_turn", tool_names)
        self.assertIn("ivy_swarm", tool_names)
        self.assertIn("ivy_run_graph", tool_names)
        self.assertIn("ivy_tool_catalog", tool_names)
        self.assertIn("ivy_search_code", tool_names)
        self.assertIn("ivy_run_sandbox_python", tool_names)

    def test_mcp_prompts_list_and_get(self) -> None:
        list_resp = handle_prompts_list(3)
        prompts = list_resp["result"]["prompts"]
        prompt_names = [p["name"] for p in prompts]
        self.assertIn("ivy_repository_audit", prompt_names)
        self.assertIn("ivy_bug_investigation", prompt_names)
        self.assertIn("ivy_feature_implementation", prompt_names)

        get_resp = handle_prompts_get(4, {"name": "ivy_repository_audit", "arguments": {"focus_area": "security"}})
        self.assertEqual(get_resp["jsonrpc"], "2.0")
        messages = get_resp["result"]["messages"]
        self.assertTrue(len(messages) > 0)
        self.assertIn("security", messages[0]["content"]["text"])

    def test_mcp_resources_list_and_read(self) -> None:
        list_resp = handle_resources_list(5)
        resources = list_resp["result"]["resources"]
        uris = [r["uri"] for r in resources]
        self.assertIn("ivy://workspace/status", uris)
        self.assertIn("ivy://models/routes", uris)

        read_resp = handle_resources_read(6, {"uri": "ivy://workspace/status"})
        self.assertEqual(read_resp["jsonrpc"], "2.0")
        content_text = read_resp["result"]["contents"][0]["text"]
        data = json.loads(content_text)
        self.assertIn("workspace_root", data)
        self.assertIn("registered_tools_count", data)

    def test_mcp_tool_call_execution(self) -> None:
        # 1. Search code
        call_resp = handle_tool_call(7, {"name": "ivy_search_code", "arguments": {"query": "AgentHarness"}})
        self.assertEqual(call_resp["jsonrpc"], "2.0")
        self.assertNotIn("error", call_resp)
        self.assertIn("content", call_resp["result"])

        # 2. Run sandbox python
        py_resp = handle_tool_call(8, {"name": "ivy_run_sandbox_python", "arguments": {"code": "print(2 + 2)"}})
        self.assertEqual(py_resp["jsonrpc"], "2.0")
        self.assertTrue(
            "4" in py_resp["result"]["content"][0]["text"]
            or "Docker sandbox is unavailable" in py_resp["result"]["content"][0]["text"]
        )

        # 3. Tool catalog
        cat_resp = handle_tool_call(9, {"name": "ivy_tool_catalog", "arguments": {}})
        self.assertIn("file.read_text", cat_resp["result"]["content"][0]["text"])

    def test_mcp_server_cli_invocation(self) -> None:
        with (
            patch("harness.mcp_server.main") as mock_mcp_main,
            patch("sys.stdout", io.StringIO()),
        ):
            exit_code = cli.main(["mcp-server"])
            self.assertEqual(exit_code, 0)
            mock_mcp_main.assert_called_once()


if __name__ == "__main__":
    unittest.main()
