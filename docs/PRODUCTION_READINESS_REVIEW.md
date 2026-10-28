# Agent Harness Production Readiness Review

**Review date:** 2026-10-28
**Scope:** Repository-wide static review of the Python service, API, agent loop, tools, persistence, integrations, configuration, packaging, and operational readiness.
**Verdict:** **Not ready for public or multi-tenant production deployment.** The project has a broad and useful feature surface and many focused tests, but the current defaults and several enforcement gaps make untrusted network exposure unsafe. Treat this as an engineering assessment, not a formal penetration test or proof that no further defects exist.

## Executive assessment

The codebase has the foundation of a capable local agent harness: model routing, resumable graph runs, tools, plugins, MCP, events, an SDK, and an API. The primary gap is that security and governance appear as configuration and feature modules without consistently forming a mandatory enforcement boundary. A public deployment can expose arbitrary code execution and workspace access; some of the advertised guardrails are optional, bypassable, or not wired into request paths.

For scenario-by-scenario product coverage and the proposed 0.2/0.3 capability sequence, see [ADVANCED_USE_CASE_SCENARIOS.md](ADVANCED_USE_CASE_SCENARIOS.md).

### Readiness by area

| Area | Assessment | Main reason |
|---|---|---|
| Core feature breadth | Promising | Broad SDK, graph, tool, model, and integration surface |
| Security boundary | **Fail** | Optional authentication, host execution fallback, protected-file read gaps, incomplete webhook verification, SSRF exposure |
| Multi-tenancy / authorization | **Fail** | Run data is not tenant scoped; governance/RBAC controls are not enforced in the API execution path |
| Reliability / concurrency | **Needs work** | Synchronous model and tool calls in async handlers; duplicate execution races; single-process background work |
| Data lifecycle | **Needs work** | Run state and traces persist sensitive prompts and tool data without retention or tenant deletion controls |
| Operability | **Needs work** | No deployment/CI/release posture is evident in the repository inventory; health endpoint is liveness only |
| Tests / evals | **Partial** | Many focused tests and evaluation assets exist, but production guarantees need CI gates and adversarial integration coverage |

## Critical findings

Priorities indicate what should block a public launch: **P0** means do not expose publicly until resolved; **P1** means high-risk and should be fixed before production; **P2** means important hardening and scale work.

### P0 — API authentication is disabled by the shipped configuration

