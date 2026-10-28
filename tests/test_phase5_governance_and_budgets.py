"""Adversarial stress-testing suite for Phase 5 (Governance, Multi-Tenancy RBAC & Cost Budgets)."""

from __future__ import annotations

import unittest

from harness.governance import (
    BudgetExceededError,
    CostBudget,
    GovernanceError,
    TenantContext,
)


class Phase5GovernanceAndBudgetsTests(unittest.TestCase):
    def test_rbac_tool_permission_gate(self) -> None:
        """Adversarial test: Role-based access control blocks unauthorized tool invocation."""
        viewer = TenantContext(tenant_id="tenant_finance", user_id="bob", roles=["viewer"])

        # Viewers cannot run arbitrary shell commands
        with self.assertRaises(GovernanceError):
            viewer.check_tool_permission("process.run")

        # Developers can run shell commands
        dev = TenantContext(tenant_id="tenant_finance", user_id="alice", roles=["developer"])
        dev.check_tool_permission("process.run")

    def test_token_budget_cap_enforcement(self) -> None:
        """Adversarial test: Token budget hard cap blocks runaway LLM loops."""
        budget = CostBudget(max_tokens=1000)

        # Under limit: 600 tokens
        budget.record_usage(prompt_tokens=400, completion_tokens=200, model="default")
        self.assertEqual(budget.used_tokens, 600)

        # Exceeds limit: adding 500 tokens breaches 1000 cap
        with self.assertRaises(BudgetExceededError):
            budget.record_usage(prompt_tokens=300, completion_tokens=200, model="default")

    def test_usd_cost_budget_cap_enforcement(self) -> None:
        """Adversarial test: USD dollar budget limits prevent costly model runaways."""
        budget = CostBudget(max_cost_usd=0.002)

        # Incur small cost
        budget.record_usage(prompt_tokens=500, completion_tokens=100, model="llama-70b")
        self.assertLess(budget.used_cost_usd, 0.002)

        # Expensive call breaches USD budget
        with self.assertRaises(BudgetExceededError):
            budget.record_usage(prompt_tokens=1000, completion_tokens=1000, model="gpt-4o")


if __name__ == "__main__":
    unittest.main()
