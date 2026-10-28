from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient

    from harness.api.main import create_app
except ModuleNotFoundError:  # pragma: no cover - allows stdlib core tests before uv sync.
    TestClient = None
    create_app = None


@unittest.skipIf(TestClient is None, "FastAPI is not installed")
class ApiTests(unittest.TestCase):
    def test_approval_requires_distinct_approver_and_bound_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(json.dumps({
                "database": {"sqlite_path": str(root / "runs.sqlite3")},
                "secrets": {"store_path": str(root / "missing-secrets.json")},
                "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
                "graphs": {"approval_flow": {"start_node": "gate", "nodes": {
                    "gate": {"gate": "human_approval", "next": "finish"},
                    "finish": {"route": "default", "next": None},
                }}},
                "tools": {"allowlist": [], "sandbox": {"workspace_roots": [str(root)], "writable_roots": [str(root)]}},
                "auth": {"api_keys": {
                    "requester": {"key_env": "TEST_REQUESTER_KEY", "tenant_id": "tenant-a", "user_id": "requester", "roles": ["user"]},
                    "approver": {"key_env": "TEST_APPROVER_KEY", "tenant_id": "tenant-a", "user_id": "approver", "roles": ["approver"]},
                    "other_tenant": {"key_env": "TEST_OTHER_KEY", "tenant_id": "tenant-b", "user_id": "approver-b", "roles": ["approver"]},
                }},
                "governance": {"approval_roles": ["approver"]},
                "mcp_servers": {},
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "TEST_REQUESTER_KEY": "requester-secret",
                "TEST_APPROVER_KEY": "approver-secret",
                "TEST_OTHER_KEY": "other-secret",
            }, clear=True):
                client = TestClient(create_app(config_path))
                requester = {"X-API-Key": "requester-secret"}
                approver = {"X-API-Key": "approver-secret"}
                other_tenant = {"X-API-Key": "other-secret"}
                created = client.post("/runs", headers=requester, json={"graph": "approval_flow", "input": {}, "wait": True})
                self.assertEqual(created.status_code, 200)
                run_id = created.json()["run_id"]
                self.assertEqual(created.json()["status"], "needs_human")

                # Legacy labels do not authorize continuation.
                legacy = client.post(f"/runs/{run_id}/resume", headers=requester, json={"approvals": ["gate_gate"], "wait": True})
                self.assertEqual(legacy.status_code, 200)
                self.assertEqual(legacy.json()["status"], "needs_human")

                self_approval = client.post(f"/runs/{run_id}/approval", headers=requester, json={"decision": "approve"})
                self.assertEqual(self_approval.status_code, 403)
                cross_tenant = client.post(f"/runs/{run_id}/approval", headers=other_tenant, json={"decision": "approve"})
                self.assertEqual(cross_tenant.status_code, 403)
                issued = client.post(f"/runs/{run_id}/approval", headers=approver, json={"decision": "approve"})
                self.assertEqual(issued.status_code, 200)
                grant_id = issued.json()["grant_id"]
                resumed = client.post(f"/runs/{run_id}/resume", headers=requester, json={"grant_id": grant_id, "wait": True})
                self.assertEqual(resumed.status_code, 200)
                self.assertEqual(resumed.json()["status"], "done")
                replayed = client.post(f"/runs/{run_id}/resume", headers=requester, json={"grant_id": grant_id, "wait": True})
                self.assertEqual(replayed.json()["status"], "done")

    def test_create_waiting_run_and_fetch_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "database": {"sqlite_path": str(root / "runs.sqlite3")},
                        "secrets": {"store_path": str(root / "missing-secrets.json")},
                        "models": {
                            "default": {
                                "provider": "fallback",
                                "model": "deterministic-local",
                            },
                            "brainstorm.generate": {
                                "provider": "fallback",
                                "model": "deterministic-brainstormer",
                            },
                            "nim.example": {
                                "provider": "nvidia_nim",
                                "model": "nvidia/nemotron-3-ultra-550b-a55b",
                                "api_key_env": "TEST_MISSING_NVIDIA_KEY",
                            },
                            "nim.gpt-oss-120b": {
                                "provider": "nvidia_nim",
                                "model": "openai/gpt-oss-120b",
                                "api_key_env": "NVIDIA_NIM_API_KEY",
                            }
                        },
                        "tools": {
                            "allowlist": [
                                "environment.info",
                                "file.list",
                                "file.search_text",
                                "file.read_lines",
                                "process.run",
                                "sandbox.inspect",
                                "tool.catalog",
                                "web.fetch_url",
                            ],
                            "protected_paths": [".env"],
                            "sandbox": {
                                "workspace_roots": [str(root)],
                                "writable_roots": [str(root)],
                                "network_enabled": False,
                            },
                        },
                        "mcp_servers": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                client = TestClient(create_app(config_path))
                response = client.post(
                    "/runs",
                    json={
                        "graph": "brainstorm",
                        "input": {"prompt": "Design a run review ritual"},
                        "wait": True,
                    },
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["status"], "done")
                result = client.get(f"/runs/{body['run_id']}/result")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["confidence"], "not_gated")

                routes = client.get("/models/routes")
                self.assertEqual(routes.status_code, 200)
                nim_route = routes.json()["routes"]["nim.example"]
                self.assertEqual(nim_route["provider"], "nvidia_nim")
                self.assertEqual(nim_route["model"], "nvidia/nemotron-3-ultra-550b-a55b")
                self.assertFalse(nim_route["has_api_key"])
                self.assertNotIn("api_key", nim_route)
                self.assertEqual(
                    routes.json()["routes"]["nim.gpt-oss-120b"]["model"],
                    "openai/gpt-oss-120b",
                )

                fallback_smoke = client.post(
                    "/models/smoke",
                    json={
                        "graph": "brainstorm",
                        "node": "generate",
                        "prompt": "Reply with ok.",
                    },
                )
                self.assertEqual(fallback_smoke.status_code, 200)
                self.assertIn("response_excerpt", fallback_smoke.json())

                nim_smoke_without_key = client.post(
                    "/models/smoke",
                    json={
                        "graph": "nim",
                        "node": "example",
                        "prompt": "Reply with ok.",
                    },
                )
                self.assertEqual(nim_smoke_without_key.status_code, 400)
                self.assertIn("TEST_MISSING_NVIDIA_KEY", nim_smoke_without_key.json()["detail"])

                typo_smoke = client.post(
                    "/models/smoke",
                    json={
                        "graph": "nim",
                        "node": "missing-route",
                        "prompt": "Reply with ok.",
                    },
                )
                self.assertEqual(typo_smoke.status_code, 404)

                tools = client.get("/tools")
                self.assertEqual(tools.status_code, 200)
                self.assertIn("file.list", tools.json()["tools"])

                sandbox = client.post("/tools/sandbox.inspect", json={"arguments": {}})
                self.assertEqual(sandbox.status_code, 200)
                self.assertFalse(sandbox.json()["result"]["network_enabled"])


if __name__ == "__main__":
    unittest.main()
