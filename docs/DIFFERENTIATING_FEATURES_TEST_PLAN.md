# Ivy Harness Differentiating Features: Test Plan

**Purpose:** Verify which capabilities make Ivy useful as an agent harness, where
those capabilities are stronger than a basic prompt/tool loop, and which claims
still need evidence. This is a test plan, not a claim that Ivy is uniquely ahead
of every competing harness.

**Scope:** The current repository implementation. The tests should use local
fixtures and deterministic model doubles by default. Hosted-model tests are
optional, separately labeled, and must use disposable workspaces.

## 1. How to interpret “different”

Agent frameworks overlap in model routing, tools, graphs, memory, approvals, and
traces. A feature being present does not prove it is unique or better. This plan
uses “differentiator” to mean a product behavior Ivy can demonstrate with an
observable contract. Competitor comparisons require a controlled, like-for-like
benchmark; do not publish “other harnesses fail this” based on intuition.

### Current candidate differentiators

| Capability | Current implementation | Status and boundary |
| --- | --- | --- |
| Dynamic workflow routing with declared candidates | Declarative YAML/JSON graphs plus model-selected route decisions, bounded graph steps, and candidate validation | **Implemented, test deeper.** Routes choose among declared nodes; this is not arbitrary safe self-modifying workflow generation. |
| Workflow/configuration pinning | A digest binds a run to its loaded graph and selected execution configuration; resume fails closed when the digest changes | **Implemented.** Test config changes, manifest changes, built-ins, and persisted resumes. |
| Context-bound human approval | Distinct same-tenant approver, role check, expiring one-use grant, and run-state/continuation binding | **Implemented.** Strong operational contract to test; it is not a general approval policy engine for every SDK tool call. |
| Evidence-aware investigation and research | Built-in investigation/research graphs, typed evidence, confidence levels, exact quote checks, and deterministic gate decisions | **Implemented with limits.** Source-linked quotes do not establish that a claim logically follows from the source. |
| Verification-aware chat reporting | CLI records recognized check commands, detects later mutations, and appends deterministic evidence when checks fail, become stale, or the tool budget ends | **Implemented in CLI path.** Recognition is pattern-based; it is not a universal semantic proof of verification and is not a cross-surface evidence ledger. |
| Persistent run traces, checkpoints, rewind | SQLite run store records runs/traces/checkpoints and supports resume/rewind | **Implemented for a local single instance.** It is not a durable distributed worker/queue or proof of safe replay of external side effects. |
| Configurable model fallback | Named model routes, retry controls, explicit fallbacks, sanitized route listing | **Implemented.** Fallback success must remain visible; route changes can alter behavior and should be included in run evidence. |
| Multiple integration surfaces | CLI, SDK, REST/SSE/WebSocket, OpenAI-compatible routes, MCP stdio, plugin tools | **Implemented unevenly.** Security and behavior need parity tests across each surface. |
| Multi-agent coordination | Fan-out and handoff swarm patterns with configured agent specs | **Partial.** This is a bounded coordinator, not a general child-agent runtime with isolated credentials, budgets, cancellation, and durable traces. |

## 2. Test goals and non-goals

The release test should prove that:

1. A route decision cannot escape the workflow’s declared candidate set, create
   approval state, or run forever.
2. A paused run cannot continue after its workflow or execution configuration
   changes, and approval grants cannot be forged, replayed, used cross-tenant, or
   applied to changed state.
3. The harness distinguishes observed test results from model claims and makes
   failed, unavailable, or stale checks visible to the caller.
4. Evidence gates never upgrade inferred or merely source-linked evidence to a
   verified pass.
5. Persistence, traces, checkpoints, and rewind have deterministic behavior, and
   their limits around external side effects are explicit.
6. Equivalent operations through CLI, SDK, API, and MCP do not silently bypass
   authorization or execution policy.
7. Hosted-model quality, latency, and cost are measured separately from
   deterministic safety invariants.

