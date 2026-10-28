from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harness.engine.runner import GraphRunner
from harness.engine.store import SQLiteRunStore


def run_async(coro):
    return asyncio.run(coro)


class GraphHarnessTests(unittest.TestCase):
    def make_runner(self, tmp: Path) -> GraphRunner:
        config = {
            "database": {"sqlite_path": str(tmp / "runs.sqlite3")},
            "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
            "tools": {
                "allowlist": [
                    "file.search_text",
                    "file.read_lines",
                    "process.run",
                    "web.fetch_url",
                ],
                "protected_paths": [".env", "secrets/", "production.yaml"],
            },
            "mcp_servers": {},
        }
        return GraphRunner(SQLiteRunStore(tmp / "runs.sqlite3"), config=config)

    def test_brainstorm_completes_without_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            snapshot = run_async(
                runner.run("brainstorm", {"prompt": "Improve incident review quality"})
            )
            self.assertEqual(snapshot.status, "done")
            self.assertEqual(snapshot.result["confidence"], "not_gated")
            self.assertGreaterEqual(len(snapshot.result["ideas"]), 1)
            event_types = [event.event_type for event in runner.store.list_trace(snapshot.run_id)]
            self.assertIn("model_called", event_types)
            self.assertNotIn("gate_decision", event_types)

    def test_research_report_requires_cited_sources_for_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            snapshot = run_async(
                runner.run(
                    "research-report",
                    {
                        "question": "What makes a verification report reliable?",
                        "sources": [
                            {
                                "url": "https://example.local/source",
                                "title": "Verification Notes",
                                "content": "Claims need concrete source or observed evidence.",
                            }
                        ],
                    },
                )
            )
            self.assertEqual(snapshot.status, "done")
            self.assertEqual(snapshot.result["confidence"], "CONCERNS")
            self.assertEqual(snapshot.result["report"]["citation_count"], 1)
            self.assertTrue(snapshot.result["report"]["semantic_review_required"])
            self.assertEqual(snapshot.result["evidence"][0]["confidence"], "source_linked")

    def test_code_investigate_runs_full_graph_with_reproduction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text(
                "def target_failure():\n    return 'target failure marker'\n",
                encoding="utf-8",
            )
            runner = self.make_runner(root)
            snapshot = run_async(
                runner.run(
                    "code-investigate",
                    {
                        "repo_path": str(root),
                        "query": "target failure marker",
                        "test_command": "python -c \"print('reproduced')\"",
                    },
                )
            )
            self.assertEqual(snapshot.status, "done")
            self.assertEqual(snapshot.result["confidence"], "CONCERNS")
            self.assertEqual(snapshot.result["reproduction"]["exit_code"], 0)
            self.assertTrue(any(item["file"] == "module.py" for item in snapshot.result["evidence"]))

    def test_code_investigate_protected_path_review_never_runs_reproduction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("SECRET_TOKEN=protected marker\n", encoding="utf-8")
            runner = self.make_runner(root)
            snapshot = run_async(
                runner.run(
                    "code-investigate",
                    {
                        "repo_path": str(root),
                        "query": "protected marker",
                        "test_command": "python -c \"print('ok')\"",
                    },
                )
            )
            self.assertEqual(snapshot.status, "needs_human")
            self.assertEqual(snapshot.state["required_approval"], "protected_path_review")
            # Public labels are advisory only and cannot authorize a continuation.
            rejected = run_async(runner.resume_run(snapshot.run_id, approvals=["protected_path_review"]))
            self.assertEqual(rejected.status, "needs_human")
            grant = runner.issue_approval(snapshot.run_id, "local", "separate-approver", ["admin"])
            resumed = run_async(runner.resume_run(snapshot.run_id, grant_id=grant["grant_id"]))
            self.assertEqual(resumed.status, "done")
            self.assertIsNone(resumed.result["reproduction"])
            self.assertEqual(resumed.result["confidence"], "NEEDS_HUMAN")
            self.assertNotIn("protected marker", str(resumed.result))
            self.assertNotIn("tool_called", [event.event_type for event in runner.store.list_trace(snapshot.run_id) if event.payload.get("tool") == "process.run"])

    def test_code_investigate_rejects_blank_query_before_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            snapshot = run_async(runner.run("code-investigate", {"query": "   "}))
            self.assertEqual(snapshot.status, "failed")
            self.assertIn("non-empty string", snapshot.error)
            self.assertFalse(any(event.payload.get("tool") == "file.search_text" for event in runner.store.list_trace(snapshot.run_id)))

    def test_code_investigate_rejects_invalid_timeout_and_search_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            for invalid in ({"timeout_seconds": 301}, {"max_search_results": 0}, {"timeout_seconds": True}):
                snapshot = run_async(runner.run("code-investigate", {"query": "needle", **invalid}))
                self.assertEqual(snapshot.status, "failed")
                self.assertFalse(any(event.payload.get("tool") == "file.search_text" for event in runner.store.list_trace(snapshot.run_id)))

    def test_research_rejects_invalid_url_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            snapshot = run_async(runner.run("research-report", {"question": "Check this", "source_urls": ["file:///etc/passwd"]}))
            self.assertEqual(snapshot.status, "failed")
            self.assertFalse(any(event.payload.get("tool") == "web.fetch_url" for event in runner.store.list_trace(snapshot.run_id)))

    def test_research_without_sources_skips_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            snapshot = run_async(runner.run("research-report", {"question": "What is known?"}))
            self.assertEqual(snapshot.status, "done")
            self.assertEqual(snapshot.result["confidence"], "NEEDS_HUMAN")
            self.assertEqual(snapshot.result["report"]["citation_count"], 0)
            self.assertFalse(any(event.event_type == "model_called" for event in runner.store.list_trace(snapshot.run_id)))

    def test_research_invalid_model_citation_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            runner.models.complete = lambda *args, **kwargs: '{"summary":"claim","claims":[{"claim":"Unsupported claim","citations":[{"source_id":"S999","quote":"invented"}]}]}'
            snapshot = run_async(runner.run("research-report", {"question": "Check claim", "sources": [{"content": "Original evidence."}]}))
            self.assertEqual(snapshot.status, "done")
            self.assertEqual(snapshot.result["confidence"], "NEEDS_HUMAN")
            self.assertEqual(snapshot.result["report"]["citation_count"], 0)

    def test_research_unhashable_source_id_is_rejected_as_bad_citation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            runner.models.complete = lambda *args, **kwargs: '{"summary":"claim","claims":[{"claim":"Unsupported claim","citations":[{"source_id":[],"quote":"invented"}]}]}'
            snapshot = run_async(runner.run("research-report", {"question": "Check claim", "sources": [{"content": "Original evidence."}]}))
            self.assertEqual(snapshot.status, "done")
            self.assertEqual(snapshot.result["confidence"], "NEEDS_HUMAN")
            self.assertEqual(snapshot.result["report"]["citation_count"], 0)

    def test_research_oversized_inline_source_fails_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            snapshot = run_async(runner.run("research-report", {
                "question": "Check source", "sources": [{"content": "x" * 32_001}],
            }))
            self.assertEqual(snapshot.status, "failed")
            self.assertFalse(any(event.event_type == "model_called" for event in runner.store.list_trace(snapshot.run_id)))

    def test_trace_survives_new_runner_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            runner = self.make_runner(tmp)
            snapshot = runner.create_run("brainstorm", {"prompt": "Durable trace"})
            run_async(runner.continue_run(snapshot.run_id))

            second_runner = self.make_runner(tmp)
            loaded = second_runner.store.get_run(snapshot.run_id)
            trace = second_runner.store.list_trace(snapshot.run_id)
            self.assertEqual(loaded.status, "done")
            self.assertGreater(len(trace), 2)


if __name__ == "__main__":
    unittest.main()
