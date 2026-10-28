"""Local subprocess sandbox provider for CLI and workstation development."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from harness.context import trim_tool_output
from harness.sandbox.base import SandboxExecutionResult, SandboxProvider
from harness.tools.policy import SandboxPolicy


class LocalSubprocessSandbox(SandboxProvider):
    """Local process sandbox enforcing path boundaries, memory/output caps, and timeouts."""

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy(
            workspace_roots=[Path.cwd()],
            writable_roots=[Path.cwd()],
        )

    def _resolve_dir(self, cwd: Path | str | None) -> Path:
        if cwd is None:
            return self.policy.workspace_roots[0]
        p = Path(cwd).resolve()
        if not self.policy.is_in_workspace(p) and self.policy.is_custom_roots:
            raise PermissionError(f"Directory '{p}' is outside workspace boundaries: {self.policy.workspace_roots}")
        return p

    def execute_command(
        self,
        command: str,
        cwd: Path | str | None = None,
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxExecutionResult:
        target_cwd = self._resolve_dir(cwd)
        timeout = min(timeout_seconds, self.policy.default_timeout_seconds)
        start_time = time.perf_counter()

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        try:
            completed = subprocess.run(
                command,
                cwd=target_cwd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=proc_env,
                check=False,
            )
            duration = time.perf_counter() - start_time
            stdout = trim_tool_output(completed.stdout, max_chars=self.policy.max_output_chars)
            stderr = trim_tool_output(completed.stderr, max_chars=self.policy.max_output_chars)
            combined = trim_tool_output(completed.stdout + completed.stderr, max_chars=self.policy.max_output_chars)
            return SandboxExecutionResult(
                command=command,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_s=duration,
                timed_out=False,
                output_excerpt=combined,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - start_time
            stdout = trim_tool_output(exc.stdout or "", max_chars=self.policy.max_output_chars) if isinstance(exc.stdout, str) else ""
            stderr = trim_tool_output(exc.stderr or "", max_chars=self.policy.max_output_chars) if isinstance(exc.stderr, str) else ""
            return SandboxExecutionResult(
                command=command,
                exit_code=124,
                stdout=stdout,
                stderr=f"Command timed out after {timeout} seconds. {stderr}",
                duration_s=duration,
                timed_out=True,
                output_excerpt="Timed out",
            )

    def execute_python(
        self,
        code: str,
        cwd: Path | str | None = None,
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxExecutionResult:
        target_cwd = self._resolve_dir(cwd)
        timeout = min(timeout_seconds, self.policy.default_timeout_seconds)
        start_time = time.perf_counter()

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=target_cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=proc_env,
                check=False,
            )
            duration = time.perf_counter() - start_time
            stdout = trim_tool_output(completed.stdout, max_chars=self.policy.max_output_chars)
            stderr = trim_tool_output(completed.stderr, max_chars=self.policy.max_output_chars)
            combined = trim_tool_output(completed.stdout + completed.stderr, max_chars=self.policy.max_output_chars)
            return SandboxExecutionResult(
                command=f"python -c {code[:60]}...",
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_s=duration,
                timed_out=False,
                output_excerpt=combined,
            )
        except subprocess.TimeoutExpired:
            duration = time.perf_counter() - start_time
            return SandboxExecutionResult(
                command=f"python -c {code[:60]}...",
                exit_code=124,
                stdout="",
                stderr=f"Python execution timed out after {timeout} seconds.",
                duration_s=duration,
                timed_out=True,
                output_excerpt="Timed out",
            )

    def read_file(self, path: Path | str) -> str:
        p = Path(path).resolve()
        if self.policy.is_protected(p):
            raise PermissionError(f"Access to protected path '{p}' is forbidden.")
        if not self.policy.is_in_workspace(p) and self.policy.is_custom_roots:
            raise PermissionError(f"File '{p}' is outside allowed read roots.")
        return p.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: Path | str, content: str) -> None:
        p = Path(path).resolve()
        if self.policy.is_protected(p):
            raise PermissionError(f"Access to protected path '{p}' is forbidden.")
        if not self.policy.is_writable(p) and self.policy.is_custom_roots:
            raise PermissionError(f"File '{p}' is outside allowed writable roots.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
