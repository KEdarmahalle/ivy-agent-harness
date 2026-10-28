"""Comprehensive Test Suite for Decoupled Events, Cloud Sandbox Providers, and OpenAI Compatibility."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from harness.api.main import create_app
from harness.events import (
    ApprovalRequiredEvent,
    EventBus,
    SubagentSwarmEvent,
    TokenStreamEvent,
    ToolExecutionEvent,
)
from harness.sandbox import (
    DockerContainerSandbox,
    LocalSubprocessSandbox,
    get_sandbox_provider,
)
from harness.tools.policy import SandboxPolicy


class AdvancedDecoupledFeaturesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            json.dumps({
                "database": {"sqlite_path": str(self.root / "runs.sqlite3")},
                "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
                "tools": {
                    "allowlist": ["file.list", "time.now"],
                    "sandbox": {
                        "workspace_roots": [str(self.root)],
                        "writable_roots": [str(self.root)],
                    },
                },
            }),
            encoding="utf-8",
        )
        self.app = create_app(self.config_path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    # ==================== 1. Decoupled Universal Event Bus ====================

    def test_typed_universal_events_emission_and_subscription(self) -> None:
        bus = EventBus()
        captured_events = []

        def on_any_event(event):
            captured_events.append(event)

        token_events = []

        def on_token(event):
            token_events.append(event)

        bus.subscribe(on_any_event)
        bus.subscribe(on_token, event_type="token")

        # Emit TokenStreamEvent
        tok_evt = TokenStreamEvent(event_id="t1", event_type="token", run_id="r1", delta="Hello")
        bus.emit(tok_evt)

        # Emit ToolExecutionEvent
        tool_evt = ToolExecutionEvent(
            event_id="t2",
            event_type="tool_completed",
            run_id="r1",
            tool_name="file.list",
            status="completed",
            result=["file1.txt"],
        )
        bus.emit(tool_evt)

        # Emit SubagentSwarmEvent
        swarm_evt = SubagentSwarmEvent(
            event_id="t3",
            event_type="subagent_completed",
            run_id="r1",
            agent_name="security_auditor",
            status="completed",
            output="No security flaws",
        )
        bus.emit(swarm_evt)

        # Emit ApprovalRequiredEvent
        appr_evt = ApprovalRequiredEvent(
            event_id="t4",
            event_type="approval_required",
            run_id="r1",
            call_id="call_99",
            tool_name="deploy.staging",
        )
        bus.emit(appr_evt)

        self.assertEqual(len(captured_events), 4)
        self.assertEqual(len(token_events), 1)
        self.assertEqual(token_events[0].payload.get("delta"), "Hello")

    # ==================== 2. Sandbox Providers ====================

    def test_local_subprocess_sandbox_execution(self) -> None:
        policy = SandboxPolicy(workspace_roots=[self.root], writable_roots=[self.root])
        sandbox = LocalSubprocessSandbox(policy=policy)

        # Command execution
        res = sandbox.execute_command("echo hello_local", cwd=self.root)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("hello_local", res.stdout)

        # Python execution
        py_res = sandbox.execute_python("print(100 * 2)", cwd=self.root)
        self.assertEqual(py_res.exit_code, 0)
        self.assertIn("200", py_res.stdout)

        # File read/write
        test_file = self.root / "test_sandbox.txt"
        sandbox.write_file(test_file, "sandbox content")
        self.assertTrue(test_file.exists())
        self.assertEqual(sandbox.read_file(test_file), "sandbox content")

    def test_docker_and_factory_sandbox(self) -> None:
        local_provider = get_sandbox_provider({"tools": {"sandbox": {"provider": "local"}}})
        self.assertIsInstance(local_provider, LocalSubprocessSandbox)

        docker_provider = get_sandbox_provider({"tools": {"sandbox": {"provider": "docker", "image": "python:3.12-slim"}}})
        self.assertIsInstance(docker_provider, DockerContainerSandbox)

    # ==================== 3. OpenAI-Compatible API Endpoints ====================

    def test_openai_models_endpoint(self) -> None:
        resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("object"), "list")
        model_ids = [m["id"] for m in data.get("data", [])]
        self.assertIn("ivy", model_ids)

    def test_openai_chat_completions_sync(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = "OpenAI compatible reply verified."
        mock_resp.tool_calls = []

        with patch.object(self.app.state.harness.models, "complete", return_value=mock_resp):
            resp = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "ivy",
                    "messages": [
                        {"role": "system", "content": "You are a tester."},
                        {"role": "user", "content": "Hello OpenAI"},
                    ],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data.get("object"), "chat.completion")
            self.assertEqual(data["choices"][0]["message"]["content"], "OpenAI compatible reply verified.")
            self.assertIn("usage", data)

    def test_openai_chat_completions_stream(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = "Streaming token response."
        mock_resp.tool_calls = []

        with patch.object(self.app.state.harness.models, "complete", return_value=mock_resp):
            resp = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "ivy",
                    "messages": [{"role": "user", "content": "Stream to me"}],
                    "stream": True,
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers["content-type"])
            text = resp.text
            self.assertIn("chat.completion.chunk", text)
            self.assertIn("[DONE]", text)


if __name__ == "__main__":
    unittest.main()