The test does not prove that an LLM is correct, that a cited source supports a
claim semantically, that a local subprocess is isolated, or that the system is
better than a named competitor. Those require separate evaluation evidence.

## 3. Run the current deterministic baseline

From the repository root:

```powershell
uv sync --extra dev
uv run pytest -q
uv run ruff check harness tests
uv run python -m harness.evals --config config.yaml
```

The eval command reads `evals/cases.json`, uses a temporary SQLite database, and
returns a nonzero exit if an expected graph result differs. Keep this suite
offline and deterministic. Run provider-backed tasks only as a separate test
job, with an explicit provider route and a disposable workspace.

Existing focused regression coverage includes:

- `tests/test_agentic_dynamic_graphs.py`
- `tests/test_secure_workflow_boundaries.py`
- `tests/test_api.py`
- `tests/test_hardening_regressions.py`
- `tests/test_cli_verification_evidence.py`
- `tests/test_engine_graphs.py`
- `tests/test_phase4_events_and_checkpoints.py`
- `tests/test_model_registry.py`
- `tests/test_advanced_enterprise_features.py`
- `tests/test_tier1_sdk.py` through `tests/test_tier4_workers.py`

These tests are the starting baseline. The scenario matrix below calls out
coverage that should be expanded before using differentiation claims in public
materials.

## 4. Scenario test matrix

### Scenario A: Workflow changes while a run is paused

**Why test it:** Dynamic workflows are only controllable if a continuation stays
bound to the workflow the requester saw.

**Setup:** Create a declarative graph with a harmless initial node, an approval
gate, and a continuation node. Start it with a deterministic model. Pause at the
gate. Mutate each execution-affecting element in a fresh subtest: a route target,
tool list, node prompt, model route, tool allowlist, pack setting, MCP server
definition, and governance setting.

**Expected:** Resume fails closed before another model or tool call. The trace
records a bounded failure reason without leaking provider secrets. The original
run and pause state remain inspectable. A new run against the changed definition
can proceed under the new digest.

**Pass condition:** Every relevant mutation invalidates the old continuation;
there are zero post-mutation model/tool invocations. Also verify that changes to
non-execution presentation metadata do not cause accidental invalidation if the
product intends those settings to be excluded from the digest.

**Coverage to extend:** The existing suite covers a changed in-memory workflow.
Add SQLite restart/resume coverage and a file-discovered YAML manifest mutation.

### Scenario B: Approval grant abuse and race

**Why test it:** The approval mechanism is a security boundary, not just a
confirmation prompt.

**Try:** requester self-approval; approver from another tenant; user without the
configured approver role; expired grant; replay; altered run state; changed
workflow digest; wrong continuation; concurrent resume requests; grant from run
one applied to run two.

**Expected:** Only an authorized, distinct, same-tenant approver receives a
grant. At most one concurrent consumer succeeds. Every invalid attempt leaves
the run paused. A grant is bound to its approval type, run, state, continuation,
expiry, and workflow digest.

**Pass condition:** Exactly one successful consumption under concurrency and
zero unauthorized transitions. Check both `InMemoryRunStore` and
`SQLiteRunStore`; repeat API authorization tests with distinct principals.

**Coverage to extend:** Existing tests cover many of these cases. Add a true
parallel API resume race against SQLite and verify trace audit details after
rejected attempts.

### Scenario C: Dynamic routing under adversarial model output

**Why test it:** Model-selected routing is useful only if model output remains a
suggestion constrained by the workflow.

**Inputs to a deterministic model double:** valid candidate; undeclared node;
case variation; missing target; invalid action; malformed/truncated JSON;
attempted `approved_gate`, `required_approval`, `_visited_history`, or oversized
state update; repeated-node cycle; provider exception.

**Expected:** Valid decisions can advance only to declared candidates. Invalid
responses stop safely or use documented recovery behavior; they cannot create
approval state, change internal history, exceed state-size bounds, or route to an
undeclared node. Circuit breakers terminate cycles within the configured limit.

