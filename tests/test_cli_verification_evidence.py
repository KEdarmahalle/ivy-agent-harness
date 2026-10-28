from __future__ import annotations

import unittest

from harness.cli import verification_command, verification_status_notice


class CliVerificationEvidenceTests(unittest.TestCase):
    def test_recognizes_test_commands_in_shell_and_python_tools(self) -> None:
        shell = verification_command(
            {"tool": "process.run", "args": {"command": "uv run pytest -q stress_fixture/tests"}}
        )
        python = verification_command(
            {"tool": "sandbox.run_python", "args": {"code": 'subprocess.run(["uv", "run", "pytest"])'}}
        )
        self.assertIn("pytest", shell)
        self.assertIn("pytest", python)
        self.assertIsNone(verification_command({"tool": "process.run", "args": {"command": "python --version"}}))

    def test_nonzero_exit_and_exhausted_budget_are_reported_deterministically(self) -> None:
        notice = verification_status_notice(
            [{"step": 9, "command": "uv run pytest", "exit_code": 2}],
            last_mutation_step=0,
            tool_budget_exhausted=True,
            max_tool_steps=10,
        )
        self.assertIn("exited with code 2", notice)
        self.assertIn("checks did not pass", notice)
        self.assertIn("budget (10) was exhausted", notice)

    def test_success_is_stale_after_later_mutating_tool(self) -> None:
        notice = verification_status_notice(
            [{"step": 4, "command": "pytest -q", "exit_code": 0}],
            last_mutation_step=5,
            tool_budget_exhausted=False,
            max_tool_steps=10,
        )
        self.assertIn("verification is stale", notice)

    def test_success_after_last_mutation_is_reported_as_observed(self) -> None:
        notice = verification_status_notice(
            [{"step": 7, "command": "pytest -q", "exit_code": 0}],
            last_mutation_step=6,
            tool_budget_exhausted=False,
            max_tool_steps=10,
        )
        self.assertIn("exited with code 0", notice)
        self.assertIn("latest observed check result", notice)


if __name__ == "__main__":
    unittest.main()
