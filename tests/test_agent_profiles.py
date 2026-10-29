from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import ClassVar

import pytest

from harness.parser.task_plan_parser import PlanResponseError, parse_task_plan_response
from harness.registries.model_registry import ModelRegistry, ModelResponse, ToolCall
from harness.registries.tool_registry import ToolRegistry
from harness.swarm import OrchestrationRunContext, RunBudget, SwarmCoordinator, TaskPlan


def _config() -> dict:
    return {
        "models": {"default": {"provider": "fallback", "model": "deterministic-local"}},
        "tools": {"allowlist": ["time.now", "file.write_text"]},
        "agents": {"profiles": {"clock": {"route": "default", "system_prompt": "Answer carefully.", "tools": ["time.now"], "max_tool_turns": 2}}},
    }


def test_profile_is_loaded_with_scoped_tools_and_limits() -> None:
    config = _config()
    models, tools = ModelRegistry.from_config(config), ToolRegistry.from_config(config)
    coordinator = SwarmCoordinator.from_default_config(models=models, tools=tools, config=config)
    profile = coordinator.agents["clock"]
    assert profile.route == "default"
    assert profile.tools == ["time.now"]
    assert profile.max_tool_turns == 2


def test_planning_is_deterministic_by_default_and_model_planning_requires_opt_in() -> None:
    config = _config()
    config["orchestration"] = {"mode": "self_directed", "chat_entry_profile": "clock"}
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    assert coordinator.planning_strategy == "deterministic"
    assert "agent.plan" not in coordinator._orchestration_tools(coordinator.agents["clock"])
    result = asyncio.run(coordinator.plan("clock", "do not call a model"))
    assert result["status"] == "unavailable"


def test_required_plan_mode_rejects_deterministic_strategy_at_startup() -> None:
    config = _config()
    config["orchestration"] = {"mode": "required", "chat_entry_profile": "clock"}
    with pytest.raises(ValueError, match="requires planning_strategy=model_directed"):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)


def test_planner_retry_limit_is_validated() -> None:
    config = _config()
    config["orchestration"] = {"planner_retries": 3}
    with pytest.raises(ValueError, match="planner_retries"):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)


def test_planner_json_mode_must_be_boolean() -> None:
    config = _config()
    config["orchestration"] = {"planner_json_mode": "yes"}
    with pytest.raises(TypeError, match="planner_json_mode"):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)


@pytest.mark.parametrize("change,field", [
    (lambda p: p.update(route="missing"), "route"),
    (lambda p: p.update(tools=["file.read_text"]), "tools"),
    (lambda p: p.update(max_tool_turns=11), "max_tool_turns"),
])
def test_invalid_profile_field_fails_at_initialization(change, field: str) -> None:
    config = _config()
    change(config["agents"]["profiles"]["clock"])
    with pytest.raises(ValueError, match=field):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)


def test_profile_rejects_unknown_fields_and_unbounded_roster() -> None:
    config = _config()
    config["agents"]["profiles"]["clock"]["toolz"] = ["time.now"]
    with pytest.raises(ValueError, match="unknown fields"):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)

    config = _config()
    config["agents"]["profiles"] = {f"a{i}": {} for i in range(101)}
    with pytest.raises(ValueError, match="at most 100"):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)


def test_unknown_profile_does_not_mutate_coordinator() -> None:
    config = _config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    before = set(coordinator.agents)
    result = asyncio.run(coordinator.run_agent("not-configured", "do work"))
    assert result["status"] == "failed"
    assert set(coordinator.agents) == before


def test_delegate_without_dispatcher_fails_closed() -> None:
    registry = ToolRegistry()
    result = registry.invoke("agent.delegate", agent="unknown", prompt="anything")
    assert result["status"] == "unavailable"
    assert result["ok"] is False


def test_delegate_rejects_unbounded_prompt() -> None:
    registry = ToolRegistry()
    result = registry.invoke("agent.delegate", agent="clock", prompt="x" * 8001)
    assert result["status"] == "failed"


