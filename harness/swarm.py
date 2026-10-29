"""Multi-Agent Swarm Orchestration Engine for Agent Harness with Live Subagent Step Tracing.

Provides:
- Handoff workflows (Agent A -> Agent B -> Agent C)
- Parallel Fan-Out / Scatter-Gather swarms (Async parallel execution across multiple agents)
- Real-time subagent tool invocation and progress callbacks
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from harness.events import global_event_bus
from harness.parser.task_plan_parser import PlanResponseError, parse_task_plan_response
from harness.registries.model_registry import ModelRegistry, ModelResponse
from harness.registries.tool_registry import ToolRegistry


@dataclass
class SwarmAgentSpec:
    name: str
    system_prompt: str
    route: str = "default"
    tools: list[str] = field(default_factory=list)
    can_handoff_to: list[str] = field(default_factory=list)
    description: str = ""
    skills: list[str] = field(default_factory=list)
    max_tool_turns: int = 3
    role: str = ""
    tags: list[str] = field(default_factory=list)
    delegation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskPlan:
    schema_version: int
    goal: str
    tasks: tuple[dict[str, Any], ...]


@dataclass
class RunBudget:
    run_id: str
    deadline: float
    max_total_agents: int
    max_total_tool_calls: int
    agents_started: int = 0
    tool_calls: int = 0
    cancelled: bool = False
    reserved_agents: int = 0
    global_semaphore: asyncio.Semaphore | None = None
    profile_semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)


@dataclass
class OrchestrationRunContext:
    budget: RunBudget
    current_profile: str
    depth: int = 0
    parent_task_id: str | None = None
    ancestry: tuple[str, ...] = ()
    principal_context: Any = None
    deadline: float | None = None
    tool_limit: int | None = None
    slot_held: bool = False
    slot_profile: str | None = None
    child_tasks_started: int = 0


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
        skills: Any = None,
        orchestration: dict[str, Any] | None = None,
    ) -> None:
        self.models = models
        self.tools = tools
        self.skills = skills
        self.orchestration = dict(orchestration or {})
        self.mode = self.orchestration.get("mode", "single")
        self.planning_strategy = self.orchestration.get("planning_strategy", "deterministic")
        if not isinstance(self.mode, str) or self.mode not in {"off", "single", "self_directed", "required"}:
            raise ValueError("orchestration.mode: expected off, single, self_directed, or required")
        if not isinstance(self.planning_strategy, str) or self.planning_strategy not in {"deterministic", "model_directed"}:
            raise ValueError("orchestration.planning_strategy: expected deterministic or model_directed")
        planner_retries = self.orchestration.get("planner_retries", 1)
        if not isinstance(planner_retries, int) or isinstance(planner_retries, bool) or not 0 <= planner_retries <= 2:
            raise ValueError("orchestration.planner_retries: expected integer from 0 to 2")
        planner_json_mode = self.orchestration.get("planner_json_mode", True)
        if not isinstance(planner_json_mode, bool):
            raise TypeError("orchestration.planner_json_mode: expected a boolean")
        if self.mode == "required" and self.planning_strategy != "model_directed":
            raise ValueError("orchestration.mode=required requires planning_strategy=model_directed")
        self.chat_entry_profile = self.orchestration.get("chat_entry_profile")
        self.planner_profile = self.orchestration.get("planner_profile")
        # Profiles are immutable runtime configuration from the coordinator's perspective.
        self.agents = {
            name: SwarmAgentSpec(
                name=spec.name,
                system_prompt=spec.system_prompt,
                route=spec.route,
                tools=list(spec.tools),
                can_handoff_to=list(spec.can_handoff_to),
                description=spec.description,
                skills=list(spec.skills),
                max_tool_turns=spec.max_tool_turns,
                role=spec.role,
                tags=list(spec.tags),
                delegation=dict(spec.delegation),
            )
            for name, spec in agents.items()
        }

    @classmethod
    def from_default_config(
        cls,
        models: ModelRegistry | None = None,
        tools: ToolRegistry | None = None,
        config: dict[str, Any] | None = None,
        agents: dict[str, SwarmAgentSpec] | None = None,
        skills: Any = None,
    ) -> SwarmCoordinator:
        cfg = config or {}
        models = models or ModelRegistry.from_config(cfg)
        tools = tools or ToolRegistry.from_config(cfg)
        built_in_agents = {
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
        agents_config = cfg.get("agents", {})
        if not isinstance(agents_config, dict):
            raise TypeError("agents must be a mapping")
        unexpected_agent_keys = set(agents_config) - {"profiles", "include_builtins"}
        if unexpected_agent_keys:
            raise ValueError(f"agents: unknown configuration keys {sorted(unexpected_agent_keys)}")
        configured = agents_config.get("profiles", {})
        if not isinstance(configured, dict):
            raise TypeError("agents.profiles must be a mapping")
        if len(configured) > 100:
            raise ValueError("agents.profiles may contain at most 100 profiles")
        include_builtins = agents_config.get("include_builtins")
        if include_builtins is not None and not isinstance(include_builtins, bool):
            raise ValueError("agents.include_builtins: expected a boolean")
        orchestration_config = cfg.get("orchestration", {})
        configured_mode = orchestration_config.get("mode", "single") if isinstance(orchestration_config, dict) else "single"
        if include_builtins is None:
            include_builtins = not (configured and configured_mode in {"self_directed", "required"})
        default_agents = dict(built_in_agents) if include_builtins else {}
        if agents:
            default_agents.update(agents)
        if configured and skills is None:
            from harness.skills import SkillRegistry
            skills = SkillRegistry(workspace_root=".")
        for name, data in configured.items():
            prefix = f"agents.profiles.{name}"
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise ValueError(f"{prefix}: invalid profile name")
            if not isinstance(data, dict):
                raise TypeError(f"{prefix} must be a mapping")
            unexpected_fields = set(data) - {
                "description", "route", "system_prompt", "tools", "skills",
                "max_tool_turns", "can_handoff_to", "role", "tags", "delegation",
            }
            if unexpected_fields:
                raise ValueError(f"{prefix}: unknown fields {sorted(unexpected_fields)}")
            route, prompt = data.get("route"), data.get("system_prompt")
            if not isinstance(route, str) or route not in models.routes:
                raise ValueError(f"{prefix}.route: unknown model route {route!r}")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
                raise ValueError(f"{prefix}.system_prompt: required bounded string")
            if "tools" not in data:
                raise ValueError(f"{prefix}.tools: required list of tool names")
            tool_names = data.get("tools")
            if not isinstance(tool_names, list) or any(not isinstance(t, str) for t in tool_names):
                raise ValueError(f"{prefix}.tools: expected a list of tool names")
            if len(tool_names) > 50:
                raise ValueError(f"{prefix}.tools: at most 50 tools are allowed")
            if len(set(tool_names)) != len(tool_names):
                raise ValueError(f"{prefix}.tools: duplicate tool names are not allowed")
            if {"agent.delegate", "agent.plan"}.intersection(tool_names):
                raise ValueError(f"{prefix}.tools: orchestration operations are configured through delegation policy")
            unknown = set(tool_names) - set(tools.names())
            if unknown:
                raise ValueError(f"{prefix}.tools: unknown tools {sorted(unknown)}")
            if tools.allowlist and not set(tool_names).issubset(set(tools.allowlist)):
                raise ValueError(f"{prefix}.tools: tools must be globally allowlisted")
            skill_names = data.get("skills", [])
            if not isinstance(skill_names, list) or any(not isinstance(s, str) for s in skill_names):
                raise ValueError(f"{prefix}.skills: expected a list of skill names")
            if len(skill_names) > 10:
                raise ValueError(f"{prefix}.skills: at most 10 skills are allowed")
            if len(set(skill_names)) != len(skill_names):
                raise ValueError(f"{prefix}.skills: duplicate skill names are not allowed")
            missing = [s for s in skill_names if skills.get_skill(s) is None]
            if missing:
                raise ValueError(f"{prefix}.skills: unknown skills {missing}")
            turns = data.get("max_tool_turns", 3)
            if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 10:
                raise ValueError(f"{prefix}.max_tool_turns: expected integer from 1 to 10")
            description = data.get("description", "")
            if not isinstance(description, str) or len(description) > 500:
                raise ValueError(f"{prefix}.description: expected text up to 500 characters")
            role = data.get("role", "")
            tags = data.get("tags", [])
            delegation = data.get("delegation", {})
            if not isinstance(role, str) or len(role) > 100:
                raise ValueError(f"{prefix}.role: expected text up to 100 characters")
            if not isinstance(tags, list) or any(not isinstance(tag, str) or len(tag) > 64 for tag in tags) or len(tags) > 30:
                raise ValueError(f"{prefix}.tags: expected up to 30 bounded strings")
            if not isinstance(delegation, dict) or set(delegation) - {"enabled", "max_depth", "max_children", "max_parallel_children", "allowed_targets", "allowed_target_tags", "budget"}:
                raise ValueError(f"{prefix}.delegation: invalid fields")
            if delegation:
                if not isinstance(delegation.get("enabled", False), bool):
                    raise ValueError(f"{prefix}.delegation.enabled: expected boolean")
                for limit_name, low, high in (("max_depth", 0, 16), ("max_children", 0, 100), ("max_parallel_children", 1, 32)):
                    value = delegation.get(limit_name)
                    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high):
                        raise ValueError(f"{prefix}.delegation.{limit_name}: invalid limit")
                for filter_name in ("allowed_targets", "allowed_target_tags"):
                    values = delegation.get(filter_name, [])
                    if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
                        raise ValueError(f"{prefix}.delegation.{filter_name}: expected unique strings")
                budget_config = delegation.get("budget", {})
                if not isinstance(budget_config, dict) or set(budget_config) - {"max_tool_calls", "timeout_seconds"}:
                    raise ValueError(f"{prefix}.delegation.budget: invalid fields")
                for limit_name, max_value in (("max_tool_calls", 1000), ("timeout_seconds", 86400)):
                    value = budget_config.get(limit_name)
                    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= max_value):
                        raise ValueError(f"{prefix}.delegation.budget.{limit_name}: invalid limit")
            default_agents[name] = SwarmAgentSpec(name=name, system_prompt=prompt, route=route, tools=list(tool_names), description=description, skills=list(skill_names), max_tool_turns=turns, role=role, tags=list(tags), delegation=dict(delegation))
        for name, data in configured.items():
            handoffs = data.get("can_handoff_to", [])
            if not isinstance(handoffs, list) or any(not isinstance(target, str) for target in handoffs):
                raise ValueError(f"agents.profiles.{name}.can_handoff_to: expected a list of profile names")
            if len(handoffs) > 50 or len(set(handoffs)) != len(handoffs):
                raise ValueError(f"agents.profiles.{name}.can_handoff_to: maximum 50 unique profile names")
            missing_targets = [target for target in handoffs if target not in default_agents]
            if missing_targets:
                raise ValueError(f"agents.profiles.{name}.can_handoff_to: unknown profiles {missing_targets}")
            default_agents[name].can_handoff_to = list(handoffs)
            policy = default_agents[name].delegation
            if not policy and handoffs:
                # Migrate the legacy edge list into the single canonical policy.
                policy = {"enabled": True, "allowed_targets": list(handoffs)}
                default_agents[name].delegation = policy
            allowed_targets = policy.get("allowed_targets", [])
            missing_targets = [target for target in allowed_targets if target not in default_agents]
            if missing_targets:
                raise ValueError(f"agents.profiles.{name}.delegation.allowed_targets: unknown profiles {missing_targets}")
        orchestration = cfg.get("orchestration", {})
        if not isinstance(orchestration, dict):
            raise TypeError("orchestration: expected a mapping")
        allowed_orchestration = {"schema_version", "mode", "planning_strategy", "planner_json_mode", "planner_retries", "chat_entry_profile", "planner_profile", "max_depth", "max_total_agents", "max_parallel_agents", "max_plan_tasks", "max_total_tool_calls", "timeout_seconds", "failure_policy", "context_policy", "result_bytes_per_task", "profile_card_fields"}
        if set(orchestration) - allowed_orchestration:
            raise ValueError(f"orchestration: unknown fields {sorted(set(orchestration) - allowed_orchestration)}")
        orchestration_version = orchestration.get("schema_version", 1)
        if not isinstance(orchestration_version, int) or isinstance(orchestration_version, bool) or orchestration_version != 1:
            raise ValueError("orchestration.schema_version: only version 1 is supported")
        planning_strategy = orchestration.get("planning_strategy", "deterministic")
        if not isinstance(planning_strategy, str) or planning_strategy not in {"deterministic", "model_directed"}:
            raise ValueError("orchestration.planning_strategy: expected deterministic or model_directed")
        mode_value = orchestration.get("mode", "single")
        if not isinstance(mode_value, str) or mode_value not in {"off", "single", "self_directed", "required"}:
            raise ValueError("orchestration.mode: expected off, single, self_directed, or required")
        if mode_value == "required" and planning_strategy != "model_directed":
            raise ValueError("orchestration.mode=required requires planning_strategy=model_directed")
        planner_retries = orchestration.get("planner_retries", 1)
        if not isinstance(planner_retries, int) or isinstance(planner_retries, bool) or not 0 <= planner_retries <= 2:
            raise ValueError("orchestration.planner_retries: expected integer from 0 to 2")
        planner_json_mode = orchestration.get("planner_json_mode", True)
        if not isinstance(planner_json_mode, bool):
            raise TypeError("orchestration.planner_json_mode: expected a boolean")
        for name in ("chat_entry_profile", "planner_profile"):
            value = orchestration.get(name)
            if value is not None and (not isinstance(value, str) or value not in default_agents):
                raise ValueError(f"orchestration.{name}: unknown profile {value!r}")
        if mode_value == "required":
            entry = orchestration.get("chat_entry_profile")
            if entry is None:
                raise ValueError("orchestration.chat_entry_profile: required when mode=required")
            if not default_agents[entry].delegation.get("enabled", False):
                raise ValueError("orchestration.mode=required needs delegation.enabled=true on the chat entry profile")
        for name, default, low, high in (("max_depth", 2, 0, 16), ("max_total_agents", 8, 1, 100), ("max_parallel_agents", 4, 1, 32), ("max_plan_tasks", 12, 0, 100), ("max_total_tool_calls", 40, 0, 1000), ("timeout_seconds", 300, 1, 86400), ("result_bytes_per_task", 16000, 256, 1_000_000)):
            value = orchestration.get(name, default)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"orchestration.{name}: expected integer from {low} to {high}")
        failure_policy = orchestration.get("failure_policy", "continue_independent")
        if not isinstance(failure_policy, str) or failure_policy not in {"fail_fast", "continue_independent"}:
            raise ValueError("orchestration.failure_policy: expected fail_fast or continue_independent")
        if orchestration.get("context_policy", "referenced_only") != "referenced_only":
            raise ValueError("orchestration.context_policy: only referenced_only is supported")
        card_fields = orchestration.get("profile_card_fields", ["description", "role", "tags", "capabilities"])
        if not isinstance(card_fields, list) or any(not isinstance(field_name, str) or field_name not in {"description", "role", "tags", "capabilities"} for field_name in card_fields) or len(set(card_fields)) != len(card_fields):
            raise ValueError("orchestration.profile_card_fields: expected unique description, role, tags, or capabilities fields")
        # Reject static target cycles. Runtime ancestry checks still guard dynamically nested runs.
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(profile: str) -> None:
            if profile in visiting:
                raise ValueError(f"agents.profiles.{profile}.delegation: target graph contains a cycle")
            if profile in visited:
                return
            visiting.add(profile)
            for target in default_agents[profile].delegation.get("allowed_targets", []):
                visit(target)
            visiting.remove(profile)
            visited.add(profile)
        for profile in default_agents:
            visit(profile)
        return cls(agents=default_agents, models=models, tools=tools, skills=skills, orchestration=orchestration)

    async def _execute_agent_turn(
        self,
        agent_name: str,
        messages: list[dict[str, Any]],
        spec: SwarmAgentSpec,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        max_tool_turns: int = 3,
        budget: RunBudget | None = None,
        run_context: OrchestrationRunContext | None = None,
    ) -> tuple[ModelResponse, list[dict[str, Any]]]:
        """Execute a subagent turn with autonomous tool execution loop."""
        graph_part, node_part = self._route_parts(spec)

        profile_tools = list(spec.tools)
        profile_tools.extend(name for name in self._orchestration_tools(spec) if name not in profile_tools)
        tools_schema = self.tools.to_openai_tools(profile_tools) if profile_tools else None
        tools_executed: list[dict[str, Any]] = []

        loop = asyncio.get_running_loop()
        turn_messages = list(messages)
        final_reply: ModelResponse | None = None
        profile_tool_scope = set(profile_tools)

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
                calls_to_execute = calls_to_execute[:10]
                turn_tool_records = []
                for call_dict in calls_to_execute:
                    t_name = call_dict.get("tool", "unknown")
                    t_args = call_dict.get("args", {})
                    try:
                        if budget is not None:
                            if budget.cancelled or time.monotonic() >= budget.deadline:
                                raise TimeoutError("orchestration run cancelled or timed out")
                            tool_limit = min(budget.max_total_tool_calls, run_context.tool_limit) if run_context and run_context.tool_limit is not None else budget.max_total_tool_calls
                            if budget.tool_calls >= tool_limit:
                                raise PermissionError("cumulative orchestration tool-call limit exceeded")
                            budget.tool_calls += 1
                        if not isinstance(t_args, dict):
                            raise TypeError("tool arguments must be an object")
                        encoded_args = json.dumps(t_args, ensure_ascii=False, separators=(",", ":"))
                        if len(encoded_args.encode("utf-8")) > 16_000:
                            raise ValueError("tool arguments exceed the 16 KB limit")
                        if t_name in {"agent.delegate", "agent.plan"}:
                            if t_name not in self._orchestration_tools(spec):
                                raise PermissionError(f"orchestration operation {t_name!r} is not configured and globally allowed")
                        elif t_name not in spec.tools:
                            raise PermissionError(f"tool {t_name!r} is outside profile scope")
                        if t_name not in profile_tool_scope:
                            raise PermissionError(f"tool {t_name!r} is outside profile scope")
                        if t_name == "agent.plan":
                            if run_context is None:
                                raise PermissionError("nested planning requires an active orchestration run")
                            nested_context = t_args.get("context", {})
                            if not isinstance(nested_context, dict) or len(json.dumps(nested_context, ensure_ascii=False, default=str).encode("utf-8")) > 8000:
                                raise ValueError("plan context must be a bounded object")
                            await self._suspend_run_slot(run_context)
                            try:
                                result = await self.plan(agent_name, str(t_args.get("prompt", "")), nested_context, run_context=run_context)
                            finally:
                                await self._resume_run_slot(run_context)
                            t_record = {"tool": t_name, "args": "[bounded]", "result": json.dumps(result, ensure_ascii=False, default=str)[:8000], "ok": bool(result.get("ok"))}
                            tools_executed.append(t_record)
                            turn_tool_records.append(t_record)
                            continue
                        if t_name == "agent.delegate":
                            if run_context is None:
                                raise PermissionError("nested delegation requires an active orchestration run")
                            target = t_args.get("agent")
                            prompt = t_args.get("prompt")
                            child_context_data = t_args.get("context", {})
                            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
                                raise ValueError("delegation prompt must be non-empty text up to 8000 characters")
                            if not isinstance(child_context_data, dict) or len(json.dumps(child_context_data, ensure_ascii=False, default=str).encode("utf-8")) > 8000:
                                raise ValueError("delegation context must be a bounded object")
                            if target not in {card["id"] for card in self.eligible_profiles(agent_name)}:
                                raise PermissionError("delegation target is not eligible")
                            if run_context.depth >= min(self.orchestration.get("max_depth", 2), spec.delegation.get("max_depth", self.orchestration.get("max_depth", 2))):
                                raise PermissionError("hierarchy depth limit exceeded")
                            if run_context.budget.agents_started >= run_context.budget.max_total_agents:
                                raise PermissionError("cumulative total agent limit exceeded")
                            max_children = spec.delegation.get("max_children", self.orchestration.get("max_plan_tasks", 12))
                            if run_context.child_tasks_started >= max_children:
                                raise PermissionError("profile cumulative child limit exceeded")
                            run_context.budget.agents_started += 1
                            run_context.child_tasks_started += 1
                            target_policy = self.agents[target].delegation.get("budget", {})
                            parent_deadline = min(run_context.budget.deadline, run_context.deadline or run_context.budget.deadline)
                            child_deadline = min(parent_deadline, time.monotonic() + target_policy.get("timeout_seconds", parent_deadline - time.monotonic()))
                            parent_tool_limit = run_context.tool_limit if run_context.tool_limit is not None else run_context.budget.max_total_tool_calls
                            child_tool_limit = min(parent_tool_limit, run_context.budget.tool_calls + target_policy.get("max_tool_calls", parent_tool_limit))
                            child_context = OrchestrationRunContext(run_context.budget, target, run_context.depth + 1, f"task_{uuid4().hex[:12]}", run_context.ancestry + (agent_name,), run_context.principal_context, child_deadline, child_tool_limit)
                            await self._suspend_run_slot(run_context)
                            try:
                                global_limit = self.orchestration.get("max_parallel_agents", 4)
                                if run_context.budget.global_semaphore is None:
                                    run_context.budget.global_semaphore = asyncio.Semaphore(global_limit)
                                profile_limit = min(global_limit, spec.delegation.get("max_parallel_children", global_limit))
                                run_context.budget.profile_semaphores.setdefault(agent_name, asyncio.Semaphore(profile_limit))
                                async with run_context.budget.global_semaphore, run_context.budget.profile_semaphores[agent_name]:
                                    child_context.slot_held = True
                                    child_context.slot_profile = agent_name
                                    result = await asyncio.wait_for(
                                        self.run_agent(target, prompt, child_context_data, budget=run_context.budget, run_context=child_context, parent_task_id=child_context.parent_task_id, requester=agent_name),
                                        timeout=max(0.001, child_deadline - time.monotonic()),
                                    )
                                child_context.slot_held = False
                            finally:
                                await self._resume_run_slot(run_context)
                            t_record = {"tool": t_name, "args": "[bounded]", "result": json.dumps(result, ensure_ascii=False, default=str)[:8000], "ok": bool(result.get("ok"))}
                            tools_executed.append(t_record)
                            turn_tool_records.append(t_record)
                            continue
                        tool_meta = self.tools.catalog().get(t_name, {})
                        from harness.governance import current_tenant
                        principal = current_tenant()
                        if (tool_meta.get("requires_approval") or tool_meta.get("mutates_state")) and (principal is None or not principal.auto_approve_tools):
                            raise PermissionError(f"tool {t_name!r} requires principal approval and cannot run inside delegation")
                        res = self.tools.invoke(t_name, **t_args)
                        tool_ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
                        t_record = {"tool": t_name, "args": str(t_args)[:2000], "result": str(res)[:4000], "ok": tool_ok}
                    except Exception as exc:  # noqa: BLE001
                        t_record = {"tool": t_name, "args": str(t_args)[:2000], "error": str(exc)[:1000], "ok": False}

                    tools_executed.append(t_record)
                    turn_tool_records.append(t_record)
                    if len(tools_executed) >= 30:
                        break
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

    async def run_agent(self, agent_name: str, task: str, context: dict[str, Any] | None = None, *, budget: RunBudget | None = None, run_context: OrchestrationRunContext | None = None, parent_task_id: str | None = None, requester: str | None = None) -> dict[str, Any]:
        """Run exactly one registered specialist profile with its configured limits."""
        from harness.events import current_event_run_id, global_event_bus
        run_id = current_event_run_id() or f"delegate_{agent_name}"
        if agent_name not in self.agents:
            global_event_bus.publish("subagent_failed", run_id, {"agent": agent_name, "status": "failed", "error": "unknown agent profile"})
            return {"ok": False, "agent": agent_name, "status": "failed", "error": "unknown agent profile"}
        if requester and self.mode in {"self_directed", "required"} and agent_name not in {card["id"] for card in self.eligible_profiles(requester)}:
            global_event_bus.publish("subagent_failed", run_id, {"agent": agent_name, "status": "failed", "error": "target is not eligible", "parent_task_id": parent_task_id})
            return {"ok": False, "agent": agent_name, "status": "failed", "error": "target is not eligible"}
        if run_context is None:
            from harness.governance import current_tenant
            budget = budget or RunBudget(run_id, time.monotonic() + self.orchestration.get("timeout_seconds", 300), self.orchestration.get("max_total_agents", 8), self.orchestration.get("max_total_tool_calls", 40), agents_started=1)
            policy_budget = self.agents[agent_name].delegation.get("budget", {})
            deadline = min(budget.deadline, time.monotonic() + policy_budget.get("timeout_seconds", self.orchestration.get("timeout_seconds", 300)))
            tool_limit = min(budget.max_total_tool_calls, budget.tool_calls + policy_budget.get("max_tool_calls", budget.max_total_tool_calls))
            run_context = OrchestrationRunContext(budget, agent_name, 1 if requester else 0, parent_task_id, ((requester,) if requester else ()) + (agent_name,), current_tenant(), deadline, tool_limit)
        budget = run_context.budget
        run_id = budget.run_id
        global_event_bus.publish("subagent_started", run_id, {"agent": agent_name, "status": "started", "parent_task_id": parent_task_id})
        spec = self.agents[agent_name]
        prompt = spec.system_prompt
        skill_budget = 32_000
        for name in spec.skills:
            skill = self.skills.get_skill(name) if self.skills else None
            if skill is None:
                result = {"ok": False, "agent": agent_name, "status": "failed", "error": f"configured skill unavailable: {name}"}
                global_event_bus.publish("subagent_failed", run_id, {"agent": agent_name, "status": "failed", "error": result["error"]})
                return result
            section = f"\n\nReference skill {name}:\n{skill.instructions[:12_000]}"
            section_size = len(section.encode("utf-8"))
            if section_size > skill_budget:
                error = "selected skill instructions exceed the 32 KB profile context limit"
                global_event_bus.publish("subagent_failed", run_id, {"agent": agent_name, "status": "failed", "error": error})
                return {"ok": False, "agent": agent_name, "status": "failed", "error": error}
            prompt += section
            skill_budget -= section_size
        content = task[:8000]
        if context:
            content += "\n\nContext:\n" + str(context)[:8000]
        try:
            response, records = await self._execute_agent_turn(agent_name, [{"role": "system", "content": prompt}, {"role": "user", "content": content}], spec, max_tool_turns=spec.max_tool_turns, budget=budget, run_context=run_context)
            failures = [record for record in records if not record.get("ok")]
            result = {"ok": not failures, "agent": agent_name, "status": "failed" if failures else "completed", "output": str(response)[:12000], "tools_executed": records}
            if failures:
                result["error"] = "one or more specialist tool calls failed"
            global_event_bus.publish("subagent_failed" if failures else "subagent_completed", run_id, {"agent": agent_name, "status": result["status"], "tools_count": len(records)})
            return result
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "agent": agent_name, "status": "failed", "error": str(exc)[:1000], "tools_executed": []}
            global_event_bus.publish("subagent_failed", run_id, {"agent": agent_name, "status": "failed", "error": result["error"]})
            return result

    def eligible_profiles(self, profile_name: str) -> list[dict[str, Any]]:
        """Return bounded descriptive cards for profiles this profile may delegate to."""
        spec = self.agents.get(profile_name)
        if spec is None:
            return []
        policy = spec.delegation
        host_deterministic = self.planning_strategy == "deterministic" and self.mode in {"single", "self_directed"}
        if not policy.get("enabled", False) or (self.mode not in {"self_directed", "required"} and not host_deterministic):
            return []
        ids = set(policy.get("allowed_targets", []))
        tags = set(policy.get("allowed_target_tags", []))
        if not ids and not tags:
            return []
        cards = []
        for name, candidate in self.agents.items():
            if name == profile_name:
                continue
            if ids and name not in ids:
                continue
            if tags and not tags.intersection(candidate.tags):
                continue
            card = {"id": name}
            if "description" in self.orchestration.get("profile_card_fields", ["description", "role", "tags", "capabilities"]):
                card["description"] = candidate.description[:300]
            if "role" in self.orchestration.get("profile_card_fields", ["description", "role", "tags", "capabilities"]):
                card["role"] = candidate.role[:100]
            if "tags" in self.orchestration.get("profile_card_fields", ["description", "role", "tags", "capabilities"]):
                card["tags"] = candidate.tags[:20]
            if "capabilities" in self.orchestration.get("profile_card_fields", ["description", "role", "tags", "capabilities"]):
                card["capabilities"] = candidate.tools[:50]
            cards.append(card)
        return cards

    def _orchestration_tools(self, spec: SwarmAgentSpec) -> list[str]:
        if not spec.delegation.get("enabled", False) or self.mode not in {"self_directed", "required"}:
            return []
        registered = set(self.tools.names())
        allowlist = set(self.tools.allowlist)
        available = ["agent.delegate"]
        if self.planning_strategy == "model_directed":
            available.append("agent.plan")
        return [name for name in available if name in registered and (not allowlist or name in allowlist)]

    async def _suspend_run_slot(self, run_context: OrchestrationRunContext | None) -> None:
        if run_context is None or not run_context.slot_held:
            return
        budget = run_context.budget
        if budget.global_semaphore is not None:
            budget.global_semaphore.release()
        profile_sem = budget.profile_semaphores.get(run_context.slot_profile or "")
        if profile_sem is not None:
            profile_sem.release()
        run_context.slot_held = False

    async def _resume_run_slot(self, run_context: OrchestrationRunContext | None) -> None:
        if run_context is None or run_context.slot_held:
            return
        budget = run_context.budget
        if budget.global_semaphore is not None:
            await budget.global_semaphore.acquire()
        profile_sem = budget.profile_semaphores.get(run_context.slot_profile or "")
        if profile_sem is not None:
            await profile_sem.acquire()
        run_context.slot_held = True

    def validate_plan(
        self,
        proposal: dict[str, Any],
        run_context: OrchestrationRunContext,
        context_refs: dict[str, Any] | None = None,
    ) -> TaskPlan:
        """Validate the entire model proposal before starting any child work."""
        if not isinstance(proposal, dict) or set(proposal) != {"schema_version", "goal", "tasks"}:
            raise ValueError("plan: expected schema_version, goal, and tasks only")
        if not isinstance(proposal["schema_version"], int) or isinstance(proposal["schema_version"], bool) or proposal["schema_version"] != 1:
            raise ValueError("plan.schema_version: unsupported version")
        goal, tasks = proposal["goal"], proposal["tasks"]
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError("plan.goal: expected non-empty text up to 4000 characters")
        max_tasks = self.orchestration.get("max_plan_tasks", 12)
        if not isinstance(tasks, list) or len(tasks) > max_tasks:
            raise ValueError("plan.tasks: malformed or over configured task limit")
        if context_refs is not None:
            if not isinstance(context_refs, dict) or len(context_refs) > 50:
                raise ValueError("plan context: expected at most 50 named references")
            if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", key) for key in context_refs):
                raise ValueError("plan context: reference names must be bounded identifiers")
            try:
                context_size = len(json.dumps(context_refs, ensure_ascii=False, default=str).encode("utf-8"))
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError("plan context: values must be serializable") from exc
            if context_size > 64_000:
                raise ValueError("plan context: exceeds the 64 KB limit")
        parent = self.agents.get(run_context.current_profile)
        if parent is None:
            raise ValueError("plan: unknown parent profile")
        delegation = parent.delegation
        host_deterministic = self.planning_strategy == "deterministic" and self.mode in {"single", "self_directed"}
        if tasks and ((self.mode not in {"self_directed", "required"} and not host_deterministic) or not delegation.get("enabled", False)):
            raise PermissionError("plan: profile delegation is not enabled")
        if run_context.depth >= min(self.orchestration.get("max_depth", 2), delegation.get("max_depth", self.orchestration.get("max_depth", 2))):
            raise ValueError("plan: hierarchy depth limit exceeded")
        if run_context.child_tasks_started + len(tasks) > delegation.get("max_children", self.orchestration.get("max_plan_tasks", 12)):
            raise ValueError("plan: profile child limit exceeded")
        allowed = {card["id"] for card in self.eligible_profiles(parent.name)}
        ids: set[str] = set()
        parsed: list[dict[str, Any]] = []
        for index, raw in enumerate(tasks):
            path = f"plan.tasks[{index}]"
            if not isinstance(raw, dict) or set(raw) - {"id", "profile", "task", "depends_on", "inputs", "expected_output_schema", "rationale"}:
                raise ValueError(f"{path}: invalid task fields")
            task_id, target, text = raw.get("id"), raw.get("profile"), raw.get("task")
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", task_id) or task_id in ids:
                raise ValueError(f"{path}.id: invalid or duplicate task ID")
            if not isinstance(target, str) or target not in allowed:
                raise PermissionError(f"{path}.profile: target is not eligible")
            if target in run_context.ancestry or target == run_context.current_profile:
                raise PermissionError(f"{path}.profile: target would create a runtime ancestry cycle")
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise ValueError(f"{path}.task: expected non-empty bounded string")
            deps, inputs = raw.get("depends_on", []), raw.get("inputs", [])
            if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
                raise ValueError(f"{path}.depends_on: expected unique task IDs")
            if not isinstance(inputs, list) or any(not isinstance(ref, str) for ref in inputs) or len(inputs) > 20:
                raise ValueError(f"{path}.inputs: expected bounded reference list")
            output_schema = raw.get("expected_output_schema")
            if output_schema is not None and (not isinstance(output_schema, dict) or len(json.dumps(output_schema, ensure_ascii=False).encode("utf-8")) > 4000 or "$ref" in output_schema or output_schema.get("type") not in {"object", "array", "string", "number", "integer", "boolean"}):
                raise ValueError(f"{path}.expected_output_schema: unsupported or oversized schema")
            rationale = raw.get("rationale", "")
            if not isinstance(rationale, str) or len(rationale) > 1000:
                raise ValueError(f"{path}.rationale: expected text up to 1000 characters")
            ids.add(task_id)
            parsed.append({"id": task_id, "profile": target, "task": text, "depends_on": deps, "inputs": inputs, "expected_output_schema": output_schema, "rationale": rationale})
        by_id = {task["id"]: task for task in parsed}
        roots = context_refs or {}
        for task in parsed:
            for dep in task["depends_on"]:
                if dep not in by_id or dep == task["id"]:
                    raise ValueError(f"plan task {task['id']}: unknown or self dependency {dep!r}")
            for ref in task["inputs"]:
                if ref.endswith(".result"):
                    source = ref[:-7]
                    if source not in task["depends_on"]:
                        raise ValueError(f"plan task {task['id']}: result reference must name a dependency")
                elif not ref.startswith("context.") or ref[8:] not in roots:
                    raise ValueError(f"plan task {task['id']}: unauthorized context reference {ref!r}")
        # Kahn's algorithm rejects cycles before any work starts.
        pending = {task["id"]: set(task["depends_on"]) for task in parsed}
        ready = [key for key, deps in pending.items() if not deps]
        visited: set[str] = set()
        while ready:
            done = ready.pop()
            if done in visited:
                continue
            visited.add(done)
            for key, deps in pending.items():
                if done in deps:
                    deps.remove(done)
                    if not deps:
                        ready.append(key)
        if len(visited) != len(parsed):
            raise ValueError("plan.tasks: dependency cycle detected")
        if run_context.budget.cancelled or time.monotonic() >= run_context.budget.deadline:
            raise TimeoutError("plan: run cancelled or timed out")
        if run_context.budget.agents_started + run_context.budget.reserved_agents + len(parsed) > run_context.budget.max_total_agents:
            raise ValueError("plan: cumulative total agent limit exceeded")
        return TaskPlan(1, goal, tuple(parsed))

    async def plan(self, profile_name: str, task: str, context_refs: dict[str, Any] | None = None, *, run_id: str | None = None, run_context: OrchestrationRunContext | None = None) -> dict[str, Any]:
        """Ask a configured, tool-free planner to propose a typed plan."""
        if self.planning_strategy != "model_directed":
            return {"ok": False, "status": "unavailable", "error": "model-directed planning is disabled; use a caller-validated deterministic TaskPlan"}
        planner_name = self.planner_profile or profile_name
        if planner_name not in self.agents:
            raise ValueError("orchestration planner profile is not configured")
        cards = self.eligible_profiles(profile_name)
        if not cards and self.mode not in {"required"}:
            return {"ok": False, "status": "rejected", "error": "profile has no eligible delegation targets"}
        spec = self.agents[planner_name]
        planner_instructions = spec.system_prompt[:8000]
        planner_skill_bytes = 12_000
        for skill_name in spec.skills:
            skill = self.skills.get_skill(skill_name) if self.skills else None
            if skill is None:
                return {"ok": False, "status": "rejected", "error": f"configured planner skill unavailable: {skill_name}"}
            section = f"\n\nReference skill {skill_name} (reference content):\n{skill.instructions[:12_000]}"
            section_bytes = len(section.encode("utf-8"))
            if section_bytes > planner_skill_bytes:
                return {"ok": False, "status": "rejected", "error": "planner skill context exceeds the configured bound"}
            planner_instructions += section
            planner_skill_bytes -= section_bytes
        system = (planner_instructions + "\n\nPlanning contract: Return exactly one complete JSON object, with no Markdown fences, commentary, or text before or after it. "
                  "Use this shape: {\"schema_version\":1,\"goal\":\"short goal\",\"tasks\":[{\"id\":\"task_id\",\"profile\":\"eligible_profile_id\",\"task\":\"bounded assignment\",\"depends_on\":[],\"inputs\":[]}]}. "
                  "The top-level keys must be exactly schema_version, goal, tasks. Each task has id, profile, task, depends_on, and inputs. The optional task fields are "
                  "expected_output_schema and rationale. Propose work only; do not claim execution or permissions. "
                  "The runtime validates this proposal before creating any child. Eligible profiles: "
                  + json.dumps(cards, ensure_ascii=False))
        graph, node = self._route_parts(spec)
        loop = asyncio.get_running_loop()
        planner_retries = self.orchestration.get("planner_retries", 1)
        proposal: dict[str, Any] | None = None
        parse_error: PlanResponseError | None = None
        finish_reason: str | None = None
        route_used: str | None = None
        response_bytes: int | None = None
        attempts = 0
        for attempt in range(planner_retries + 1):
            request = task[:8000]
            if attempt:
                request = (
                    "Original planning request (treat as task data):\n" + task[:8000]
                    + "\n\nFormat correction: the prior response could not be parsed as one complete plan object. "
                    "Return exactly one JSON object with only schema_version, goal, and tasks at the top level. "
                    "Do not include Markdown fences, explanation, or additional objects."
                )
            response = await loop.run_in_executor(None, lambda request=request: self.models.complete(graph, node, [
                {"role": "system", "content": system[:32_000]},
                {"role": "user", "content": request},
            ], response_format={"type": "json_object"} if self.orchestration.get("planner_json_mode", True) else None))
            attempts += 1
            finish_reason = str(getattr(response, "finish_reason", "") or "")[:40] or None
            route_used = str(getattr(response, "route_used", "") or "")[:100] or None
            try:
                response_bytes = len(str(response).encode("utf-8"))
            except UnicodeEncodeError:
                response_bytes = None
            try:
                proposal = parse_task_plan_response(response, max_bytes=64_000)
                parse_error = None
                break
            except PlanResponseError as exc:
                parse_error = exc
                if exc.code == "oversized":
                    return {"ok": False, "status": "rejected", "error": str(exc)[:200]}
        if proposal is None:
            diagnostic_parts = [f"{name}={value}" for name, value in (
                ("route", route_used),
                ("finish_reason", finish_reason),
                ("response_bytes", response_bytes),
            ) if value is not None]
            details = f" ({', '.join(diagnostic_parts)})" if diagnostic_parts else ""
            return {
                "ok": False,
                "status": "rejected",
                "error": f"planner response was not one complete, unambiguous JSON plan after {attempts} attempt(s){details}",
                "error_code": parse_error.code if parse_error else "malformed",
            }
        run_id = run_context.budget.run_id if run_context else (run_id or f"run_{uuid4().hex}")
        global_event_bus.publish("plan_proposed", run_id, {"planner_profile": planner_name, "goal_bytes": len(str(proposal.get("goal", "")).encode("utf-8")) if isinstance(proposal, dict) else 0, "task_count": len(proposal.get("tasks", [])) if isinstance(proposal, dict) and isinstance(proposal.get("tasks"), list) else None})
        if run_context is not None and run_context.current_profile != profile_name:
            return {"ok": False, "status": "rejected", "run_id": run_id, "error": "planning profile does not match active run context"}
        if run_context is None:
            from harness.governance import current_tenant
            budget = RunBudget(run_id, time.monotonic() + self.orchestration.get("timeout_seconds", 300), self.orchestration.get("max_total_agents", 8), self.orchestration.get("max_total_tool_calls", 40))
            policy_budget = self.agents[profile_name].delegation.get("budget", {})
            deadline = min(budget.deadline, time.monotonic() + policy_budget.get("timeout_seconds", self.orchestration.get("timeout_seconds", 300)))
            tool_limit = min(budget.max_total_tool_calls, budget.tool_calls + policy_budget.get("max_tool_calls", budget.max_total_tool_calls))
            ctx = OrchestrationRunContext(budget, profile_name, principal_context=current_tenant(), deadline=deadline, tool_limit=tool_limit)
        else:
            ctx = run_context
        try:
            validated = self.validate_plan(proposal, ctx, context_refs)
        except (ValueError, PermissionError, TimeoutError) as exc:
            return {"ok": False, "status": "rejected", "run_id": run_id, "error": str(exc)[:500]}
        return await self.execute_plan(validated, ctx, context_refs or {})

    async def run_configured_plan(self, profile_name: str, proposal: dict[str, Any], context_refs: dict[str, Any] | None = None, *, run_id: str | None = None) -> dict[str, Any]:
        """Validate and execute a host-supplied plan without asking a model to decompose it."""
        if profile_name not in self.agents:
            return {"ok": False, "status": "rejected", "error": "unknown orchestration entry profile"}
        from harness.governance import current_tenant
        run_id = run_id or f"run_{uuid4().hex}"
        budget = RunBudget(
            run_id,
            time.monotonic() + self.orchestration.get("timeout_seconds", 300),
            self.orchestration.get("max_total_agents", 8),
            self.orchestration.get("max_total_tool_calls", 40),
            agents_started=1,
        )
        profile_budget = self.agents[profile_name].delegation.get("budget", {})
        deadline = min(budget.deadline, time.monotonic() + profile_budget.get("timeout_seconds", self.orchestration.get("timeout_seconds", 300)))
        context = OrchestrationRunContext(
            budget,
            profile_name,
            ancestry=(profile_name,),
            principal_context=current_tenant(),
            deadline=deadline,
            tool_limit=min(budget.max_total_tool_calls, profile_budget.get("max_tool_calls", budget.max_total_tool_calls)),
        )
        try:
            validated = self.validate_plan(proposal, context, context_refs)
        except (TypeError, ValueError, PermissionError, TimeoutError) as exc:
            return {"ok": False, "status": "rejected", "run_id": run_id, "error": str(exc)[:500]}
        return await self.execute_plan(validated, context, context_refs)

    async def execute_plan(self, plan: TaskPlan, run_context: OrchestrationRunContext, context_refs: dict[str, Any] | None = None, cancellation: Any = None) -> dict[str, Any]:
        """Execute a previously validated DAG with bounded concurrency and truthful statuses."""
        refs = context_refs or {}
        events = global_event_bus
        run_id = run_context.budget.run_id
        results: dict[str, dict[str, Any]] = {}
        try:
            plan = self.validate_plan({"schema_version": plan.schema_version, "goal": plan.goal, "tasks": list(plan.tasks)}, run_context, refs)
        except (TypeError, ValueError, PermissionError, TimeoutError) as exc:
            return {"ok": False, "status": "rejected", "run_id": run_id, "error": str(exc)[:500]}
        runtime_ids = {task["id"]: f"task_{uuid4().hex[:16]}" for task in plan.tasks}
        if run_context.budget.agents_started + run_context.budget.reserved_agents + len(plan.tasks) > run_context.budget.max_total_agents:
            return {"ok": False, "status": "rejected", "run_id": run_id, "error": "cumulative total agent limit exceeded"}
        # Reserve the complete validated plan before any await, so parallel nested
        # submissions cannot all pass validation against the same free slots.
        run_context.budget.reserved_agents += len(plan.tasks)
        run_context.child_tasks_started += len(plan.tasks)
        global_limit = self.orchestration.get("max_parallel_agents", 4)
        if run_context.budget.global_semaphore is None:
            run_context.budget.global_semaphore = asyncio.Semaphore(global_limit)
        for profile_name, profile_spec in self.agents.items():
            profile_limit = min(global_limit, profile_spec.delegation.get("max_parallel_children", global_limit))
            run_context.budget.profile_semaphores.setdefault(profile_name, asyncio.Semaphore(profile_limit))
        events.publish("plan_validated", run_id, {"task_count": len(plan.tasks), "parent_task_id": run_context.parent_task_id})
        pending = {task["id"]: task for task in plan.tasks}
        fail_fast = self.orchestration.get("failure_policy", "continue_independent") == "fail_fast"
        while pending:
            deadline = min(run_context.budget.deadline, run_context.deadline) if run_context.deadline else run_context.budget.deadline
            if run_context.budget.cancelled or (cancellation is not None and cancellation.is_set()) or time.monotonic() >= deadline:
                run_context.budget.cancelled = True
                events.publish("plan_cancelled", run_id, {"parent_task_id": run_context.parent_task_id})
                for task_id in pending:
                    results[task_id] = {"status": "cancelled", "ok": False}
                    events.publish("task_failed", run_id, {"task_id": runtime_ids[task_id], "status": "cancelled", "parent_task_id": run_context.parent_task_id})
                    run_context.budget.reserved_agents = max(0, run_context.budget.reserved_agents - 1)
                pending.clear()
                break
            ready = [t for t in pending.values() if all(dep in results for dep in t["depends_on"])]
            if not ready:
                break
            if fail_fast and any(r["status"] != "succeeded" for r in results.values()):
                for task in pending.values():
                    results[task["id"]] = {"status": "blocked", "ok": False}
                    events.publish("task_failed", run_id, {"task_id": runtime_ids[task["id"]], "status": "blocked", "parent_task_id": run_context.parent_task_id})
                    run_context.budget.reserved_agents = max(0, run_context.budget.reserved_agents - 1)
                pending.clear()
                break
            executable = []
            for task in ready:
                events.publish("task_queued", run_id, {"task_id": runtime_ids[task["id"]], "parent_task_id": run_context.parent_task_id})
                failed_deps = [dep for dep in task["depends_on"] if results[dep]["status"] != "succeeded"]
                if failed_deps:
                    results[task["id"]] = {"status": "blocked", "ok": False, "error": "dependency failed"}
                    events.publish("task_failed", run_id, {"task_id": runtime_ids[task["id"]], "status": "blocked", "parent_task_id": run_context.parent_task_id})
                    run_context.budget.reserved_agents = max(0, run_context.budget.reserved_agents - 1)
                    pending.pop(task["id"])
                else:
                    executable.append(task)
            async def execute_one(task: dict[str, Any], wave_deadline: float = deadline) -> tuple[str, dict[str, Any]]:
                async with run_context.budget.global_semaphore, run_context.budget.profile_semaphores[run_context.current_profile]:
                        task_id = task["id"]
                        runtime_task_id = runtime_ids[task_id]
                        events.publish("task_started", run_id, {"task_id": runtime_task_id, "profile": task["profile"], "parent_task_id": run_context.parent_task_id})
                        projected = {}
                        for ref in task["inputs"]:
                            if ref.endswith(".result"):
                                source = ref[:-7]
                                projected[ref] = results[source].get("output", "")[:self.orchestration.get("result_bytes_per_task", 16000)]
                            else:
                                projected[ref] = str(refs[ref[8:]])[:4000]
                        run_context.budget.reserved_agents = max(0, run_context.budget.reserved_agents - 1)
                        run_context.budget.agents_started += 1
                        try:
                            child_policy = self.agents[task["profile"]].delegation.get("budget", {})
                            child_deadline = min(wave_deadline, time.monotonic() + child_policy.get("timeout_seconds", wave_deadline - time.monotonic()))
                            inherited_limit = run_context.tool_limit if run_context.tool_limit is not None else run_context.budget.max_total_tool_calls
                            child_tool_limit = min(inherited_limit, run_context.budget.tool_calls + child_policy.get("max_tool_calls", inherited_limit))
                            child_context = OrchestrationRunContext(run_context.budget, task["profile"], run_context.depth + 1, runtime_task_id, run_context.ancestry + (run_context.current_profile,), run_context.principal_context, child_deadline, child_tool_limit, True, run_context.current_profile)
                            child = await asyncio.wait_for(self.run_agent(task["profile"], task["task"], projected, budget=run_context.budget, run_context=child_context, parent_task_id=runtime_task_id, requester=run_context.current_profile), timeout=max(0.001, child_deadline - time.monotonic()))
                            status = "succeeded" if child.get("ok") else "failed"
                        except TimeoutError:
                            child = {"ok": False, "error": "task timed out", "tools_executed": []}
                            status = "timed_out"
                        out = {"status": status, "ok": status == "succeeded", "profile": task["profile"], "output": str(child.get("output", child.get("error", "")))[:self.orchestration.get("result_bytes_per_task", 16000)], "tools_executed": child.get("tools_executed", [])}
                        etype = "task_completed" if status == "succeeded" else "task_failed"
                        events.publish("task_progress", run_id, {"task_id": runtime_task_id, "parent_task_id": run_context.parent_task_id, "tools_count": len(out["tools_executed"])})
                        events.publish(etype, run_id, {"task_id": runtime_task_id, "parent_task_id": run_context.parent_task_id, "status": status, "output_bytes": len(out["output"].encode("utf-8"))})
                        return task_id, out
            if executable:
                for task_id, result in await asyncio.gather(*(execute_one(task) for task in executable)):
                    results[task_id] = result
                    pending.pop(task_id, None)
        result = {"ok": all(value["ok"] for value in results.values()), "status": "completed" if all(value["ok"] for value in results.values()) else "partial", "run_id": run_id, "goal": plan.goal, "tasks": results}
        events.publish("orchestration_completed", run_id, {"status": result["status"], "task_count": len(results)})
        return result
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
                current_agent_name, messages, spec, on_event=on_event, max_tool_turns=spec.max_tool_turns
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
                raise KeyError(f"agent {name!r} not found in swarm")
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
                name, messages, spec, on_event=on_event, max_tool_turns=spec.max_tool_turns
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
                raise KeyError(f"agent {synthesizer_agent!r} not found in swarm")
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
            synth_graph, synth_node = self._route_parts(synth_spec)
            synth_reply = await loop.run_in_executor(
                None,
                lambda: self.models.complete(synth_graph, synth_node, synth_messages),
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

    def _route_parts(self, spec: SwarmAgentSpec) -> tuple[str, str]:
        """Resolve a configured route using the model registry's route naming rules."""
        parts = spec.route.split(".", 1)
        if len(parts) > 1:
            return parts[0], parts[1]
        if spec.route in getattr(self.models, "routes", {}):
            return spec.route, ""
        return "swarm", spec.name
