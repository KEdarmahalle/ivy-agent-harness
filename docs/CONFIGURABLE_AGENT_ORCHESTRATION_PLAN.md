# Configurable Agent Orchestration: Implementation Plan

**Status:** Implemented and verified locally
**Owner:** Harness maintainers
**Updated:** 2026-10-29
**Scope:** Config-defined agent profiles invoked from the normal conversational agen

## Product outcome

An operator can define named specialist agents in `config.yaml`. The chat agent can discover those names and delegate a bounded subtask through `agent.delegate`. The selected specialist runs with its configured route, prompt, tools, and skills; its result and tool activity return as the tool observation in the same conversation. No unknown agent name receives an implicit profile or broad default tools.

This makes the existing swarm runtime composable with chat; it does not claim arbitrary integrations without a configured model route, tool, skill, and policy.

## Existing implementation verified

- `harness/swarm.py`: `SwarmAgentSpec` already models prompt, route, tool allowlist, and handoff targets. `run_fan_out` runs real LLM calls concurrently. It currently fabricates profiles for unknown names and does not load profile config or skills.
- `harness/sdk.py`: `AgentHarness` builds model/tool/skill registries and a `SwarmCoordinator`; `stream` executes synchronous tools and appends observations to conversation context. This is the clean common integration point for SDK/API users.
- `harness/cli.py`: standalone `/swarm` is a fixed two-agent demonstration; the chat has tool execution and approval but does not install a real delegate executor.
- `harness/registries/tool_registry.py`: `agent.delegate` is currently a prompt-pattern simulator with hardcoded actions, not a model-backed agent.
- `harness/skills.py`: skills are discoverable and can be retrieved by name; global instructions are currently offered to the primary agent only.
- Existing model routes and tool allowlists are already configuration-backed and should remain authoritative.

## Architecture and invariants

1. **Config is the source of truth for specialist identity.** A request may select only a registered profile. Unknown profiles fail closed; no generated profile and no implicit tool grants.
2. **Least privilege is enforced twice.** Each profile's tools are validated against registered and globally allowed tools when loaded, then only that profile's tools are exposed to its model turn. The delegate caller cannot override the profile's route, tools, skills, or budgets.
3. **One invocation path is reused.** `agent.delegate` dispatches through the existing `SwarmCoordinator`; it must not duplicate model/tool execution logic or use a scripted fake response.
4. **Conversation continuity is explicit.** The delegate result is returned as a bounded, structured tool observation; the primary chat loop appends it and then continues its own response.
5. **Skills remain scoped.** A profile may name installed skills only. Their instructions are loaded from the existing registry, bounded, and included as specialist context; they do not silently grant tools.
6. **No accidental concurrency expansion.** Single delegation executes one profile. Existing fan-out remains available as a separate explicit operation; agent profile lookup never mutates the coordinator registry.
7. **Errors are truthful and bounded.** Unknown agent, invalid configuration, missing skill, provider failure, and tool failure are surfaced as errors/status, not reported as a successful delegation. Inputs, model turns, tool calls, and returned observations are bounded.

## Configuration contrac

```yaml
agents:
  profiles:
    researcher:
      description: Gather evidence and summarize uncertainty.
      route: gemini.flash
      system_prompt: You are a careful research specialist.
      tools: [web.fetch_url]
      skills: [research-citations]
      max_tool_turns: 3
```

Fields: `description` (optional bounded text), `route` (existing model route name, including `graph.node` names), `system_prompt` (required bounded text), `tools` (required list of registered/global-allowlisted names), `skills` (optional list of installed skill names), `max_tool_turns` (bounded positive integer), and `can_handoff_to` (optional list of registered profile names). Existing built-in profiles remain defaults for backward compatibility; config may add or explicitly replace them. A malformed profile fails configuration loading with its path and field identified.

## Ordered implementation sequence

### 1. Define and validate profiles

Add a single profile parser in the swarm layer. Parse existing built-ins plus `agents.profiles`, validate names, field types/limits, tool availability and allowlist membership, skill existence, handoff targets, and turn limits. Copy collections to avoid caller mutation. Do not create profiles during request execution.

### 2. Implement real single-agent delegation

Add a coordinator method that executes a named profile through the existing specialist turn loop. Resolve its model route; inject only selected skill instructions as clearly labeled reference material; enforce profile tool scope and bounded tool turns. Return stable fields (`agent`, `status`, `output`, `tools_executed`) and fail accurately on errors.

### 3. Replace the fake delegate handler with a dispatcher

Give `ToolRegistry` an optional delegate callback. `agent.delegate` validates bounded agent/prompt/context input and dispatches only to that callback. The callback is installed by `AgentHarness` and by CLI chat construction; it invokes the coordinator, never trusts caller-provided tool lists, and preserves run correlation. If no dispatcher is installed, report unavailable instead of fabricating work.

### 4. Join the result to normal cha

Expose configured profile names/descriptions in the chat tool catalog and system prompt. Ensure SDK, CLI, and API chat paths use the configured dispatcher. Keep tool observations bounded and structured so the primary agent can synthesize them. Keep existing approval and global tool policies effective for tools run by the specialist.

### 5. Make explicit swarm interfaces use profiles

Update CLI `/swarm` and the API/SDK swarm entrypoints to reject unknown profile names, avoid mutation of the shared registry, and use configured profiles. Preserve existing public return keys where possible; document any output changes.

### 6. Verify and documen

