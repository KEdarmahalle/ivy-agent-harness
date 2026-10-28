"""Comprehensive Security, Vulnerability, and Defense Verification Tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from harness.api.main import create_app
from harness.mcp_server import handle_tool_call
from harness.sandbox import LocalSubprocessSandbox
from harness.secrets import mask_secret_value, redact_secrets
from harness.tools.packs.filesystem import (
    list_files,
    read_lines,
    read_text,
    replace_text,
    search_text,
    write_text,
)
from harness.tools.policy import SandboxPolicy
from harness.workers.github import post_github_pr_comment
from harness.workers.jira import post_jira_comment
from harness.workers.slack import post_slack_message


class SecurityAndVulnerabilitiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            json.dumps({
                "auth": {"api_key": "sec_test_token_999"},
                "webhooks": {
                    "github_secret": "gh_secret_123",
                    "slack_signing_secret": "slack_secret_123",
                },
                "database": {"sqlite_path": str(self.root / "runs.sqlite3")},
                "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
                "tools": {
                    "allowlist": ["file.list", "file.read_text", "file.write_text", "data.sql_query"],
                    "sandbox": {
                        "workspace_roots": [str(self.root)],
                        "writable_roots": [str(self.root)],
                    },
                },
            }),
            encoding="utf-8",
        )
        self.policy = SandboxPolicy(workspace_roots=[self.root], writable_roots=[self.root])
        self.app = create_app(self.config_path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    # ==================== 1. Secret Redaction & Masking ====================

    def test_secret_masking_and_redaction(self) -> None:
        masked = mask_secret_value("sk-nvapi-9876543210")
        self.assertEqual(masked, "sk-***3210")

        text = "Connecting with secret token nvapi-secret-key-12345 to API."
        custom_secrets = {"NIM_KEY": "nvapi-secret-key-12345"}
        redacted = redact_secrets(text, custom_secrets=custom_secrets)
        self.assertNotIn("nvapi-secret-key-12345", redacted)
        self.assertIn("[REDACTED_NIM_KEY]", redacted)

    # ==================== 2. Protected Files and Sandbox Traversal ====================

    def test_protected_path_policy_is_case_insensitive(self) -> None:
        self.assertTrue(self.policy.is_protected(".ENV"))
        self.assertTrue(self.policy.is_protected("Secrets/Production.KEY"))

    def test_protected_files_blocked_in_filesystem(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("SUPER_SECRET=12345", encoding="utf-8")

        # 1. read_text on .env must fail
        with self.assertRaises(PermissionError):
            read_text(".env", root=self.root, policy=self.policy)

        # 2. read_lines on .env must fail
        with self.assertRaises(PermissionError):
            read_lines(".env", root=self.root, policy=self.policy)

        # 3. write_text to .env must fail
        with self.assertRaises(PermissionError):
            write_text(".env", "MALICIOUS_OVERWRITE", root=self.root, policy=self.policy)

        # 4. replace_text on .env must fail
        with self.assertRaises(PermissionError):
            replace_text(".env", "SUPER_SECRET", "HACKED", root=self.root, policy=self.policy)

        # 5. search_text must skip .env
        res = search_text("SUPER_SECRET", root=self.root, policy=self.policy)
        self.assertEqual(res["total"], 0)

        # 6. list_files must exclude .env
        listed = list_files(".", root=self.root, policy=self.policy)
        entry_names = [e["name"] for e in listed["entries"]]
        self.assertNotIn(".env", entry_names)

    def test_local_subprocess_sandbox_protected_access(self) -> None:
        sandbox = LocalSubprocessSandbox(policy=self.policy)
        secret_file = self.root / ".secrets"
        secret_file.write_text("password=abc", encoding="utf-8")

        with self.assertRaises(PermissionError):
            sandbox.read_file(secret_file)

        with self.assertRaises(PermissionError):
            sandbox.write_file(secret_file, "hack")

    # ==================== 3. SQL Query Hardening & MCP ====================

    def test_mcp_sqlite_query_blocks_ddl_and_mutations(self) -> None:
        res = handle_tool_call(1, {"name": "sqlite_query", "arguments": {"query": "DROP TABLE test_table;"}})
        self.assertTrue(res.get("result", {}).get("isError"))
        self.assertIn("forbidden", str(res.get("result", {}).get("content", [{}])[0].get("text", "")))

    # ==================== 4. API Authentication & Headers ====================

    def test_api_key_authentication_enforcement(self) -> None:
        # 1. No key -> 401 Unauthorized
        unauth_resp = self.client.get("/health")
        self.assertEqual(unauth_resp.status_code, 401)

        # 2. Valid X-API-Key header -> 200 OK
        auth_resp_header = self.client.get("/health", headers={"X-API-Key": "sec_test_token_999"})
        self.assertEqual(auth_resp_header.status_code, 200)

        # 3. Valid Authorization: Bearer <key> header -> 200 OK
        auth_resp_bearer = self.client.get("/health", headers={"Authorization": "Bearer sec_test_token_999"})
        self.assertEqual(auth_resp_bearer.status_code, 200)

    def test_websocket_authentication(self) -> None:
        # 1. WebSocket without key -> Closed
        with self.assertRaises(WebSocketDisconnect), self.client.websocket_connect("/ws/chat"):
            pass

        # 2. WebSocket with valid query param -> Accepted
        with self.client.websocket_connect(
            "/ws/chat", headers={"X-API-Key": "sec_test_token_999"}
        ) as ws:
            ws.send_json({"type": "ping"})
            data = ws.receive_json()
            self.assertEqual(data.get("type"), "pong")

    # ==================== 5. Webhook Signature Verification ====================

    def test_github_webhook_signature_verification(self) -> None:
        payload = {"action": "opened", "issue": {"number": 1, "title": "Bug", "body": "Details"}}
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        valid_sig = "sha256=" + hmac.new(b"gh_secret_123", body_bytes, hashlib.sha256).hexdigest()

        # 1. Invalid signature -> 401
        invalid_resp = self.client.post(
            "/api/v1/webhooks/github",
            json=payload,
            headers={"X-API-Key": "sec_test_token_999", "X-Hub-Signature-256": "sha256=invalid"},
        )
        self.assertEqual(invalid_resp.status_code, 401)

        # 2. Valid signature -> 200
        valid_resp = self.client.post(
            "/api/v1/webhooks/github",
            json=payload,
            headers={"X-API-Key": "sec_test_token_999", "X-Hub-Signature-256": valid_sig},
        )
        self.assertEqual(valid_resp.status_code, 200)

    def test_slack_webhook_signature_verification(self) -> None:
        payload = {"event": {"type": "app_mention", "text": "@Ivy review", "user": "U1", "channel": "C1"}}
        now_ts = str(int(time.time()))
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sig_base = f"v0:{now_ts}:{body_bytes.decode('utf-8')}".encode()
        valid_sig = "v0=" + hmac.new(b"slack_secret_123", sig_base, hashlib.sha256).hexdigest()

        # 1. Invalid signature -> 401
        invalid_resp = self.client.post(
            "/api/v1/webhooks/slack",
            json=payload,
            headers={
                "X-API-Key": "sec_test_token_999",
                "X-Slack-Signature": "v0=invalid",
                "X-Slack-Request-Timestamp": now_ts,
            },
        )
        self.assertEqual(invalid_resp.status_code, 401)

        # 2. Valid signature -> 200
        valid_resp = self.client.post(
            "/api/v1/webhooks/slack",
            json=payload,
            headers={
                "X-API-Key": "sec_test_token_999",
                "X-Slack-Signature": valid_sig,
                "X-Slack-Request-Timestamp": now_ts,
            },
        )
        self.assertEqual(valid_resp.status_code, 200)

    # ==================== 6. Resilient Worker Error Handling ====================

    def test_worker_network_error_graceful_handling(self) -> None:
        # All workers catch network exceptions gracefully without raising unhandled errors
        with patch("urllib.request.urlopen", side_effect=Exception("Network Timeout")):
            gh_res = post_github_pr_comment(pr_number=1, comment="test", repo="org/repo", github_token="fake_token")
            self.assertEqual(gh_res.get("status"), "error")

            slack_res = post_slack_message(channel="C1", text="test", bot_token="xoxb-fake")
            self.assertEqual(slack_res.get("status"), "error")

            jira_res = post_jira_comment(issue_key="DEV-1", comment="test", jira_url="https://jira.example.com", jira_email="a@b.com", jira_token="tok")
            self.assertEqual(jira_res.get("status"), "error")


if __name__ == "__main__":
    unittest.main()
