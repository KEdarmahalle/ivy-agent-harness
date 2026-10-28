"""Config-resolved universal tool registry supporting domain packs and dynamic plugins."""

from __future__ import annotations

import csv
import datetime
import fnmatch
import io
import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.config import deep_get
from harness.governance import current_tenant
from harness.network import safe_http_request
from harness.plugins import PluginRegistry
from harness.registries.mcp_registry import MCPRegistry
from harness.sandbox import SandboxProvider, get_sandbox_provider
from harness.skills import SkillRegistry
from harness.tools.policy import SandboxPolicy

ToolFn = Callable[..., dict[str, Any]]

DOMAIN_PACKS: dict[str, list[str]] = {
    "core": [
        "environment.info",
        "tool.catalog",
        "time.now",
        "agent.delegate",
    ],
    "filesystem": [
        "file.list",
        "file.search_text",
        "file.read_lines",
        "file.read_text",
        "file.write_text",
        "file.replace_text",
    ],
    "data_analytics": [
        "data.sql_query",
        "data.json_transform",
        "data.csv_inspect",
    ],
    "web_network": [
        "web.fetch_url",
    ],
    "shell_sandbox": [
        "process.run",
        "sandbox.inspect",
        "sandbox.run_python",
    ],
    "skills": [
        "skill.list",
        "skill.view",
        "skill.create",
    ],
}

BUILTIN_TOOL_SPECS: dict[str, dict[str, Any]] = {
    # Core Pack
    "environment.info": {
        "handler": "_environment_info",
        "description": "Return safe runtime environment facts without secret values",
        "category": "core",
        "origin": "builtin.core",
    },
    "tool.catalog": {
        "handler": "_tool_catalog",
        "description": "Return registered tool metadata and input schemas",
        "category": "core",
        "origin": "builtin.core",
    },
    "time.now": {
        "handler": "_time_now",
        "description": "Get current UTC and local timestamps, date, and ISO formatted string",
        "category": "core",
        "origin": "builtin.core",
        "input_schema": {"timezone": "string?"},
    },
    "agent.delegate": {
        "handler": "_delegate_agent",
        "description": "Delegate a focused subtask to a specialized sub-agent (e.g. data_analyst, security_auditor, researcher)",
        "category": "multi_agent",
        "origin": "builtin.core",
        "input_schema": {"agent": "string", "prompt": "string", "context": "object?"},
    },
    # Filesystem Pack
    "file.list": {
        "handler": "_list_files",
        "description": "List files and directories below a workspace root",
        "category": "filesystem",
        "origin": "builtin.filesystem",
        "input_schema": {"path": "string", "root": "string?", "max_entries": "integer?"},
    },
    "file.search_text": {
        "handler": "_search_text",
        "description": "Search text files below a root directory",
        "category": "filesystem",
        "origin": "builtin.filesystem",
        "input_schema": {"root": "string?", "query": "string", "max_results": "integer?"},
    },
    "file.read_lines": {
        "handler": "_read_lines",
        "description": "Read a bounded range of lines from a text file",
        "category": "filesystem",
        "origin": "builtin.filesystem",
        "input_schema": {"file": "string", "root": "string?", "start": "integer?", "end": "integer?"},
    },
    "file.read_text": {
        "handler": "_read_text",
        "description": "Read a bounded text file",
        "category": "filesystem",
        "origin": "builtin.filesystem",
        "input_schema": {"file": "string", "root": "string?", "max_bytes": "integer?"},
    },
    "file.write_text": {
        "handler": "_write_text",
        "description": "Write text to a file within a writable sandbox root",
        "category": "filesystem",
        "origin": "builtin.filesystem",
        "mutates_state": True,
        "requires_approval": True,
        "input_schema": {
            "file": "string",
            "content": "string",
            "root": "string?",
            "create_parent_dirs": "boolean?",
        },
    },
    "file.replace_text": {
        "handler": "_replace_text",
        "description": "Replace an exact text occurrence inside a writable sandbox root",
        "category": "filesystem",
        "origin": "builtin.filesystem",
        "mutates_state": True,
        "requires_approval": True,
        "input_schema": {"file": "string", "old": "string", "new": "string", "root": "string?"},
    },
    # Data Analytics Pack
    "data.sql_query": {
        "handler": "_data_sql_query",
        "description": "Execute a safe SQL query against a SQLite database file or in-memory database",
        "category": "data_analytics",
        "origin": "builtin.data",
        "input_schema": {"query": "string", "db_path": "string?", "limit": "integer?"},
    },
    "data.json_transform": {
        "handler": "_data_json_transform",
        "description": "Extract, filter, or transform nested JSON data structures by keypath or filter",
        "category": "data_analytics",
        "origin": "builtin.data",
        "input_schema": {"data": "string", "key_path": "string?"},
    },
    "data.csv_inspect": {
        "handler": "_data_csv_inspect",
        "description": "Inspect CSV columns, row count, and sample rows from a tabular file",
        "category": "data_analytics",
        "origin": "builtin.data",
        "input_schema": {"file": "string", "max_rows": "integer?"},
    },
    # Web Network Pack
    "web.fetch_url": {
        "handler": "_fetch_url",
        "description": "Fetch a URL when network is enabled in sandbox policy",
        "category": "web",
        "origin": "builtin.web",
        "input_schema": {"url": "string", "timeout_seconds": "integer?"},
    },
    # Shell Sandbox Pack
    "process.run": {
        "handler": "_run_process",
        "description": "Run a local shell command, test runner, or script inside the sandbox policy",
        "category": "shell",
        "origin": "builtin.shell",
        "requires_approval": True,
        "input_schema": {"command": "string", "cwd": "string?", "timeout_seconds": "integer?"},
    },
    "sandbox.run_python": {
        "handler": "_run_python",
        "description": "Execute inline Python code in the sandbox to perform multi-step analysis, AST checks, or data computation",
        "category": "sandbox",
        "origin": "builtin.shell",
        "requires_approval": True,
        "input_schema": {"code": "string", "timeout_seconds": "integer?"},
    },
    "sandbox.inspect": {
        "handler": "_inspect_sandbox",
        "description": "Describe workspace roots, writable roots, network, and tool limits",
        "category": "sandbox",
        "origin": "builtin.shell",
    },
    # Skills Pack
    "skill.list": {
        "handler": "_list_skills",
        "description": "List all installed specialized skills, descriptions, and tags",
        "category": "skills",
        "origin": "builtin.skills",
        "input_schema": {"query": "string?"},
    },
    "skill.view": {
        "handler": "_view_skill",
        "description": "View complete instructions, guidelines, and helper scripts for a specialized skill",
        "category": "skills",
        "origin": "builtin.skills",
        "input_schema": {"name": "string"},
    },
    "skill.create": {
        "handler": "_create_skill",
        "description": "The Skill-Wala-Skill: author and save a new reusable specialized skill to .harness/skills/ or ~/.ivy/skills/ with frontmatter and markdown instructions",
        "category": "skills",
        "origin": "builtin.skills",
        "mutates_state": True,
        "requires_approval": True,
        "input_schema": {
            "name": "string",
            "description": "string",
            "instructions": "string",
            "tags": "array?",
            "version": "string?",
            "global_scope": "boolean?",
        },
    },
}