**Pass condition:** Assert the exact next-node choice, terminal status, state
updates, model-call count, and emitted route-decision event for every case.
Never accept a test that only checks “no exception.”

**Coverage to extend:** Add property-based or generated tests over malformed
JSON and arbitrary candidate/target strings. Check empty candidate sets and
all-candidates-visited cases explicitly.

### Scenario D: Verification honesty after failure, exhaustion, or edits

**Why test it:** Most coding agents can invoke a test command; the harness should
make the result observable and stop a polished final response from overriding
the command outcome.

**CLI cases:**

1. Test command exits zero and there are no later edits.
2. Test command exits nonzero, then the model claims success.
3. Command is unavailable or approval is declined.
4. Successful check is followed by a file write or command that may mutate code.
5. Tool-step budget ends before verification.
6. A command string contains a test-like word but does not actually run a test.
7. A compound shell command runs tests and then mutates files.

**Expected:** The CLI reports observed exit evidence and stale status for the
covered cases. It does not convert a failed exit to success. Cases 6 and 7 are
important adversarial tests because current detection uses command-name pattern
matching and mutation heuristics; do not treat this as a shell semantic analyzer.

**Pass condition:** The final CLI output contains the correct, deterministic
status even when the model claims otherwise. Record the raw process exit, tool
step, and later mutations. Repeat separately through SDK/API to expose current
surface-parity gaps rather than implying CLI behavior applies everywhere.

**Coverage to extend:** Current tests cover core reporting branches. Add tests
for aliases, command chaining, shell wrappers, test commands run through scripts,
and a non-CLI surface. Long-term, prefer explicit structured verification
events from the process runner over regex inference.

### Scenario E: Evidence gate resists plausible but unsupported claims

**Why test it:** Quote presence and semantic support are different properties.

**Research fixtures:** exact quote supporting a claim; exact quote unrelated to
the claim; near-match quote; invented source ID; duplicate citation; hostile
prompt injection in source text; empty sources; malformed model output; claim
with one valid and one invalid citation.

**Investigation fixtures:** confirmed test output; adjacent-code inference; no
reproduction command; protected file; contradictory test evidence.

**Expected:** Exact excerpts are checked against supplied content. Untrusted
source instructions are treated as data. Unsupported or unverified claims remain
visible and cannot get a `PASS`. Source-linked evidence should be labeled as
source-linked and require semantic review. Contradictions and missing evidence
must lower the verdict or pause for a human.

**Pass condition:** No claim is reported as runtime-verified without an observed
reproduction result. No research claim is called semantically verified solely
because its quote matches exactly. The test asserts claim-level evidence and
gate verdict, not just citation count.

### Scenario F: Checkpoint, rewind, crash/restart, and side effects

**Why test it:** Time-travel state is useful for debugging, but external side
effects cannot be safely rewound by restoring a database snapshot.

**Run:** Persist a run and trace in SQLite, create a checkpoint, advance state,
rewind, inspect trace ordering, and reopen a new store instance on the same
database. Separately simulate a node that performs an idempotent fake external
write before a process interruption.

**Expected:** Run state and current node return to the selected checkpoint;
historical trace remains available and records the rewind. Reopening SQLite
preserves runs and checkpoints. A side-effecting action is not silently claimed
to be undone; the workflow needs idempotency or compensation before retry.

**Pass condition:** Exact persisted state and trace sequence survive restart.
Rewind changes the run to a safe runnable/claimable status. Duplicate execution
does not duplicate a fake side effect when idempotency is part of the fixture.

**Limit to document:** SQLite persistence is local durability, not multi-worker
lease recovery, replication, or exactly-once external effects.

### Scenario G: One policy across integrations

**Why test it:** A capability should not become more powerful merely because it
was called through a different adapter.

**Matrix:** Invoke the same harmless read tool and a denied mutation through the
CLI, Python SDK, REST API, OpenAI-compatible route, MCP-exposed tool, and plugin
tool. Vary unauthenticated local, authenticated same-tenant, authenticated
foreign-tenant, and insufficient-role contexts where applicable.

