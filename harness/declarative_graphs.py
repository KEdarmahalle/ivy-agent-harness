"""Universal declarative graph loader and dynamic agentic workflow DAG engine."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.config import deep_get

logger = logging.getLogger(__name__)
from harness.context import apply_observation_masking
from harness.engine.agentic_router import AgenticGraphRouter
from harness.engine.graph import GraphSpec, NodeSpec
from harness.engine.schemas import NodeResult, RunSnapshot

MAX_WORKFLOW_FILE_BYTES = 512 * 1024
MAX_WORKFLOW_FILES = 200


@dataclass
class DeclarativeNodeConfig:
    name: str
    description: str = ""
    route: str = "default"
    system_prompt: str = ""
    tools: list[str] = field(default_factory=list)
    inputs: list[str] | None = None  # State channel projection (only expose specific keys)
    outputs: dict[str, str] | None = None  # State output mapping
    gate: str | None = None  # e.g., 'human_approval'
    next_node: str | dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> DeclarativeNodeConfig:
        return cls(
            name=name,
            description=str(data.get("description") or data.get("goal") or ""),
            route=str(data.get("route", "default")),
            system_prompt=str(data.get("system_prompt", "")),
            tools=list(data.get("tools", [])),
            inputs=list(data.get("inputs")) if data.get("inputs") is not None else None,
            outputs=dict(data.get("outputs")) if data.get("outputs") is not None else None,
            gate=data.get("gate"),
            next_node=data.get("next"),
        )


@dataclass
class DeclarativeGraphSpec:
    name: str
    description: str
    start_node: str
    routing_mode: str = "deterministic"  # "deterministic", "agentic", "hybrid"
    nodes: dict[str, DeclarativeNodeConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> DeclarativeGraphSpec:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("graph name must be 1-64 letters, digits, underscores, or hyphens")
        if not isinstance(data, dict):
            raise TypeError("graph manifest must be an object")
        if "name" in data and data["name"] != name:
            raise ValueError("manifest name must match its registered graph name")
        description = data.get("description", f"Declarative workflow {name}")
        if not isinstance(description, str) or len(description) > 2_000:
            raise ValueError("description must be a string of at most 2000 characters")
        unknown_fields = set(data) - {"name", "description", "start_node", "entry", "routing_mode", "nodes"}
        if unknown_fields:
            raise ValueError(f"unsupported graph manifest fields: {sorted(unknown_fields)}")
        start = data.get("start_node", data.get("entry"))
        if not isinstance(start, str) or not start:
            raise ValueError("start_node is required")
        routing_mode = data.get("routing_mode", "deterministic")
        if routing_mode not in {"deterministic", "agentic", "hybrid"}:
            raise ValueError("routing_mode must be deterministic, agentic, or hybrid")
        raw_nodes = data.get("nodes", {})
        if not isinstance(raw_nodes, dict) or not raw_nodes or len(raw_nodes) > 128:
            raise ValueError("nodes must be a non-empty object with at most 128 entries")
        if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", k) for k in raw_nodes):
            raise ValueError("node names must be 1-64 letters, digits, underscores, or hyphens")
        for node_name, node_data in raw_nodes.items():
            if not isinstance(node_data, dict):
                raise TypeError(f"node {node_name!r} must be an object")
            allowed_node_fields = {"description", "goal", "route", "system_prompt", "tools", "inputs", "outputs", "gate", "next"}
            if set(node_data) - allowed_node_fields:
                raise ValueError(f"node {node_name!r} has unsupported fields")
            for key in ("description", "goal", "route", "system_prompt"):
                if key in node_data and not isinstance(node_data[key], str):
                    raise ValueError(f"node {node_name!r} field {key!r} must be a string")
                if key in node_data and len(node_data[key]) > (20000 if key == "system_prompt" else 2000):
                    raise ValueError(f"node {node_name!r} field {key!r} exceeds its length limit")
            if len(str(node_data.get("system_prompt", ""))) > 20000:
                raise ValueError(f"node {node_name!r} system_prompt exceeds 20000 characters")
            for key in ("tools", "inputs"):
                value = node_data.get(key, [])
                if not isinstance(value, list) or len(value) > 50 or any(not isinstance(v, str) or len(v) > 128 for v in value):
                    raise ValueError(f"node {node_name!r} field {key!r} must be a bounded string list")
            tools = node_data.get("tools", [])
            if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", tool) for tool in tools):
                raise ValueError(f"node {node_name!r} has an invalid tool capability name")
            inputs = node_data.get("inputs", [])
            if any(value.startswith("_") for value in inputs):
                raise ValueError(f"node {node_name!r} cannot project internal runtime state")
            outputs = node_data.get("outputs")
            if outputs is not None and (
                not isinstance(outputs, dict) or len(outputs) > 64
                or any(not isinstance(k, str) or len(k) > 128 or not isinstance(v, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", v) for k, v in outputs.items())
            ):
                raise ValueError(f"node {node_name!r} outputs must be a bounded string-to-state-key map")
            if outputs and any(v.startswith(("_", "approved_")) or v in {"required_approval", "resume_node", "approvals"} for v in outputs.values()):
                raise ValueError(f"node {node_name!r} outputs cannot write reserved runtime or approval state")
            if node_data.get("gate") not in (None, "human_approval"):
                raise ValueError(f"node {node_name!r} has unsupported gate")
        nodes = {
            node_name: DeclarativeNodeConfig.from_dict(node_name, node_data)
            for node_name, node_data in raw_nodes.items()
        }
        if start not in nodes:
            raise ValueError("start_node must name a declared node")
        if any(node.gate == "human_approval" for node in nodes.values()) and routing_mode == "agentic":
            raise ValueError("agentic routing cannot be combined with human approval gates; use explicit deterministic gate edges")
        for node_name, node_data in raw_nodes.items():
            target = node_data.get("next")
            targets: list[Any] = []
            if isinstance(target, str):
                if target != "agentic":
                    targets.append(target)
                elif any(node.gate == "human_approval" for node in nodes.values()):
                    raise ValueError("agentic routing cannot be combined with human approval gates; use explicit deterministic gate edges")
            elif target is not None:
                if not isinstance(target, dict):
                    raise TypeError(f"node {node_name!r} next must be a node, route map, or agentic object")
                if target.get("mode") == "agentic":
                    if set(target) - {"mode", "candidates"}:
                        raise ValueError(f"node {node_name!r} agentic route has unsupported fields")
                    if any(node.gate == "human_approval" for node in nodes.values()):
                        raise ValueError("agentic routing cannot be combined with human approval gates; use explicit deterministic gate edges")
                    candidates = target.get("candidates", list(nodes))
                    if not isinstance(candidates, list) or not candidates:
                        raise ValueError(f"node {node_name!r} agentic candidates must be a non-empty list")
                    targets.extend(candidates)
                else:
                    if "mode" in target:
                        raise ValueError(f"node {node_name!r} has an unsupported routing mode")
                    if len(target) > 128:
                        raise ValueError(f"node {node_name!r} has too many route conditions")
                    if any(not isinstance(k, str) or len(k) > 128 for k in target):
                        raise ValueError(f"node {node_name!r} route conditions must be bounded strings")
                    targets.extend(v for k, v in target.items() if k != "mode")
            if any(not isinstance(v, str) or v not in nodes for v in targets):
                raise ValueError(f"node {node_name!r} routes to an undeclared node")
        return cls(
            name=name,
            description=description,
            start_node=start,
            routing_mode=routing_mode,
            nodes=nodes,
        )

    def to_engine_spec(self) -> GraphSpec:
        """Convert declarative graph configuration into executable GraphSpec for GraphRunner."""
        engine_nodes: dict[str, NodeSpec] = {}

        for node_name, node_cfg in self.nodes.items():
            handler = self._make_node_handler(node_cfg)
            engine_nodes[node_name] = NodeSpec(name=node_name, handler=handler)

        return GraphSpec(
            name=self.name,
            start_node=self.start_node,
            nodes=engine_nodes,
        )

    def _make_node_handler(self, node_cfg: DeclarativeNodeConfig):
        def handler(context: Any, snapshot: RunSnapshot) -> NodeResult:
            context.trace("node_started", {"node": node_cfg.name, "graph": self.name})

            # Track visited history
            visited_history: list[str] = snapshot.state.setdefault("_visited_history", [])
            visited_history.append(node_cfg.name)

            # 1. Human-in-the-loop Gate evaluation
            if node_cfg.gate == "human_approval":
                context.trace("gate_triggered", {"node": node_cfg.name, "gate": "human_approval"})
                next_step, routing_updates = self._resolve_next(node_cfg, snapshot, context)
                gate_updates = {
                    "required_approval": f"gate_{node_cfg.name}",
                    "resume_node": next_step,
                }
                gate_updates.update(routing_updates)
                return NodeResult(
                    updates=gate_updates,
                    status="needs_human",
                    next_node=next_step,
                )

            # 2. Model completion if route or system_prompt configured
            prompt_input = str(snapshot.input.get("prompt") or snapshot.input.get("task") or snapshot.state.get("prompt") or "")

            # Channel / Input state projection (only expose specified inputs if defined)
            if node_cfg.inputs is not None:
                projected_state = {k: snapshot.state[k] for k in node_cfg.inputs if k in snapshot.state}
            else:
                projected_state = {k: v for k, v in snapshot.state.items() if not str(k).startswith("_")}

            state_context = json.dumps(projected_state, indent=2, default=str)
            messages = [
                {
                    "role": "system",
                    "content": node_cfg.system_prompt or f"Execute node {node_cfg.name} in graph {self.name}.",
                },
                {
                    "role": "user",
                    "content": f"Task input: {prompt_input}\n\nCurrent workflow state:\n{state_context}",
                },
            ]

            tools_schema = (
                context.tools.to_openai_tools(node_cfg.tools)
                if node_cfg.tools and hasattr(context.tools, "to_openai_tools")
                else None
            )

            parts = node_cfg.route.split(".", 1)
            graph_part = parts[0] if len(parts) > 1 else "chat"
            node_part = parts[1] if len(parts) > 1 else node_cfg.name

            # Autonomous tool calling loop (up to max_turns)
            max_turns = 5
            turn = 0
            executed_tools: list[dict[str, Any]] = []
            reply: Any = ""

            while turn < max_turns:
                turn += 1
                # Apply rolling observation masking
                masked_messages = apply_observation_masking(messages, keep_recent=2)
                reply = context.models.complete(
                    graph_part,
                    node_part,
                    masked_messages,
                    tools=tools_schema,
                )

                if hasattr(reply, "tool_calls") and reply.tool_calls:
                    messages.append({"role": "assistant", "content": str(reply)})
                    for tc in reply.tool_calls:
                        try:
                            if tc.name not in node_cfg.tools:
                                raise PermissionError(f"tool {tc.name!r} is not declared for node {node_cfg.name!r}")
                            t_res = context.tools.invoke(tc.name, **tc.arguments)
                            executed_tools.append({"tool": tc.name, "arguments": tc.arguments, "result": t_res})
                            context.trace("tool_called", {"tool": tc.name, "arguments": tc.arguments, "result": t_res})
                            messages.append({
                                "role": "tool",
                                "name": tc.name,
                                "content": json.dumps(t_res, default=str),
                            })
                        except Exception as tool_err:  # noqa: BLE001
                            context.trace("tool_failed", {"tool": tc.name, "error": str(tool_err)})
                            messages.append({
                                "role": "tool",
                                "name": tc.name,
                                "content": json.dumps({"error": str(tool_err)}),
                            })
                    continue

                break

            context.trace("model_completed", {"node": node_cfg.name, "route": node_cfg.route})

            updates: dict[str, Any] = {
                f"{node_cfg.name}_output": str(reply),
                f"{node_cfg.name}_tools": executed_tools,
                "last_output": str(reply),
            }

            # Map outputs if custom channel output mapping is configured
            if node_cfg.outputs:
                for out_src, out_dst in node_cfg.outputs.items():
                    if out_src in {"output", "reply", "result"}:
                        updates[out_dst] = str(reply)
                    elif out_src in {"tools", "executed_tools"}:
                        updates[out_dst] = executed_tools
                    elif out_src in updates:
                        updates[out_dst] = updates[out_src]

            # 3. Dynamic Node Routing Resolution (Deterministic vs Agentic vs Hybrid)
            next_step, routing_updates = self._resolve_next(node_cfg, snapshot, context)
            updates.update(routing_updates)

            return NodeResult(updates=updates, next_node=next_step)

        return handler

    def _resolve_next(
        self,
        node_cfg: DeclarativeNodeConfig,
        snapshot: RunSnapshot,
        context: Any,
    ) -> tuple[str | None, dict[str, Any]]:
        """Resolve next node deterministically or through AgenticGraphRouter."""
        next_spec = node_cfg.next_node
        is_agentic = (
            self.routing_mode == "agentic"
            or next_spec == "agentic"
            or (isinstance(next_spec, dict) and next_spec.get("mode") == "agentic")
        )

        if is_agentic:
            router = AgenticGraphRouter(models=context.models)
            nodes_catalog = {
                n_name: {
                    "description": n.description or n.system_prompt,
                    "tools": n.tools,
                    "route": n.route,
                }
                for n_name, n in self.nodes.items()
            }
            allowed_candidates = None
            if isinstance(next_spec, dict) and "candidates" in next_spec:
                allowed_candidates = list(next_spec["candidates"])

            goal = str(snapshot.input.get("prompt") or snapshot.input.get("task") or self.description)
            visited = snapshot.state.get("_visited_history", [])

            decision = router.decide_next_node(
                graph_name=self.name,
                current_node=node_cfg.name,
                nodes_catalog=nodes_catalog,
                goal=goal,
                state=snapshot.state,
                visited_nodes=visited,
                allowed_candidates=allowed_candidates,
            )

            if decision.state_updates.get("routing_error"):
                raise RuntimeError("agentic workflow routing failed closed")

            routing_data = {
                "_last_agentic_decision": {
                    "from_node": node_cfg.name,
                    "to_node": decision.next_node,
                    "action": decision.action,
                    "rationale": decision.rationale,
                }
            }
            routing_data.update(decision.state_updates)
            context.trace("agentic_route_decision", routing_data["_last_agentic_decision"])
            return decision.next_node, routing_data

        # Standard Deterministic Routing
        if not next_spec:
            return None, {}
        if isinstance(next_spec, str):
            return next_spec, {}
        if isinstance(next_spec, dict):
            for condition_key, target_node in next_spec.items():
                if snapshot.state.get(condition_key) or snapshot.state.get("last_verdict") == condition_key:
                    return target_node, {}
            return next_spec.get("default"), {}
        return None, {}


class DeclarativeGraphRegistry:
    """Registry that discovers and parses declarative workflows from YAML/JSON files and harness config."""

    def __init__(self, workspace_root: str | Path = ".", config: dict[str, Any] | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.config = config or {}
        self._graphs: dict[str, DeclarativeGraphSpec] = {}
        self._load_from_config()
        self._discover_files()

    def _load_from_config(self) -> None:
        raw_workflows = deep_get(self.config, "workflows", {}) or deep_get(self.config, "graphs", {}) or {}
        if not isinstance(raw_workflows, dict):
            raise TypeError("workflows configuration must be an object")
        for name, data in raw_workflows.items():
            # Legacy graph metadata entries are not manifests and cannot execute.
            if isinstance(data, dict) and not any(k in data for k in ("nodes", "start_node", "entry")):
                continue
            self._register_manifest(name, data)

    def _register_manifest(self, name: str, data: dict[str, Any]) -> None:
        graph = DeclarativeGraphSpec.from_dict(name, data)
        configured = deep_get(self.config, "tools.allowlist")
        if configured is not None:
            if not isinstance(configured, list) or any(not isinstance(v, str) for v in configured):
                raise ValueError("tools.allowlist must be a string list")
            allowed = set(configured)
            for node_name, node in graph.nodes.items():
                if not set(node.tools) <= allowed:
                    raise ValueError(f"node {node_name!r} declares a tool outside tools.allowlist")
        self._graphs[name] = graph

    def _discover_files(self) -> None:
        search_dirs = [
            self.workspace_root / "workflows",
            self.workspace_root / "graphs",
            self.workspace_root / ".harness" / "workflows",
        ]
        files_seen = 0
        for s_dir in search_dirs:
            if not s_dir.exists() or not s_dir.is_dir():
                continue
            if not self._is_within_workspace(s_dir):
                logger.warning("Skipping workflow directory outside workspace: %s", s_dir)
                continue
            for ext in ("*.json", "*.yaml", "*.yml"):
                for file_path in s_dir.glob(ext):
                    files_seen += 1
                    if files_seen > MAX_WORKFLOW_FILES:
                        raise ValueError(f"workflow discovery exceeds the {MAX_WORKFLOW_FILES}-file limit")
                    try:
                        self._load_file(file_path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Skipping invalid workflow file %s (%s)", file_path, type(exc).__name__)

    def _load_file(self, path: Path) -> None:
        if not self._is_within_workspace(path):
            raise ValueError("workflow file resolves outside the workspace")
        if path.stat().st_size > MAX_WORKFLOW_FILE_BYTES:
            raise ValueError(f"workflow file exceeds {MAX_WORKFLOW_FILE_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)

        if not isinstance(data, dict):
            raise TypeError("workflow manifest must be an object")
        name = data.get("name", path.stem)
        self._register_manifest(name, data)

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.workspace_root)
            return True
        except (OSError, ValueError):
            return False

    def get_graph(self, name: str) -> DeclarativeGraphSpec | None:
        return self._graphs.get(name)

    def discover_graphs(self) -> dict[str, DeclarativeGraphSpec]:
        return dict(self._graphs)
