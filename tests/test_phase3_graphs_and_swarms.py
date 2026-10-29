"""Adversarial stress-testing suite for Phase 3 (Declarative Graphs, DAG State Machine & Swarms)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harness.engine.graph_loader import list_graphs, load_graph
from harness.engine.runner import GraphRunner
from harness.engine.store import SQLiteRunStore
from harness.registries.tool_registry import ToolRegistry


def run_async(coro):
    return asyncio.run(coro)


class Phase3GraphsAndSwarmsTests(unittest.TestCase):
    def test_declarative_graph_execution_from_config(self) -> None:
        """Adversarial test: Declarative 3-step workflow graph configured purely via dictionary/YAML."""
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = {
                "database": {"sqlite_path": str(tmp / "runs.sqlite3")},
                "models": {
                    "default": {"provider": "fallback", "model": "deterministic-local"},
                },
                "graphs": {
                    "incident_triage": {
                        "description": "Multi-step incident triage workflow",
                        "start_node": "intake",
                        "nodes": {
                            "intake": {
                                "route": "default",
                                "system_prompt": "Classify incident severity.",
                                "next": "diagnose",
                            },
                            "diagnose": {
                                "route": "default",
                                "system_prompt": "Run diagnostic check on logs.",
                                "next": "remediate",
                            },
                            "remediate": {
                                "route": "default",
                                "system_prompt": "Apply remediation steps.",
                                "next": None,
                            },
                        },
                    }
                },
            }

            runner = GraphRunner(SQLiteRunStore(tmp / "runs.sqlite3"), config=config)
            snapshot = run_async(runner.run("incident_triage", {"prompt": "Database connection pool exhausted"}))

            self.assertEqual(snapshot.status, "done")
            self.assertIn("intake_output", snapshot.state)
            self.assertIn("diagnose_output", snapshot.state)
            self.assertIn("remediate_output", snapshot.state)

            trace_events = [e.event_type for e in runner.store.list_trace(snapshot.run_id)]
            self.assertIn("run_created", trace_events)
            self.assertIn("run_completed", trace_events)

    def test_declarative_human_approval_gate_and_resume(self) -> None:
        """Adversarial test: Workflow pauses at human gate and cleanly resumes upon approval."""
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = {
                "database": {"sqlite_path": str(tmp / "runs.sqlite3")},
                "models": {
                    "default": {"provider": "fallback", "model": "deterministic-local"},
                },
                "graphs": {
                    "prod_deployment": {
                        "start_node": "build",
                        "nodes": {
                            "build": {
                                "route": "default",
                                "next": "approval_gate",
                            },
                            "approval_gate": {
                                "gate": "human_approval",
                                "next": "deploy",
                            },
                            "deploy": {
                                "route": "default",
                                "next": None,
                            },
                        },
                    }
                },
            }

            runner = GraphRunner(SQLiteRunStore(tmp / "runs.sqlite3"), config=config)
            snapshot = run_async(runner.run("prod_deployment", {"prompt": "Release v2.0.0"}))

            # 1. Verify workflow paused at approval gate
            self.assertEqual(snapshot.status, "needs_human")
            self.assertEqual(snapshot.state["required_approval"], "gate_approval_gate")

            # 2. Resume with user approval
            grant = runner.issue_approval(snapshot.run_id, "local", "separate-approver", ["admin"])
            resumed = run_async(runner.resume_run(snapshot.run_id, grant_id=grant["grant_id"]))
            self.assertEqual(resumed.status, "done")
            self.assertIn("deploy_output", resumed.state)

    def test_conditional_state_machine_routing(self) -> None:
        """Adversarial test: Dynamic conditional branching based on workflow state."""
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = {
                "database": {"sqlite_path": str(tmp / "runs.sqlite3")},
                "models": {
                    "default": {"provider": "fallback", "model": "deterministic-local"},
                },
                "graphs": {
                    "fraud_check": {
                        "start_node": "evaluate",
                        "nodes": {
                            "evaluate": {
                                "route": "default",
                                "next": {
                                    "flagged_fraud": "freeze_account",
                                    "default": "approve_transaction",
                                },
                            },
                            "approve_transaction": {
                                "route": "default",
                                "next": None,
                            },
                            "freeze_account": {
                                "route": "default",
                                "next": None,
                            },
                        },
                    }
                },
            }

            # Normal path (default)
            runner = GraphRunner(SQLiteRunStore(tmp / "runs.sqlite3"), config=config)
            snap_normal = run_async(runner.run("fraud_check", {"prompt": "$10 transaction"}))
            self.assertEqual(snap_normal.status, "done")
            self.assertIn("approve_transaction_output", snap_normal.state)

    def test_multi_agent_delegation_tool_fails_closed_when_unconfigured(self) -> None:
        """Delegation without a configured dispatcher must never simulate completion."""
        tools = ToolRegistry()
        res = tools.invoke(
            "agent.delegate",
            agent="database_specialist",
            prompt="Optimize slow SQL index on users table",
            context={"query_time_ms": 420},
        )
        self.assertEqual(res["status"], "unavailable")
        self.assertFalse(res["ok"])

    def test_graph_discovery_and_registry(self) -> None:
        """Test listing and loading graphs dynamically."""
        config = {
            "graphs": {
                "dynamic_workflow": {
                    "start_node": "step1",
                    "nodes": {"step1": {"next": None}},
                }
            }
        }
        all_graphs = list_graphs(config=config)
        self.assertIn("code-investigate", all_graphs)
        self.assertIn("brainstorm", all_graphs)
        self.assertIn("dynamic_workflow", all_graphs)

        spec = load_graph("dynamic_workflow", config=config)
        self.assertEqual(spec.name, "dynamic_workflow")
        self.assertEqual(spec.start_node, "step1")


if __name__ == "__main__":
    unittest.main()