Add offline deterministic tests for config parsing, unknown profiles, profile tool-scope enforcement, skill injection/scoping, true model-backed delegation, result continuity, provider failure, disabled delegate, and existing fan-out behavior. Run focused tests, the full offline suite, Ruff, compile, and package build. Update README/configuration docs with a real two-specialist configuration and CLI/API/SDK usage, with clear approval and trust boundaries.

## Acceptance criteria

- Given valid profile configuration, when the harness starts, then each profile is available with its configured route, prompt, tools, skills, and limit.
- Given a malformed profile or unavailable tool/skill, when config is loaded, then startup fails with a field-specific error before model/tool execution.
- Given an unknown requested agent, when `agent.delegate` or a swarm entrypoint is called, then it returns a bounded failure and executes no model or tool call.
- Given a chat request that selects `agent.delegate`, when the specialist completes, then the main chat receives its output and continues to a user-facing answer.
- Given a specialist asks for a tool outside its configured profile, when its model call is built or invocation is attempted, then that tool is not exposed and cannot run.
- Given a configured skill, when a specialist runs, then only that specialist receives its selected skill instructions; no skill can expand tool permissions.
- Given provider/tool failure, when delegation runs, then the result records failure and never claims completion.
- Existing configured tool policy, approval behavior, fan-out, SDK, CLI, and API contracts continue to pass regression tests.

## Security and operational boundaries

This is an orchestration feature, not a multi-tenant security certification. It inherits the process identity and sandbox limits of the parent harness. A specialist does not get an isolated OS identity. Any mutating or privileged tool remains subject to the existing approval and policy layers. Configuration and prompts are operator-trusted; skill bodies and retrieved content must still be treated as untrusted data where appropriate. Concurrent fan-out needs separate concurrency/cost budgets before being exposed as a default chat behavior.

## Revision record

- **Draft 1:** Joined current swarm, chat, skills, and delegate paths. Review found two unsafe design shortcuts: dynamic unknown-agent creation and caller-provided tool overrides. Both are explicitly prohibited above.
- **Revision 2:** Added startup-time cross-validation, scoped skills, truthful errors, run correlation, compatibility requirements, offline acceptance coverage, and explicit security boundaries. Implementation must satisfy these before marking complete.
- **Revision 3 (2026-10-29):** Compared the completed code and focused/full verification against every task and acceptance criterion. Confirmed the important operational boundary that specialist agents cannot execute approval-gated or mutating tools unless an active auto-approval principal is already present. Corrected the package verification command to the available `uv build`; `python -m build` is unavailable in this environment because the `build` module is not installed.
- **Revision 4 (2026-10-29):** Final adversarial review added strict rejection of misspelled profile fields and malformed `agents` sections, maximum roster/tool/skill/handoff counts, a per-delegation skill-context budget, and a serialized tool-argument byte limit before invocation. It also corrected synthesis to resolve the synthesizer profile's configured model route. These findings were caught after the first passing full-suite run, so all checks are being rerun.
- **Revision 5 (2026-10-29):** Strengthened the SDK delegation regression to assert the specialist call, parent continuation call, final parent output, and presence of specialist evidence in the parent conversation. The first test attempt exposed a test-double string-conversion mismatch; the stub was corrected, the targeted test passed, and the full suite was rerun successfully.

## Implementation log

- 2026-10-28: Planning and code map created from direct inspection. Implementation and verification results will be appended here; unfinished criteria must remain explicit.
- 2026-10-29: Implemented startup-validated `agents.profiles`, including route/tool/skill references, bounds, duplicate checks, handoff references, and globally allowlisted tool scope. Coordinator copies profile specs, rejects unknown fan-out/synthesis names, and applies each profile's tool-turn cap. Added one-agent model-backed delegation with scoped skill instructions, bounded prompts/context/observations, truthful provider/tool failure results, and run-correlated lifecycle events. Replaced the scripted `agent.delegate` implementation with a fail-closed injected dispatcher and wired it into SDK and CLI chat; explicit CLI/SDK swarm selection now uses configured profiles. Mutation and approval-gated tools require an active auto-approval principal within delegation.
- 2026-10-29 verification: focused command `uv run pytest -q tests/test_agent_profiles.py tests/test_phase3_graphs_and_swarms.py tests/test_tier1_sdk.py tests/test_cli.py` passed (49 tests). Full `uv run pytest -q` passed (199 tests, 12 subtests). `uv run ruff check harness tests` passed. `uv run python -m compileall -q harness` passed. The specified `uv run python -m build` could not run because the environment lacks the `build` module; equivalent `uv build` succeeded and produced sdist and wheel. No remaining implementation criteria identified.
- 2026-10-29 independent verification: focused pytest rerun passed (49); full pytest rerun passed (199 tests, 12 subtests); Ruff, compileall, `uv build`, and `git diff --check` passed. `uv build` produced `dist/agent_harness-0.1.0.tar.gz` and `dist/agent_harness-0.1.0-py3-none-any.whl`. The `python -m build` command remains unavailable as noted above. Existing README edits and untracked `docs/DIFFERENTIATING_FEATURES_TEST_PLAN.md` were present before this feature and were preserved.
- 2026-10-29 final verification: focused `uv run pytest -q tests/test_agent_profiles.py tests/test_phase3_graphs_and_swarms.py tests/test_tier1_sdk.py tests/test_cli.py` passed (52 tests before the final assertion strengthening); targeted SDK continuity test passed (1 test); final full `uv run pytest -q` passed (202 tests, 12 subtests). `uv run ruff check harness tests`, `uv run python -m compileall -q harness`, `uv build`, and `git diff --check` passed. The source and wheel distributions built successfully. The earlier `uv run python -m build` attempt remains unavailable because `build` is not installed.
