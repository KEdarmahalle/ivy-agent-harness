"""Multi-Agent Swarm Orchestration Engine for Agent Harness with Live Subagent Step Tracing.

Provides:
- Handoff workflows (Agent A -> Agent B -> Agent C)
- Parallel Fan-Out / Scatter-Gather swarms (Async parallel execution across multiple agents)
- Real-time subagent tool invocation and progress callbacks
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harness.events import global_event_bus
from harness.registries.model_registry import ModelRegistry, ModelResponse
from harness.registries.tool_registry import ToolRegistry


@dataclass
class SwarmAgentSpec:
    name: str
    system_prompt: str
    route: str = "default"
    tools: list[str] = field(default_factory=list)
    can_handoff_to: list[str] = field(default_factory=list)


@dataclass
class SwarmTurnResult:
    agent_name: str
    response: ModelResponse
    handoff_to: str | None = None
    tools_executed: list[dict[str, Any]] = field(default_factory=list)


class SwarmCoordinator:
    """Coordinates multi-agent swarms using handoffs or parallel scatter-gather with live logging."""

    def __init__(
        self,
        agents: dict[str, SwarmAgentSpec],
        models: ModelRegistry,
        tools: ToolRegistry,
    ) -> None:
        self.agents = agents
        self.models = models
        self.tools = tools

    @classmethod
    def from_default_config(
        cls,
        models: ModelRegistry | None = None,
        tools: ToolRegistry | None = None,
        config: dict[str, Any] | None = None,
        agents: dict[str, SwarmAgentSpec] | None = None,
    ) -> SwarmCoordinator:
        cfg = config or {}
        models = models or ModelRegistry.from_config(cfg)
        tools = tools or ToolRegistry.from_config(cfg)
        default_agents = {
            "analyst": SwarmAgentSpec(name="analyst", system_prompt="Analyze repository, list files, and inspect status.", tools=["file.list", "time.now"]),
            "reviewer": SwarmAgentSpec(name="reviewer", system_prompt="Review findings and synthesize executive report.", tools=[]),
            "security": SwarmAgentSpec(name="security", system_prompt="Audit security risks and permissions.", tools=["file.search_text"]),
            "code_auditor": SwarmAgentSpec(name="code_auditor", system_prompt="Audit repository architecture and AST symbols.", tools=["file.search_text", "file.read_lines"]),
            "security_sentinel": SwarmAgentSpec(name="security_sentinel", system_prompt="Audit security guardrails and sandbox permissions.", tools=["sandbox.inspect", "file.read_text"]),
            "security_auditor": SwarmAgentSpec(name="security_auditor", system_prompt="Audit source diffs and codebases for vulnerabilities, secret leaks, and security flaws.", tools=["file.search_text", "file.read_lines"]),
            "performance_profiler": SwarmAgentSpec(name="performance_profiler", system_prompt="Analyze code complexity, potential memory leaks, unoptimized queries, and hot paths.", tools=["file.search_text", "sandbox.run_python"]),
            "tech_lead": SwarmAgentSpec(name="tech_lead", system_prompt="You are a principal tech lead. Synthesize multi-agent code audits, PR diff reviews, and architectural findings into a clear, structured decision.", tools=[]),
            "test_runner": SwarmAgentSpec(name="test_runner", system_prompt="Execute pytest test runs and report status.", tools=["process.run"]),
            "chaos_tester": SwarmAgentSpec(name="chaos_tester", system_prompt="Execute performance stress testing and optimization.", tools=["file.write_text", "process.run"]),
            "ast_architect": SwarmAgentSpec(name="ast_architect", system_prompt="Search AST structures and inspect code models.", tools=["file.search_text", "file.read_lines"]),
            "data_analyst": SwarmAgentSpec(name="data_analyst", system_prompt="Perform data analysis and JSON transformations.", tools=["sandbox.run_python", "data.json_transform"]),
        }
        if agents:
            default_agents.update(agents)
        return cls(agents=default_agents, models=models, tools=tools)

    async def _execute_agent_turn(
        self,
        agent_name: str,
        messages: list[dict[str, Any]],
        spec: SwarmAgentSpec,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        max_tool_turns: int = 3,
    ) -> tuple[ModelResponse, list[dict[str, Any]]]:
        """Execute a subagent turn with autonomous tool execution loop."""
        parts = spec.route.split(".", 1)
        graph_part = parts[0] if len(parts) > 1 else "swarm"
        node_part = parts[1] if len(parts) > 1 else spec.name

        tools_schema = self.tools.to_openai_tools(spec.tools) if spec.tools else None
        tools_executed: list[dict[str, Any]] = []

        loop = asyncio.get_running_loop()
        turn_messages = list(messages)
        final_reply: ModelResponse | None = None

        for _ in range(max_tool_turns):
            reply = await loop.run_in_executor(
                None,
                lambda: self.models.complete(
                    graph_part,
                    node_part,
                    turn_messages,
                    tools=tools_schema,
                ),
            )
            final_reply = reply

            # Dual-mode extraction: native tool_calls OR prompt JSON calls
            calls_to_execute: list[dict[str, Any]] = []
            if reply.tool_calls:
                for tc in reply.tool_calls:
                    if hasattr(tc, "name"):
                        calls_to_execute.append({"tool": tc.name, "args": tc.arguments or {}})
                    elif isinstance(tc, dict):
                        calls_to_execute.append({
                            "tool": tc.get("tool") or tc.get("name") or "unknown",
                            "args": tc.get("args") or tc.get("arguments") or {},
                        })
            else:
                from harness.cli import parse_tool_calls
                calls_to_execute = parse_tool_calls(str(reply))

            if calls_to_execute:
                turn_tool_records = []
                for call_dict in calls_to_execute:
                    t_name = call_dict.get("tool", "unknown")
                    t_args = call_dict.get("args", {})
                    try:
                        res = self.tools.invoke(t_name, **t_args)
                        t_record = {"tool": t_name, "args": t_args, "result": res, "ok": True}
                    except Exception as exc:  # noqa: BLE001
                        t_record = {"tool": t_name, "args": t_args, "error": str(exc), "ok": False}

                    tools_executed.append(t_record)
                    turn_tool_records.append(t_record)
                    if on_event:
                        on_event({
                            "type": "subagent_tool",
                            "agent": agent_name,
                            "tool": t_name,
                            "args": t_args,
                            "result": t_record.get("result") or t_record.get("error"),
                            "ok": t_record["ok"],
                        })

                # Append tool observations back to the subagent conversation for next turn
                turn_messages.append({"role": "assistant", "content": str(reply)})
                turn_messages.append({
                    "role": "user",
                    "content": f"Subagent Tool Observations:\n{turn_tool_records}\nContinue your task or synthesize your final findings.",
                })
            else:
                break

        return final_reply or ModelResponse("No response generated"), tools_executed

    async def run_handoff(
        self,
        entry_agent: str,
        task: str,
        context: dict[str, Any] | None = None,
        max_hops: int = 5,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute a sequential handoff swarm where agents pass control to specialized peers."""
        current_agent_name = entry_agent
        current_task = task
        shared_context = dict(context or {})
        history: list[SwarmTurnResult] = []

        for hop in range(1, max_hops + 1):
            if current_agent_name not in self.agents:
                raise KeyError(f"agent {current_agent_name!r} not found in swarm")

            spec = self.agents[current_agent_name]

            if on_event:
                on_event({
                    "type": "subagent_started",
                    "agent": current_agent_name,
                    "hop": hop,
                    "task": current_task,
                })
            global_event_bus.publish("swarm_handoff_hop", "swarm", {"agent": current_agent_name, "hop": hop})

            messages = [
                {"role": "system", "content": spec.system_prompt},
                {
                    "role": "user",
                    "content": f"Task: {current_task}\n\nShared Swarm Context:\n{shared_context}",
                },
            ]

            reply, tools_exec = await self._execute_agent_turn(
                current_agent_name, messages, spec, on_event=on_event
            )

            # Check if agent or policy requested a handoff to another agent
            handoff_target: str | None = None
            for candidate in spec.can_handoff_to:
                if (
                    f"HANDOFF:{candidate}" in str(reply)
                    or f"handoff to {candidate}" in str(reply).lower()
                    or f"HANDOFF:{candidate}" in spec.system_prompt
                ):
                    handoff_target = candidate
                    break

            turn = SwarmTurnResult(
                agent_name=current_agent_name,
                response=reply,
                handoff_to=handoff_target,
                tools_executed=tools_exec,
            )
            history.append(turn)
            shared_context[f"{current_agent_name}_output"] = str(reply)

            if on_event:
                on_event({
                    "type": "subagent_completed",
                    "agent": current_agent_name,
                    "hop": hop,
                    "handoff_to": handoff_target,
                    "tools_count": len(tools_exec),
                    "output": str(reply),
                })

            if handoff_target and handoff_target != current_agent_name:
                current_agent_name = handoff_target
            else:
                break

        return {
            "status": "completed",
            "final_agent": current_agent_name,
            "hop_count": len(history),
            "final_output": str(history[-1].response) if history else "",
            "shared_context": shared_context,
            "turns": [
                {
                    "agent": t.agent_name,
                    "output": str(t.response),
                    "handoff_to": t.handoff_to,
                    "tools_executed": t.tools_executed,
                }
                for t in history
            ],
        }

    async def run_fan_out(
        self,
        agent_names: list[str],
        task: str,
        synthesizer_agent: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute parallel scatter-gather: runs all agents concurrently and synthesizes results."""
        async def _run_single(idx: int, name: str) -> dict[str, Any]:
            if name not in self.agents:
                self.agents[name] = SwarmAgentSpec(
                    name=name,
                    system_prompt=f"You are the specialized '{name}' persona in an autonomous swarm. Fulfill your assigned subtask.",
                    tools=["file.list", "file.read_text", "file.search_text", "sandbox.inspect", "sandbox.run_python", "process.run", "time.now"],
                )
            spec = self.agents[name]

            if on_event:
                on_event({
                    "type": "subagent_started",
                    "agent": name,
                    "index": idx + 1,
                    "total": len(agent_names),
                    "task": task,
                })
            global_event_bus.publish("swarm_subagent_start", "swarm", {"agent": name, "index": idx + 1})

            messages = [
                {"role": "system", "content": spec.system_prompt},
                {"role": "user", "content": task},
            ]

            reply, tools_exec = await self._execute_agent_turn(
                name, messages, spec, on_event=on_event
            )

            if on_event:
                on_event({
                    "type": "subagent_completed",
                    "agent": name,
                    "index": idx + 1,
                    "total": len(agent_names),
                    "tools_count": len(tools_exec),
                    "output": str(reply),
                })

            return {"agent": name, "output": str(reply), "tools_executed": tools_exec}

        # Run all agents in parallel
        results = await asyncio.gather(*[_run_single(i, name) for i, name in enumerate(agent_names)])
        agent_outputs = {r["agent"]: r["output"] for r in results}

        # Optional synthesis agent
        synthesis = ""
        if synthesizer_agent:
            if synthesizer_agent not in self.agents:
                self.agents[synthesizer_agent] = SwarmAgentSpec(
                    name=synthesizer_agent,
                    system_prompt=f"You are the senior '{synthesizer_agent}' synthesizer. Consolidate and synthesize multi-agent findings into a final conclusion.",
                    tools=[],
                )
            if on_event:
                on_event({
                    "type": "subagent_started",
                    "agent": synthesizer_agent,
                    "role": "synthesizer",
                    "task": "Synthesizing multi-agent findings",
                })
            synth_spec = self.agents[synthesizer_agent]
            synth_messages = [
                {"role": "system", "content": synth_spec.system_prompt},
                {
                    "role": "user",
                    "content": f"Task: {task}\n\nAgent Findings:\n{agent_outputs}\n\nSynthesize final conclusion.",
                },
            ]
            loop = asyncio.get_running_loop()
            synth_reply = await loop.run_in_executor(
                None,
                lambda: self.models.complete("swarm", synthesizer_agent, synth_messages),
            )
            synthesis = str(synth_reply)

            if on_event:
                on_event({
                    "type": "subagent_completed",
                    "agent": synthesizer_agent,
                    "role": "synthesizer",
                    "output": synthesis,
                })

        return {
            "status": "completed",
            "agent_count": len(agent_names),
            "findings": agent_outputs,
            "synthesis": synthesis or "\n".join(f"[{k}]: {v}" for k, v in agent_outputs.items()),
            "subagents": results,
        }
