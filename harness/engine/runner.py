"""Durable domain-neutral graph executor."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from harness.declarative_graphs import DeclarativeGraphRegistry
from harness.engine.graph_loader import graph_fingerprint, load_graph_with_fingerprint
from harness.engine.schemas import NodeResult, RunSnapshot, utc_now
from harness.engine.store import SQLiteRunStore
from harness.observability import Observability
from harness.registries.mcp_registry import MCPRegistry
from harness.registries.model_registry import ModelRegistry
from harness.registries.tool_registry import ToolRegistry

TERMINAL_STATUSES = {"done", "failed", "needs_human"}


@dataclass
class RuntimeContext:
    config: dict[str, Any]
    store: SQLiteRunStore
    tools: ToolRegistry
    models: ModelRegistry
    mcp: MCPRegistry
    observability: Observability
    run_id: str
    node: str | None = None

    def trace(self, event_type: str, payload: dict | None = None, node: str | None = None) -> None:
        event = self.store.append_trace(self.run_id, event_type, payload or {}, node or self.node)
        self.observability.record(event)


class GraphRunner:
    def __init__(
        self,
        store: SQLiteRunStore,
        config: dict[str, Any] | None = None,
        tools: ToolRegistry | None = None,
        models: ModelRegistry | None = None,
        mcp: MCPRegistry | None = None,
        observability: Observability | None = None,
    ) -> None:
        self.config = config or {}
        self.store = store
        self.tools = tools or ToolRegistry.from_config(self.config)
        self.models = models or ModelRegistry.from_config(self.config)
        self.mcp = mcp or MCPRegistry.from_config(self.config)
        self.observability = observability or Observability.from_config(self.config)
        max_steps = self.config.get("engine", {}).get("max_graph_steps", 256)
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 10_000:
            raise ValueError("engine.max_graph_steps must be an integer between 1 and 10000")
        self.max_graph_steps = max_steps

    def create_run(
        self,
        graph_name: str,
        input_data: dict[str, Any],
        tenant_id: str = "local",
        user_id: str = "local",
        roles: list[str] | None = None,
    ) -> RunSnapshot:
        graph, graph_digest = load_graph_with_fingerprint(graph_name, config=self.config)
        # Fail before persisting/starting any run if a declarative graph
        # advertises an unavailable capability. ToolRegistry enforces the
        # allowlist again at the actual invocation boundary.
        if graph_name not in {"code-investigate", "research-report", "brainstorm", "ai-accelerator"}:
            registry = DeclarativeGraphRegistry(config=self.config)
            manifest = registry.get_graph(graph_name)
            if manifest is None:
                raise KeyError(f"graph not found: {graph_name}")
            registered_tools = set(self.tools.names())
            for node_name, node in manifest.nodes.items():
                unavailable = set(node.tools) - registered_tools
                if unavailable:
                    raise ValueError(f"node {node_name!r} declares unavailable tools: {sorted(unavailable)}")
        snapshot = self.store.create_run(
            graph.name,
            input_data,
            graph.start_node,
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles,
            graph_digest=graph_digest,
        )
        self.store.append_trace(
            snapshot.run_id,
            "run_created",
            {"graph": graph.name, "start_node": graph.start_node},
        )
        return snapshot

    async def run(self, graph_name: str, input_data: dict[str, Any]) -> RunSnapshot:
        snapshot = self.create_run(graph_name, input_data)
        return await self.continue_run(snapshot.run_id)

    async def continue_run(self, run_id: str) -> RunSnapshot:
        snapshot = self.store.get_run(run_id)
        if snapshot.status == "done":
            return snapshot
        if snapshot.status == "failed":
            return snapshot
        if snapshot.status == "needs_human":
            return snapshot
        if not snapshot.graph_digest:
            snapshot.status = "failed"
            snapshot.error = "workflow definition version is not pinned; refusing to continue this legacy run"
            snapshot.completed_at = utc_now()
            self.store.save_run(snapshot)
            self.store.append_trace(run_id, "workflow_version_missing", {"graph": snapshot.graph})
            return self.store.get_run(run_id)
        if not self.store.claim_run(run_id):
            return self.store.get_run(run_id)
        snapshot = self.store.get_run(run_id)
        try:
            graph, current_graph_digest = load_graph_with_fingerprint(snapshot.graph, config=self.config)
        except Exception as exc:  # noqa: BLE001 - persist a terminal state after a successful claim.
            snapshot.status = "failed"
            snapshot.error = f"workflow definition could not be loaded ({type(exc).__name__})"
            snapshot.completed_at = utc_now()
            self.store.save_run(snapshot)
            self.store.append_trace(run_id, "workflow_load_failed", {"error_type": type(exc).__name__})
            return self.store.get_run(run_id)
        if snapshot.graph_digest and snapshot.graph_digest != current_graph_digest:
            snapshot.status = "failed"
            snapshot.error = "workflow definition changed after this run was created; refusing to resume under a different workflow version"
            snapshot.completed_at = utc_now()
            self.store.save_run(snapshot)
            self.store.append_trace(run_id, "workflow_version_mismatch", {"graph": snapshot.graph})
            return self.store.get_run(run_id)
        execution_steps = 0
        while snapshot.current_node:
            if execution_steps >= self.max_graph_steps:
                snapshot.status = "failed"
                snapshot.error = f"workflow exceeded the configured step limit ({self.max_graph_steps})"
                snapshot.completed_at = utc_now()
                self.store.append_trace(run_id, "graph_step_limit", {"limit": self.max_graph_steps})
                return self.store.save_run(snapshot)
            execution_steps += 1
            node_name = snapshot.current_node
            node = graph.node(node_name)
            context = RuntimeContext(
                config=self.config,
                store=self.store,
                tools=self.tools,
                models=self.models,
                mcp=self.mcp,
                observability=self.observability,
                run_id=run_id,
                node=node_name,
            )
            context.trace("node_started", {"node": node_name}, node=node_name)
            try:
                if inspect.iscoroutinefunction(node.handler):
                    raw_result = node.handler(context, snapshot)
                else:
                    raw_result = await asyncio.to_thread(node.handler, context, snapshot)
                result = await raw_result if inspect.isawaitable(raw_result) else raw_result
                if not isinstance(result, NodeResult):
                    raise TypeError(f"node {node_name!r} returned {type(result).__name__}")
                snapshot.state.update(result.updates)
                snapshot.current_node = result.next_node
                if result.status:
                    snapshot.status = result.status
                if result.result is not None:
                    snapshot.result = result.result
                if result.error:
                    snapshot.error = result.error
                if result.status in TERMINAL_STATUSES:
                    snapshot.completed_at = utc_now() if result.status in {"done", "failed"} else None
                    context.trace(
                        "node_completed",
                        {"node": node_name, "status": result.status, "next_node": result.next_node},
                        node=node_name,
                    )
                    self.store.save_run(snapshot)
                    return snapshot
                context.trace("node_completed", {"node": node_name, "next_node": result.next_node}, node=node_name)
                self.store.save_run(snapshot)
            except Exception as exc:  # noqa: BLE001  # Graph node failures must checkpoint.
                snapshot.status = "failed"
                snapshot.error = f"{type(exc).__name__}: {exc}"
                snapshot.completed_at = utc_now()
                context.trace("node_failed", {"node": node_name, "error": snapshot.error}, node=node_name)
                self.store.save_run(snapshot)
                return snapshot
        snapshot.status = "done"
        snapshot.completed_at = utc_now()
        self.store.append_trace(run_id, "run_completed", {"status": "done"})
        return self.store.save_run(snapshot)

    @staticmethod
    def approval_context_digest(snapshot: RunSnapshot) -> str:
        payload = {
            "run_id": snapshot.run_id, "graph": snapshot.graph, "graph_digest": snapshot.graph_digest, "tenant_id": snapshot.tenant_id,
            "requester_id": snapshot.user_id, "roles": snapshot.roles, "input": snapshot.input,
            "state": snapshot.state, "current_node": snapshot.current_node,
            "status": snapshot.status,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def issue_approval(self, run_id: str, tenant_id: str, approver_id: str, roles: list[str]) -> dict[str, str]:
        snapshot = self.store.get_run(run_id)
        if not snapshot.graph_digest:
            raise ValueError("run has no pinned workflow definition and cannot receive an approval grant")
        if snapshot.graph_digest != graph_fingerprint(snapshot.graph, config=self.config):
            raise ValueError("workflow definition changed after this run was created")
        approver_roles = set(self.config.get("governance", {}).get("approval_roles", ["approver", "admin"]))
        if not any(role in approver_roles for role in roles):
            raise PermissionError("principal is not authorized to approve workflow gates")
        if tenant_id != snapshot.tenant_id:
            raise PermissionError("approver and run must belong to the same tenant")
        if approver_id == snapshot.user_id:
            raise PermissionError("requester cannot approve their own run")
        if snapshot.status != "needs_human" or not snapshot.state.get("required_approval") or not snapshot.state.get("resume_node"):
            raise ValueError("run has no active approval request")
        from datetime import UTC, datetime, timedelta
        lifetime = max(1, min(3600, int(self.config.get("governance", {}).get("approval_grant_ttl_seconds", 600))))
        grant = {
            "grant_id": uuid4().hex + uuid4().hex,
            "run_id": run_id,
            "tenant_id": tenant_id,
            "requester_id": snapshot.user_id,
            "approver_id": approver_id,
            "approval_type": str(snapshot.state["required_approval"]),
            "continuation_node": str(snapshot.state["resume_node"]),
            "context_digest": self.approval_context_digest(snapshot),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=lifetime)).isoformat(),
        }
        self.store.issue_approval_grant(grant)
        self.store.append_trace(run_id, "approval_granted", {k: v for k, v in grant.items() if k != "grant_id" and k != "context_digest"})
        return {"grant_id": grant["grant_id"], "expires_at": grant["expires_at"]}

    async def resume_run(self, run_id: str, grant_id: str | None = None, approvals: list[str] | None = None) -> RunSnapshot:
        snapshot = self.store.get_run(run_id)
        if snapshot.status != "needs_human" or not grant_id:
            return snapshot
        result = self.store.consume_approval_grant(grant_id, self.approval_context_digest(snapshot))
        if result is None:
            return snapshot
        _, receipt = result
        self.store.append_trace(run_id, "approval_consumed", receipt)
        return await self.continue_run(run_id)
