"""Model Context Protocol (MCP) Server Registry & Client Engine for Agent Harness.

Provides:
- Config-driven MCP server connections via stdio JSON-RPC 2.0 transport.
- Auto-discovery of tools from external MCP servers.
- Namespaced tool exposure (`mcp.<server_name>.<tool_name>`).
- Direct integration into ToolRegistry for seamless agent reasoning loop invocation.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.config import deep_get


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MCPToolSpec:
    server_name: str
    tool_name: str
    full_name: str  # e.g. mcp.sqlite.query
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    mutates_state: bool = False
    requires_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "full_name": self.full_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "mutates_state": self.mutates_state,
            "requires_approval": self.requires_approval,
            "origin": f"mcp.{self.server_name}",
            "category": "mcp",
        }


class StdioMCPClient:
    """Synchronous JSON-RPC 2.0 stdio client for communicating with MCP servers."""

    def __init__(self, config: MCPServerConfig, timeout_seconds: float = 30.0) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self._proc: subprocess.Popen[str] | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        if not self.config.command:
            raise RuntimeError(f"MCP server '{self.config.name}' has no command specified")

        env = os.environ.copy()
        env.update(self.config.env)

        cmd = [self.config.command, *(self.config.args or [])]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )

        # Send initialize handshake
        self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "ivy-agent-harness", "version": "0.1.0"},
            },
        )
        # Send initialized notification
        self._send_notification("notifications/initialized", {})
        return self._proc

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except OSError:
            pass

    def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params is not None:
                payload["params"] = params

            if not self._proc or not self._proc.stdin or not self._proc.stdout:
                raise RuntimeError(f"MCP server '{self.config.name}' process is not running")

            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

            # Read response
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    stderr = self._proc.stderr.read() if self._proc.stderr else ""
                    raise RuntimeError(f"MCP server '{self.config.name}' closed stdout: {stderr}")

                line = line.strip()
                if not line:
                    continue
                try:
                    res = json.loads(line)
                    if res.get("id") == req_id:
                        if "error" in res:
                            raise RuntimeError(f"MCP error from {self.config.name}: {res['error']}")
                        return res.get("result", {})
                except json.JSONDecodeError:
                    continue

    def list_tools(self) -> list[MCPToolSpec]:
        """Discover tools published by the MCP server."""
        self._ensure_process()
        res = self._send_request("tools/list", {})
        raw_tools = res.get("tools", [])
        tools: list[MCPToolSpec] = []
        for t in raw_tools:
            name = t.get("name", "unknown")
            desc = t.get("description", "")
            schema = t.get("inputSchema", {})
            full_name = f"mcp.{self.config.name}.{name}"
            tools.append(
                MCPToolSpec(
                    server_name=self.config.name,
                    tool_name=name,
                    full_name=full_name,
                    description=desc,
                    input_schema=schema,
                    mutates_state=True,
                    requires_approval=True,
                )
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool call on the MCP server."""
        self._ensure_process()
        res = self._send_request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        content_list = res.get("content", [])
        output_parts: list[str] = []
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "text":
                output_parts.append(str(item.get("text", "")))
            else:
                output_parts.append(str(item))

        is_error = bool(res.get("isError", False))
        output_text = "\n".join(output_parts) if output_parts else json.dumps(res, default=str)
        if is_error:
            return {"ok": False, "tool": f"mcp.{self.config.name}.{name}", "error": output_text}
        return {"ok": True, "tool": f"mcp.{self.config.name}.{name}", "result": output_text}

    def close(self) -> None:
        """Terminate server process."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001, S110
                    pass
            self._proc = None


class MCPRegistry:
    """Registry managing all configured MCP server connections and tool routing."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self.servers = servers
        self._clients: dict[str, StdioMCPClient] = {}
        self._tools_cache: dict[str, MCPToolSpec] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any], workspace_root: str | Path = ".") -> MCPRegistry:
        root = Path(workspace_root).resolve()
        raw_servers = dict(deep_get(config, "mcp_servers", {}) or {})

        # Auto-discover from .cursor/mcp.json, .vscode/mcp.json, .mcp/servers.json, .mcp.json
        discovery_paths = [
            root / ".cursor" / "mcp.json",
            root / ".vscode" / "mcp.json",
            root / ".mcp" / "servers.json",
            root / ".mcp.json",
        ]
        for p in discovery_paths:
            if p.exists() and p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                    mcp_block = data.get("mcpServers") or data.get("servers") or {}
                    if isinstance(mcp_block, dict):
                        for s_name, s_cfg in mcp_block.items():
                            if isinstance(s_cfg, dict) and s_name not in raw_servers:
                                raw_servers[s_name] = s_cfg
                except Exception:  # noqa: BLE001, S110
                    pass

        servers = {
            name: MCPServerConfig(
                name=name,
                transport=value.get("transport", "stdio"),
                command=value.get("command"),
                args=value.get("args", []),
                env=value.get("env", {}),
                url=value.get("url"),
            )
            for name, value in raw_servers.items()
            if isinstance(value, dict)
        }
        return cls(servers)

    def names(self) -> list[str]:
        return sorted(self.servers)

    def get_server_config(self, name: str) -> MCPServerConfig:
        try:
            return self.servers[name]
        except KeyError as exc:
            raise KeyError(f"unknown MCP server: {name}") from exc

    def _get_client(self, server_name: str) -> StdioMCPClient:
        if server_name not in self._clients:
            cfg = self.get_server_config(server_name)
            self._clients[server_name] = StdioMCPClient(cfg)
        return self._clients[server_name]

    def discover_tools(self, server_name: str | None = None) -> list[MCPToolSpec]:
        """Discover tools from all configured MCP servers or a specific one."""
        target_names = [server_name] if server_name else list(self.servers.keys())
        discovered: list[MCPToolSpec] = []
        for name in target_names:
            try:
                client = self._get_client(name)
                tools = client.list_tools()
                for tool in tools:
                    self._tools_cache[tool.full_name] = tool
                    discovered.append(tool)
            except Exception:  # noqa: BLE001, S112
                # Server might be offline or optional
                continue
        return discovered

    def get_tool(self, full_name: str) -> MCPToolSpec | None:
        return self._tools_cache.get(full_name)

    def call_tool(self, full_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route tool call: mcp.<server_name>.<tool_name>."""
        parts = full_name.split(".")
        if len(parts) < 3 or parts[0] != "mcp":
            return {"ok": False, "error": f"Invalid MCP tool name: '{full_name}'"}

        server_name = parts[1]
        raw_tool_name = ".".join(parts[2:])

        if server_name not in self.servers:
            return {"ok": False, "error": f"Unknown MCP server: '{server_name}'"}

        try:
            client = self._get_client(server_name)
            return client.call_tool(raw_tool_name, arguments)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "tool": full_name, "error": str(exc)}

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