`config.yaml` sets `auth.api_key: null`, and `require_api_key` explicitly allows every request when no key is configured ([main.py](../harness/api/main.py#L93), [main.py](../harness/api/main.py#L99), [config.yaml](../config.yaml#L17)). The same app exposes direct tool invocation, model calls, autonomous runs, run resume, and rewind endpoints. The agent API also defaults to automatic tool approval ([main.py](../harness/api/main.py#L43), [main.py](../harness/api/main.py#L221)).

**Impact:** A deployment using the sample configuration can allow unauthenticated callers to spend model budget, read/write accessible files, execute commands, manipulate runs, and invoke integrations.

**Required change:** Make network bind local-only by default and fail startup when a non-loopback listener is configured without explicit authentication. Replace the shared static API key with a supported identity layer for public deployments (OIDC/JWT or equivalent), authenticated principals, roles/scopes, key rotation, and audit events. Make approval policy server-controlled; never trust a caller-provided `auto_approve` to grant itself execution rights.

### P0 — Local execution is not an OS sandbox

The configured `local` provider runs approved shell and Python commands as the current user through `shell=True` ([local.py](../harness/sandbox/local.py)). Configured workspace roots protect file-tool handlers, but do not confine arbitrary shell processes. The optional Docker provider fails closed when its runtime is unavailable; it is not silently substituted.

**Impact:** Local commands can access host files, environment variables, and network resources available to the user. Tool approval and file-path policy do not create process isolation.

**Required change:** Keep execution-provider choice explicit and configurable. Treat `local` as trusted workstation mode. For untrusted prompts or public multi-tenant deployments, configure an actually isolated disposable worker with per-run mounts, non-root identity, resource/PID limits, restricted egress, and cleanup on timeout/cancellation.

### P1 — Protected paths are not protected from read/search tools

Path checks for protected paths are called from writable resolution ([tool_registry.py](../harness/registries/tool_registry.py#L538)), while read resolution only checks workspace roots ([tool_registry.py](../harness/registries/tool_registry.py#L521)). `file.read_text`, `file.read_lines`, and recursive `file.search_text` read files without `_ensure_not_protected` ([tool_registry.py](../harness/registries/tool_registry.py#L721), [tool_registry.py](../harness/registries/tool_registry.py#L745), [tool_registry.py](../harness/registries/tool_registry.py#L766)). The local sandbox's protected-file check does not help these handlers because they read paths directly.

**Impact:** Files such as `.env`, private keys, and configured protected files can be returned to callers or sent to a model. Recursive search can also traverse secret-bearing files.

**Required change:** Apply canonical protected-path policy to every read, list, search, write, and execution path; prune protected directories from traversal; resolve symlinks before authorization; test Windows and POSIX path variants. Prefer deny-by-default path grants over a small filename denylist.

### P1 — Webhook signature verification is optional and hashes reconstructed JSON

GitHub and Slack checks run only when a secret and signature headers are present; missing secrets or missing headers skip verification ([main.py](../harness/api/main.py#L279), [main.py](../harness/api/main.py#L280), [main.py](../harness/api/main.py#L335), [main.py](../harness/api/main.py#L336)). Both handlers serialize parsed JSON to reconstruct bytes before hashing ([main.py](../harness/api/main.py#L283), [main.py](../harness/api/main.py#L345)); provider signatures are defined over the original request body. Jira has no signature verification in its handler ([main.py](../harness/api/main.py#L372)).

**Impact:** Forged requests can enqueue agent work when secrets are unset; legitimate requests can fail signature validation if the reconstructed body differs from the raw body. If API-key auth is enabled globally, webhook providers also need a documented secure auth arrangement rather than accidental reliance on a shared API key.

**Required change:** Require configured signing secrets and their signature/timestamp headers, verify against raw request bytes, reject stale/replayed requests, and fail startup if an enabled integration lacks a secret. Add Jira authentication appropriate to its webhook mechanism. Add payload size limits and delivery-id deduplication.

### P1 — Server-side request forgery through URL fetching and completion callbacks

`web.fetch_url` accepts a caller URL and uses `urllib.request.urlopen` when network access is enabled ([tool_registry.py](../harness/registries/tool_registry.py#L905)); the shipped config enables network access ([config.yaml](../config.yaml#L71)). Agent-run requests also accept an arbitrary `webhook_url` and dispatch to it ([main.py](../harness/api/main.py#L45), [main.py](../harness/api/main.py#L246), [main.py](../harness/api/main.py#L71)). Neither path restricts private, loopback, link-local, or metadata-service destinations, and redirects can change the final target.

**Impact:** A caller or model-influenced workflow may probe internal services or cloud metadata, or send task/result data to an attacker-controlled endpoint.

**Required change:** Use a central outbound network policy with scheme/host allowlists, DNS and redirect revalidation, private-address blocking, egress firewall enforcement, response size/content-type limits, and audit logs. Remove caller-selected callback URLs or require an administrator-registered destination with signed payload delivery.

### P1 — Run APIs have no tenant or owner isolation

Runs are identified by UUID and fetched directly from the shared store; handlers return run snapshots, trace, result, checkpoints, and allow resume/rewind after only checking that the run exists ([main.py](../harness/api/main.py#L487), [main.py](../harness/api/main.py#L518), [main.py](../harness/api/main.py#L526), [main.py](../harness/api/main.py#L536), [main.py](../harness/api/main.py#L545)). The schema has no tenant/owner identity ([schemas.py](../harness/engine/schemas.py#L51)).

**Impact:** Any authenticated user with access to the service can read or mutate any known run ID. UUID unpredictability is not an authorization policy.

**Required change:** Establish authenticated principal and tenant context at ingress; persist ownership with every run, checkpoint, trace, and artifact; enforce ownership in every read, resume, rewind, and delete query. Add tenant-specific filesystem roots, quotas, encryption/key separation where required, export and deletion workflows.

### P1 — Declared rate limits, concurrency caps, budgets, and RBAC are not enforced

The sample configuration declares RPM, concurrency, token/cost budgets, and role restrictions ([config.yaml](../config.yaml#L95)). Related primitives exist in `governance.py`, but repository references show no call from the API or SDK request paths to enforce them; role checks are not wired into `ToolRegistry.invoke` or tool execution ([governance.py](../harness/governance.py#L23), [tool_registry.py](../harness/registries/tool_registry.py#L437)). Tool `requires_approval` is metadata, and the network API can set `auto_approve` itself. In the SDK loop, parsed tool calls are passed directly to `execute_chat_tool` without checking the `auto_approve_tools` field or tool approval metadata ([sdk.py](../harness/sdk.py#L285), [sdk.py](../harness/sdk.py#L294)).

**Impact:** These settings can give operators a false sense of cost, abuse, or privilege controls. A burst of expensive calls can exceed intended service limits.

**Required change:** Enforce limits centrally at authenticated ingress and at model/tool boundaries. Use distributed rate/concurrency state for multi-worker deployments, provider-reported token/cost accounting, maximum input/output sizes, per-tenant budgets, and role/scope checks immediately before tool execution. Treat unknown tools/roles as denied.

### P1 — Synchronous agent work blocks the async API event loop

Async FastAPI handlers call synchronous `harness.run`, `harness.stream`, provider code, and tool handlers inline ([main.py](../harness/api/main.py#L221), [openai.py](../harness/api/openai.py#L186), [sdk.py](../harness/sdk.py#L250)). Model retry backoff uses blocking `time.sleep` ([model_registry.py](../harness/registries/model_registry.py#L227), [model_registry.py](../harness/registries/model_registry.py#L231)).

**Impact:** One slow model/provider/tool call can stall unrelated requests, health checks, and stream consumers in the worker. Timeouts and cancellation do not reliably stop underlying subprocess or provider work.

**Required change:** Move execution to a bounded worker/job system or fully async implementations. Apply explicit connection/read deadlines, cancellation propagation, bounded queues, per-provider concurrency pools, and child-process-tree termination. Keep API handlers responsive while jobs run.

### P1 — Chat SSE and WebSocket requests pass an invalid RunConfig field

`RunConfig` defines `auto_approve_tools`, but the SSE chat handler constructs it with `auto_approve` ([sdk.py](../harness/sdk.py#L39), [main.py](../harness/api/main.py#L191)). The WebSocket handler does the same ([main.py](../harness/api/main.py#L424)). Since `RunConfig` is a dataclass without that parameter, these requests raise `TypeError` before agent execution.

**Impact:** The advertised chat stream and WebSocket chat paths fail when a chat request is handled. This is a direct runtime correctness defect, independent of deployment configuration.

**Required change:** Map the request flag to `auto_approve_tools` (and preferably remove caller authority to elevate approval). Add endpoint-level tests for valid and invalid request options.

## Important engineering findings

### P2 — Read and search limits do not bound actual I/O

`file.read_text` reads the entire file then slices by character count ([tool_registry.py](../harness/registries/tool_registry.py#L766)); `file.read_lines` reads and splits the entire file ([tool_registry.py](../harness/registries/tool_registry.py#L745)); `file.search_text` recursively reads entire files before enforcing `max_results` ([tool_registry.py](../harness/registries/tool_registry.py#L721)). This can consume substantial memory and CPU, and `max_bytes` is not a byte-level I/O limit. `max_entries` and `max_results` are not clamped to safe positive bounds.

**Improve:** Stream bounded byte/line chunks, skip binary/oversized files, cap traversal depth and total files/bytes/time, and validate all caller-supplied limits.

### P2 — SQLite is a single-node persistence backend without lifecycle controls

Run snapshots, inputs, results, traces, and checkpoints are stored as JSON in SQLite ([store.py](../harness/engine/store.py#L69)). There is no visible retention/TTL, tenant-scoped deletion, encryption-at-rest integration, backup/restore procedure, migration framework, or production database abstraction. This does not establish acceptable durability or multi-instance behavior for a public service.

**Improve:** Define data classification and retention policies; add tested backup/restore and schema migrations; set SQLite busy timeout/WAL where it remains supported; use a transactional shared database for horizontally scaled service; encrypt sensitive data and support tenant export/deletion. Avoid logging full prompts, tool arguments, or secrets by default.

### P2 — Run execution is not protected against duplicate concurrent continuation

`continue_run` loads a snapshot, marks it running, then invokes nodes without a per-run lease/claim ([runner.py](../harness/engine/runner.py#L67)). Concurrent `/resume` or wait requests can execute the same node more than once. Since graph nodes may call external APIs or mutate files, retries and races can create duplicate side effects.

**Improve:** Add atomic state transitions/leases with fencing tokens and idempotency keys. Make side-effecting tools idempotent or require an approval/commit boundary. Record attempt IDs and recovery state.

### P2 — MCP/plugin discovery failures are silently swallowed

Tool registration suppresses all discovery exceptions ([tool_registry.py](../harness/registries/tool_registry.py#L481), [tool_registry.py](../harness/registries/tool_registry.py#L498), [tool_registry.py](../harness/registries/tool_registry.py#L501), [tool_registry.py](../harness/registries/tool_registry.py#L518)). A service can start without configured capabilities and no clear diagnostic. MCP subprocess lifecycle, protocol compatibility, stderr handling, and health are not exposed as readiness signals.

**Improve:** Log structured, redacted startup diagnostics; distinguish optional from required integrations; fail startup/readiness for required components; cap MCP message sizes and calls; close subprocesses on application shutdown; enforce per-call timeout and process isolation.

### P2 — The OpenAI-compatible API silently drops parts of the protocol

`ChatCompletionRequest` accepts `tools`, `tool_choice`, and multimodal content, but prompt extraction only joins system/user/assistant content and ignores tool messages, tool calls, and structured content ([openai.py](../harness/api/openai.py#L27), [openai.py](../harness/api/openai.py#L71), [openai.py](../harness/api/openai.py#L129)). The response fabricates token usage from character counts ([openai.py](../harness/api/openai.py#L189)). Clients may believe tool calling or multimodal input is supported when it is ignored.

**Improve:** Either implement and contract-test supported OpenAI semantics, including tool calls, history, roles, streaming finish/error behavior and real usage accounting, or reject unsupported fields/content explicitly. Publish a compatibility matrix.

### P2 — Error and trace paths may disclose sensitive values

Provider failures are returned with raw exception text from model smoke ([main.py](../harness/api/main.py#L175)); run failures store exception text ([runner.py](../harness/engine/runner.py#L116)); tool results, prompts, and state can enter run traces. Secret redaction exists but is not visibly applied at all API/log/trace boundaries.

**Improve:** Return stable public error codes and correlation IDs; store restricted diagnostic detail separately; redact secrets and sensitive headers/arguments before persistence; define trace access and retention.

### P2 — The package configuration story is inconsistent

The config loader documents a bundled default path ([config.py](../harness/config.py#L3)) and looks beside the installed package ([config.py](../harness/config.py#L68)), but the wheel configuration packages only `harness` ([pyproject.toml](../pyproject.toml#L25)); root `config.yaml` is not declared as package data. In addition, an explicit missing config path falls through to discovery rather than clearly failing ([config.py](../harness/config.py#L58)).

**Improve:** Package a safe default config or make configuration mandatory and validate it at startup. Fail on an explicitly requested missing/invalid path. Add a versioned config schema with validation of URLs, roots, limits, routes, and security-sensitive defaults.

### P2 — Production delivery and support artifacts are missing or not evident

The repository inventory contains no CI workflow, container build definition, deployment manifest, public security policy, release notes/versioning guidance, or operational runbook. `GET /health` returns a constant `ok` rather than reporting readiness of storage, required model routes, or required integrations ([main.py](../harness/api/main.py#L135)). This is an inventory observation, not proof those assets are maintained elsewhere.

**Improve:** Establish supported deployment targets, non-root container builds, pinned/locked dependency install, CI lint/type/security/test gates, SBOM and vulnerability response, readiness/liveness probes, graceful shutdown, metrics, alerting, backup/restore drills, incident response, and upgrade/migration docs.

## Missing capabilities for a public-grade harness

Prioritize capabilities that establish safety and operational contracts before adding more agent features:

1. **Identity and authorization:** OIDC/JWT, principals, tenant and project boundaries, scopes, operator/admin separation, revocation, and audit trail.
2. **Safe execution service:** fail-closed isolated workers, per-run scratch storage, network egress policy, resource quotas, cancellation, and artifact scanning.
3. **Policy engine:** centralized decisions for tool access, approvals, data classification, model routes, outbound destinations, and human escalation.
4. **Durable job orchestration:** queue-backed runs, leases, retries with idempotency, dead-letter handling, and worker autoscaling.
5. **Cost and abuse controls:** request limits, concurrency, token/cost accounting, tenant quotas, input/output caps, and spend alerts.
6. **Data governance:** retention, deletion/export, encryption, redaction, configurable trace detail, and clear provider data handling controls.
7. **Operational visibility:** structured logs, metrics, traces, health/readiness, provider/integration health, SLOs, and dashboards/alerts.
8. **Quality governance:** CI, static/type/security checks, dependency updates, API contract tests, concurrency and failure-injection tests, eval regression gates, and a documented supported-version matrix.
9. **Public API contract:** versioning/deprecation, pagination, idempotency keys, standard error envelopes, request limits, OpenAPI compatibility tests, and explicit feature support.
10. **Deployment and support:** production configuration examples, secret-manager integration, container hardening, backup/restore, migrations, scaling guide, security reporting, and incident runbooks.

## Recommended delivery sequence

### Gate 0 — Close exposure paths (before any public endpoint)

- Require identity/auth on every HTTP, SSE, WebSocket, OpenAI, and admin endpoint.
- Disable public access by default; reject unsafe startup configuration.
- Fail closed when the isolated sandbox backend is unavailable.
- Fix protected file checks for all read/search/list/write paths.
- Require and correctly validate all webhook signatures; remove arbitrary URL callbacks and constrain outbound requests.
- Disable network access and arbitrary process/Python tools for untrusted users until egress and isolation boundaries are enforced.

### Gate 1 — Make enforcement real

- Wire roles, tool policy, approval requirements, rate limits, concurrency, and budgets into the actual request and tool execution path.
- Add tenant ownership checks for all run/artifact endpoints.
- Bound file traversal, request bodies, model inputs/outputs, tool outputs, provider timeouts, and subprocess lifetime.
- Move blocking work off the API event loop and prevent duplicate run execution.

### Gate 2 — Establish durable operations

- Define storage retention, encryption, backups, restore, migrations, and deletion behavior.
- Add readiness checks, structured/redacted logs, metrics, tracing, graceful shutdown, and alerting.
- Add CI and release automation with pinned dependency resolution and a supported deployment image.
- Publish threat model, security policy, configuration reference, API compatibility matrix, and runbooks.

### Gate 3 — Prove readiness continuously

- Add isolated integration tests for auth bypasses, tenant boundary violations, protected-path traversal/symlinks, SSRF/redirects, webhook replay/signature handling, process escape/resource exhaustion, and concurrent resume.
- Add fault injection for model/provider timeouts, worker death, SQLite/database failure, webhook failure, and partial persistence.
- Gate releases on deterministic eval suites, safety regressions, load tests, restore drills, and dependency/security scans.

## Review limitations and verification status

### Implementation follow-up (2026-10-28)

The working tree now implements a substantial subset of the findings above. The changes include loopback defaults and startup checks for configured external binds, API-key principal resolution, run ownership and tenant fields, owner checks on run operations, atomic queued-run claiming, webhook signature verification against raw request bodies, outbound URL validation with DNS-pinned connections, protected-path checks for file tools, bounded file reads/search traversal, fail-closed Docker selection, Docker hardening flags, model/tool execution moved off the async event loop in the API paths, and package inclusion of the shipped configuration. Regression coverage was added or updated for authentication, ownership, run claims, webhooks, network policy, and protected files.

The initial follow-up verification reported **150 passed and 5 subtests passed**. On 2026-10-28, a subsequent runtime and quality pass added subagent event isolation coverage and corrected line-aware context output trimming. Current verification reports **152 passed and 5 subtests passed**, `uv run ruff check harness tests` passes, `uv run python -m compileall -q harness` passes, and `uv build` produces both source distribution and wheel. A live local API startup also completed and `/health` returned HTTP 200. These results establish local runnability for this environment, not deployment safety or cross-platform guarantees.

These changes do **not** close every required change above. Before general public or multi-tenant production, the project still needs a real OIDC/JWT or trusted gateway identity layer, persisted tool-action approval lifecycle, distributed rate/concurrency/budget enforcement, crash-recoverable durable workers and leases, data retention/export/deletion and encryption policy, robust tenant-scoped artifacts and secrets, actual container/firewall integration evidence, security scanning and CI/release gates, and current MCP protocol conformance. Graph-continuation approvals are now persisted and independently granted, but they do not authorize individual tool effects. The API-key and local SQLite modes should be treated as a restricted single-instance profile. A passing test suite is not a penetration test and does not validate deployment infrastructure.

For the additional 2026 ecosystem and architecture gap analysis, see [DYNAMIC_HARNESS_EVOLUTION_AND_GAP_ANALYSIS.md](DYNAMIC_HARNESS_EVOLUTION_AND_GAP_ANALYSIS.md).

### Additional findings from the October 28, 2026 review

The follow-up research and direct code inspection identified additional issues that should be treated as launch blockers or feature-accuracy issues:

- **P1 — Caller-selected Python graph imports:** `RunCreateRequest.graph` accepts arbitrary strings, and `load_graph()` imports unknown names using `importlib.import_module(name)`. Remove arbitrary imports from API-facing loading; only resolve registered built-ins and administrator-approved declarative graphs. See the detailed risk and remediation in the dynamic harness gap analysis.
- **P0 — MCP subprocesses inherit service secrets:** `StdioMCPClient` starts every configured MCP command with a copy of the entire process environment. Replace it with a minimal allowlisted environment and broker only the credentials that server requires.
- **P0 — Graph approval labels are self-asserted:** **Addressed in the current working tree.** Caller labels no longer authorize resume. A separate authenticated same-tenant principal with an approver role issues an expiring one-use grant; storage atomically validates and consumes it against the paused run state and workflow-definition digest. Tests cover self-approval, wrong-role/cross-tenant approval, altered-state rejection, expiry, replay, and concurrent SQLite/in-memory consumption. This is graph-continuation approval only; it does not provide pending tool-action approval or independently verify operational identity-provider assurance.
- **P1 — Caller-selected Python graph imports:** **Addressed in the current working tree.** `load_graph()` resolves only built-ins or validated declarative manifests. Unknown names fail without import. Declarative manifests have bounded fields/file discovery, validated route references and capability names, and reject unsafe model-generated approval state. Each run stores the graph-definition digest and fails closed if the workflow changes before continuation. Tests cover import non-execution, malformed manifests, route capability validation, router approval-state forgery, unavailable tools, and version drift.
- **P1 — MCP calls can hang indefinitely:** the stdio client does blocking `readline()` without applying its timeout; its stderr pipe is not drained during normal operation. A hung or noisy server can tie up workers and degrade service availability.
- **P1 — Delegation and streaming do not meet their apparent contracts:** `agent.delegate` uses a few hard-coded prompt/agent branches and returns a completed-shaped result; the SDK buffers model token callbacks and emits them only after completion. Either implement the promised behavior or clearly disable/label the demo behavior.
- **P1 — Workspace memory/instructions are not tenant- or trust-scoped:** one workspace memory context is injected into SDK prompts, and workspace instruction files are elevated into system prompt context. Partition memory and distinguish operator policy from untrusted repository/user content.
- **P1 — Tool approval can be skipped by caller paths without a principal:** `ToolRegistry.invoke()` only enforces approval when a `TenantContext` exists. Require an explicit policy context for API, SDK, CLI, MCP, and background invocation paths; reserve implicit local privilege for a documented developer profile.

These findings, current AI workflow developments, citations, severity rationale, and full remediation recommendations are maintained in [DYNAMIC_HARNESS_EVOLUTION_AND_GAP_ANALYSIS.md](DYNAMIC_HARNESS_EVOLUTION_AND_GAP_ANALYSIS.md). The October 28 source scan also found emerging AAIF projects, A2A 1.0, OWASP ACS, updated MCP, recent reports of unintended agent actions, and new evidence about human deference to agent recommendations. They are treated as architecture signals; the document does not recommend adopting immature standards without independent review.

### Supplemental findings from the October 28, 2026 second pass

- **P0 — SDK event disclosure fixed in the working tree.** SDK turns now carry unique run IDs into delegate events, filter subscriptions to that run, and publish only bounded lifecycle metadata. Regression tests cover foreign-run suppression and absence of prompt/tool payloads. Verify isolation under real multi-principal concurrent API workloads before public deployment.
- **P1 — Provider recovery can bypass safety and data policy.** `ModelRegistry.complete()` retries all exceptions and then tries configured fallbacks. Classify provider failures; hard-stop safety/policy/authorization/quota errors; retry only transient errors; and authorize fallback against capability, data, residency, idempotency, and cost policy.
- **P1 — Long-running workflow continuity is incomplete.** A queue claim is not a durable execution lifecycle. Define crash recovery, leases, cancellation, idempotency, human waits, reconciliation, and structured task handoff/acceptance state.
- **Urgent operational item — eval portability.** OpenAI documents its Evals platform becoming read-only on 2026-10-31 and shutting down on 2026-11-30. Export any dependent eval assets immediately and keep canonical datasets, graders, and artifacts provider-neutral.
- **P1 — Protected wildcard checks fixed.** The shared registry guard now honors wildcard patterns such as `*.pem`, `*.key`, and `*credentials*.json` across read/write/list paths; new regressions cover private-key and credential patterns.
- **P1 — Installed config lookup fixed.** The bundled default is now resolved from the package directory used by the wheel, with a regression simulating an installed package layout.
- **P1 — CLI bind validation aligned.** The CLI now recognizes per-principal `auth.api_keys` configurations when validating a non-loopback bind; startup separately requires their configured key environment variables.
- **P2 — Late run-stream subscriptions fixed.** The SSE endpoint replays persisted trace events, uses sequence-based reads to reconcile queue races, and emits a terminal event for completed runs without one in the trace.

See the supplemental scan in the dynamic harness gap analysis for code evidence, source links, and detailed remediation. This review remains static; the newly identified concurrent event disclosure needs an isolated regression check before a public multi-user deployment.

The event disclosure now has isolated regression coverage, so the earlier sentence about a pending isolated check is superseded. Deployment-level concurrency and multi-principal checks remain open. Current local verification after the follow-up fixes: **156 passed and 8 subtests passed**, repository Ruff passes, compile and package build pass, and a live local `/health` request returned 200. These local checks do not replace infrastructure and external-service validation.

### Finding-by-finding implementation status

| Original finding | Status after implementation | Remaining work |
|---|---|---|
| P0 API authentication and unsafe bind defaults | **Partially addressed** | Loopback defaults, configured external-bind checks, API key principals, constant-time key comparison, and remote-peer rejection without credentials are implemented. OIDC/JWT, scopes, key rotation/revocation, audit, and trusted-proxy identity remain open. |
| P0 Docker backend silently falling back to host | **Addressed in code** | Docker selection fails closed and unsupported providers are rejected. Local mode is selected explicitly in the workstation config and runs with host-user privileges. Deployment isolation verification remains open. |
| P1 protected file read/search gaps | **Addressed in tool paths** | Canonical path checks, protected defaults, read/search limits, search pruning, database exclusions, and regression tests are present. Continue fuzzing path/symlink behavior across supported Windows/POSIX deployments. |
| P1 webhook signatures optional/reconstructed body | **Partially addressed** | GitHub/Slack raw-body signatures and Jira secret validation exist. Replay/delivery-ID deduplication, payload caps, and integration startup policy need completion and tests. |
| P1 SSRF through URL fetch/callback | **Partially addressed** | Central scheme/IP validation, DNS pinning, no redirects, callback host allowlist, and bounded response reads exist. Enforce egress at network infrastructure, define per-tool policy, and test proxy/content-type behavior. |
| P1 run APIs lack owner isolation | **Partially addressed** | Ownership is stored and checked for run reads, updates, resume/rewind/delete/result/trace paths. Artifact/checkpoint-level tenant partitioning, tenant deletion/export, encryption/key separation, and multi-tenant stress tests remain. |
| P1 rate limits, concurrency, budgets, RBAC unenforced | **Partially addressed** | Ingress rate/concurrency guards and tool RBAC/approval checks are wired. Rate state is process-local; durable provider usage accounting, hard token/cost budgets, distributed quotas, and tool-action approvals remain open. Graph-continuation approval is implemented separately. |
| P1 synchronous work blocks async API | **Partially addressed** | Blocking SDK/model/node/iterator work is offloaded in covered API paths. A bounded durable worker queue, cancellation propagation, child process tree termination, and provider pools remain. |
| P1 invalid RunConfig fields in stream/websocket | **Addressed** | Caller approval elevation is ignored; request limits and endpoint coverage were added. |
| P2 I/O limits do not bound actual reads | **Addressed in common file handlers** | Reads and recursive search are byte/entry bounded, and search prunes sensitive directories. Continue resource tests for adversarial filesystem trees and symlinks. |
| P2 SQLite lifecycle/retention | **Partially addressed** | WAL, busy timeout, migrations, foreign keys, and run deletion are implemented. Retention, tenant-wide deletion, export, backup/restore, encryption, and multi-node storage remain. |
| P2 duplicate concurrent continuation | **Addressed for queued claim** | Atomic queued-run claim prevents duplicate initial continuation. Running-job leases and crash recovery are still needed for worker failure. |
| P2 MCP/plugin discovery failures swallowed | **Open** | Discovery diagnostics and startup health reporting need implementation; custom MCP protocol version is also behind the 2026-07-28 specification. |
| P2 OpenAI compatibility silently drops fields | **Partially addressed** | Unsupported tool semantics are rejected and request fields are bounded. The supported protocol subset must be documented; true token usage, multimodal content, tool calling, and disconnect semantics are not implemented. |
| P2 errors/traces may disclose sensitive values | **Partially addressed** | Some API error paths are sanitized. A repository-wide redaction policy for logs, traces, stored prompts, headers, and tool output remains. |
| P2 package configuration inconsistent | **Addressed** | `config.yaml` is included in the built wheel. |
| P2 production delivery/support artifacts missing | **Open** | CI security gates, deployment guides, support/security policy, release automation, SBOM, dependency scanning, and incident/rollback procedures remain. |

### Critical workflow-boundary implementation update (2026-10-28)

This update supersedes earlier findings in this document that described caller-selected Python graph imports and caller-supplied graph approval labels as still open. Unknown graph names now fail without import; declarative graphs are bounded and validated; node tool use is checked against declared per-node capabilities; workflow discovery rejects symlink escapes; and graph execution has a step limit. Dynamic route failures stop safely instead of selecting an arbitrary fallback node, and router state updates have a total size bound. Runs pin the loaded graph plus execution-affecting configuration digest. Graph approvals require a separate same-tenant principal with an allowed role and are persisted as expiring one-use grants consumed atomically against the paused run context. Checkpoint rewind returns a run to the claimable `queued` state. See [ADVANCED_USE_CASE_SCENARIOS.md](ADVANCED_USE_CASE_SCENARIOS.md) for operator-facing behavior and `tests/test_secure_workflow_boundaries.py` / `tests/test_api.py` for regression coverage.

This does not make the entire dynamic harness public-production ready. Tool-effect approvals, OIDC/JWT identity, distributed rate/concurrency/budget enforcement, durable worker leases and crash recovery, data encryption/retention/export/deletion, and deployment-level isolation/egress verification remain open. **Verification for this update:** the full offline suite passes (180 tests and 12 subtests); Ruff passes for `harness` and `tests`; `compileall` and source/wheel builds pass. Repository-wide Ruff still reports eight pre-existing findings in `examples/demo_for_genai_leadership.py` and `scratch/verify_dedup.py`. Local checks are not a penetration test.

- This review inspected repository code and configuration statically. It did not execute tests, benchmark, launch the service, call external providers, inspect production infrastructure, or perform a penetration test.
- The repository contains substantial tests and eval assets, but their existence does not prove that the critical production controls above are covered or enforced.
- Findings should be converted into tracked issues, assigned owners, and acceptance criteria before a public launch decision.
