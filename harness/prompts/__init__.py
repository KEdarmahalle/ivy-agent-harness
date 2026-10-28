"""System prompts and context construction for Agent Harness."""

from harness.prompts.builder import (
    build_cacheable_system_prompt_prefix,
    build_harness_context,
    build_smart_system_prompt,
)
from harness.prompts.templates import (
    COGNITIVE_PROTOCOLS_TEMPLATE,
    render_identity,
    render_platform_constraints,
    render_tool_protocol,
)

__all__ = [
    "COGNITIVE_PROTOCOLS_TEMPLATE",
    "build_cacheable_system_prompt_prefix",
    "build_harness_context",
    "build_smart_system_prompt",
    "render_identity",
    "render_platform_constraints",
    "render_tool_protocol",
]