def test_profile_skill_instructions_are_injected_only_for_that_run() -> None:
    config = _config()
    config["agents"]["profiles"]["clock"]["skills"] = ["citations"]

    class Skills:
        def get_skill(self, name):
            return SimpleNamespace(instructions="Cite evidence precisely.") if name == "citations" else None

    class FakeModels:
        routes: ClassVar[dict[str, object]] = {"default": object()}

        def __init__(self):
            self.messages = None

        def complete(self, _graph, _node, messages, **_kwargs):
            self.messages = messages
            return ModelResponse("Summary complete.")

    models = ModelRegistry.from_config(config)
    coordinator = SwarmCoordinator.from_default_config(models=models, tools=ToolRegistry.from_config(config), config=config, skills=Skills())
    fake_models = FakeModels()
    coordinator.models = fake_models
    result = asyncio.run(coordinator.run_agent("clock", "summarize"))
    assert result["status"] == "completed", result
    assert "Cite evidence precisely." in fake_models.messages[0]["content"]


def test_provider_failure_is_reported_as_failed() -> None:
    config = _config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)

    class BrokenModels:
        routes: ClassVar[dict[str, object]] = {"default": object()}

        def complete(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    coordinator.models = BrokenModels()
    result = asyncio.run(coordinator.run_agent("clock", "summarize"))
    assert result["status"] == "failed"
    assert "provider unavailable" in result["error"]


def test_profile_cannot_run_approval_gated_mutation_directly() -> None:
    config = _config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    profile = coordinator.agents["clock"]
    profile.tools = ["file.write_text"]
    called = []

    class FakeModels:
        def complete(self, *_args, **_kwargs):
            return ModelResponse("", [ToolCall("call-1", "file.write_text", {"file": "x", "content": "y"})])

    coordinator.models = FakeModels()
    original = coordinator.tools.invoke
    coordinator.tools.invoke = lambda *args, **kwargs: called.append(args)  # type: ignore[method-assign]
    result = asyncio.run(coordinator.run_agent("clock", "write a file"))
    coordinator.tools.invoke = original  # type: ignore[method-assign]
    assert not called
    assert result["status"] == "failed"
    assert result["tools_executed"][0]["ok"] is False
    assert "requires principal approval" in result["tools_executed"][0]["error"]


def test_model_cannot_bypass_profile_tool_scope_by_naming_registered_tool() -> None:
    config = _config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    invoked = []

    class FakeModels:
        def complete(self, *_args, **_kwargs):
            return ModelResponse("", [ToolCall("call-1", "file.write_text", {"file": "x", "content": "y"})])

    coordinator.models = FakeModels()
    original = coordinator.tools.invoke
    coordinator.tools.invoke = lambda *args, **kwargs: invoked.append(args)  # type: ignore[method-assign]
    try:
        result = asyncio.run(coordinator.run_agent("clock", "try out-of-scope tool"))
    finally:
        coordinator.tools.invoke = original  # type: ignore[method-assign]
    assert invoked == []
    assert result["status"] == "failed"
    assert "outside profile scope" in result["tools_executed"][0]["error"]


def test_tool_call_arguments_are_bounded_before_invocation() -> None:
    config = _config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    coordinator.agents["clock"].tools = ["time.now"]
    called = []

    class FakeModels:
        def complete(self, *_args, **_kwargs):
            return ModelResponse("", [ToolCall("call-1", "time.now", {"timezone": "x" * 17_000})])

    coordinator.models = FakeModels()
    original = coordinator.tools.invoke
    coordinator.tools.invoke = lambda *args, **kwargs: called.append(args)  # type: ignore[method-assign]
    result = asyncio.run(coordinator.run_agent("clock", "check time"))
    coordinator.tools.invoke = original  # type: ignore[method-assign]
    assert not called
    assert result["status"] == "failed"
    assert "16 KB limit" in result["tools_executed"][0]["error"]


def test_synthesizer_uses_its_configured_route() -> None:
    config = _config()
    config["models"]["synth.route"] = {"provider": "fallback", "model": "deterministic-local"}
    config["agents"]["profiles"]["synth"] = {
        "route": "synth.route",
        "system_prompt": "Synthesize findings.",
        "tools": [],
    }
    models = ModelRegistry.from_config(config)
    coordinator = SwarmCoordinator.from_default_config(models=models, tools=ToolRegistry.from_config(config), config=config)

    class FakeModels:
        routes = models.routes

        def __init__(self):
            self.calls = []

        def complete(self, graph, node, *_args, **_kwargs):
            self.calls.append((graph, node))
            return ModelResponse("synthesized")

    fake_models = FakeModels()
    coordinator.models = fake_models
    result = asyncio.run(coordinator.run_fan_out(["clock"], "task", synthesizer_agent="synth"))
    assert result["synthesis"] == "synthesized"
    assert fake_models.calls[-1] == ("synth", "route")


def _topology_config() -> dict:
    config = _config()
    config["tools"]["allowlist"].append("agent.plan")
    config["orchestration"] = {
        "schema_version": 1, "mode": "self_directed", "planning_strategy": "model_directed", "chat_entry_profile": "entry-x",
        "max_depth": 2, "max_total_agents": 5, "max_parallel_agents": 2,
        "max_plan_tasks": 5, "max_total_tool_calls": 10, "timeout_seconds": 20,
    }
    config["agents"]["profiles"] = {
        "entry-x": {"route": "default", "system_prompt": "entry", "tools": [], "delegation": {"enabled": True, "allowed_targets": ["tag-a", "tag-b"]}},
        "tag-a": {"route": "default", "system_prompt": "a", "tools": [], "tags": ["a"]},
        "tag-b": {"route": "default", "system_prompt": "b", "tools": [], "tags": ["b"]},
    }
    return config


@pytest.mark.parametrize("wrapped", [
    '{"schema_version":1,"goal":"one","tasks":[]}',
    '```json\n{"schema_version":1,"goal":"one","tasks":[]}\n```',
    'Here is the requested plan:\n{"schema_version":1,"goal":"one","tasks":[]}\nThat is the plan.',
])
def test_plan_parser_accepts_plain_fenced_and_prose_wrapped_json(wrapped: str) -> None:
    assert parse_task_plan_response(wrapped) == {"schema_version": 1, "goal": "one", "tasks": []}


@pytest.mark.parametrize("response,code", [
    ('{"schema_version":1,"goal":"unfinished","tasks":[', "malformed"),
    ('{"schema_version":1,"goal":"one","tasks":[]} {"schema_version":1,"goal":"two","tasks":[]}', "ambiguous"),
    ('{"schema_version":1,"schema_version":1,"goal":"one","tasks":[]}', "malformed"),
])
def test_plan_parser_rejects_truncated_ambiguous_and_duplicate_key_responses(response: str, code: str) -> None:
    with pytest.raises(PlanResponseError) as exc:
        parse_task_plan_response(response)
    assert exc.value.code == code


def test_plan_parser_rejects_oversized_responses_before_parsing() -> None:
    with pytest.raises(PlanResponseError, match="64 KB") as exc:
        parse_task_plan_response("x" * 64_001)
    assert exc.value.code == "oversized"


def test_plan_parser_rejects_nested_valid_object_inside_malformed_envelope() -> None:
    nested_plan = '{"schema_version":1,"goal":"nested","tasks":[]}'
    response = '{"wrapper":' + nested_plan + ',"broken": }'
    with pytest.raises(PlanResponseError) as exc:
        parse_task_plan_response(response)
    assert exc.value.code == "malformed"


def test_plan_parser_rejects_repeated_duplicate_plan_objects() -> None:
    plan = '{"schema_version":1,"goal":"same","tasks":[]}'
    with pytest.raises(PlanResponseError) as exc:
        parse_task_plan_response(plan + "\n" + plan)
    assert exc.value.code == "ambiguous"


def test_shared_json_parser_enforces_utf8_bytes_bom_and_json_constants() -> None:
    from harness.parser.structured_json import StructuredJSONError, parse_json_object_response

    envelope = {"é": "café"}
    assert parse_json_object_response("\ufeff{\"é\":\"café\"}", required_keys={"é"}, max_bytes=20) == envelope
    with pytest.raises(StructuredJSONError) as exc:
        parse_json_object_response('{"é":"café"}', required_keys={"é"}, max_bytes=10)
    assert exc.value.code == "oversized"
    with pytest.raises(StructuredJSONError) as exc:
        parse_json_object_response('{"schema_version":NaN,"goal":"x","tasks":[]}', required_keys={"schema_version", "goal", "tasks"}, max_bytes=100)
    assert exc.value.code == "malformed"
    with pytest.raises(StructuredJSONError) as exc:
        parse_json_object_response("\ud800", required_keys={"key"}, max_bytes=100)
    assert exc.value.code == "malformed"


def test_validated_plan_executes_dependency_dag_with_arbitrary_profile_ids() -> None:
    config = _topology_config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    assert set(coordinator.agents) == {"entry-x", "tag-a", "tag-b"}
    assert set(coordinator.agents) == {"entry-x", "tag-a", "tag-b"}
    assert [card["id"] for card in coordinator.eligible_profiles("entry-x")] == ["tag-a", "tag-b"]
    plan = TaskPlan(1, "compose result", (
        {"id": "first", "profile": "tag-a", "task": "collect", "depends_on": [], "inputs": []},
        {"id": "second", "profile": "tag-b", "task": "review", "depends_on": ["first"], "inputs": ["first.result"]},
    ))
    order = []

    async def run_agent(profile, task, context, **_kwargs):
        order.append((profile, context.copy()))
        return {"ok": True, "output": f"result:{profile}", "tools_executed": []}

    coordinator.run_agent = run_agent
    budget = RunBudget("run-test", time.monotonic() + 20, 5, 10)
    result = asyncio.run(coordinator.execute_plan(plan, OrchestrationRunContext(budget, "entry-x")))
    assert result["status"] == "completed"
    assert [profile for profile, _ in order] == ["tag-a", "tag-b"]
    assert order[1][1] == {"first.result": "result:tag-a"}


def test_independent_plan_tasks_run_in_parallel() -> None:
    config = _topology_config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    plan = TaskPlan(1, "parallel", (
        {"id": "left", "profile": "tag-a", "task": "left", "depends_on": [], "inputs": []},
        {"id": "right", "profile": "tag-b", "task": "right", "depends_on": [], "inputs": []},
    ))
    both_started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def run_agent(profile, _task, _context, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        try:
            await asyncio.wait_for(release.wait(), timeout=1)
            return {"ok": True, "output": profile, "tools_executed": []}
        finally:
            active -= 1

    async def run():
        coordinator.run_agent = run_agent
        budget = RunBudget("parallel-run", time.monotonic() + 5, 5, 10)
        task = asyncio.create_task(coordinator.execute_plan(plan, OrchestrationRunContext(budget, "entry-x")))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        return await task

    result = asyncio.run(run())
    assert result["status"] == "completed"
    assert peak == 2


def test_one_task_timeout_does_not_cancel_independent_sibling() -> None:
    config = _topology_config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    plan = TaskPlan(1, "independent outcomes", (
        {"id": "slow", "profile": "tag-a", "task": "slow", "depends_on": [], "inputs": []},
        {"id": "fast", "profile": "tag-b", "task": "fast", "depends_on": [], "inputs": []},
    ))

    async def run_agent(profile, _task, _context, **_kwargs):
        if profile == "tag-a":
            raise TimeoutError("child-specific timeout")
        return {"ok": True, "output": "finished", "tools_executed": []}

    coordinator.run_agent = run_agent
    budget = RunBudget("timeout-run", time.monotonic() + 5, 5, 10)
    result = asyncio.run(coordinator.execute_plan(plan, OrchestrationRunContext(budget, "entry-x")))
    assert result["tasks"]["slow"]["status"] == "timed_out"
    assert result["tasks"]["fast"]["status"] == "succeeded"
    assert budget.cancelled is False


def test_invalid_plan_rejects_before_any_child_launch() -> None:
    config = _topology_config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    budget = RunBudget("run-invalid", time.monotonic() + 20, 5, 10)
    context = OrchestrationRunContext(budget, "entry-x")
    proposal = {"schema_version": 1, "goal": "invalid", "tasks": [
        {"id": "a", "profile": "tag-a", "task": "a", "depends_on": ["b"], "inputs": []},
        {"id": "b", "profile": "tag-b", "task": "b", "depends_on": ["a"], "inputs": []},
    ]}
    launches = []
    coordinator.run_agent = lambda *args, **kwargs: launches.append(args)
    with pytest.raises(ValueError, match="cycle"):
        coordinator.validate_plan(proposal, context)
    assert launches == []


def test_plan_context_references_are_bounded_and_validated_before_launch() -> None:
    config = _topology_config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    budget = RunBudget("context-plan", time.monotonic() + 20, 5, 10)
    context = OrchestrationRunContext(budget, "entry-x")
    proposal = {"schema_version": 1, "goal": "context", "tasks": []}
    with pytest.raises(ValueError, match="reference names"):
        coordinator.validate_plan(proposal, context, {"bad key": "value"})
    with pytest.raises(ValueError, match="64 KB"):
        coordinator.validate_plan(proposal, context, {"large": "x" * 65_000})


def test_scheduler_revalidates_untrusted_task_plan_objects() -> None:
    config = _topology_config()
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    budget = RunBudget("tampered-plan", time.monotonic() + 20, 5, 10)
    context = OrchestrationRunContext(budget, "entry-x")
    plan = TaskPlan(1, "tampered", (
        {"id": "escape", "profile": "reviewer-not-configured", "task": "run unauthorized", "depends_on": [], "inputs": []},
    ))
    launches = []
    coordinator.run_agent = lambda *args, **kwargs: launches.append(args)
    result = asyncio.run(coordinator.execute_plan(plan, context))
    assert result["status"] == "rejected"
    assert launches == []
    assert budget.agents_started == 0


def test_configured_target_cycle_is_rejected() -> None:
    config = _topology_config()
    config["agents"]["profiles"]["tag-a"]["delegation"] = {"enabled": True, "allowed_targets": ["tag-b"]}
    config["agents"]["profiles"]["tag-b"]["delegation"] = {"enabled": True, "allowed_targets": ["tag-a"]}
    with pytest.raises(ValueError, match="cycle"):
        SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)


def test_nested_plan_inherits_total_agent_budget_and_run_id() -> None:
    config = _topology_config()
    config["agents"]["profiles"]["tag-a"]["delegation"] = {"enabled": True, "max_depth": 2, "allowed_targets": ["tag-b"]}
    config["agents"]["profiles"]["entry-x"]["tools"] = []
    config["tools"]["allowlist"].append("agent.plan")
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)

    class NestedPlanner:
        routes: ClassVar[dict[str, object]] = {"default": object()}

        def __init__(self):
            self.plans = iter([
                ModelResponse('{"schema_version":1,"goal":"root","tasks":[{"id":"outer","profile":"tag-a","task":"work","depends_on":[],"inputs":[]}]}'),
                ModelResponse('{"schema_version":1,"goal":"nested","tasks":[{"id":"inner","profile":"tag-b","task":"nested work","depends_on":[],"inputs":[]}]}'),
            ])
            self.agent_calls = 0

        def complete(self, _graph, _node, _messages, **kwargs):
            tools = kwargs.get("tools")
            if tools and any(tool.get("function", {}).get("name") == "agent.plan" for tool in tools) and self.agent_calls == 0:
                self.agent_calls += 1
                return ModelResponse("", [ToolCall("plan-call", "agent.plan", {"prompt": "nested work", "context": {}})])
            if tools and any(tool.get("function", {}).get("name") == "agent.plan" for tool in tools):
                self.agent_calls += 1
                return ModelResponse("outer complete")
            try:
                return next(self.plans)
            except StopIteration:
                return ModelResponse("nested complete")

    fake = NestedPlanner()
    coordinator.models = fake
    result = asyncio.run(coordinator.plan("entry-x", "root task", run_id="run-inherited"))
    assert result["run_id"] == "run-inherited"
    assert result["status"] == "completed", result
    assert result["tasks"]["outer"]["status"] == "succeeded", result
    # One child plan call and both task profiles ran under the same cumulative budget.
    assert fake.agent_calls >= 2


