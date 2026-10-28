"""Comprehensive Test Suite for Tier 4: Event-Driven Background Workers (GitHub, Slack, Jira)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from harness.api.main import create_app
from harness.workers.github import on_issue_opened, on_pull_request_opened
from harness.workers.jira import on_jira_issue_created
from harness.workers.slack import on_slack_mention


class Tier4WorkersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            json.dumps({
                "webhooks": {
                    "github_secret": "gh_test_secret",
                    "slack_signing_secret": "slack_test_secret",
                    "jira_secret": "jira_test_secret",
                },
                "database": {"sqlite_path": str(self.root / "runs.sqlite3")},
                "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
                "tools": {
                    "allowlist": ["file.list", "file.read_lines", "file.search_text", "time.now"],
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

    def test_github_pr_review_swarm(self) -> None:
        async def run_test():
            with patch("harness.swarm.SwarmCoordinator.run_fan_out") as mock_fanout:
                mock_fanout.return_value = {
                    "synthesis": "PR approved. Zero security vulnerabilities found.",
                    "findings": {
                        "security_auditor": "No secrets or injections.",
                        "performance_profiler": "Complexity is O(N).",
                        "ast_architect": "AST aligns with module boundaries.",
                    },
                }

                diff = "+ def add(a, b): return a + b\n"
                res = await on_pull_request_opened(
                    pr_diff=diff,
                    pr_number=42,
                    repo="owner/repo",
                    title="Add add function",
                    post_comment=False,
                )

                self.assertEqual(res["status"], "completed")
                self.assertEqual(res["pr_number"], 42)
                self.assertIn("Ivy Autonomous PR Review", res["synthesis"])
                self.assertIn("security_auditor", res["findings"])

        asyncio.run(run_test())

    def test_github_issue_triage(self) -> None:
        async def run_test():
            with patch("harness.sdk.AgentHarness.arun") as mock_arun:
                mock_result = MagicMock()
                mock_result.final_output = "Root cause identified in config.py line 40."
                mock_result.duration_s = 0.45
                mock_result.steps = 2
                mock_arun.return_value = mock_result

                res = await on_issue_opened(
                    issue_number=101,
                    issue_title="Config parsing error",
                    issue_body="Cannot parse yaml config",
                    post_comment=False,
                )
                self.assertEqual(res["status"], "completed")
                self.assertEqual(res["issue_number"], 101)
                self.assertIn("Root cause identified", res["analysis"])

        asyncio.run(run_test())

    def test_slack_mention_handler(self) -> None:
        async def run_test():
            with patch("harness.sdk.AgentHarness.arun") as mock_arun:
                mock_result = MagicMock()
                mock_result.final_output = "Summary of active PRs: 3 open."
                mock_result.to_dict.return_value = {"final_output": "Summary of active PRs: 3 open."}
                mock_arun.return_value = mock_result

                res = await on_slack_mention(
                    text="<@IVY> what PRs are open?",
                    user="U12345",
                    channel="C67890",
                    reply_in_thread=False,
                )
                self.assertEqual(res["status"], "completed")
                self.assertEqual(res["user"], "U12345")

        asyncio.run(run_test())

    def test_jira_issue_breakdown(self) -> None:
        async def run_test():
            with patch("harness.sdk.AgentHarness.arun") as mock_arun:
                mock_result = MagicMock()
                mock_result.final_output = "1. Architecture Invariants: Use token auth.\n2. Acceptance: Test with 401."
                mock_arun.return_value = mock_result

                res = await on_jira_issue_created(
                    issue_key="PROJ-456",
                    summary="Implement Auth",
                    description="Support JWT auth",
                    post_comment=False,
                )
                self.assertEqual(res["status"], "completed")
                self.assertEqual(res["issue_key"], "PROJ-456")
                self.assertIn("Architecture Invariants", res["breakdown"])

        asyncio.run(run_test())

    def test_webhook_endpoints(self) -> None:
        import hashlib
        import hmac

        # 1. GitHub PR webhook
        gh_body = json.dumps({
            "action": "opened",
            "pull_request": {"number": 55, "title": "Test PR", "diff_url": "https://diff.url"},
            "repository": {"full_name": "org/repo"},
        }, separators=(",", ":")).encode()
        gh_sig = "sha256=" + hmac.new(b"gh_test_secret", gh_body, hashlib.sha256).hexdigest()
        gh_resp = self.client.post("/api/v1/webhooks/github", content=gh_body, headers={
            "X-GitHub-Event": "pull_request", "X-Hub-Signature-256": gh_sig,
            "Content-Type": "application/json",
        })
        self.assertEqual(gh_resp.status_code, 200)
        self.assertEqual(gh_resp.json().get("status"), "enqueued")

        # 2. Slack URL challenge & app_mention webhook
        slack_body = json.dumps({"challenge": "test_challenge_token"}, separators=(",", ":")).encode()
        slack_ts = str(int(time.time()))
        slack_base = f"v0:{slack_ts}:{slack_body.decode()}".encode()
        slack_sig = "v0=" + hmac.new(b"slack_test_secret", slack_base, hashlib.sha256).hexdigest()
        slack_challenge_resp = self.client.post(
            "/api/v1/webhooks/slack",
            content=slack_body,
            headers={"X-Slack-Request-Timestamp": slack_ts, "X-Slack-Signature": slack_sig, "Content-Type": "application/json"},
        )
        self.assertEqual(slack_challenge_resp.status_code, 200)
        self.assertEqual(slack_challenge_resp.json().get("challenge"), "test_challenge_token")

        # 3. Jira webhook
        jira_resp = self.client.post(
            "/api/v1/webhooks/jira",
            json={
                "webhookEvent": "jira:issue_created",
                "issue": {
                    "key": "DEV-789",
                    "fields": {"summary": "New task", "description": "Details here"},
                },
            },
            headers={"X-Webhook-Token": "jira_test_secret"},
        )
        self.assertEqual(jira_resp.status_code, 200)
        self.assertEqual(jira_resp.json().get("status"), "enqueued")


if __name__ == "__main__":
    unittest.main()
