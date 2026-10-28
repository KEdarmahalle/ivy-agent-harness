"""Modular Tool Registry and Built-in Tool Packs."""

from harness.tools.policy import SandboxPolicy

__all__ = ["SandboxPolicy", "ToolRegistry"]


def __getattr__(name: str):
    if name == "ToolRegistry":
        from harness.registries.tool_registry import ToolRegistry

        return ToolRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
