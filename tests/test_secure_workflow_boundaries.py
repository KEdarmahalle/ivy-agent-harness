from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from harness.declarative_graphs import DeclarativeGraphSpec
from harness.engine.agentic_router import AgenticGraphRouter
from harness.engine.graph_loader import load_graph
from harness.engine.runner import GraphRunner
from harness.engine.schemas import RunSnapshot
from harness.engine.store import InMemoryRunStore, SQLiteRunStore
from harness.registries.model_registry import ModelRegistry


class SecureWorkflowBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_graph_name_never_becomes_import_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("harness.engine.graph_loader.importlib.import_module") as importer:
            with self.assertRaises(KeyError):
                load_graph("os;side_effect", workspace_root=directory)
            with self.assertRaises(KeyError):
                load_graph("os", workspace_root=directory)
            importer.assert_not_called()

    def test_router_fails_closed_on_provider_error(self) -> None:
        class BrokenModels:
            def complete(self, **kwargs):
                raise RuntimeError("provider secret detail")

        decision = AgenticGraphRouter(models=BrokenModels()).decide_next_node(
            graph_name="sample", current_node="first", nodes_catalog={"second": {}},
            goal="goal", state={}, visited_nodes=[],
        )
        self.assertIsNone(decision.next_node)
        self.assertEqual(decision.action, "finish")
        self.assertEqual(decision.state_updates["routing_error"], "RuntimeError")
        self.assertNotIn("provider secret", decision.rationale)

    async def test_graph_execution_has_a_bounded_step_count(self) -> None:
        config = {
            "engine": {"max_graph_steps": 3},
            "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
            "graphs": {"cycle": {"start_node": "loop", "nodes": {
                "loop": {"route": "default", "next": "loop"},
            }}},
        }
        store = InMemoryRunStore()
        runner = GraphRunner(store=store, config=config)
        result = await runner.run("cycle", {"prompt": "keep running"})
        self.assertEqual(result.status, "failed")
        self.assertIn("step limit", result.error)
        self.assertEqual(sum(trace.event_type == "node_completed" for trace in store.list_trace(result.run_id)), 3)

    def test_router_state_updates_have_a_total_size_limit(self) -> None:
        router = AgenticGraphRouter(models=ModelRegistry.from_config({}))
        decision = router._parse_decision(
            '{"action":"advance","next_node":"next","state_updates":{"a":"' + "x" * 3900 + '","b":"' + "y" * 3900 + '","c":"' + "z" * 3900 + '"}}',
            {"next": {}},
        )
        self.assertLessEqual(sum(len(str(k)) + len(str(v)) for k, v in decision.state_updates.items()), 8_100)

    def test_declarative_manifest_validates_start_and_route_references(self) -> None:
        valid = {"start_node": "begin", "nodes": {"begin": {"next": "finish"}, "finish": {}}}
        self.assertEqual(DeclarativeGraphSpec.from_dict("sample", valid).start_node, "begin")
        with self.assertRaisesRegex(ValueError, "start_node"):
            DeclarativeGraphSpec.from_dict("sample", {"nodes": {"begin": {}}})
        with self.assertRaisesRegex(ValueError, "undeclared"):
            DeclarativeGraphSpec.from_dict("sample", {"start_node": "begin", "nodes": {"begin": {"next": "missing"}}})

    def test_declarative_manifest_rejects_malformed_route_capability_and_internal_projection(self) -> None:
        bad_manifests = [
            {"start_node": "begin", "nodes": {"begin": {"next": {"mode": "unexpected"}}}},
            {"start_node": "begin", "nodes": {"begin": {"next": {"mode": "agentic", "candidates": ["missing"]}}}},
            {"start_node": "begin", "nodes": {"begin": {"tools": [None]}}},
            {"start_node": "begin", "nodes": {"begin": {"inputs": ["_visited_history"]}}},
        ]
        for manifest in bad_manifests:
            with self.subTest(manifest=manifest), self.assertRaises((ValueError, TypeError)):
                DeclarativeGraphSpec.from_dict("sample", manifest)

    def test_router_cannot_create_approval_or_internal_state(self) -> None:
        router = AgenticGraphRouter(models=ModelRegistry.from_config({}))
        decision = router._parse_decision(
            '{"action":"advance","next_node":"next","state_updates":{"approved_gate":true,"_visited_history":[],"required_approval":"x","safe_flag":true}}',
            {"next": {}, "gate": {}},
        )
        self.assertEqual(decision.state_updates, {"safe_flag": True})

    def _paused_runner(self) -> tuple[GraphRunner, InMemoryRunStore, RunSnapshot]:
        store = InMemoryRunStore()
        runner = GraphRunner(store=store, config={"governance": {"approval_roles": ["approver"]}})
        snapshot = runner.create_run("brainstorm", {"prompt": "test"}, "tenant-a", "requester", ["user"])
        snapshot.status = "needs_human"
        snapshot.state = {"required_approval": "release", "resume_node": "deploy", "unchanged": True}
        store.save_run(snapshot)
        return runner, store, snapshot

    async def test_label_is_not_authorization_and_grant_is_one_use(self) -> None:
        runner, store, snapshot = self._paused_runner()
        unchanged = await runner.resume_run(snapshot.run_id, approvals=["release"])
        self.assertEqual(unchanged.status, "needs_human")
        with self.assertRaises(PermissionError):
            runner.issue_approval(snapshot.run_id, "tenant-a", "requester", ["approver"])
        with self.assertRaises(PermissionError):
            runner.issue_approval(snapshot.run_id, "tenant-a", "other", ["user"])
        with self.assertRaises(PermissionError):
            runner.issue_approval(snapshot.run_id, "tenant-b", "approver-1", ["approver"])
        grant = runner.issue_approval(snapshot.run_id, "tenant-a", "approver-1", ["approver"])
        digest = runner.approval_context_digest(store.get_run(snapshot.run_id))
        first = store.consume_approval_grant(grant["grant_id"], digest)
        self.assertIsNotNone(first)
        self.assertIsNone(store.consume_approval_grant(grant["grant_id"], digest))

    def test_concurrent_consumption_only_succeeds_once(self) -> None:
        runner, store, snapshot = self._paused_runner()
        grant = runner.issue_approval(snapshot.run_id, "tenant-a", "approver-1", ["approver"])
        digest = runner.approval_context_digest(snapshot)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: store.consume_approval_grant(grant["grant_id"], digest), range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_sqlite_concurrent_consumption_only_succeeds_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRunStore(Path(directory) / "runs.sqlite3")
            runner = GraphRunner(store=store, config={"governance": {"approval_roles": ["approver"]}})
            snapshot = runner.create_run("brainstorm", {"prompt": "test"}, "tenant-a", "requester", ["user"])
            snapshot.status = "needs_human"
            snapshot.state = {"required_approval": "release", "resume_node": "deploy"}
            store.save_run(snapshot)
            grant = runner.issue_approval(snapshot.run_id, "tenant-a", "approver-user", ["approver"])
            digest = runner.approval_context_digest(store.get_run(snapshot.run_id))
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.consume_approval_grant(grant["grant_id"], digest), range(8)))
            self.assertEqual(sum(result is not None for result in results), 1)

    def test_run_rejects_unavailable_declared_tool_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory():
            store = InMemoryRunStore()
            config = {
                "graphs": {"bad_workflow": {"start_node": "begin", "nodes": {"begin": {"tools": ["tool.missing"]}}}},
                "tools": {"allowlist": ["tool.missing"]},
            }
            runner = GraphRunner(store=store, config=config)
            with self.assertRaisesRegex(ValueError, "unavailable tools"):
                runner.create_run("bad_workflow", {})
            self.assertEqual(store._runs, {})

    async def test_changed_workflow_definition_fails_closed_before_resume(self) -> None:
        store = InMemoryRunStore()
        config = {"graphs": {"dynamic_flow": {"start_node": "first", "nodes": {
            "first": {"route": "default", "next": None},
        }}}}
        runner = GraphRunner(store=store, config=config)
        snapshot = runner.create_run("dynamic_flow", {})
        config["graphs"]["dynamic_flow"]["nodes"]["first"]["system_prompt"] = "changed after run creation"
        resumed = await runner.continue_run(snapshot.run_id)
        self.assertEqual(resumed.status, "failed")
        self.assertIn("workflow definition changed", resumed.error)
        self.assertFalse(any(event.event_type == "model_called" for event in store.list_trace(snapshot.run_id)))

    def test_expired_grant_is_rejected(self) -> None:
        runner, store, snapshot = self._paused_runner()
        grant = runner.issue_approval(snapshot.run_id, "tenant-a", "approver-1", ["approver"])
        store._approval_grants[grant["grant_id"]]["expires_at"] = "2000-01-01T00:00:00+00:00"
        digest = runner.approval_context_digest(snapshot)
        self.assertIsNone(store.consume_approval_grant(grant["grant_id"], digest))

    async def test_state_change_invalidates_grant(self) -> None:
        runner, store, snapshot = self._paused_runner()
        grant = runner.issue_approval(snapshot.run_id, "tenant-a", "approver-1", ["approver"])
        changed = store.get_run(snapshot.run_id)
        changed.state["unchanged"] = False
        store.save_run(changed)
        current_digest = runner.approval_context_digest(store.get_run(snapshot.run_id))
        self.assertIsNone(store.consume_approval_grant(grant["grant_id"], current_digest))

    async def test_legacy_unpinned_runs_fail_closed(self) -> None:
        store = InMemoryRunStore()
        snapshot = store.create_run("brainstorm", {"prompt": "legacy"}, "generate")
        runner = GraphRunner(store=store, config={"governance": {"approval_roles": ["approver"]}})
        result = await runner.continue_run(snapshot.run_id)
        self.assertEqual(result.status, "failed")
        self.assertIn("not pinned", result.error)
        self.assertFalse(any(event.event_type == "model_called" for event in store.list_trace(snapshot.run_id)))

        snapshot = store.create_run("brainstorm", {}, "generate")
        snapshot.status = "needs_human"
        snapshot.state = {"required_approval": "release", "resume_node": "deploy"}
        store.save_run(snapshot)
        with self.assertRaisesRegex(ValueError, "no pinned workflow"):
            runner.issue_approval(snapshot.run_id, "local", "approver", ["approver"])


if __name__ == "__main__":
    unittest.main()