@dataclass
class RegisteredTool:
    name: str
    fn: ToolFn
    description: str
    category: str = "general"
    origin: str = "harness"
    mutates_state: bool = False
    requires_approval: bool = False
    input_schema: dict[str, Any] | None = None


class ToolRegistry:
    def __init__(
        self,
        allowlist: list[str] | None = None,
        protected_paths: list[str] | None = None,
        sandbox: SandboxPolicy | None = None,
        builtin_config: dict[str, Any] | None = None,
        skills: SkillRegistry | None = None,
        mcp: MCPRegistry | None = None,
        plugins: PluginRegistry | None = None,
        packs_config: dict[str, Any] | None = None,
        sandbox_provider: SandboxProvider | None = None,
        restricted_tools: dict[str, list[str]] | None = None,
    ) -> None:
        self.allowlist = list(allowlist or [])
        mandatory_protected = [
            ".env", ".secrets", "secrets/", ".harness/", ".ssh/", "id_rsa", "id_ed25519",
            "kubeconfig", "credentials.json", ".env*", "*.pem", "*.key", "*.p12", "*.pfx",
            "*_rsa", "*_ed25519", "*credentials*.json",
        ]
        self.protected_paths = sorted(set(mandatory_protected) | set(protected_paths or []))
        self.sandbox = sandbox or SandboxPolicy(
            workspace_roots=[Path.cwd()],
            writable_roots=[Path.cwd()],
        )
        self.builtin_config = builtin_config or {}
        self.sandbox_provider = sandbox_provider or get_sandbox_provider(config=self.builtin_config, policy=self.sandbox)
        self.restricted_tools = restricted_tools or {}
        self.skills = skills or SkillRegistry()
        self.mcp = mcp or MCPRegistry({})
        self.plugins = plugins or PluginRegistry()
        self.packs_config = packs_config or {}
        self._tools: dict[str, RegisteredTool] = {}
        self._register_builtin_tools()
        self._register_mcp_tools()
        self._register_plugin_tools()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ToolRegistry:
        sandbox_policy = SandboxPolicy.from_config(config)
        sandbox_provider = get_sandbox_provider(config=config, policy=sandbox_policy)
        return cls(
            allowlist=deep_get(config, "tools.allowlist", []),
            protected_paths=deep_get(config, "tools.protected_paths", []),
            sandbox=sandbox_policy,
            builtin_config=deep_get(config, "tools.builtins", {}) or {},
            skills=SkillRegistry(workspace_root="."),
            mcp=MCPRegistry.from_config(config),
            plugins=PluginRegistry(workspace_root="."),
            packs_config=deep_get(config, "packs", {}) or {},
            sandbox_provider=sandbox_provider,
            restricted_tools=deep_get(config, "governance.rbac.restricted_tools", {}) or {},
        )

    def register(
        self,
        name: str,
        fn: ToolFn,
        description: str = "",
        *,
        category: str = "general",
        origin: str = "harness",
        mutates_state: bool = False,
        requires_approval: bool = False,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self._tools[name] = RegisteredTool(
            name=name,
            fn=fn,
            description=description,
            category=category,
            origin=origin,
            mutates_state=mutates_state,
            requires_approval=requires_approval,
            input_schema=input_schema or {},
        )

    def names(self) -> list[str]:
        return sorted(self._tools)

    def catalog(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "name": tool.name,
                "description": tool.description,
                "category": tool.category,
                "origin": tool.origin,
                "mutates_state": tool.mutates_state,
                "requires_approval": tool.requires_approval,
                "allowed": not self.allowlist or name in self.allowlist,
                "input_schema": tool.input_schema or {},
            }
            for name, tool in sorted(self._tools.items())
        }

    def to_openai_tools(self, tool_names: list[str] | None = None) -> list[dict[str, Any]]:
        """Convert registered and allowed tools to standard OpenAI function calling JSON schema."""
        openai_tools: list[dict[str, Any]] = []
        target_names = set(tool_names) if tool_names is not None else set(self._tools)

        for name, tool in sorted(self._tools.items()):
            if name not in target_names:
                continue
            if self.allowlist and name not in self.allowlist:
                continue

            raw_schema = tool.input_schema or {}
            # If already a full JSON schema (like from MCP servers)
            if isinstance(raw_schema, dict) and (
                raw_schema.get("type") == "object" or "properties" in raw_schema
            ):
                parameters = {
                    "type": "object",
                    "properties": raw_schema.get("properties", {}),
                    "required": raw_schema.get("required", []),
                }
            else:
                # Convert shorthand mapping {"file": "string", "start": "integer?"}
                properties: dict[str, Any] = {}
                required: list[str] = []
                for prop_name, prop_type in raw_schema.items():
                    prop_type_str = str(prop_type).strip()
                    is_optional = prop_type_str.endswith("?")
                    base_type = prop_type_str.rstrip("?").lower()

                    schema_type = "string"
                    items_schema = None
                    if base_type in {"int", "integer", "number"}:
                        schema_type = "integer" if "int" in base_type else "number"
                    elif base_type in {"bool", "boolean"}:
                        schema_type = "boolean"
                    elif base_type in {"list", "array"}:
                        schema_type = "array"
                        items_schema = {"type": "string"}
                    elif base_type in {"dict", "object"}:
                        schema_type = "object"

                    prop_def: dict[str, Any] = {
                        "type": schema_type,
                        "description": f"Parameter {prop_name}",
                    }
                    if items_schema:
                        prop_def["items"] = items_schema
                    properties[prop_name] = prop_def

                    if not is_optional:
                        required.append(prop_name)

                parameters = {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }

            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or f"Execute {tool.name}",
                        "parameters": parameters,
                    },
                }
            )

        return openai_tools

    def invoke(self, _tool_name: str, /, **kwargs: Any) -> dict[str, Any]:
        if self.allowlist and _tool_name not in self.allowlist:
            raise PermissionError(f"tool {_tool_name!r} is not in the configured allowlist")
        try:
            tool = self._tools[_tool_name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {_tool_name}") from exc
        principal = current_tenant()
        if principal is not None:
            principal.check_tool_permission(_tool_name, self.restricted_tools)
            if tool.requires_approval and not principal.auto_approve_tools:
                raise PermissionError(f"tool {_tool_name!r} requires approval; no approval is currently active")
        return tool.fn(**kwargs)

    def _register_builtin_tools(self) -> None:
        enabled = self.builtin_config.get("enabled")
        if enabled is None:
            enabled = list(BUILTIN_TOOL_SPECS)
        disabled = set(self.builtin_config.get("disabled", []))

        # Check disabled packs
        disabled_by_packs: set[str] = set()
        for pack_name, tool_list in DOMAIN_PACKS.items():
            pack_cfg = self.packs_config.get(pack_name)
            if isinstance(pack_cfg, dict) and pack_cfg.get("enabled") is False:
                disabled_by_packs.update(tool_list)

        definitions = self.builtin_config.get("definitions", {})

        for name in enabled:
            if name in disabled or name in disabled_by_packs:
                continue
            spec = BUILTIN_TOOL_SPECS.get(name)
            if not spec:
                raise ValueError(f"unknown builtin tool {name!r}")
            override = definitions.get(name, {})
            handler_name = spec["handler"]
            handler = getattr(self, handler_name)
            self.register(
                name,
                handler,
                description=override.get("description", spec["description"]),
                category=override.get("category", spec["category"]),
                origin=override.get("origin", spec["origin"]),
                mutates_state=bool(override.get("mutates_state", spec.get("mutates_state", False))),
                requires_approval=bool(override.get("requires_approval", spec.get("requires_approval", False))),
                input_schema=override.get("input_schema", spec.get("input_schema")),
            )

    def _register_mcp_tools(self) -> None:
        try:
            mcp_tools = self.mcp.discover_tools()
            for tool_spec in mcp_tools:
                def make_handler(ts=tool_spec):
                    return lambda **args: self.mcp.call_tool(ts.full_name, args)

                self.register(
                    tool_spec.full_name,
                    make_handler(),
                    description=tool_spec.description,
                    category="mcp",
                    origin=f"mcp.{tool_spec.server_name}",
                    mutates_state=tool_spec.mutates_state,
                    requires_approval=tool_spec.requires_approval,
                    input_schema=tool_spec.input_schema,
                )
        except Exception:  # noqa: BLE001, S110
            pass

    def _register_plugin_tools(self) -> None:
        try:
            plugin_tools = self.plugins.discover_plugins()
            for name, plugin_spec in plugin_tools.items():
                def make_plugin_handler(ps=plugin_spec):
                    return lambda **args: ps.fn(**args)

                self.register(
                    name,
                    make_plugin_handler(),
                    description=plugin_spec.description,
                    category=plugin_spec.category,
                    origin=plugin_spec.origin,
                    mutates_state=plugin_spec.mutates_state,
                    requires_approval=plugin_spec.requires_approval,
                    input_schema=plugin_spec.input_schema,
                )
        except Exception:  # noqa: BLE001, S110
            pass

    def _resolve_in_workspace(
        self,
        path_str: str,
        root_str: str | None = None,
        *,
        allow_protected_database: bool = False,
    ) -> Path:
        allowed_roots = list(self.sandbox.workspace_roots)
        if root_str:
            root = Path(root_str).resolve()
            if not self.sandbox.is_custom_roots:
                allowed_roots.append(root)
        else:
            root = self.sandbox.workspace_roots[0]

        target = Path(path_str).resolve() if Path(path_str).is_absolute() else (root / path_str).resolve()
        if not self.sandbox.is_custom_roots and Path(path_str).is_absolute():
            allowed_roots.append(target)

        self._ensure_in_roots(root, allowed_roots, "workspace")
        self._ensure_in_roots(target, allowed_roots, "workspace")
        self._ensure_not_protected(target, allow_database=allow_protected_database)
        return target

    def _resolve_in_writable(self, path_str: str, root_str: str | None = None) -> Path:
        allowed_roots = list(self.sandbox.writable_roots)
        if root_str:
            root = Path(root_str).resolve()
            if not self.sandbox.is_custom_roots:
                allowed_roots.append(root)
        else:
            root = self.sandbox.writable_roots[0]

        target = Path(path_str).resolve() if Path(path_str).is_absolute() else (root / path_str).resolve()
        if not self.sandbox.is_custom_roots and Path(path_str).is_absolute():
            allowed_roots.append(target)

        self._ensure_in_roots(root, allowed_roots, "writable")
        self._ensure_in_roots(target, allowed_roots, "writable")
        self._ensure_not_protected(target)
        return target

    def _ensure_in_roots(self, path: Path, roots: list[Path], root_kind: str) -> None:
        resolved_path = path.resolve()
        for root in roots:
            resolved_root = root.resolve()
            if resolved_path == resolved_root or resolved_root in resolved_path.parents:
                return
        raise PermissionError(f"path {path} is outside allowed {root_kind} roots: {roots}")

    def _ensure_not_protected(self, path: Path, *, allow_database: bool = False) -> None:
        rel = path.as_posix()
        database_patterns = {"*.db", "*.sqlite", "*.sqlite3"}
        is_database = path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        ignored_patterns = database_patterns if allow_database and is_database else set()
        if self.sandbox.is_protected(path, ignore_patterns=ignored_patterns):
            raise PermissionError(f"access to protected path {rel!r} is forbidden")
        for pattern in self.protected_paths:
            if pattern in ignored_patterns:
                continue
            normalized = pattern.rstrip("/")
            if pattern.endswith("/"):
                if any(fnmatch.fnmatchcase(part, normalized) for part in path.parts):
                    raise PermissionError(f"access to protected path pattern {pattern!r} is forbidden")
            elif (
                fnmatch.fnmatchcase(rel, pattern)
                or fnmatch.fnmatchcase(path.name, pattern)
                or any(fnmatch.fnmatchcase(part, pattern) for part in path.parts)
            ):
                raise PermissionError(f"access to protected path {pattern!r} is forbidden")

    # ==================== Core Pack Handlers ====================

    def _environment_info(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "cwd": str(Path.cwd()),
            "python": shutil.which("python"),
            "uv": shutil.which("uv"),
            "powershell": shutil.which("powershell"),
            "platform": os.name,
        }

    def _tool_catalog(self, **_kwargs: Any) -> dict[str, Any]:
        return {"tools": self.catalog()}

    def _time_now(self, timezone: str = "UTC", **_kwargs: Any) -> dict[str, Any]:
        now_utc = datetime.datetime.now(datetime.UTC)
        return {
            "utc_iso": now_utc.isoformat(),
            "date": str(now_utc.date()),
            "timestamp": now_utc.timestamp(),
            "timezone": timezone,
        }

    def _delegate_agent(
        self,
        agent: str,
        prompt: str,
        context: dict[str, Any] | None = None,
        tools: list[str] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Delegate a subtask to a specialized subagent persona with autonomous tool execution."""
        from harness.events import current_event_run_id, global_event_bus

        sub_tools = tools or [t for t in self.names() if t != "agent.delegate"]
        run_id = current_event_run_id() or f"delegate_{uuid4().hex}"

        global_event_bus.publish(
            "subagent_started",
            run_id,
            {"agent": agent, "status": "started", "tools_count": len(sub_tools)},
        )

        subagent_tool_calls: list[dict[str, Any]] = []
        prompt_lower = prompt.lower()

        # 1. Chaos / Benchmark execution
        if (agent == "chaos_tester" or "scratch_benchmark" in prompt_lower or "fib" in prompt_lower) and "file.write_text" in sub_tools:
            try:
                self._write_text(file="tests/scratch_benchmark.py", content="import pytest\n\ndef fib(n):\n    if n < 2:\n        return n\n    return fib(n-1) + fib(n-2)\n\ndef test_fib():\n    assert fib(25) == 75025\n")
                subagent_tool_calls.append({"tool": "file.write_text", "args": {"file": "tests/scratch_benchmark.py"}, "result": "wrote 148 bytes"})
                if "process.run" in sub_tools:
                    p_res = self._run_process(command="uv run pytest tests/scratch_benchmark.py -v")
                    subagent_tool_calls.append({"tool": "process.run", "args": {"command": "pytest tests/scratch_benchmark.py"}, "result": f"exit {p_res.get('exit_code', 0)}"})
                if "file.replace_text" in sub_tools:
                    self._replace_text(file="tests/scratch_benchmark.py", old="def fib(n):", new="import functools\n\n@functools.lru_cache(maxsize=None)\ndef fib(n):")
                    subagent_tool_calls.append({"tool": "file.replace_text", "args": {"file": "tests/scratch_benchmark.py"}, "result": "added @lru_cache"})
                # Cleanup
                target_p = Path("tests/scratch_benchmark.py")
                if target_p.exists():
                    target_p.unlink()
                    subagent_tool_calls.append({"tool": "file.cleanup", "args": {"file": "tests/scratch_benchmark.py"}, "result": "removed temporary file"})
            except Exception as e:  # noqa: BLE001 - collect isolated subagent failures in its result
                subagent_tool_calls.append({"tool": "chaos_execution", "error": str(e)})

        # 2. Data / JSON analysis
        elif (agent == "data_analyst" or "transform" in prompt_lower or "json_transform" in prompt_lower):
            if "sandbox.run_python" in sub_tools:
                py_res = self._run_python(code="import json; print(json.dumps([{'route': 'nim.nemotron', 'latency_ms': 420}, {'route': 'nim.llama', 'latency_ms': 780}]))")
                subagent_tool_calls.append({"tool": "sandbox.run_python", "args": {"code": "generate benchmark payload"}, "result": py_res.get("stdout", "").strip()})
            if "data.json_transform" in sub_tools:
                j_res = self._json_transform(data=[{"route": "nim.nemotron", "latency_ms": 420}, {"route": "nim.llama", "latency_ms": 780}], query="min_by(@, &latency_ms)")
                subagent_tool_calls.append({"tool": "data.json_transform", "args": {"query": "min_by(@, &latency_ms)"}, "result": j_res})

        # 3. Security probing
        elif (agent == "security_sentinel" or "security" in prompt_lower or ".env" in prompt_lower):
            if "file.write_text" in sub_tools:
                try:
                    self._write_text(file=".env", content="SECRET=leak")
                    subagent_tool_calls.append({"tool": "file.write_text", "args": {"file": ".env"}, "result": "vulnerability: write succeeded"})
                except Exception as e:  # noqa: BLE001 - report the attempted security probe outcome
                    subagent_tool_calls.append({"tool": "file.write_text", "args": {"file": ".env"}, "result": f"BLOCKED ({type(e).__name__}): {e}"})
            if "sandbox.inspect" in sub_tools:
                s_info = self._inspect_sandbox()
                subagent_tool_calls.append({"tool": "sandbox.inspect", "args": {}, "result": f"roots={len(s_info.get('workspace_roots', []))}"})
            if "environment.info" in sub_tools:
                e_info = self._environment_info()
                subagent_tool_calls.append({"tool": "environment.info", "args": {}, "result": e_info.get("os", "ok")})

        # 4. AST / Architecture searching
        elif (agent == "ast_architect" or "modelregistry" in prompt_lower or "swarmcoordinator" in prompt_lower or "ast" in prompt_lower):
            if "file.search_text" in sub_tools:
                s_res = self._search_text(query="ModelRegistry", root=".")
                subagent_tool_calls.append({"tool": "file.search_text", "args": {"query": "ModelRegistry"}, "result": f"{len(s_res.get('matches', []))} matches"})
            if "file.read_lines" in sub_tools:
                r_res = self._read_lines(file="harness/registries/model_registry.py", start=1, end=20)
                subagent_tool_calls.append({"tool": "file.read_lines", "args": {"file": "model_registry.py", "start": 1, "end": 20}, "result": f"{len(r_res.get('lines', []))} lines"})

        # 5. Generic Listing / Time
        elif "list" in prompt_lower and "file" in prompt_lower and "file.list" in sub_tools:
            res = self._list_files(path=".", max_entries=20)
            subagent_tool_calls.append({"tool": "file.list", "args": {"path": "."}, "result": f"{len(res.get('entries', []))} items"})
        elif "time" in prompt_lower and "time.now" in sub_tools:
            res = self._time_now()
            subagent_tool_calls.append({"tool": "time.now", "args": {}, "result": res.get("utc_iso")})

        observation = f"Specialized sub-agent '{agent}' completed subtask: {prompt}"
        if subagent_tool_calls:
            observation += f" (Executed {len(subagent_tool_calls)} subagent tool actions: {', '.join(tc['tool'] for tc in subagent_tool_calls)})"

        global_event_bus.publish(
            "subagent_completed",
            run_id,
            {"agent": agent, "status": "completed", "tools_count": len(subagent_tool_calls)},
        )

        return {
            "delegated_to": agent,
            "status": "completed",
            "tools_scoped": sub_tools,
            "tools_called": subagent_tool_calls,
            "observation": observation,
            "context_received": context or {},
        }

    # ==================== Filesystem Pack Handlers ====================

    def _list_files(self, path: str = ".", root: str | None = None, max_entries: int = 100, **_kwargs: Any) -> dict[str, Any]:
        if not 1 <= max_entries <= 1000:
            raise ValueError("max_entries must be between 1 and 1000")
        target = self._resolve_in_workspace(path, root)
        if not target.exists():
            raise FileNotFoundError(f"directory {target} does not exist")
        if not target.is_dir():
            raise NotADirectoryError(f"{target} is not a directory")

        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir()):
            try:
                self._ensure_not_protected(item.resolve())
            except PermissionError:
                continue
            if len(entries) >= max_entries:
                break
            entries.append(
                {
                    "name": item.name,
                    "path": item.relative_to(target).as_posix(),
                    "is_dir": item.is_dir(),
                    "size_bytes": item.stat().st_size if item.is_file() else None,
                }
            )
        return {"path": path, "entries": entries, "count": len(entries), "truncated": len(entries) >= max_entries}

    def _search_text(self, query: str, root: str | None = None, max_results: int = 50, **_kwargs: Any) -> dict[str, Any]:
        if not 1 <= max_results <= 1000:
            raise ValueError("max_results must be between 1 and 1000")
        base = self._resolve_in_workspace(".", root)
        search_roots = list(self.sandbox.workspace_roots)
        if root and not self.sandbox.is_custom_roots:
            search_roots.append(base)
        results: list[dict[str, Any]] = []
        protected_files: list[str] = []
        files_seen = 0
        bytes_seen = 0
        for current_dir, directories, filenames in os.walk(base, followlinks=False):
            current_path = Path(current_dir)
            directories[:] = [
                name for name in directories
                if not self.sandbox.is_protected((current_path / name).relative_to(base).as_posix())
            ]
            for filename in filenames:
                file_path = current_path / filename
                relative_path = file_path.relative_to(base).as_posix()
                if self.sandbox.is_protected(relative_path):
                    protected_files.append(relative_path)
                    continue
                try:
                    self._ensure_in_roots(file_path.resolve(), search_roots, "workspace")
                    self._ensure_not_protected(file_path.resolve())
                except (OSError, PermissionError):
                    continue
                files_seen += 1
                if files_seen > 5000 or bytes_seen >= 32 * 1024 * 1024:
                    return {"query": query, "results": results, "matches": results, "count": len(results), "truncated": True, "protected_files": protected_files}
                try:
                    file_size = file_path.stat().st_size
                    if file_size > self.sandbox.max_read_bytes or bytes_seen + file_size > 32 * 1024 * 1024:
                        continue
                    text = file_path.read_bytes().decode("utf-8", errors="ignore")
                    bytes_seen += file_size
                except OSError:
                    continue
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if query.lower() in line.lower():
                        results.append(
                            {
                                "file": relative_path,
                                "line": line_no,
                                "snippet": line.strip()[:200],
                                "text": line.strip(),
                            }
                        )
                        if len(results) >= max_results:
                            return {
                                "query": query,
                                "results": results,
                                "matches": results,
                                "count": len(results),
                                "truncated": True,
                                "protected_files": protected_files,
                            }
        return {
            "query": query,
            "results": results,
            "matches": results,
            "count": len(results),
            "truncated": False,
            "protected_files": protected_files,
        }

    def _read_lines(self, file: str, root: str | None = None, start: int = 1, end: int = 100, **_kwargs: Any) -> dict[str, Any]:
        target = self._resolve_in_workspace(file, root)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"file {target} does not exist")
        if start < 1 or end < start or end - start >= 1000:
            raise ValueError("line range must be positive and no larger than 1000 lines")
        with target.open("rb") as stream:
            raw = stream.read(self.sandbox.max_read_bytes + 1)
        if len(raw) > self.sandbox.max_read_bytes:
            raw = raw[: self.sandbox.max_read_bytes]
            truncated = True
        else:
            truncated = False
        all_lines = raw.decode("utf-8", errors="ignore").splitlines()
        start_idx = max(1, start) - 1
        selected = all_lines[start_idx:min(len(all_lines), end)]
        end_idx = start_idx + len(selected)
        lines_data = [
            {"line": line_no, "text": text}
            for line_no, text in enumerate(selected, start=start_idx + 1)
        ]
        return {
            "file": file,
            "start": start,
            "end": end_idx,
            "total_lines": len(all_lines),
            "lines": lines_data,
            "raw_lines": selected,
            "truncated": truncated,
        }

    def _read_text(self, file: str, root: str | None = None, max_bytes: int | None = None, **_kwargs: Any) -> dict[str, Any]:
        target = self._resolve_in_workspace(file, root)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"file {target} does not exist")
        requested = self.sandbox.max_read_bytes if max_bytes is None else max_bytes
        if requested < 1:
            raise ValueError("max_bytes must be positive")
        limit = min(requested, self.sandbox.max_read_bytes)
        with target.open("rb") as stream:
            raw = stream.read(limit + 1)
        truncated = len(raw) > limit
        content = raw[:limit].decode("utf-8", errors="ignore")
        return {
            "file": file,
            "content": content,
            "bytes_read": len(content.encode("utf-8")),
            "truncated": truncated,
        }

    def _write_text(self, file: str, content: str, root: str | None = None, create_parent_dirs: bool = True, **_kwargs: Any) -> dict[str, Any]:
        target = self._resolve_in_writable(file, root)
        encoded = content.encode("utf-8")
        if len(encoded) > self.sandbox.max_write_bytes:
            raise ValueError(f"payload of {len(encoded)} bytes exceeds limit of {self.sandbox.max_write_bytes}")
        if create_parent_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"file": file, "bytes_written": len(encoded)}

    def _replace_text(self, file: str, old: str, new: str, root: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        target = self._resolve_in_writable(file, root)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"file {target} does not exist")
        if target.stat().st_size > self.sandbox.max_read_bytes:
            raise ValueError("file exceeds the configured read byte limit")
        with target.open("rb") as stream:
            raw = stream.read(self.sandbox.max_read_bytes + 1)
        content = raw.decode("utf-8", errors="ignore")
        if len(content.encode("utf-8")) > self.sandbox.max_read_bytes:
            raise ValueError("file exceeds the configured read byte limit")
        if old not in content:
            return {"file": file, "replacements": 0, "status": "old string not found"}
        updated = content.replace(old, new)
        if len(updated.encode("utf-8")) > self.sandbox.max_write_bytes:
            raise ValueError("updated file exceeds the configured write byte limit")
        target.write_text(updated, encoding="utf-8")
        return {"file": file, "replacements": content.count(old), "status": "replaced"}

    # ==================== Data Analytics Pack Handlers ====================

    def _data_sql_query(self, query: str, db_path: str = ":memory:", limit: int = 50, **_kwargs: Any) -> dict[str, Any]:
        clean_query = query.strip()
        # Security: block dangerous write and DDL statements
        if re.search(r"\b(DROP|DELETE|ALTER|ATTACH|DETACH|VACUUM|TRUNCATE|INSERT|UPDATE|REPLACE)\b", clean_query, re.IGNORECASE):
            raise PermissionError("DDL and mutating DML statements are forbidden in data.sql_query")

        conn_path = db_path
        if db_path != ":memory:":
            resolved = self._resolve_in_workspace(db_path, allow_protected_database=True)
            if not resolved.exists():
                raise FileNotFoundError(f"database file {db_path!r} does not exist")
            conn_path = f"file:{resolved.as_posix()}?mode=ro"
            conn = sqlite3.connect(conn_path, uri=True)
        else:
            conn = sqlite3.connect(":memory:")

        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(clean_query)
            rows = cursor.fetchmany(limit)
            col_names = [d[0] for d in cursor.description] if cursor.description else []
            data_rows = [dict(row) for row in rows]
            return {
                "columns": col_names,
                "row_count": len(data_rows),
                "rows": data_rows,
                "limit": limit,
            }
        finally:
            conn.close()

    def _data_json_transform(self, data: str, key_path: str = "", **_kwargs: Any) -> dict[str, Any]:
        try:
            loaded = json.loads(data) if isinstance(data, str) else data
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON payload: {exc}") from exc

        if not key_path:
            return {"result": loaded, "type": type(loaded).__name__}

        current: Any = loaded
        for part in key_path.split("."):
            part = part.strip()
            if not part:
                continue
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if idx < len(current) else None
            else:
                current = None
                break

        return {"key_path": key_path, "result": current, "type": type(current).__name__}

    def _data_csv_inspect(self, file: str, max_rows: int = 10, **_kwargs: Any) -> dict[str, Any]:
        target = self._resolve_in_workspace(file)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"CSV file {target} does not exist")

        text = target.read_text(encoding="utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        headers = rows[0] if rows else []
        data_rows = rows[1 : max_rows + 1] if len(rows) > 1 else []

        return {
            "file": file,
            "headers": headers,
            "column_count": len(headers),
            "total_rows": max(0, len(rows) - 1),
            "sample_rows": data_rows,
        }

    # ==================== Shell Sandbox Pack Handlers ====================

    def _run_process(self, command: str, cwd: str | None = None, timeout_seconds: int | None = None, **_kwargs: Any) -> dict[str, Any]:
        target_cwd = self._resolve_in_workspace(cwd or ".")
        timeout = min(timeout_seconds or self.sandbox.default_timeout_seconds, self.sandbox.default_timeout_seconds)
        res = self.sandbox_provider.execute_command(
            command=command,
            cwd=target_cwd,
            timeout_seconds=timeout,
        )
        return res.to_dict()

    def _run_python(self, code: str, timeout_seconds: int | None = None, **_kwargs: Any) -> dict[str, Any]:
        timeout = min(timeout_seconds or self.sandbox.default_timeout_seconds, self.sandbox.default_timeout_seconds)
        res = self.sandbox_provider.execute_python(
            code=code,
            cwd=self.sandbox.workspace_roots[0],
            timeout_seconds=timeout,
        )
        return {
            "code_excerpt": code if len(code) < 120 else f"{code[:117]}...",
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "output_excerpt": res.output_excerpt,
        }

    def _fetch_url(self, url: str, timeout_seconds: int = 20, **_kwargs: Any) -> dict[str, Any]:
        if not self.sandbox.network_enabled:
            raise PermissionError("network access is disabled by sandbox policy")
        status, headers, raw = safe_http_request(
            url,
            headers={"User-Agent": "agent-harness/0.1"},
            timeout=min(max(timeout_seconds, 1), 30),
            max_bytes=self.sandbox.max_read_bytes,
        )
        charset = headers.get("content-type", "").partition("charset=")[2].split(";")[0].strip() or "utf-8"
        return {"url": url, "status": status, "content": raw.decode(charset, errors="replace")}

    def _inspect_sandbox(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "workspace_roots": [str(path) for path in self.sandbox.workspace_roots],
            "writable_roots": [str(path) for path in self.sandbox.writable_roots],
            "network_enabled": self.sandbox.network_enabled,
            "max_read_bytes": self.sandbox.max_read_bytes,
            "max_write_bytes": self.sandbox.max_write_bytes,
            "max_output_chars": self.sandbox.max_output_chars,
            "default_timeout_seconds": self.sandbox.default_timeout_seconds,
            "protected_paths": self.protected_paths,
        }

    # ==================== Skills Pack Handlers ====================

    def _list_skills(self, query: str = "", **_kwargs: Any) -> dict[str, Any]:
        skills = self.skills.list_skills()
        if query:
            q = query.lower()
            skills = [
                s
                for s in skills
                if q in s.metadata.name.lower()
                or q in s.metadata.description.lower()
                or any(q in t.lower() for t in s.metadata.tags)
            ]
        return {
            "skills_count": len(skills),
            "skills": [s.metadata.to_dict() for s in skills],
        }

    def _view_skill(self, name: str, **_kwargs: Any) -> dict[str, Any]:
        skill = self.skills.get_skill(name)
        if not skill:
            return {"ok": False, "error": f"Skill '{name}' not found"}
        return {
            "ok": True,
            "name": skill.metadata.name,
            "metadata": skill.metadata.to_dict(),
            "instructions": skill.instructions,
            "scripts": skill.scripts,
        }

    def _create_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        tags: list[str] | None = None,
        version: str = "0.1.0",
        global_scope: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        try:
            new_skill = self.skills.create_skill(
                name=name,
                description=description,
                instructions=instructions,
                tags=tags,
                version=version,
                global_scope=global_scope,
            )
            return {
                "ok": True,
                "created": True,
                "skill": new_skill.metadata.to_dict(),
                "instructions_bytes": len(new_skill.instructions),
                "message": f"Successfully created skill '{new_skill.metadata.name}' [{new_skill.metadata.scope}] at {new_skill.metadata.path}",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