def test_model_planner_retries_common_format_failure_then_executes_validated_plan() -> None:
    config = _topology_config()
    config["orchestration"]["planner_retries"] = 1
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)
    plan_json = '{"schema_version":1,"goal":"inspect","tasks":[{"id":"inspect","profile":"tag-a","task":"inspect safely","depends_on":[],"inputs":[]}]}'

    class FlakyFormatModel:
        routes: ClassVar[dict[str, object]] = {"default": object()}

        def __init__(self):
            self.calls = 0
            self.formats = []

        def complete(self, *_args, **kwargs):
            self.calls += 1
            self.formats.append(kwargs.get("response_format"))
            if self.calls == 1:
                return ModelResponse("Here is the plan in the requested format:")
            if self.calls == 2:
                return ModelResponse(f"```json\n{plan_json}\n```")
            return ModelResponse("child evidence")

    fake = FlakyFormatModel()
    coordinator.models = fake
    result = asyncio.run(coordinator.plan("entry-x", "inspect the repository"))
    assert result["status"] == "completed", result
    assert result["tasks"]["inspect"]["output"] == "child evidence"
    assert fake.calls == 3  # malformed response, one format retry, then the child agent
    assert fake.formats[:2] == [{"type": "json_object"}, {"type": "json_object"}]


