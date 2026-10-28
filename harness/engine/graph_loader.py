"""Graph discovery and dynamic declarative graph loading."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.declarative_graphs import DeclarativeGraphRegistry
from harness.engine.graph import GraphSpec

GRAPH_MODULES = {
    "code-investigate": "harness.graphs.code_investigate",
    "research-report": "harness.graphs.research_report",
    "brainstorm": "harness.graphs.brainstorm",
    "ai-accelerator": "harness.graphs.ai_accelerator",
}


def load_graph_with_fingerprint(
    name: str,
    config: dict[str, Any] | None = None,
    workspace_root: str = ".",
) -> tuple[GraphSpec, str]:
    """Load one graph definition and fingerprint that same immutable in-memory definition."""
    if not isinstance(name, str) or len(name) > 64 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise KeyError("graph not found")
    if name in GRAPH_MODULES:
        module = importlib.import_module(GRAPH_MODULES[name])
        module_path = getattr(module, "__file__", None)
        payload = Path(module_path).read_bytes() if module_path else GRAPH_MODULES[name].encode()
        graph = module.build_graph()
        if not isinstance(graph, GraphSpec):
            raise TypeError(f"{GRAPH_MODULES[name]}.build_graph() did not return GraphSpec")
    else:
        registry = DeclarativeGraphRegistry(workspace_root=workspace_root, config=config)
        declarative = registry.get_graph(name)
        if declarative is None:
            raise KeyError(f"graph not found: {name}")
        payload = json.dumps(asdict(declarative), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        graph = declarative.to_engine_spec()

    runtime_sections = {key: (config or {}).get(key) for key in ("models", "tools", "mcp_servers", "packs", "governance", "prompts")}
    runtime_payload = json.dumps(runtime_sections, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    digest = hashlib.sha256(payload + b"\0" + runtime_payload).hexdigest()
    return graph, digest


def graph_fingerprint(name: str, config: dict[str, Any] | None = None, workspace_root: str = ".") -> str:
    """Return a stable digest for the current graph and execution-affecting config."""
    return load_graph_with_fingerprint(name, config=config, workspace_root=workspace_root)[1]


def load_graph(
    name: str,
    config: dict[str, Any] | None = None,
    workspace_root: str = ".",
) -> GraphSpec:
    """Load a graph by name from builtin Python modules or declarative YAML/JSON configurations."""
    return load_graph_with_fingerprint(name, config=config, workspace_root=workspace_root)[0]


def list_graphs(config: dict[str, Any] | None = None, workspace_root: str = ".") -> list[str]:
    """List all available graph names including builtins and declarative workflows."""
    names = set(GRAPH_MODULES.keys())
    registry = DeclarativeGraphRegistry(workspace_root=workspace_root, config=config)
    names.update(registry.discover_graphs().keys())
    return sorted(names)