**Expected:** The tool allowlist, root boundary, protected paths, tenant policy,
approval rule, and output bounds are consistent for each adapter. Any unsupported
policy context is recorded as a gap rather than silently counted as a pass.

**Pass condition:** Zero unauthorized side effects in every supported path.
Capture a per-surface policy matrix and API response status. No public-use claim
should rely on controls that are only enforced by the interactive CLI.

**Known limitation to include in results:** The SDK `RunConfig` approval flag is
not sufficient by itself to enforce approvals; registry checks depend on an
active tenant principal. Test actual context setup, not only the config value.

### Scenario H: Provider failure, retries, and fallback transparency

**Why test it:** Resilience must not hide that the requested route was not used.

**Inject:** timeout, rate limit, authentication failure, malformed tool-call
payload, transient provider error, and a fallback route with a different model.

**Expected:** Retry counts remain bounded; fallback follows only configured
routes; final metadata identifies the route actually used. Non-retryable errors
do not create retry storms. Fallback text is not mistaken for a successful
hosted-model result.

**Pass condition:** Assert exact call counts, route selection, reported error,
and final `route_used`. Add integration tests for the provider/model pair used
in deployment; a deterministic fallback does not prove hosted provider health.

### Scenario I: Swarm containment and useful synthesis

**Why test it:** Fan-out and handoff can improve coverage, but can also multiply
cost, tool access, and contradictory output.

**Run:** Fixed analyst and reviewer agents receive the same scoped task. Add an
invalid handoff target, a repeated handoff, one worker failure, conflicting
findings, and a tool request outside the allowed list.

**Expected:** Handoffs stay in the allowed agent set and terminate at a hop
limit. Worker failure is visible. The synthesizer preserves disagreement and
source evidence rather than claiming consensus. Tool access is constrained per
agent or the test explicitly records that it is shared.

**Pass condition:** Bounded calls and hops, complete trace of each agent, explicit
worker errors, no unauthorized tool call, and synthesis that retains unresolved
conflicts. Do not market this as isolated child agents until budgets, identity,
policy, cancellation, and trace isolation are demonstrated.

## 5. Hosted-model stress run

Run hosted-model tests only after deterministic tests pass. Use the same provider
and model for every harness under comparison, with the same prompt, context,
tool catalog, timeout, temperature, workspace snapshot, and allowed side effects.
Pin model/API versions where the provider permits it. Use a fresh disposable
workspace for each trial and never include credentials in prompts or test output.

Repeat each task at least 10 times for an initial signal and 30 times before
publishing a comparative reliability claim. Record:

- task success judged by machine-checkable postconditions;
- security invariant violations and unauthorized tool attempts;
- evidence correctness and unsupported-claim rate;
- number of tool calls, retries, and fallback activations;
- wall-clock time, time-to-first-token, token use, and estimated cost;
- stale/failed verification reporting accuracy;
- run completion, interruption, resume, and recovery outcomes.

Report median and p95 latency, success rate with sample count, and confidence
intervals for stochastic quality rates. Publish failures and abstentions, not
only successful demos. A competitor result is valid only if that harness receives
equivalent capabilities and the exact run configuration is archived.

## 6. Release acceptance checklist

Do not call a candidate differentiator release-ready until:

- deterministic regression tests pass in CI from a clean checkout;
- security scenarios have zero unauthorized state changes;
- each supported integration surface has a passing policy-parity test;
- failed, skipped, stale, and unavailable checks are distinguished from passing;
- model-dependent quality is measured separately from deterministic correctness;
- traces and benchmark artifacts identify code revision, configuration digest,
  provider/model, and test fixture version without storing secret values;
- known gaps and non-goals appear beside the result.

The strongest current story is the combination of declared dynamic workflows,
context-bound approval for graph continuations, and evidence-aware verification
with inspectable run traces. The test plan should prove the end-to-end composition
of those controls; isolated unit tests for each component are not enough.
