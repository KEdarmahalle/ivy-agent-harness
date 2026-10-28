"""Dynamic system prompt and context builder."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from harness.config import deep_get
from harness.prompts.templates import (
    COGNITIVE_PROTOCOLS_TEMPLATE,
    render_identity,
    render_platform_constraints,
    render_tool_protocol,
)


def _read_excerpt(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return text[:max_chars].strip()
    except OSError:
        return ""


def build_harness_context(config: dict[str, Any], workspace_root: str | Path | None = None) -> str:
    """Build local repository and harness execution context."""
    route_names = ", ".join(sorted((config.get("models") or {}).keys()))
    tool_names = ", ".join(deep_get(config, "tools.builtins.enabled", []) or [])
    chat_graph = deep_get(config, "chat.graph", "nim")
    chat_node = deep_get(config, "chat.node", "example")
    root = Path(workspace_root).resolve() if workspace_root else Path.cwd()
    readme_excerpt = _read_excerpt(root / "README.md", max_chars=1500)
    app_name = str(deep_get(config, "app.name", "Agent Harness"))
    host_os = f"{sys.platform} ({'Windows NT' if os.name == 'nt' else os.name})"

    workspace_files: list[str] = []
    for directory in ["harness", "tests"]:
        dir_path = root / directory
        if dir_path.exists():
            for p in sorted(dir_path.rglob("*.py")):
                if "__pycache__" not in p.parts:
                    workspace_files.append(p.relative_to(root).as_posix())

    file_tree_str = "\n".join(f"  - {f}" for f in workspace_files[:40])

    return (
        f"Project: {app_name} at {root.as_posix()}\n"
        f"Operating System: {host_os}\n"
        f"Default chat route: {chat_graph}.{chat_node}\n"
        f"Configured model routes: {route_names}\n"
        f"Enabled builtin tools: {tool_names}\n"
        f"Active Codebase Modules:\n{file_tree_str}\n\n"
        "Architecture: FastAPI service plus direct CLI chat, config-driven model registry, "
        "config-driven tool registry, sandbox policy, SQLite run store, graph runner, traces, "
        "resume flow, model smoke, and verification gates.\n"
        f"README excerpt:\n{readme_excerpt}"
    )


def build_smart_system_prompt(
    base_prompt: str,
    tools_catalog: dict[str, Any],
    harness_context: str,
    memory_context: str = "",
    skills_context: str = "",
    project_context: str = "",
    config: dict[str, Any] | None = None,
) -> str:
    """Compose full agent system prompt with tools, identity, rules, and workspace context."""
    catalog_json = json.dumps(tools_catalog, indent=2)[:6000]
    memory_section = f"\n\n{memory_context}" if memory_context else ""
    skills_section = f"\n\n{skills_context}" if skills_context else ""
    project_section = f"\n\n# DETECTED PROJECT CONTEXT & STACK\n{project_context}" if project_context else ""
    cfg = config or {}
    agent_name = str(deep_get(cfg, "chat.agent_name", "Ivy"))
    app_name = str(deep_get(cfg, "app.name", "Agent Harness"))
    host_os = f"{sys.platform} ({'Windows NT' if os.name == 'nt' else os.name})"
    workspace_root = Path.cwd().as_posix()
    sandbox_provider = str(deep_get(cfg, "tools.sandbox.provider", "local"))
    sandbox_network_enabled = bool(deep_get(cfg, "tools.sandbox.network_enabled", False))

    identity = render_identity(agent_name=agent_name, app_name=app_name)
    platform = render_platform_constraints(
        host_os=host_os,
        workspace_root=workspace_root,
        sandbox_provider=sandbox_provider,
        network_enabled=sandbox_network_enabled,
    )
    protocol = render_tool_protocol(catalog_json=catalog_json)

    return (
        f"{base_prompt}\n\n"
        f"{identity}\n\n"
        f"{platform}\n\n"
        f"{protocol}\n\n"
        f"{COGNITIVE_PROTOCOLS_TEMPLATE}\n\n"
        f"Local harness context:\n{harness_context}"
        f"{project_section}"
        f"{memory_section}"
        f"{skills_section}"
    )


def build_cacheable_system_prompt_prefix(
    base_prompt: str,
    tools_catalog: dict[str, Any],
    harness_context: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Build strictly invariant, deterministic system prompt prefix for 95%+ prompt cache hit rate."""
    return build_smart_system_prompt(
        base_prompt=base_prompt,
        tools_catalog=tools_catalog,
        harness_context=harness_context,
        memory_context="",
        skills_context="",
        project_context="",
        config=config,
    )
