"""Base interfaces and data structures for Agent Sandbox Providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SandboxExecutionResult:
    """Standard execution result from a local, containerized, or cloud sandbox."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float = 0.0
    timed_out: bool = False
    output_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "output_excerpt": self.output_excerpt,
        }


class SandboxProvider(ABC):
    """Abstract sandbox provider interface supporting Local, Docker, and Cloud execution."""

    @abstractmethod
    def execute_command(
        self,
        command: str,
        cwd: Path | str | None = None,
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxExecutionResult:
        """Execute a shell command inside the sandbox."""

    @abstractmethod
    def execute_python(
        self,
        code: str,
        cwd: Path | str | None = None,
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxExecutionResult:
        """Execute Python code inside the sandbox."""

    @abstractmethod
    def read_file(self, path: Path | str) -> str:
        """Read a text file from the sandbox."""

    @abstractmethod
    def write_file(self, path: Path | str, content: str) -> None:
        """Write a text file into the sandbox."""
