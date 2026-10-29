"""Comprehensive Test Suite for Tier 1: Embedded Python SDK (AgentHarness)."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.events import global_event_bus
from harness.sdk import AgentHarness, AgentResult


class Tier1SDKTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        (self.root / "pyproject.toml").write_text('[project]\nname = "test_project"\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sdk_initialization(self) -> None:
        harness = AgentHarness.from_project(workspace_root=self.root)
        self.assertEqual(harness.workspace_root, self.root.resolve())
        self.assertIsNotNone(harness.tools)
        self.assertIsNotNone(harness.models)
        self.assertIsNotNone(harness.skills)
        self.assertIsNotNone(harness.memory)
        self.assertIsNotNone(harness.swarm_coordinator)
        self.assertIn("file.read_text", harness.list_tools())

    def test_sdk_direct_tool_invocation(self) -> None:
        harness = AgentHarness(workspace_root=self.root)
        test_file = self.root / "sample.txt"
        harness.tool("file.write_text", file="sample.txt", content="Hello SDK")
        self.assertTrue(test_file.exists())
        self.assertEqual(test_file.read_text(encoding="utf-8"), "Hello SDK")

        read_res = harness.tool("file.read_text", file="sample.txt")
        self.assertIn("Hello SDK", read_res.get("content", ""))

    def test_sdk_stream_and_run(self) -> None:
        harness = AgentHarness(workspace_root=self.root)

        mock_resp_1 = MagicMock()
        mock_resp_1.content = '{"tool": "file.list", "args": {"path": "."}}'
        mock_resp_1.tool_calls = []

        mock_resp_2 = MagicMock()
        mock_resp_2.content = "Analysis completed. Project structure is clean."
        mock_resp_2.tool_calls = []

        with patch.object(harness.models, "complete", side_effect=[mock_resp_1, mock_resp_2]):
            events = list(harness.stream("Audit workspace files"))

        event_types = [e.type for e in events]
        self.assertIn("tool_start", event_types)
        self.assertIn("tool_result", event_types)
        self.assertIn("turn_completed", event_types)

        tool_start_ev = next(e for e in events if e.type == "tool_start")
        self.assertEqual(tool_start_ev.tool_name, "file.list")

        turn_completed_ev = next(e for e in events if e.type == "turn_completed")
        self.assertIn("Analysis completed", turn_completed_ev.final_output)

        # Test harness.run()
        with patch.object(harness.models, "complete", side_effect=[mock_resp_1, mock_resp_2]):
            result = harness.run("Audit workspace files")

        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.success)
        self.assertIn("Analysis completed", result.final_output)
        self.assertEqual(result.steps, 1)

    def test_sdk_chat_enforces_entry_profile_tool_scope_at_execution(self) -> None:
        config = {
            "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
            "tools": {"allowlist": ["time.now", "file.write_text"]},
            "orchestration": {"mode": "self_directed", "chat_entry_profile": "assistant"},
            "agents": {"include_builtins": False, "profiles": {
                "assistant": {"route": "default", "system_prompt": "Only use configured tools.", "tools": ["time.now"]},
            }},
        }
        harness = AgentHarness(workspace_root=self.root, config=config)
        malicious_call = MagicMock()
        malicious_call.name = "file.write_text"
        malicious_call.arguments = {"file": "unauthorized.txt", "content": "must not be written"}
        first = MagicMock()
        first.content = ""
        first.tool_calls = [malicious_call]
        final = MagicMock()
        final.content = "The unconfigured tool was denied."
        final.tool_calls = []
        with patch.object(harness.models, "complete", side_effect=[first, final]):
            events = list(harness.stream("Try to write a file"))
        self.assertFalse((self.root / "unauthorized.txt").exists())
        denied = [event for event in events if event.type == "tool_result"]
        self.assertEqual(len(denied), 1)
        self.assertFalse(denied[0].ok)
        self.assertIn("outside the configured chat profile scope", denied[0].error or "")

    def test_sdk_executes_host_supplied_plan_without_model_planning(self) -> None:
        config = {
            "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
            "tools": {"allowlist": ["time.now", "agent.plan"]},
            "orchestration": {
                "mode": "single", "planning_strategy": "deterministic",
                "chat_entry_profile": "assistant", "max_parallel_agents": 2,
            },
            "agents": {"include_builtins": False, "profiles": {
                "assistant": {"route": "default", "system_prompt": "entry", "tools": [],
                              "delegation": {"enabled": True, "allowed_targets": ["specialist"]}},
                "specialist": {"route": "default", "system_prompt": "specialist", "tools": []},
            }},
        }
        harness = AgentHarness(workspace_root=self.root, config=config)

        async def fake_run_agent(profile, task, context, **_kwargs):
            return {"ok": True, "output": f"{profile}:{task}", "tools_executed": []}

        harness.swarm_coordinator.run_agent = fake_run_agent
        proposal = {"schema_version": 1, "goal": "fixed workflow", "tasks": [
            {"id": "inspect", "profile": "specialist", "task": "inspect configured files", "depends_on": [], "inputs": []},
        ]}
        result = harness.execute_plan(proposal)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tasks"]["inspect"]["output"], "specialist:inspect configured files")

    def test_rejected_plan_does_not_unlock_required_mode_tools(self) -> None:
        config = {
            "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
            "tools": {"allowlist": ["time.now", "agent.plan"]},
            "orchestration": {
                "mode": "required", "planning_strategy": "model_directed",
                "chat_entry_profile": "assistant",
            },
            "agents": {"include_builtins": False, "profiles": {
                "assistant": {"route": "default", "system_prompt": "entry", "tools": ["time.now"],
                              "delegation": {"enabled": True, "allowed_targets": []}},
            }},
        }
        harness = AgentHarness(workspace_root=self.root, config=config)
        harness.tools.plan_dispatcher = lambda *_args: {"ok": False, "status": "rejected", "error": "invalid plan"}
        plan_call = MagicMock()
        plan_call.name = "agent.plan"
        plan_call.arguments = {"prompt": "do work"}
        forbidden_call = MagicMock()
        forbidden_call.name = "time.now"
        forbidden_call.arguments = {}
        first = MagicMock()
        first.content = ""
        first.tool_calls = [plan_call]
        second = MagicMock()
        second.content = ""
        second.tool_calls = [forbidden_call]
        final = MagicMock()
        final.content = "Plan rejected; no tools ran."
        final.tool_calls = []
        invoked = []
        original = harness.tools.invoke

        def track(name, **kwargs):
            invoked.append(name)
            return original(name, **kwargs)

        harness.tools.invoke = track
        with patch.object(harness.models, "complete", side_effect=[first, second, final]):
            events = list(harness.stream("Do work"))
        self.assertEqual(invoked, ["agent.plan"])
        time_events = [event for event in events if event.tool_name == "time.now" and event.type == "tool_result"]
        self.assertEqual(len(time_events), 1)
        self.assertFalse(time_events[0].ok)
        self.assertIn("validated orchestration plan is required", time_events[0].error or "")

    def test_sdk_stream_ignores_subagent_events_from_other_runs(self) -> None:
        harness = AgentHarness(workspace_root=self.root)
        mock_response = MagicMock()
        mock_response.content = "Finished without delegation."
        mock_response.tool_calls = []

        def publish_foreign_event(*_args, **_kwargs):
            global_event_bus.publish(
                "subagent_completed",
                "another-run",
                {"agent": "foreign", "observation": "private prompt from another user"},
            )
            return mock_response

        with patch.object(harness.models, "complete", side_effect=publish_foreign_event):
            events = list(harness.stream("A local request"))

        self.assertFalse(any(event.type == "subagent_completed" for event in events))

    def test_sdk_delegate_event_is_run_scoped_and_contains_no_prompt_or_tool_data(self) -> None:
        harness = AgentHarness(workspace_root=self.root)
        delegate_response = MagicMock()
        delegate_response.content = (
            '{"tool":"agent.delegate","args":{"agent":"code_auditor",'
            '"prompt":"private delegated task text"}}'
        )
        delegate_response.tool_calls = []
        specialist_response = MagicMock()
        specialist_response.content = "Specialist evidence: delegated result was returned."
        specialist_response.tool_calls = []
        specialist_response.__str__.return_value = specialist_response.content
        final_response = MagicMock()
        final_response.content = "Parent synthesized the specialist result."
        final_response.tool_calls = []
        observed_events = []
        listener = observed_events.append
        global_event_bus.subscribe(listener)

        try:
            with patch.object(
                harness.models,
                "complete",
                side_effect=[delegate_response, specialist_response, final_response],
            ) as complete:
                streamed = list(harness.stream("Perform a private delegated task"))
        finally:
            global_event_bus.unsubscribe(listener)

        subagent_events = [event for event in streamed if event.type == "subagent_completed"]
        self.assertEqual(len(subagent_events), 1)
        self.assertEqual(subagent_events[0].agent, "code_auditor")
        self.assertNotIn("private delegated task text", subagent_events[0].observation or "")
        self.assertEqual(subagent_events[0].tools_called, [])
        completed = [event for event in streamed if event.type == "turn_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].final_output, "Parent synthesized the specialist result.")
        self.assertEqual(complete.call_count, 3)
        parent_messages = complete.call_args_list[2].args[2]
        self.assertIn("Specialist evidence: delegated result was returned.", repr(parent_messages))

        published = [event for event in observed_events if event.event_type.startswith("subagent_")]
        self.assertEqual(len(published), 2)
        self.assertEqual(published[0].run_id, published[1].run_id)
        self.assertNotEqual(published[0].run_id, "swarm")
        self.assertNotIn("private delegated task text", repr([event.payload for event in published]))
        self.assertNotIn("prompt", repr([event.payload for event in published]))

    def test_sdk_async_run_and_stream(self) -> None:
        async def run_async_test():
            harness = AgentHarness(workspace_root=self.root)
            mock_resp = MagicMock()
            mock_resp.content = "Async response ok."
            mock_resp.tool_calls = []

            with patch.object(harness.models, "complete", return_value=mock_resp):
                # Test arun
                result = await harness.arun("Quick async check")
                self.assertEqual(result.final_output, "Async response ok.")

                # Test astream
                streamed = []
                async for event in harness.astream("Stream async check"):
                    streamed.append(event)
                self.assertTrue(any(e.type == "turn_completed" for e in streamed))

        asyncio.run(run_async_test())

    def test_sdk_swarm_execution(self) -> None:
        harness = AgentHarness(workspace_root=self.root)
        with patch.object(
            harness.swarm_coordinator,
            "run_fan_out",
            return_value={"task": "audit", "synthesis": "Swarm synthesis complete", "subagents": {}},
        ):
            res = harness.swarm(task="audit", agents="code_auditor,security_sentinel")
            self.assertEqual(res["synthesis"], "Swarm synthesis complete")


if __name__ == "__main__":
    unittest.main()
