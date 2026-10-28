"""Tests for Model Context Protocol (MCP) server integration and tool routing."""

from __future__ import annotations

import sys

from harness.registries.mcp_registry import MCPRegistry, MCPServerConfig
from harness.registries.tool_registry import ToolRegistry


def test_mcp_registry_stdio_discovery_and_call() -> None:
    # Configure the reference MCP server running via Python stdio
    server_cfg = MCPServerConfig(
        name="utility",
        transport="stdio",
        command=sys.executable,
        args=["-m", "harness.mcp_server"],
    )
    registry = MCPRegistry({"utility": server_cfg})

    try:
        # Discover tools
        tools = registry.discover_tools("utility")
        assert len(tools) >= 3
        tool_names = [t.tool_name for t in tools]
        assert "hash_text" in tool_names
        assert "generate_uuid" in tool_names
        assert "sqlite_query" in tool_names

        # Call hash_text
        res_hash = registry.call_tool("mcp.utility.hash_text", {"text": "hello", "algorithm": "sha256"})
        assert res_hash.get("ok") is True
        assert "sha256:" in res_hash.get("result", "")

        # Call generate_uuid
        res_uuid = registry.call_tool("mcp.utility.generate_uuid", {"count": 2})
        assert res_uuid.get("ok") is True
        lines = res_uuid.get("result", "").splitlines()
        assert len(lines) == 2
    finally:
        registry.close_all()


def test_tool_registry_mcp_integration() -> None:
    server_cfg = MCPServerConfig(
        name="utility",
        transport="stdio",
        command=sys.executable,
        args=["-m", "harness.mcp_server"],
    )
    mcp_reg = MCPRegistry({"utility": server_cfg})
    tool_reg = ToolRegistry(mcp=mcp_reg)

    try:
        # Check that MCP tools are dynamically registered in ToolRegistry catalog
        catalog = tool_reg.catalog()
        assert "mcp.utility.hash_text" in catalog
        assert "mcp.utility.generate_uuid" in catalog

        # Call MCP tool via ToolRegistry.invoke
        res = tool_reg.invoke("mcp.utility.hash_text", text="agent-harness-mcp", algorithm="md5")
        assert res.get("ok") is True
        assert "md5:" in res.get("result", "")
    finally:
        mcp_reg.close_all()