def test_planner_native_json_mode_can_be_disabled_for_incompatible_providers() -> None:
    config = _topology_config()
    config["orchestration"].update(planner_retries=0, planner_json_mode=False)
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)

    class CapturePlannerRequest:
        routes: ClassVar[dict[str, object]] = {"default": object()}

        def __init__(self):
            self.response_formats = []

        def complete(self, *_args, **kwargs):
            self.response_formats.append(kwargs.get("response_format"))
            return ModelResponse("not a plan")

    fake = CapturePlannerRequest()
    coordinator.models = fake
    result = asyncio.run(coordinator.plan("entry-x", "inspect"))
    assert result["status"] == "rejected"
    assert fake.response_formats == [None]


@pytest.mark.parametrize("bad_response", [
    "not json",
    '{"schema_version":1,"goal":"cut off","tasks":[',
    '{"schema_version":1,"goal":"one","tasks":[]} {"schema_version":1,"goal":"two","tasks":[]}',
])
def test_planner_retry_exhaustion_rejects_without_launching_children(bad_response: str) -> None:
    config = _topology_config()
    config["orchestration"]["planner_retries"] = 1
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)

    class AlwaysMalformed:
        routes: ClassVar[dict[str, object]] = {"default": object()}

        def __init__(self):
            self.calls = 0

        def complete(self, *_args, **_kwargs):
            self.calls += 1
            reason = "length" if bad_response.endswith("[") else "stop"
            return ModelResponse(bad_response, route_used="nim.test", finish_reason=reason)

    fake = AlwaysMalformed()
    coordinator.models = fake
    launches = []

    async def run_agent(*args, **kwargs):
        launches.append(args)
        return {"ok": True, "output": "should not run", "tools_executed": []}

    coordinator.run_agent = run_agent
    result = asyncio.run(coordinator.plan("entry-x", "inspect"))
    assert result["status"] == "rejected"
    assert "after 2 attempt(s)" in result["error"]
    assert "route=nim.test" in result["error"]
    assert "response_bytes=" in result["error"]
    if bad_response.endswith("["):
        assert "finish_reason=length" in result["error"]
    assert fake.calls == 2
    assert launches == []


def test_oversized_planner_response_is_rejected_without_retry_or_execution() -> None:
    config = _topology_config()
    config["orchestration"]["planner_retries"] = 2
    coordinator = SwarmCoordinator.from_default_config(models=ModelRegistry.from_config(config), tools=ToolRegistry.from_config(config), config=config)

    class OversizedModel:
        routes: ClassVar[dict[str, object]] = {"default": object()}
        calls = 0

        def complete(self, *_args, **_kwargs):
            self.calls += 1
            return ModelResponse("x" * 64_001)

    fake = OversizedModel()
    coordinator.models = fake
    result = asyncio.run(coordinator.plan("entry-x", "inspect"))
    assert result["status"] == "rejected"
    assert "64 KB" in result["error"]
    assert fake.calls == 1
