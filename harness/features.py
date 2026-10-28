from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.config import deep_get


@dataclass
class FeatureFlag:
    key: str
    name: str
    description: str
    config_path: str
    value_type: type
    default: Any
    category: str
    options: list[Any] | None = None
    validator: Callable[[Any], bool] | None = None


class FeatureRegistry:
    """Centralized dynamic feature flag and configuration registry for Agent Harness."""

    def __init__(self) -> None:
        self._features: dict[str, FeatureFlag] = {}
        self._register_default_features()

    def register(self, feature: FeatureFlag) -> None:
        self._features[feature.key] = feature

    def get_feature(self, key: str) -> FeatureFlag | None:
        return self._features.get(key)

    def list_features(self) -> list[FeatureFlag]:
        return list(self._features.values())

    def get_value(self, key: str, config: dict[str, Any]) -> Any:
        feat = self._features.get(key)
        if not feat:
            raise KeyError(f"Unknown feature flag: {key}")
        return deep_get(config, feat.config_path, feat.default)

    def set_value(self, key: str, value: Any, config: dict[str, Any]) -> bool:
        feat = self._features.get(key)
        if not feat:
            raise KeyError(f"Unknown feature flag: {key}")

        # Cast or validate type
        parsed_val = self._cast_value(feat, value)
        if feat.options and parsed_val not in feat.options:
            raise ValueError(f"Value '{parsed_val}' not in allowed options: {feat.options}")
        if feat.validator and not feat.validator(parsed_val):
            raise ValueError(f"Value '{parsed_val}' failed validation for feature '{key}'")

        # Set into config dictionary using dot-path
        parts = feat.config_path.split(".")
        target = config
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = parsed_val
        return True

    def toggle(self, key: str, config: dict[str, Any]) -> bool:
        feat = self._features.get(key)
        if not feat:
            raise KeyError(f"Unknown feature flag: {key}")
        if feat.value_type is not bool:
            raise TypeError(f"Cannot toggle non-boolean feature '{key}' (type={feat.value_type.__name__})")
        current = bool(self.get_value(key, config))
        new_val = not current
        self.set_value(key, new_val, config)
        return new_val

    def _cast_value(self, feat: FeatureFlag, raw_val: Any) -> Any:
        if isinstance(raw_val, feat.value_type):
            return raw_val
        if feat.value_type is bool:
            if isinstance(raw_val, str):
                return raw_val.lower() in {"true", "1", "yes", "on", "enable", "enabled"}
            return bool(raw_val)
        if feat.value_type is int:
            return int(raw_val)
        if feat.value_type is float:
            return float(raw_val)
        if feat.value_type is str:
            return str(raw_val)
        return raw_val

    def snapshot(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a structured summary of all features and their current runtime values."""
        results = []
        for feat in self._features.values():
            val = self.get_value(feat.key, config)
            results.append({
                "key": feat.key,
                "name": feat.name,
                "description": feat.description,
                "config_path": feat.config_path,
                "category": feat.category,
                "value": val,
                "default": feat.default,
                "type": feat.value_type.__name__,
                "options": feat.options,
            })
        return results

    def _register_default_features(self) -> None:
        # Agent Loop & Autonomy
        self.register(
            FeatureFlag(
                key="agent_loop.enabled",
                name="Agentic Loop Execution",
                description="Enable multi-step tool reasoning and iterative execution loop.",
                config_path="chat.agent_loop.enabled",
                value_type=bool,
                default=True,
                category="Agent Loop",
            )
        )
        self.register(
            FeatureFlag(
                key="agent_loop.max_tool_steps",
                name="Max Tool Steps",
                description="Maximum number of consecutive tool execution steps per user prompt.",
                config_path="chat.agent_loop.max_tool_steps",
                value_type=int,
                default=10,
                category="Agent Loop",
            )
        )
        self.register(
            FeatureFlag(
                key="agent_loop.max_tokens",
                name="Model Token Generation Budget",
                description="Maximum generation tokens allocated for each LLM turn.",
                config_path="chat.agent_loop.max_tokens",
                value_type=int,
                default=4096,
                category="Agent Loop",
            )
        )
        self.register(
            FeatureFlag(
                key="agent_loop.auto_repair_truncated_tools",
                name="Truncated JSON Auto-Repair",
                description="Automatically balance unclosed strings and braces from partial model outputs.",
                config_path="chat.agent_loop.auto_repair_truncated_tools",
                value_type=bool,
                default=True,
                category="Agent Loop",
            )
        )
        self.register(
            FeatureFlag(
                key="agent_loop.continuation_on_truncation",
                name="Truncation Continuation Prompting",
                description="Prompt model for immediate completion when cut off mid-tool call.",
                config_path="chat.agent_loop.continuation_on_truncation",
                value_type=bool,
                default=True,
                category="Agent Loop",
            )
        )
        self.register(
            FeatureFlag(
                key="agent_loop.parallel_tool_execution",
                name="Parallel Tool Calling",
                description="Allow model to execute multiple tool calls in a single turn.",
                config_path="chat.agent_loop.parallel_tool_execution",
                value_type=bool,
                default=True,
                category="Agent Loop",
            )
        )
        self.register(
            FeatureFlag(
                key="agent_loop.auto_approve_tools",
                name="Autonomous Tool Auto-Approval",
                description="Bypass manual approval prompts for state-mutating tools.",
                config_path="chat.agent_loop.auto_approve_tools",
                value_type=bool,
                default=True,
                category="Agent Loop",
            )
        )

        # UX & Streaming
        self.register(
            FeatureFlag(
                key="streaming.enabled",
                name="Real-Time Streaming",
                description="Stream completion tokens live for instant Time-To-First-Token feedback.",
                config_path="chat.streaming.enabled",
                value_type=bool,
                default=True,
                category="UX & Streaming",
            )
        )
        self.register(
            FeatureFlag(
                key="ui.tool_display",
                name="Tool Display Style",
                description="Display mode for tool execution events in the terminal.",
                config_path="chat.ui.tool_display",
                value_type=str,
                default="compact",
                category="UX & Streaming",
                options=["compact", "verbose"],
            )
        )
        self.register(
            FeatureFlag(
                key="ui.show_tool_args",
                name="Show Tool Arguments",
                description="Display JSON payload arguments with tool execution badges.",
                config_path="chat.ui.show_tool_args",
                value_type=bool,
                default=True,
                category="UX & Streaming",
            )
        )
        self.register(
            FeatureFlag(
                key="ui.show_step_timing",
                name="Show Execution Timing",
                description="Display elapsed latency in milliseconds for each tool step and turn.",
                config_path="chat.ui.show_step_timing",
                value_type=bool,
                default=True,
                category="UX & Streaming",
            )
        )

        # Performance & Network
        self.register(
            FeatureFlag(
                key="performance.connection_pooling",
                name="Persistent HTTP Connection Pooling",
                description="Reuse TCP/SSL connections across model completion turns to reduce latency.",
                config_path="chat.performance.connection_pooling",
                value_type=bool,
                default=True,
                category="Performance",
            )
        )

        # Memory & Context
        self.register(
            FeatureFlag(
                key="memory.guidelines_ingestion",
                name="Multi-Tier Guidelines Ingestion",
                description="Automatically parse and inject CLAUDE.md, AGENTS.md, and .cursorrules into context.",
                config_path="chat.memory.guidelines_ingestion",
                value_type=bool,
                default=True,
                category="Memory & Context",
            )
        )
        self.register(
            FeatureFlag(
                key="memory.custom_commands",
                name="Custom Slash Commands",
                description="Load project-specific custom slash commands from .harness/commands/.",
                config_path="chat.memory.custom_commands",
                value_type=bool,
                default=True,
                category="Memory & Context",
            )
        )
