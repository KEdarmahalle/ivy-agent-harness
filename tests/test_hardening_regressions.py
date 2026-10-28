from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from harness.api.main import create_app
from harness.config import load_config
from harness.engine.store import SQLiteRunStore
from harness.network import OutboundRequestError, validate_outbound_url
from harness.sdk import AgentHarness


class HardeningRegressionTests(unittest.TestCase):
    def test_protected_wildcards_block_registry_read_write_and_listing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "private.pem").write_text("private key", encoding="utf-8")
            (root / "service.credentials.json").write_text("{}", encoding="utf-8")
            (root / "private_ed25519").write_text("private key", encoding="utf-8")
            harness = AgentHarness.from_project(root)

            for filename in ("private.pem", "service.credentials.json", "private_ed25519"):
                with self.subTest(filename=filename), self.assertRaises(PermissionError):
                    harness.tool("file.read_text", file=filename)

            with self.assertRaises(PermissionError):
                harness.tool("file.write_text", file="new-secret.key", content="secret")

            listed = harness.tool("file.list", path=".")
            listed_names = {entry["name"] for entry in listed["entries"]}
            self.assertTrue(
                {"private.pem", "service.credentials.json", "private_ed25519"}.isdisjoint(listed_names)
            )

    def test_outbound_policy_rejects_private_ip_and_unlisted_host(self) -> None:
        with self.assertRaises(OutboundRequestError):
            validate_outbound_url("http://127.0.0.1/admin")
        with (
            patch("harness.network.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.8", 80))]),
            self.assertRaises(OutboundRequestError),
        ):
            validate_outbound_url("http://internal.example/")
        with (
            patch("harness.network.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 80))]),
            self.assertRaises(OutboundRequestError),
        ):
            validate_outbound_url("http://example.com/", allowed_hosts=["allowed.example"])

    def test_run_store_claim_is_single_winner_and_delete_removes_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRunStore(Path(directory) / "runs.sqlite3")
            run = store.create_run("brainstorm", {"prompt": "hi"}, "generate", "tenant-a", "alice", ["user"])
            self.assertTrue(store.claim_run(run.run_id))
            self.assertFalse(store.claim_run(run.run_id))
            self.assertEqual(store.get_run(run.run_id).tenant_id, "tenant-a")
            store.delete_run(run.run_id)
            with self.assertRaises(KeyError):
                store.get_run(run.run_id)

    def test_api_run_ownership_is_tenant_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                "auth": {
                    "api_keys": {
                        "alice": {"key_env": "TEST_ALICE_KEY", "tenant_id": "tenant-a", "user_id": "alice", "roles": ["user"]},
                        "bob": {"key_env": "TEST_BOB_KEY", "tenant_id": "tenant-b", "user_id": "bob", "roles": ["user"]},
                    }
                },
                "database": {"sqlite_path": str(root / "runs.sqlite3")},
                "secrets": {"store_path": str(root / "no-secrets.json")},
                "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
                "tools": {"allowlist": [], "sandbox": {"workspace_roots": [str(root)], "writable_roots": [str(root)]}},
                "mcp_servers": {},
            }
            path = root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(os.environ, {"TEST_ALICE_KEY": "alice-secret", "TEST_BOB_KEY": "bob-secret"}, clear=True):
                client = TestClient(create_app(path))
                created = client.post(
                    "/runs",
                    headers={"X-API-Key": "alice-secret"},
                    json={"graph": "brainstorm", "input": {"prompt": "tenant test"}, "wait": True},
                )
                self.assertEqual(created.status_code, 200)
                run_id = created.json()["run_id"]
                own = client.get(f"/runs/{run_id}", headers={"X-API-Key": "alice-secret"})
                foreign = client.get(f"/runs/{run_id}", headers={"X-API-Key": "bob-secret"})
                self.assertEqual(own.status_code, 200)
                self.assertEqual(own.json()["tenant_id"], "tenant-a")
                self.assertEqual(foreign.status_code, 404)
                streamed = client.get(f"/runs/{run_id}/stream", headers={"X-API-Key": "alice-secret"})
                self.assertEqual(streamed.status_code, 200)
                self.assertIn("event: run_completed", streamed.text)

    def test_non_loopback_config_requires_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps({"api": {"host": "0.0.0.0"}, "database": {"sqlite_path": str(root / "runs.sqlite3")}}))
            with (
                patch.dict(os.environ, {"AGENT_HARNESS_BIND_HOST": "0.0.0.0"}, clear=True),
                self.assertRaisesRegex(RuntimeError, "non-loopback"),
            ):
                create_app(path)

    def test_unauthenticated_remote_peer_is_rejected_even_with_loopback_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps({"database": {"sqlite_path": str(root / "runs.sqlite3")}}))
            client = TestClient(create_app(path), client=("8.8.8.8", 12345))
            response = client.get("/health")
            self.assertEqual(response.status_code, 401)

    def test_unauthenticated_remote_websocket_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps({"database": {"sqlite_path": str(root / "runs.sqlite3")}}))
            client = TestClient(create_app(path), client=("8.8.8.8", 12345))
            with (
                self.assertRaises(WebSocketDenialResponse) as raised,
                client.websocket_connect("/ws/chat"),
            ):
                pass
            self.assertEqual(raised.exception.status_code, 401)

    def test_bundled_config_is_resolved_from_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_dir = root / "site-packages" / "harness"
            package_dir.mkdir(parents=True)
            package_config = package_dir / "config.yaml"
            package_config.write_text('{"package_default": true}', encoding="utf-8")
            workspace = root / "empty-workspace"
            workspace.mkdir()
            home = root / "empty-home"
            home.mkdir()

            with (
                patch("harness.config.__file__", str(package_dir / "config.py")),
                patch.object(Path, "cwd", return_value=workspace),
                patch.object(Path, "home", return_value=home),
            ):
                config = load_config()

            self.assertTrue(config.get("package_default"))


if __name__ == "__main__":
    unittest.main()
