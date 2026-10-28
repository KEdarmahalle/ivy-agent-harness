"""Comprehensive Test Suite for Tier 3: Network APIs (REST, SSE, WebSockets, Webhooks)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from harness.api.main import create_app


class Tier3NetworkAPITests(unittest.TestCase):
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

    def test_health_and_catalog_endpoints(self) -> None:
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "ok")

        tools_resp = self.client.get("/tools")
        self.assertEqual(tools_resp.status_code, 200)
        self.assertIn("tools", tools_resp.json())

    def test_post_chat_stream_sse(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = "Streaming response verified."
        mock_resp.tool_calls = []

        def complete(*_args, **kwargs):
            if kwargs.get("on_chunk"):
                kwargs["on_chunk"]("Streaming")
            return mock_resp

        with patch.object(self.app.state.harness.models, "complete", side_effect=complete):
            resp = self.client.post(
                "/api/v1/chat/stream",
                json={"message": "Test SSE stream", "max_tool_steps": 2},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers["content-type"])
            text = resp.text
            self.assertIn("event: token", text)
            self.assertIn("event: turn_completed", text)
            self.assertIn("event: done", text)

    def test_post_agent_run_sync_and_stream(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = "Sync run response verified."
        mock_resp.tool_calls = []

        with patch.object(self.app.state.harness.models, "complete", return_value=mock_resp):
            # 1. Sync run
            resp = self.client.post(
                "/api/v1/agent/run",
                json={"task": "Execute autonomous task", "max_tool_steps": 2},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("success"))
            self.assertIn("Sync run response verified", data.get("final_output"))

            # 2. Stream run
            stream_resp = self.client.post(
                "/api/v1/agent/run",
                json={"task": "Execute streaming task", "stream": True},
            )
            self.assertEqual(stream_resp.status_code, 200)
            self.assertIn("text/event-stream", stream_resp.headers["content-type"])

    def test_post_agent_swarm(self) -> None:
        with patch.object(
            self.app.state.harness.swarm_coordinator,
            "run_fan_out",
            return_value={"task": "swarm task", "synthesis": "Swarm synthesis ok", "subagents": {}},
        ):
            resp = self.client.post(
                "/api/v1/agent/swarm",
                json={"task": "swarm task", "agents": "analyst,reviewer", "pattern": "fanout"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json().get("synthesis"), "Swarm synthesis ok")

    def test_websocket_chat_communication(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = "WebSocket agent reply."
        mock_resp.tool_calls = []

        def complete(*_args, **kwargs):
            if kwargs.get("on_chunk"):
                kwargs["on_chunk"]("WebSocket")
            return mock_resp

        with (
            patch.object(self.app.state.harness.models, "complete", side_effect=complete),
            self.client.websocket_connect("/ws/chat") as ws,
        ):
            # 1. Ping
            ws.send_text(json.dumps({"type": "ping"}))
            reply = ws.receive_json()
            self.assertEqual(reply.get("type"), "pong")

            # 2. Chat message
            ws.send_text(json.dumps({"type": "chat", "message": "Hello via WebSocket"}))

            received_types = []
            while True:
                data = ws.receive_json()
                t = data.get("type")
                received_types.append(t)
                if t == "turn_completed":
                    self.assertIn("WebSocket agent reply", data.get("final_output"))
                    break

            self.assertIn("token", received_types)
            self.assertIn("turn_completed", received_types)


if __name__ == "__main__":
    unittest.main()
