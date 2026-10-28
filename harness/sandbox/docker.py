"""Docker container and Cloud multi-tenant sandbox provider."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from harness.sandbox.base import SandboxExecutionResult, SandboxProvider
from harness.tools.policy import SandboxPolicy


class DockerContainerSandbox(SandboxProvider):
    """Docker container sandbox for multi-tenant isolation, cloud deployments, and SaaS APIs."""

    def __init__(
        self,
        image: str = "python:3.12-slim",
        policy: SandboxPolicy | None = None,
        memory_limit: str = "512m",
        cpu_limit: float = 1.0,
        network_mode: str = "none",
    ) -> None:
        self.image = image
        self.policy = policy or SandboxPolicy(
            workspace_roots=[Path.cwd()],
            writable_roots=[Path.cwd()],
        )
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.network_mode = network_mode

    def _is_docker_available(self) -> bool:
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, timeout=3, check=False)
            return res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _add_protected_mounts(self, docker_cmd: list[str], workspace: Path, mask_dir: Path) -> None:
        """Overlay protected workspace paths so container code cannot read host secrets."""
        mask_dir.mkdir(parents=True, exist_ok=True)
        mask_index = 0
        for item in workspace.rglob("*"):
            try:
                if not self.policy.is_protected(item):
                    continue
                relative = item.relative_to(workspace).as_posix()
                destination = f"/workspace/{relative}"
                if item.is_dir():
                    docker_cmd.extend(["--tmpfs", f"{destination}:rw,noexec,nosuid,size=8m"])
                    continue
                mask_path = mask_dir / f"mask-{mask_index}"
                mask_index += 1
                mask_path.touch()
                docker_cmd.extend(["-v", f"{mask_path}:{destination}:ro"])
            except (OSError, ValueError):
                continue

    def execute_command(
        self,
        command: str,
        cwd: Path | str | None = None,
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxExecutionResult:
        if not self._is_docker_available():
            raise RuntimeError("Docker sandbox is unavailable; refusing host execution")

        host_dir = Path(cwd).resolve() if cwd else self.policy.workspace_roots[0]
        timeout = min(timeout_seconds, self.policy.default_timeout_seconds)
        start_time = time.perf_counter()

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpu_limit}",
            f"--network={self.network_mode}",
            "-v",
            f"{host_dir}:/workspace",
            "-w",
            "/workspace",
        ]
        if env:
            for k, v in env.items():
                docker_cmd.extend(["-e", f"{k}={v}"])

        docker_cmd.extend([self.image, "sh", "-c", command])

        mask_context = tempfile.TemporaryDirectory(prefix="ivy-sandbox-masks-")
        self._add_protected_mounts(docker_cmd, host_dir, Path(mask_context.name))
        try:
            completed = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            duration = time.perf_counter() - start_time
            stdout = completed.stdout[-self.policy.max_output_chars :]
            stderr = completed.stderr[-self.policy.max_output_chars :]
            return SandboxExecutionResult(
                command=command,
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_s=duration,
                timed_out=False,
                output_excerpt=(stdout + stderr)[-self.policy.max_output_chars :],
            )
        except subprocess.TimeoutExpired:
            duration = time.perf_counter() - start_time
            return SandboxExecutionResult(
                command=command,
                exit_code=124,
                stdout="",
                stderr=f"Docker container execution timed out after {timeout} seconds.",
                duration_s=duration,
                timed_out=True,
                output_excerpt="Timed out",
            )
        finally:
            mask_context.cleanup()

    def execute_python(
        self,
        code: str,
        cwd: Path | str | None = None,
        timeout_seconds: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxExecutionResult:
        if not self._is_docker_available():
            raise RuntimeError("Docker sandbox is unavailable; refusing host execution")

        host_dir = Path(cwd).resolve() if cwd else self.policy.workspace_roots[0]
        timeout = min(timeout_seconds, self.policy.default_timeout_seconds)
        start_time = time.perf_counter()

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--read-only",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=128",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpu_limit}",
            f"--network={self.network_mode}",
            "-v",
            f"{host_dir}:/workspace",
            "-w",
            "/workspace",
        ]
        if env:
            for k, v in env.items():
                docker_cmd.extend(["-e", f"{k}={v}"])

        docker_cmd.extend([self.image, "python", "-c", code])

        mask_context = tempfile.TemporaryDirectory(prefix="ivy-sandbox-masks-")
        self._add_protected_mounts(docker_cmd, host_dir, Path(mask_context.name))
        try:
            completed = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            duration = time.perf_counter() - start_time
            stdout = completed.stdout[-self.policy.max_output_chars :]
            stderr = completed.stderr[-self.policy.max_output_chars :]
            return SandboxExecutionResult(
                command=f"python -c {code[:60]}...",
                exit_code=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_s=duration,
                timed_out=False,
                output_excerpt=(stdout + stderr)[-self.policy.max_output_chars :],
            )
        except subprocess.TimeoutExpired:
            duration = time.perf_counter() - start_time
            return SandboxExecutionResult(
                command=f"python -c {code[:60]}...",
                exit_code=124,
                stdout="",
                stderr=f"Docker container python execution timed out after {timeout} seconds.",
                duration_s=duration,
                timed_out=True,
                output_excerpt="Timed out",
            )
        finally:
            mask_context.cleanup()

    def read_file(self, path: Path | str) -> str:
        raise RuntimeError("Docker file access is not implemented")

    def write_file(self, path: Path | str, content: str) -> None:
        raise RuntimeError("Docker file access is not implemented")
