"""Adversarial stress-testing suite for advanced enterprise features: Swarms, Webhooks, Ranked Memory & Rate Limiting."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harness.events import AgentEvent
from harness.governance import (
    RateLimiter,
    RateLimitExceededError,
)
from harness.memory import MemoryStore
from harness.registries.model_registry import ModelRegistry, ModelRoute
from harness.registries.tool_registry import ToolRegistry
from harness.swarm import SwarmAgentSpec, SwarmCoordinator
from harness.webhooks import WebhookDispatcher, WebhookSubscription, compute_hmac_signature


def run_async(coro):
    return asyncio.run(coro)


class AdvancedEnterpriseFeaturesTests(unittest.TestCase):
    def test_multi_agent_handoff_swarm(self) -> None:
        """Adversarial test: Sequential multi-agent handoff chain."""
        models = ModelRegistry(
            routes={"default": ModelRoute(provider="fallback", model="deterministic-local")}
        )
        tools = ToolRegistry()

        agents = {
            "triage": SwarmAgentSpec(
                name="triage",
                system_prompt="Classify issue and handoff to specialist. HANDOFF:analyst",
                can_handoff_to=["analyst"],
            ),
            "analyst": SwarmAgentSpec(
                name="analyst",
                system_prompt="Analyze logs and provide resolution.",
                can_handoff_to=[],
            ),
        }

        coordinator = SwarmCoordinator(agents=agents, models=models, tools=tools)
        res = run_async(coordinator.run_handoff(entry_agent="triage", task="Memory leak detected"))

        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["hop_count"], 2)
        self.assertEqual(res["final_agent"], "analyst")
        self.assertIn("analyst_output", res["shared_context"])

    def test_multi_agent_parallel_fan_out_swarm(self) -> None:
        """Adversarial test: Parallel scatter-gather execution across multiple agents."""
        models = ModelRegistry(
            routes={"default": ModelRoute(provider="fallback", model="deterministic-local")}
        )
        tools = ToolRegistry()

        agents = {
            "security_auditor": SwarmAgentSpec(
                name="security_auditor",
                system_prompt="Audit security vulnerabilities.",
            ),
            "performance_engineer": SwarmAgentSpec(
                name="performance_engineer",
                system_prompt="Analyze latency bottlenecks.",
            ),
            "synthesizer": SwarmAgentSpec(
                name="synthesizer",
                system_prompt="Synthesize combined recommendations.",
            ),
        }

        coordinator = SwarmCoordinator(agents=agents, models=models, tools=tools)
        res = run_async(
            coordinator.run_fan_out(
                agent_names=["security_auditor", "performance_engineer"],
                task="Review checkout API endpoint",
                synthesizer_agent="synthesizer",
            )
        )

        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["agent_count"], 2)
        self.assertIn("security_auditor", res["findings"])
        self.assertIn("performance_engineer", res["findings"])
        self.assertTrue(len(res["synthesis"]) > 0)

    def test_webhook_dispatcher_hmac_signature(self) -> None:
        """Adversarial test: HMAC signature generation for webhook payloads."""
        secret = "super_secret_webhook_token"
        payload = b'{"event": "run_completed", "status": "done"}'
        sig1 = compute_hmac_signature(payload, secret)
        sig2 = compute_hmac_signature(payload, secret)
        self.assertEqual(sig1, sig2)
        self.assertEqual(len(sig1), 64)  # SHA-256 hex string

        # Verify filtering
        sub = WebhookSubscription(
            url="http://localhost:9999/webhook",
            secret=secret,
            event_filter=["run_completed"],
        )
        dispatcher = WebhookDispatcher([sub])
        evt_ignored = AgentEvent(event_id="e1", event_type="tool_called", run_id="r1")
        self.assertFalse(dispatcher._on_event(evt_ignored))

    def test_ranked_semantic_memory_search(self) -> None:
        """Adversarial test: Memory relevance ranking and keyword search."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mem = MemoryStore(workspace_root=root)
            mem.add("PostgreSQL connection timeout must be set to 30 seconds", category="database")
            mem.add("Use uv for all Python package installations", category="packaging")
            mem.add("SQLite locks occur when WAL mode is not enabled", category="database")

            # Search database topic
            results = mem.search_entries("PostgreSQL database timeout", top_k=2)
            self.assertGreaterEqual(len(results), 1)
            self.assertIn("PostgreSQL", results[0].text)

    def test_rate_limiter_and_concurrency_gate(self) -> None:
        """Adversarial test: Token bucket and concurrency limits prevent overload."""
        limiter = RateLimiter(max_requests_per_minute=3, max_concurrent=2)

        # 1. Acquire up to concurrency limit (2)
        limiter.acquire()
        limiter.acquire()

        # 2. Third concurrent request should fail with concurrency limit
        with self.assertRaises(RateLimitExceededError):
            limiter.acquire()

        # 3. Release one slot
        limiter.release()

        # 4. Third request now succeeds (RPM is at 3)
        limiter.acquire()

        # 5. Fourth request exceeds RPM (3 max/min)
        with self.assertRaises(RateLimitExceededError):
            limiter.acquire()


if __name__ == "__main__":
    unittest.main()
