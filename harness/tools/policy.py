"""Sandbox security policy and workspace path resolution."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.config import deep_get


@dataclass(frozen=True)
class SandboxPolicy:
    """Security boundaries and execution limits for tool invocations."""
    workspace_roots: list[Path]
    writable_roots: list[Path]
    is_custom_roots: bool = False
    network_enabled: bool = False
    max_read_bytes: int = 256 * 1024
    max_write_bytes: int = 256 * 1024
    max_output_chars: int = 4000
    default_timeout_seconds: int = 120
    protected_paths: tuple[str, ...] = (
        ".env", ".secrets", "secrets/", ".harness/", ".ssh/", "id_rsa", "id_ed25519",
        "kubeconfig", "credentials.json", ".env*", "*.pem", "*.key", "*.p12", "*.pfx",
        "*_rsa", "*_ed25519", "*credentials*.json", "*.db", "*.sqlite", "*.sqlite3",
        ".git/", "__pycache__/",
    )

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> SandboxPolicy:
        tools_cfg = deep_get(config, "tools", {}) or {}
        sandbox_cfg = deep_get(tools_cfg, "sandbox", {}) or {}
        has_custom_ws = "workspace_roots" in sandbox_cfg
        has_custom_wr = "writable_roots" in sandbox_cfg
        workspace_roots = [
            Path(p).resolve() for p in sandbox_cfg.get("workspace_roots", ["."])
        ]
        writable_roots = [
            Path(p).resolve() for p in sandbox_cfg.get("writable_roots", ["."])
        ]
        defaults = {
            ".env", ".secrets", "secrets/", ".harness/", ".ssh/", "id_rsa", "id_ed25519",
            "kubeconfig", "credentials.json", ".env*", "*.pem", "*.key", "*.p12", "*.pfx",
            "*_rsa", "*_ed25519", "*credentials*.json", "*.db", "*.sqlite", "*.sqlite3",
            ".git/", "__pycache__/",
        }
        custom_protected = tuple(sorted(defaults | set(deep_get(tools_cfg, "protected_paths", []) or [])))
        limits = {
            "max_read_bytes": int(sandbox_cfg.get("max_read_bytes", 256 * 1024)),
            "max_write_bytes": int(sandbox_cfg.get("max_write_bytes", 256 * 1024)),
            "max_output_chars": int(sandbox_cfg.get("max_output_chars", 4000)),
            "default_timeout_seconds": int(sandbox_cfg.get("default_timeout_seconds", 120)),
        }
        if any(value <= 0 for value in limits.values()):
            raise ValueError("sandbox byte, output, and timeout limits must all be positive")
        return cls(
            workspace_roots=workspace_roots,
            writable_roots=writable_roots,
            is_custom_roots=has_custom_ws or has_custom_wr,
            network_enabled=bool(sandbox_cfg.get("network_enabled", False)),
            **limits,
            protected_paths=custom_protected,
        )

    def is_in_workspace(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(root in resolved.parents or resolved == root for root in self.workspace_roots)

    def is_writable(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(root in resolved.parents or resolved == root for root in self.writable_roots)

    def is_protected(self, path: Path | str, ignore_patterns: set[str] | None = None) -> bool:
        # Windows paths are case-insensitive, so protection checks must be too
        # even when the same policy is used by a POSIX runtime.
        rel = Path(path).as_posix().casefold()
        for pattern in self.protected_paths:
            if ignore_patterns and pattern in ignore_patterns:
                continue
            pattern = pattern.casefold()
            if fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(Path(rel).name, pattern):
                return True
            normalized = pattern.rstrip("/")
            if pattern.endswith("/"):
                if f"/{normalized}/" in f"/{rel}/" or rel.endswith(normalized):
                    return True
            elif rel.endswith(pattern) or f"/{pattern}" in rel or rel == pattern or Path(rel).name == pattern:
                return True
        return False
