"""Universal Python SDK for attaching the Agent Harness to any client project."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.config import deep_get, load_config
from harness.context import apply_observation_masking
from harness.detector import ProjectProfile, detect_project
from harness.engine.runner import GraphRunner
from harness.engine.schemas import RunSnapshot
from harness.engine.store import InMemoryRunStore
from harness.events import (
    global_event_bus,
    reset_current_event_run_id,
    set_current_event_run_id,
)
from harness.memory import MemoryStore
from harness.parser import (
    execute_chat_tool,
    parse_tool_calls,
    repair_truncated_json,
    summarize_tool_observations,
)
from harness.plugins import PluginRegistry, tool
from harness.prompts import build_harness_context, build_smart_system_prompt
from harness.registries.mcp_registry import MCPRegistry
from harness.registries.model_registry import ModelRegistry
from harness.registries.tool_registry import ToolRegistry
from harness.secrets import load_secrets
from harness.skills import SkillRegistry
from harness.swarm import SwarmCoordinator


@dataclass
class RunConfig:
    """Runtime overrides and execution parameters for an agent invocation."""
    graph: str = "nim"
    node: str = "nemotron"
    route: str | None = None
    max_tool_steps: int = 10
    max_tokens: int = 4096
    temperature: float = 0.0
    auto_approve_tools: bool = True
    tool_calling_mode: str = "auto"
    stream: bool = True
    system_prompt: str | None = None
    tools_filter: list[str] | None = None
    context_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamEvent:
    """Typed streaming event emitted by AgentHarness during execution."""
    type: str  # "token" | "tool_start" | "tool_result" | "subagent_start" | "subagent_completed" | "turn_completed" | "error"
    delta: str = ""
    tool_name: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    summary: str | None = None
    ok: bool = True
    agent: str | None = None
    observation: str | None = None
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    duration_s: float = 0.0
    final_output: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "delta": self.delta,
            "tool_name": self.tool_name,
            "args": self.args,
            "result": self.result,
            "summary": self.summary,
            "ok": self.ok,
            "agent": self.agent,
            "observation": self.observation,
            "tools_called": self.tools_called,
            "step": self.step,
            "duration_s": self.duration_s,
            "final_output": self.final_output,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class AgentResult:
    """Structured result returned by AgentHarness run / arun execution."""
    final_output: str
    steps: int
    tool_calls: list[dict[str, Any]]
    duration_s: float
    success: bool = True
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_output": self.final_output,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "duration_s": self.duration_s,
            "success": self.success,
            "error": self.error,
            "metadata": self.metadata,
        }


class AgentHarness:
    """Universal embedding interface for attaching autonomous agents to any client app."""

    def __init__(
        self,
        workspace_root: str | Path = ".",
        config: dict[str, Any] | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        if config_path:
            self.config = load_config(Path(config_path))
        else:
            self.config = config or load_config()

        # Propagate workspace root to sandbox tools
        tools_cfg = self.config.setdefault("tools", {})
        sandbox_cfg = tools_cfg.setdefault("sandbox", {})
        if self.workspace_root != Path.cwd().resolve():
            sandbox_cfg["workspace_roots"] = [str(self.workspace_root)]
            sandbox_cfg["writable_roots"] = [str(self.workspace_root)]
        else:
            sandbox_cfg.setdefault("workspace_roots", [str(self.workspace_root)])
            sandbox_cfg.setdefault("writable_roots", [str(self.workspace_root)])

        self.secrets = load_secrets()
        self.tools = ToolRegistry.from_config(self.config)
        self.models = ModelRegistry.from_config(self.config)
        self.skills = SkillRegistry(workspace_root=self.workspace_root)
        self.memory = MemoryStore(workspace_root=self.workspace_root)
        self.mcp = MCPRegistry.from_config(self.config)
        self.plugins = PluginRegistry(workspace_root=self.workspace_root)
        self.profile: ProjectProfile = detect_project(self.workspace_root)
        self.swarm_coordinator = SwarmCoordinator.from_default_config(
            tools=self.tools,
            models=self.models,
            config=self.config,
        )

    @classmethod
    def from_project(
        cls,
        workspace_root: str | Path = ".",
        config_path: str | Path | None = None,
    ) -> AgentHarness:
        """Create a harness instance attached to a specific workspace root."""
        return cls(workspace_root=workspace_root, config_path=config_path)

    def stream(
        self,
        prompt: str,
        config: RunConfig | None = None,
    ) -> Iterator[StreamEvent]:
        """Stream autonomous agent reasoning, tokens, tool actions, and subagent events synchronously."""
        cfg = config or RunConfig()
        start_time = time.perf_counter()

        graph = cfg.graph
        node = cfg.node
        if cfg.route:
            parts = cfg.route.split(".", 1)
            graph = parts[0]
            node = parts[1] if len(parts) > 1 else "default"

        max_steps = cfg.max_tool_steps
        max_tokens = cfg.max_tokens

        # Resolve system prompt & context
        base_system = cfg.system_prompt or str(
            deep_get(
                self.config,
                "chat.system_prompt",
                "You are Ivy, an expert autonomous software engineering agent running inside Agent Harness.",
            )
        )
        harness_ctx = build_harness_context(self.config)
        skills_ctx = self.skills.render_skills_prompt_catalog()
        project_ctx = self.profile.to_prompt_context()
        memory_ctx = self.memory.format_context(prompt)

        tools_catalog = self.tools.catalog()
        if cfg.tools_filter:
            tools_catalog = {k: v for k, v in tools_catalog.items() if k in cfg.tools_filter}

        system_content = build_smart_system_prompt(
            base_prompt=base_system,
            tools_catalog=tools_catalog,
            harness_context=harness_ctx,
            memory_context=memory_ctx,
            skills_context=skills_ctx,
            project_context=project_ctx,
            config=self.config,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ]

        step_count = 0
        all_tool_records: list[dict[str, Any]] = []
        final_text = ""
        run_id = f"chat_{uuid4().hex}"

        # Event listener for subagent and swarm lifecycle events
        def handle_subagent_event(event: Any) -> None:
            if (
                hasattr(event, "event_type")
                and event.event_type == "subagent_completed"
                and event.run_id == run_id
            ):
                payload = getattr(event, "payload", {})
                yielded_event = StreamEvent(
                    type="subagent_completed",
                    agent=payload.get("agent"),
                    observation=f"Subagent {payload.get('agent', 'unknown')} completed.",
                    tools_called=[],
                )
                event_queue.append(yielded_event)

        event_queue: list[StreamEvent] = []
        global_event_bus.subscribe(handle_subagent_event)

        try:
            for step in range(1, max_steps + 1):
                # Drain any asynchronous subagent events
                while event_queue:
                    yield event_queue.pop(0)

                # Execute LLM completion
                token_chunks: list[str] = []

                def on_token(delta: str, target: list[str] = token_chunks) -> None:
                    target.append(delta)

                # Apply rolling observation masking for context optimization
                messages_payload = apply_observation_masking(messages, keep_recent=2)

                try:
                    response = self.models.complete(
                        graph,
                        node,
                        messages_payload,
                        max_tokens=max_tokens,
                        stream=cfg.stream,
                        on_chunk=on_token,
                    )
                    reply = response.content if hasattr(response, "content") else str(response)
                except Exception as exc:  # noqa: BLE001 - convert provider failures to stream errors
                    yield StreamEvent(type="error", error=f"Model error: {exc}")
                    return

                # Yield tokens if streamed
                for chunk in token_chunks:
                    yield StreamEvent(type="token", delta=chunk)

                # Check for tool calls (native or parsed)
                tool_calls: list[dict[str, Any]] = []
                if hasattr(response, "tool_calls") and response.tool_calls:
                    for tc in response.tool_calls:
                        tool_calls.append({"tool": tc.name, "args": tc.arguments})
                else:
                    tool_calls = parse_tool_calls(reply)

                # Auto-repair truncated JSON
                if not tool_calls and re.search(r'\{\s*["\']tool["\']\s*:', reply):
                    repaired = repair_truncated_json(reply)
                    tool_calls = parse_tool_calls(repaired)

                # If no tool calls, the agent has finished thinking
                if not tool_calls:
                    final_text = reply
                    break

                # Execute tools
                step_observations: list[dict[str, Any]] = []
                for tc in tool_calls:
                    step_count += 1
                    t_name = tc.get("tool", "unknown")
                    t_args = tc.get("args", {})
                    yield StreamEvent(type="tool_start", tool_name=t_name, args=t_args, step=step_count)

                    t_start = time.perf_counter()
                    run_token = set_current_event_run_id(run_id)
                    try:
                        observation = execute_chat_tool(self.tools, tc)
                    finally:
                        reset_current_event_run_id(run_token)
                    t_dur_ms = (time.perf_counter() - t_start) * 1000.0

                    summary_str = f"status={observation.get('ok')}"
                    if observation.get("ok"):
                        res_val = observation.get("result")
                        if isinstance(res_val, dict):
                            summary_str = ", ".join(f"{k}={v}" for k, v in list(res_val.items())[:3])
                        else:
                            summary_str = str(res_val)[:60]

                    all_tool_records.append(observation)
                    step_observations.append(observation)

                    yield StreamEvent(
                        type="tool_result",
                        tool_name=t_name,
                        args=t_args,
                        result=observation.get("result"),
                        summary=summary_str,
                        ok=bool(observation.get("ok")),
                        error=observation.get("error"),
                        step=step_count,
                        duration_s=t_dur_ms / 1000.0,
                    )

                # Append tool observation to chat context
                obs_data = step_observations if len(step_observations) > 1 else step_observations[0] if step_observations else {}
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Tool result(s):\n"
                            f"{json.dumps(obs_data, indent=2, default=str)}\n\n"
                            "Synthesize findings or output next tool call."
                        ),
                    }
                )

            # If loop finished after tool steps without final text, generate synthesis
            if not final_text:
                final_text = summarize_tool_observations(all_tool_records)

            duration = time.perf_counter() - start_time
            yield StreamEvent(
                type="turn_completed",
                final_output=final_text,
                step=step_count,
                duration_s=duration,
                tools_called=all_tool_records,
            )

        finally:
            global_event_bus.unsubscribe(handle_subagent_event)

    def run(
        self,
        prompt: str,
        config: RunConfig | None = None,
    ) -> AgentResult:
        """Execute autonomous agent synchronously and return aggregated AgentResult."""
        final_output = ""
        step_count = 0
        tool_records: list[dict[str, Any]] = []
        duration = 0.0
        success = True
        err_msg = None

        for event in self.stream(prompt, config=config):
            if event.type == "turn_completed":
                final_output = event.final_output or ""
                step_count = event.step
                duration = event.duration_s
                tool_records = event.tools_called
            elif event.type == "error":
                success = False
                err_msg = event.error

        return AgentResult(
            final_output=final_output,
            steps=step_count,
            tool_calls=tool_records,
            duration_s=duration,
            success=success,
            error=err_msg,
        )

    async def astream(
        self,
        prompt: str,
        config: RunConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Asynchronously stream agent execution events."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

        def producer() -> None:
            for event in self.stream(prompt, config=config):
                loop.call_soon_threadsafe(queue.put_nowait, event)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, producer)

        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    async def arun(
        self,
        prompt: str,
        config: RunConfig | None = None,
    ) -> AgentResult:
        """Execute autonomous agent asynchronously and return AgentResult."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.run, prompt, config)

    def swarm(
        self,
        task: str,
        agents: list[str] | str = "analyst,reviewer",
        pattern: str = "fanout",
        synthesizer: str = "reviewer",
    ) -> dict[str, Any]:
        """Run a multi-agent swarm delegation task (fanout / handoff)."""
        agent_names = [a.strip() for a in agents.split(",")] if isinstance(agents, str) else agents
        if pattern == "handoff":
            return asyncio.run(self.swarm_coordinator.run_handoff(agent_names=agent_names, task=task))
        return asyncio.run(
            self.swarm_coordinator.run_fan_out(
                agent_names=agent_names,
                task=task,
                synthesizer_agent=synthesizer,
            )
        )

    def workflow(self, graph_name: str, input_data: dict[str, Any]) -> RunSnapshot:
        """Execute any multi-node declarative or built-in workflow graph."""
        store = InMemoryRunStore()
        runner = GraphRunner(
            store=store,
            config=self.config,
            tools=self.tools,
            models=self.models,
            mcp=self.mcp,
        )
        return asyncio.run(runner.run(graph_name, input_data))

    def tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Directly invoke a registered tool."""
        return self.tools.invoke(name, **kwargs)

    def list_tools(self) -> list[str]:
        """List all currently allowed and registered tool names."""
        return self.tools.names()


__all__ = ["AgentHarness", "AgentResult", "RunConfig", "StreamEvent", "tool"]
