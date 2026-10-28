"""Comprehensive Test Suite for Dynamic Agentic Graphs & AI-Driven Workflows."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.declarative_graphs import DeclarativeGraphSpec
from harness.engine.agentic_router import AgenticGraphRouter
from harness.engine.graph_loader import list_graphs
from harness.engine.runner import GraphRunner
from harness.engine.store import SQLiteRunStore
from harness.events import AgenticRouteDecisionEvent, global_event_bus
from harness.registries.model_registry import ModelRegistry


class AgenticDynamicGraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "test_runs.sqlite3"
        self.store = SQLiteRunStore(self.db_path)
        self.config = {
            "database": {"sqlite_path": str(self.db_path)},
            "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
        }
        self.models = ModelRegistry.from_config(self.config)
        self.runner = GraphRunner(store=self.store, config=self.config, models=self.models)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    # ==================== 1. Agentic Graph Router Tests ====================

    def test_agentic_router_decision_and_event_emission(self) -> None:
        captured_events: list[AgenticRouteDecisionEvent] = []

        def on_route_event(evt):
            if isinstance(evt, AgenticRouteDecisionEvent):
                captured_events.append(evt)

        global_event_bus.subscribe(on_route_event, event_type="agentic_route_decision")

        router = AgenticGraphRouter(models=self.models)
        nodes_catalog = {
            "node_a": {"description": "Collect telemetry", "tools": []},
            "node_b": {"description": "Execute optimization", "tools": ["process.run"]},
            "node_c": {"description": "Verify results", "tools": []},
        }

        mock_reply = MagicMock()
        mock_reply.content = json.dumps({
            "action": "advance",
            "next_node": "node_b",
            "rationale": "Telemetry collected, ready for optimization.",
            "state_updates": {"telemetry_ready": True},
        })

        with patch.object(self.models, "complete", return_value=mock_reply):
            decision = router.decide_next_node(
                graph_name="test_graph",
                current_node="node_a",
                nodes_catalog=nodes_catalog,
                goal="Optimize system throughput",
                state={"latency": 120},
                visited_nodes=["node_a"],
            )

            self.assertEqual(decision.action, "advance")
            self.assertEqual(decision.next_node, "node_b")
            self.assertTrue(decision.state_updates.get("telemetry_ready"))
            self.assertEqual(len(captured_events), 1)
            self.assertEqual(captured_events[0].payload.get("to_node"), "node_b")

    def test_agentic_router_circuit_breaker(self) -> None:
        router = AgenticGraphRouter(models=self.models, max_loop_count=3)
        nodes_catalog = {
            "loop_node": {"description": "Repeated node", "tools": []},
            "escape_node": {"description": "Escape node", "tools": []},
        }

        decision = router.decide_next_node(
            graph_name="test_loop",
            current_node="loop_node",
            nodes_catalog=nodes_catalog,
            goal="Loop test",
            state={},
            visited_nodes=["loop_node", "loop_node", "loop_node"],
        )

        self.assertEqual(decision.next_node, "escape_node")
        self.assertTrue(decision.state_updates.get("circuit_breaker_triggered"))

    # ==================== 2. Declarative Agentic Graph Workflow ====================

    async def test_declarative_agentic_graph_execution(self) -> None:
        decl_spec = DeclarativeGraphSpec.from_dict("agentic-swe-pipeline", {
            "name": "agentic-swe-pipeline",
            "description": "Dynamic SWE pipeline with AI routing",
            "start_node": "plan",
            "routing_mode": "agentic",
            "nodes": {
                "plan": {
                    "description": "Plan the implementation",
                    "system_prompt": "Create architectural plan.",
                    "next": "agentic",
                },
                "implement": {
                    "description": "Implement code changes",
                    "system_prompt": "Write code.",
                    "next": "agentic",
                },
                "verify": {
                    "description": "Run tests and verify",
                    "system_prompt": "Verify tests pass.",
                    "next": "agentic",
                },
            },
        })

        mock_decision = MagicMock()
        mock_decision.content = json.dumps({
            "action": "advance",
            "next_node": "verify",
            "rationale": "Plan ready and trivial, skipping directly to verify.",
            "state_updates": {"skipped_implement": True},
        })

        engine_spec = decl_spec.to_engine_spec()
        self.assertEqual(len(engine_spec.nodes), 3)

    # ==================== 3. AI Accelerator Built-in Graph Execution ====================

    async def test_ai_accelerator_pipeline_end_to_end(self) -> None:
        graphs = list_graphs(config=self.config)
        self.assertIn("ai-accelerator", graphs)

        # 1. Start AI Accelerator Run
        snapshot = await self.runner.run(
            "ai-accelerator",
            input_data={"model": "deepseek-r1-70b", "target_latency_ms": 35.0},
        )

        # Pipeline pauses at production_gate requiring human approval
        self.assertEqual(snapshot.status, "needs_human")
        self.assertEqual(snapshot.state.get("required_approval"), "production_deployment_token")
        self.assertTrue(snapshot.state.get("quantized"))

        # 2. Resume with Human Approval Token
        grant = self.runner.issue_approval(snapshot.run_id, "local", "separate-approver", ["admin"])
        resumed = await self.runner.resume_run(snapshot.run_id, grant_id=grant["grant_id"])

        self.assertEqual(resumed.status, "done")
        self.assertEqual(resumed.result.get("status"), "deployed")
        self.assertIn("endpoint", resumed.result)


if __name__ == "__main__":
    unittest.main()
