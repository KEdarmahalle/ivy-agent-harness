"""Factory for creating configured SandboxProvider instances."""

from __future__ import annotations

from typing import Any

from harness.config import deep_get
from harness.sandbox.base import SandboxProvider
from harness.sandbox.docker import DockerContainerSandbox
from harness.sandbox.local import LocalSubprocessSandbox
from harness.tools.policy import SandboxPolicy


def get_sandbox_provider(
    config: dict[str, Any] | None = None,
    policy: SandboxPolicy | None = None,
) -> SandboxProvider:
    """Get SandboxProvider based on configuration ('local' or 'docker')."""
    cfg = config or {}
    resolved_policy = policy or SandboxPolicy.from_config(cfg)
    provider_type = str(deep_get(cfg, "tools.sandbox.provider", "local")).lower()

    if provider_type in {"docker", "container", "cloud"}:
        image = str(deep_get(cfg, "tools.sandbox.image", "python:3.12-slim"))
        memory = str(deep_get(cfg, "tools.sandbox.memory_limit", "512m"))
        cpus = float(deep_get(cfg, "tools.sandbox.cpu_limit", 1.0))
        network = str(deep_get(cfg, "tools.sandbox.network_mode", "none"))
        return DockerContainerSandbox(
            image=image,
            policy=resolved_policy,
            memory_limit=memory,
            cpu_limit=cpus,
            network_mode=network,
        )

    if provider_type == "local":
        return LocalSubprocessSandbox(policy=resolved_policy)

    raise ValueError(f"unsupported sandbox provider {provider_type!r}; expected 'local' or 'docker'")
