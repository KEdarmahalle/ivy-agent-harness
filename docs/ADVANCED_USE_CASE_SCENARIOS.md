# Advanced Use-Case Scenarios and Product Coverage

**Status:** Product capability assessment and proposed roadmap
**Assessment date:** 2026-10-28
**Current project version:** 0.1.0
**Audience:** Product, engineering, security, deployment, and design partners

### Secure dynamic workflow boundary update (2026-10-28)

Graph selection now resolves only the built-in graph registry and validated declarative YAML/JSON manifests. Unknown names never become Python import paths. Declarative manifests require an explicit declared start node, bounded file/node/route/capability lists, supported field types, valid in-graph routing targets, and registered tool capabilities. Unsupported or malformed manifests are rejected before graph execution. Runs persist a graph-definition digest and fail closed if the definition changes before continuation. Model-generated routing cannot write internal or approval state; agentic routing is rejected in workflows with human approval gates so it cannot route around them.

Runs created before this graph digest was persisted cannot continue or receive approval grants; recreate those runs under the current runtime.

Human approval now uses the authenticated `POST /runs/{run_id}/approval` route. A configured approver role (`governance.approval_roles`, default `approver` and `admin`) must be held by a principal in the run's tenant who is distinct from the requester. The persisted grant expires, binds to the exact run/input/state/pause/continuation and workflow-definition digest, and is consumed atomically on resume. The previous `approvals: [label]` request no longer authorizes a state transition. The grant ID is a bearer secret and must be returned to the run owner through an authenticated application channel.

These controls reduce two execution-boundary risks; they do not certify the service for public multi-tenant use. API-key identity, local SQLite, process-local quotas, data retention, encryption, worker recovery, deployment isolation, and infrastructure security remain launch gaps.

## Purpose

This document tests the Agent Harness against advanced, realistic agent workflows. For each scenario it states what the repository can do today, what prevents a reliable solution, and what a future version should add. It is deliberately evidence-based: a class, API route, graph, or configuration option is not treated as a finished product capability unless its execution and safety contract are implemented.

The harness is currently best understood as a **local and single-instance agent workflow runtime with graph, tool, model, and API adapters**. It is not yet a general-purpose autonomous operations platform or a public multi-tenant agent service. Its graph engine, file/process tools, model registry, HTTP API, SDK, webhooks, and swarm code provide useful primitives, but the scenarios below expose missing product-level contracts.

## Capability labels

- **Available for restricted use:** The main path exists and can be used within the stated local/trusted boundary. It is not a general production certification.
- **Partial:** A demonstrator or first execution path exists, but a critical workflow, safety, scale, or reliability contract is absent.
- **Not currently supported:** The repository does not provide the required product capability end to end.
- **Future candidate:** A next-version proposal, not an implemented feature or commitment.

## Current capability baseline

| Area | What exists in the repository | What the existence does not establish |
|---|---|---|
| Graph workflows | Built-in `code-investigate`, `research-report`, and `brainstorm` graphs; declarative YAML/JSON graphs; deterministic and model-selected routing. | A declarative graph is not automatically safe, validated, versioned, or suitable for arbitrary public users. Unknown Python graph names still have an import fallback in `harness/engine/graph_loader.py`; remove this authority path before accepting user-selected graph names publicly. |
| Run state | SQLite and in-memory stores, trace events, checkpoints, queued-run claiming, resume and rewind routes. | A queued claim is not a durable worker system. Running leases, heartbeats, process-death recovery, idempotent side effects, multi-node coordination, retention, and backup/restore remain incomplete. |
| Chat and tools | SDK/API loops, tool catalog and filters, bounded filesystem tools, process execution, URL fetching, output trimming, event streaming. | Broad tool availability is not equivalent to least privilege. The SDK does not provide a general policy-bound child-agent sandbox; streaming and cancellation behavior vary by path; hostile-workspace isolation needs deployment evidence. |
| Models | Configured routes and fallbacks through the model registry and provider adapters. | Fallback does not yet provide a complete provider capability, data-residency, safety-error, cost, or idempotency contract. A fallback provider is not guaranteed to preserve semantics. |
| Human intervention | Graphs can pause at `needs_human`; resume endpoints accept approval labels. | Labels supplied by the run owner are not independent, action-bound approval. There is no complete persisted approver identity, policy scope, approval expiry, separation-of-duties, or immutable action-digest lifecycle. Do not use this as authorization for high-impact actions. |
| Multi-agent work | `SwarmCoordinator` supports configured personas and fan-out/handoff execution. `agent.delegate` has hard-coded behavior branches. | A configured swarm is not an arbitrary dynamic agent runtime. `agent.delegate` does not launch a general child agent with isolated policy, budget, memory, cancellation, and trace. |
| Integrations | GitHub, Slack, and Jira webhook/worker paths; MCP registry and stdio support; plugins and skills. | No general durable trigger catalog, delivery dedupe, dead-letter/replay lifecycle, secure public extension marketplace, current protocol-conformance proof, or integration-level tenant credential broker. |
| Quality | Unit/API tests, fixed-case graph eval runner, verification-gate utilities. | No provider-neutral stochastic agent eval program with quality/safety/cost thresholds and release canaries. A fixed graph case suite does not establish model behavior quality. |
| Identity and operations | API-key principals, ownership checks, loopback defaults, process-local rate limiting, health endpoint, local secret store. | This is not a complete OIDC/JWT platform, distributed quota service, production readiness system, tenant key isolation, secrets broker, observability/SLO stack, or infrastructure security proof. |

Code references: [graph loader](../harness/engine/graph_loader.py), [declarative workflow engine](../harness/declarative_graphs.py), [run runner](../harness/engine/runner.py), [SDK](../harness/sdk.py), [swarm coordinator](../harness/swarm.py), [API](../harness/api/main.py), [production readiness review](PRODUCTION_READINESS_REVIEW.md).

## Scenario portfolio

| # | Advanced scenario | Current coverage | Can the product safely solve it today? | Recommended target |
|---:|---|---|---|---|
| 1 | Repository root-cause investigation and test reproduction | Usable for a trusted local workspace with bounded inputs and sandbox policy | **Conditionally**, for trusted local workspaces; caller-supplied commands remain executable code and are not appropriate for arbitrary public input. | Isolated workers and reproducible environment remain future work |
| 2 | Pull-request review and patch proposal | Partial GitHub webhook/worker plus swarm components | **Partially**; review can be demonstrated, but delivery idempotency, precise permission scopes, and verified patch publication need work. | 0.3 |
| 3 | Production incident triage and containment | Partial run graphs, webhooks, tools, and model routing | **No** for autonomous containment; no durable incident workflow, verified integrations, or action-bound approval. | 0.3 after 0.2 controls |
| 4 | Release and infrastructure change management | Graph gates and process tools | **No** for protected production changes; current approval labels do not establish independent authorization. | 0.3 |
| 5 | Evidence-backed research and decision brief | Bounded source records, secure URL fetch adapter, exact excerpt validation, source-linked claims | **Partially**; excerpts establish source linkage, not logical entailment, freshness, or completeness. | Independent entailment/freshness assessment and connector policy remain future work |
| 6 | Governed analytics over operational databases | Read-oriented SQL tool and filesystem access | **Partially** for a local trusted SQLite file; not for governed warehouse access or sensitive multi-tenant data. | 0.3 |
| 7 | Event-driven internal operations (tickets, chat, repository events) | GitHub, Slack, Jira webhook handlers and workers | **Partially** for demonstrations; no common reliable trigger, dedupe, replay, and delivery contract. | 0.3 |
| 8 | Customer support with account-aware retrieval and actions | Generic graphs/tools only | **No.** No retrieval connector, user/account authorization model, policy-aware action broker, or conversation service exists end to end. | Later product vertical; foundation in 0.2/0.3 |
| 9 | Multi-tenant hosted agent service | API keys, run ownership, SQLite | **No** for public SaaS. Identity, distributed limits, tenant-scoped artifacts/secrets, key rotation, and multi-node run coordination are incomplete. | 0.2 blocker |
| 10 | Long-running, multi-session project execution | Checkpoints and resumable graph state | **Partially**; no robust leases, crash recovery, durable schedule/timers, or reliable handoff/acceptance ledger. | 0.2 blocker |
| 11 | Dynamic planning, routing, and specialist delegation | Declarative graphs, agentic router, configured swarms | **Partially**; the graph supervisor can choose among declared nodes, while general `agent.delegate` is a stub-like fixed dispatcher. | 0.3 after capability contracts |
| 12 | Multimodal voice, image, and browser/computer-use agent | Text-centric model/tool interfaces | **No.** No end-to-end multimodal content, browser-control, or voice session contract. | Future candidate after provider-neutral API work |
| 13 | Third-party MCP/plugin/skill ecosystem | MCP stdio, plugin discovery, workspace skills | **No** as a public ecosystem. Extension provenance, isolated validation, versioning, permission manifests, revocation, and conformance are missing. | 0.3+ |
| 14 | Model/prompt/tool upgrade regression and cost optimization | Fixed eval cases and a budget utility | **No** as a release gate. No representative multi-trial workflow evals, actual per-run spend reservation, or canary thresholds. | 0.2 foundation |

## Detailed scenario analyses

### 1. Repository root-cause investigation and test reproduction

**User goal:** A developer provides a failing behavior, a repository, and optionally a test command. The agent locates relevant code, builds a hypothesis, runs the reproduction, and returns evidence and a confidence-qualified verdict.

**Current path:** `code-investigate` searches files, reads nearby lines, applies a protected-path gate, optionally runs a caller-supplied command, and returns evidence plus a score. File and process tools enforce workspace restrictions and configured resource bounds. The SDK and CLI can target a project workspace.

**Coverage:** **Available for restricted use.** Inputs are validated before search; result count, command length, and timeout are bounded. The active sandbox policy protects mandatory and configured paths. A protected-path review pause resumes only at scoring, never at command execution. A command's exit code is explicitly evidence of execution outcome, not proof of the hypothesis.

**Limits:** A caller-supplied command is still executable code. Isolation depends on selected sandbox backend and deployment configuration. Search skips protected content; if no readable match exists while protected candidates are present, the graph can pause conservatively without exposing their names or bytes. A review acknowledgment cannot authorize a command or reveal protected content. The workflow does not guarantee hermetic dependencies, stable test data, deterministic model output, cleanup, or a complete patch review. Run state does not yet provide full worker crash recovery. The public graph loader must not permit caller-controlled Python module imports.

**Next-version solution:** Add an explicit investigation profile: read-only by default; command allowlist and immutable workspace snapshot; isolated worker per run; pinned interpreter/dependency environment; bounded CPU, memory, disk, process tree, and network; generated reproduction artifact; patch proposal as a separate approval-bound action. Store repo commit, config/model/prompt versions, commands, outputs, and evidence in a replayable run manifest.

**Acceptance evidence:** Tests show the same pinned checkout and command are run; the worker cannot read outside its mount or reach unapproved network targets; no patch is applied without a signed approval bound to its diff hash; a killed worker can be safely resumed or reported as interrupted without repeating completed side effects.

### 2. Pull-request review and patch proposal

**User goal:** A signed repository event starts a review, the agent inspects a bounded diff and related code, runs targeted checks, and posts a concise finding set. A patch may be proposed separately.

**Current path:** GitHub webhooks and worker functions exist. The `SwarmCoordinator` has code-audit/security personas, and API routes expose agent/swarm execution.

**Coverage:** **Partial.** The integration path can demonstrate review work, but it is not yet an enterprise-grade GitHub app or safe autonomous patch publisher.

**Limits:** Delivery deduplication/replay and durable job state are incomplete. The hard-coded swarm catalog is not a permissioned per-repository agent policy. PR content is untrusted prompt input. Review findings and model output need a stable schema and evidence validation. Posting comments and writing commits are distinct side effects but do not have a shared action-bound approval protocol. Rate/concurrency limits are process-local.

**Next-version solution:** Add an installation-based connector with repository-level scopes, webhook delivery ID dedupe, durable queue/dead letter, repo/branch policy, diff-size bounds, untrusted-content isolation, deterministic finding schema (path, line, evidence, severity, confidence), and model-free validators for line ranges and secret redaction. Make patch generation produce a review artifact; require an approver to authorize a diff digest before opening or updating a branch.

**Acceptance evidence:** Replayed webhooks produce one run; cross-repository credentials cannot be selected by a model; a PR comment only contains validated findings; a patch cannot be pushed after the approved diff changes.

### 3. Production incident triage and containment

**User goal:** An alert starts a workflow that correlates logs, traces, deploy history, and runbooks; forms ranked hypotheses; requests targeted diagnostics; and applies a mitigation only when policy and an authorized human permit it.

**Current path:** The graph engine, source fetch, local tools, traces, and integration webhook patterns are composable building blocks.

**Coverage:** **Not safe for autonomous containment.** There is no complete alert connector catalog, incident data model, durable event workflow, or action policy service.

**Limits:** No deduplicated alert-to-run lifecycle, tenant-aware telemetry adapters, query budgets, blast-radius boundaries, SLO-based escalation, or automatic rollback proof. A model route fallback could use a provider with different data terms. Graph `needs_human` and caller-supplied approval labels are insufficient for production mitigation.

**Next-version solution:** Treat incidents as durable state machines with explicit stages: intake, enrich, diagnose, propose, approve, execute, verify, close. Integrate read-only telemetry first; enforce per-service scopes and query budgets; show evidence and uncertainty; require separate incident commander authorization bound to the exact mitigation and target. Execute mitigation through a connector with idempotency key, timeout, precondition check, compensating action, and post-action health verification.

**Acceptance evidence:** Replay, duplicate alerts, partial observability failure, provider outage, and worker restart do not duplicate actions. Simulated actions cannot escape the declared service scope. No production mutation occurs before independently recorded approval.

### 4. Release and infrastructure change management

**User goal:** Prepare a release plan, run validation, request change approval, deploy to a specified environment, verify health, and roll back if post-deploy checks fail.

**Current path:** Declarative nodes can sequence models/tools and pause at a human gate. `process.run` can invoke commands in a configured sandbox.

**Coverage:** **No for protected production changes.** The primitives are suitable for a local demo or non-production rehearsal only.

**Limits:** Approval is not bound to a canonical action, approver role, environment, expiry, or artifact digest. Deployment credential brokerage, environment promotion rules, artifact signing, provenance, rollback contract, and durable post-deploy monitoring are absent. A prompt or graph config can describe a gate without making it authoritative.

**Next-version solution:** Add environment-scoped deployment connector interfaces; immutable release manifests; signed artifact provenance; policy checks for change window, tests, ownership, and environment; two-person approval for production; deployment idempotency and rollback actions; continuous health verification with bounded observation window. Keep model-generated plans advisory; deterministic policy decides whether an action is allowed.

**Acceptance evidence:** Changing the commit, target, or artifact invalidates approval. An unapproved production action is denied by the connector even if the model calls it directly. Rollback is rehearsed and observable.

### 5. Evidence-backed research and decision brief

**User goal:** Given a question, gather sources from approved web/internal connectors, compare claims, identify unknowns, and create a cited brief suitable for a decision.

**Current path:** `research-report` validates and bounds source/question inputs before network calls, uses the outbound URL validation and DNS-pinned fetch adapter, asks for structured claims with exact excerpts, validates source IDs and quote presence, and marks accepted claims `source_linked`. Source hashes and counts appear in traces rather than source bodies.

**Coverage:** **Partial.** Citation-to-source linkage is checked mechanically. The gate returns `CONCERNS` for source-linked claims and `NEEDS_HUMAN` for absent or malformed evidence. The workflow does not establish that a quote logically entails its claim or that source coverage is sufficient.

**Limits:** Exact quote presence is not entailment. Source trust, freshness, duplicate detection, publication metadata, internal connector access control, and source coverage assessment remain absent. Run input/state currently persist source text in SQLite and do not have a general encryption/retention mechanism; treat this as a restricted local-profile feature for non-sensitive content.

**Next-version solution:** Add source records with retrieval time, publisher, trust class, canonical URL, and explicit source policy. Add an independent calibrated entailment grader with abstention, citation-coverage checks, retrieval freshness controls, and inspectable redacted retrieval manifests. Encrypt sensitive run inputs/state and add retention/deletion controls before handling sensitive research content.

**Acceptance evidence:** Every factual claim has resolvable support or is labeled inference/unknown; URLs are not fetched outside policy; stale-source policy is explicit; repeated eval questions track coverage, citation precision, and unsupported claim rate.

### 6. Governed analytics over operational databases

**User goal:** An authorized analyst asks a question over approved operational data and receives an answer with query, data lineage, limits, and privacy controls.

**Current path:** `data.sql_query` supports read-oriented SQLite queries, with tool-role policies and query restrictions; filesystem sandboxing can expose local database files to that tool.

**Coverage:** **Partial** for local test/sample data. It is not a governed data warehouse agent.

**Limits:** There is no row/column-level policy engine, semantic catalog, query planner with cost estimate, query timeout/row-byte cap contract across all backends, data classification, audit lineage, or authorization propagation into a remote warehouse. Read-only SQL can still expose sensitive rows if the caller can reach the database.

**Next-version solution:** Add connector-specific identity passthrough, dataset/table allowlists, row and column masking, query AST validation, explain/cost threshold, execution timeout, result redaction, and lineage records. Distinguish a safe analytic query tool from raw database file access. Require explicit data owner approval for restricted datasets.

**Acceptance evidence:** Every query runs under the requesting principal's data grants; policy filters are enforced in the connector; excessive queries fail before execution; audit records capture dataset, query hash, row count, and policy decision without persisting sensitive result values by default.

### 7. Event-driven internal operations

**User goal:** Repository, ticket, or chat events start version-pinned workflows that can retry, pause for approval, resume, and expose delivery history.

**Current path:** GitHub, Slack, and Jira webhook routes and worker functions exist. Graph runs can be started through APIs.

**Coverage:** **Partial.** Individual integration demos exist; there is no general event automation product lifecycle.

**Limits:** No shared event envelope, connector-specific identity isolation, idempotent delivery ledger, retry schedule, dead-letter queue, replay UI/API, workflow version pinning at trigger time, or sustained queue worker lifecycle. Callback delivery and background tasks are not a durable scheduler.

**Next-version solution:** Introduce a trigger registry with versioned event schemas, signature verification, dedupe key, tenant/connector identity, trigger-to-workflow binding, retry/dead-letter policy, replay authorization, and run correlation. Store both event receipt and workflow version before acknowledging the delivery. Define delivery semantics as at-least-once with idempotent effects unless a connector proves stronger guarantees.

**Acceptance evidence:** Duplicate/reordered events create deterministic outcomes; poison events do not block healthy tenants; replay is auditable and permissioned; connector credential rotation does not alter historical run identity.

### 8. Customer support with account-aware retrieval and actions

**User goal:** Answer a customer using approved account-specific knowledge, cite applicable policy, update a case or issue a refund only within authority, and transfer ambiguous/high-risk cases to a human.

**Current path:** Generic chat tools, model routes, API principals, and webhook patterns could be composed into a prototype.

**Coverage:** **Not currently supported end to end.** There is no customer conversation product, retrieval/CRM connector, account-level authorization, or secure refund/action workflow.

**Limits:** Tenant identity currently does not equal customer record authorization. No retrieval ACL propagation or document freshness controls. Refunds and account changes need policy checks, amount limits, dual approval, and action receipts. Prompt injection in tickets/attachments must stay outside trusted policy instructions.

**Next-version solution:** Build a support adapter only after the identity and policy foundation: authenticated customer/account context; retrieval filters derived by server-side identity; citations and policy versions; action tools with typed schemas, preconditions, idempotency, risk scoring, and approval; mandatory human escalation for defined risk classes. Do not allow the model to select account IDs or roles as authority.

**Acceptance evidence:** Cross-account retrieval tests, prompt-injection tests, stale-policy tests, duplicate refund requests, and missing-identity cases all fail closed. Support agents can explain which evidence and policy version justified an answer or action.

### 9. Multi-tenant hosted agent service

**User goal:** Several organizations run private workflows through a shared API with isolated data, tools, model credentials, budgets, and operator access.

**Current path:** Per-principal API keys, run ownership fields/checks, local SQLite persistence, and process-local request/concurrency limiting exist.

**Coverage:** **Not ready for public multi-tenant use.** Treat the current profile as restricted single-instance service.

**Limits:** No OIDC/JWT federation, project scopes, distributed quota state, encryption/key separation, tenant-isolated memory/artifact stores, durable worker tenancy, admin audit, key revocation lifecycle, or infrastructure-enforced tenant network separation. Shared process globals and extension runtimes require careful isolation.

**Next-version solution:** Implement an identity adapter and authorization policy contract; move runs, traces, artifacts, memory, connector secrets, and usage ledger behind tenant-scoped repositories; use distributed quota counters and worker leases; use per-tenant encryption keys and scoped short-lived run capabilities; add tenant boundary threat tests and cross-tenant canaries. Make local API-key mode an explicit development profile.

**Acceptance evidence:** A tenant cannot discover or access another tenant's run, artifact, secret, event, model usage, or connector. Restarting/scaling workers does not reset quotas or duplicate a run. Revoked identities stop active and subsequent tool use according to policy.

### 10. Long-running multi-session project execution

**User goal:** An agent works for hours/days, survives process restarts, waits on a human or external event, continues from verified progress, and knows when acceptance criteria are actually complete.

**Current path:** SQLite checkpoints, trace history, pause/resume statuses, and declarative graph state are available.

**Coverage:** **Partial.** Persistence exists, but full durable execution semantics do not.

**Limits:** A queued-run claim does not renew a lease or recover a worker that dies after claiming. Timers, cancellation, approval expiry, retry scheduling, dead-letter handling, replay-safe activities, and structured session handoff are absent or incomplete. A stored graph name does not freeze its code/config/provider version.

**Next-version solution:** Persist an immutable run manifest, activity ledger, versioned workflow, and acceptance checklist. Add lease/heartbeat, durable timers, cancellation, per-activity idempotency, retry budgets, external wait callbacks, and operator replay. At each session boundary, write a machine-readable handoff plus evidence; on resume, reconcile actual workspace and external side-effect state.

**Acceptance evidence:** Kill workers at every state transition and verify safe recovery; ensure external effects do not repeat; resume old runs against pinned workflow/config versions; require all acceptance items to have evidence before `done`.

### 11. Dynamic planning, routing, and specialist delegation

**User goal:** Given a goal, an agent chooses a safe plan, selects declared capabilities, delegates bounded subtasks, evaluates results, and changes course when evidence requires it.

**Current path:** Declarative graphs support deterministic conditionals and an AI route decision among declared graph nodes. `SwarmCoordinator` supports configured agent personas with fan-out/handoff. The chat `agent.delegate` tool is implemented with a small set of prompt/agent branches.

**Coverage:** **Partial.** There is dynamic routing within a declared graph and limited configured multi-agent execution. This is not arbitrary self-assembling agent teams or general child-run delegation.

**Limits:** Candidate descriptions and state projections are not a full capability policy; graph schemas and tool contracts are weakly validated; the delegate stub may return a completed-shaped response without doing the requested general task; per-child identity, memory, budget, cancellation, deadlines, and result verification are missing. Model-selected edges must never grant new authority.

**Next-version solution:** Define a versioned workflow/agent manifest with typed input/output schemas, declared capabilities, side-effect class, model constraints, data labels, budget, and SLA. Let the planner select only among server-authorized capabilities. Implement child runs with distinct traces, scoped tools, inherited restrictions, independent budgets, deadlines, and structured result contracts. Keep “plan”, “authorize”, and “execute” as separate steps.

**Acceptance evidence:** Router fuzzing cannot leave declared candidates; child agents cannot exceed delegated capability; planner errors fall back safely; delegated completion requires task-specific validation and never reports success solely because a tool call returned.

### 12. Multimodal voice, image, and browser/computer-use agent

**User goal:** Accept voice/image/screen context, keep a real-time session, use browser/computer actions, and recover or stop safely when the environment changes.

**Current path:** Text-oriented model requests, HTTP fetching, chat streams, and WebSocket chat exist.

**Coverage:** **Not currently supported end to end.** A WebSocket or URL fetcher is not a real-time voice stack or computer-use runtime.

**Limits:** No common multimodal content model, audio session state, image artifact lifecycle, browser DOM/screenshot action interface, visual grounding, interruption model, or action verification. Arbitrary web fetching does not establish browser isolation or user consent.

**Next-version solution:** First add a provider-neutral content/event contract and artifact store. Then implement one isolated modality adapter at a time, beginning with image inputs or voice transcription rather than broad computer control. For computer actions, use an isolated browser session, strict domain policy, visible action preview, cancellation, and independent state verification.

**Acceptance evidence:** Content and artifacts are tenant scoped and retention controlled; audio interruption/cancellation works; browser actions are bounded to approved origins; visual state changes are verified before consequential follow-up actions.

### 13. Third-party MCP, plugin, and skill ecosystem

**User goal:** Install trusted extensions that declare what they can read, write, call, and cost; validate and stage them; revoke or roll back them without restarting the whole service unsafely.

**Current path:** MCP stdio discovery, plugin discovery, skills, and configurable tools exist.

**Coverage:** **Not ready as a public extension ecosystem.** These are runtime extension points, not a safe marketplace or trust framework.

**Limits:** Current MCP client compatibility and lifecycle need conformance work. Extension discovery errors have historically been easy to miss. Child process environment, package provenance, permission manifests, dependency pinning, code isolation, health checks, signing, revocation, and rollback need a unified control plane.

**Next-version solution:** Define signed extension bundles with immutable version/digest, publisher identity, dependency lock, declared permissions, data handling, network destinations, and tool schemas. Validate in an isolated staging environment; require operator activation; issue per-run short-lived capabilities; record every extension version in the run manifest; provide immediate disable/revoke and rollback.

**Acceptance evidence:** Malicious extension fixtures cannot read unrelated secrets or access undeclared network/path capabilities; version changes are detected; revoked extensions cannot continue on new runs; required vs optional integration health is visible.

### 14. Model, prompt, and tool upgrade regression

**User goal:** Change a model, prompt, route, tool, or workflow and know whether quality, safety, latency, cost, and side effects improve or regress before release.

**Current path:** `evals/cases.json`, a small graph eval runner, and automated tests provide initial regression infrastructure.

**Coverage:** **Partial for deterministic graph behavior; not sufficient as an agent release gate.**

**Limits:** No broad representative real-task corpus, adversarial safety suite, repeated stochastic trials, judge calibration, cost reservation/reconciliation, provider capability checks, model/prompt version pinning, or canary/rollback controller. Tests of deterministic code do not measure model behavior drift.

**Next-version solution:** Create a provider-neutral evaluation format containing task, permitted tools/data, expected observable outcomes, safety invariants, graders, and version metadata. Run repeated trials and grade complete traces plus external side effects. Track task success, unsupported claims, policy violations, tool choice, latency, input/output tokens, and actual spend. Require thresholds and human review for high-impact regressions.

**Acceptance evidence:** Every production model/prompt/tool change has comparable baseline and candidate runs; safety regressions block promotion; variance is reported; spend is reserved before calls and reconciled afterwards.

## Recommended next-version plan

The sequence below is a dependency order. Do not sell or enable the later scenarios merely because the code shape exists.

### Proposed 0.2 — Safe, durable runtime foundation

1. Remove arbitrary module imports from public graph selection; validate declarative workflow schemas before activation.
2. Deliver action-bound, independently authenticated approvals with expiry, approver scopes, immutable request digests, and revalidation immediately before execution.
3. Add durable worker leases/heartbeats, crash recovery, cancellation, idempotency keys, replay/dead-letter handling, and version-pinned run manifests.
4. Complete tenant identity, authorization, distributed limits, budgets, scoped secrets/artifacts, retention, deletion/export, and audit lifecycle.
5. Normalize provider capabilities and typed errors; stop safety/authorization failures from retrying or falling through; authorize fallbacks against data and route policy.
6. Enforce per-run resource and network boundaries in the actual deployment environment; add a supportable readiness model and production runbooks.
7. Add provider-neutral eval records and release gates for security, quality, cost, and latency.

**Exit condition:** No advanced scenario requiring production mutation or multiple tenants can bypass policy, approval, resource, durability, and audit controls. Worker crash and replay tests pass in supported deployment profiles.

### Proposed 0.3 — Reliable event integrations and composable workflows

1. Add a durable trigger/connector registry with delivery IDs, dedupe, retries, replay, dead letter, per-tenant credentials, and workflow version pinning.
2. Replace the `agent.delegate` stub with scoped child runs and structured task contracts, or remove/disable the tool in production.
3. Add real-time streaming/backpressure/cancellation guarantees across SDK, SSE, WebSocket, and OpenAI-compatible endpoints.
4. Complete GitHub PR review flow as a read-only finding product first, then add diff-hash-bound patch proposal and approval.
5. Add verified source/citation records to research workflows and allowlisted internal source connectors.
6. Add workflow manifests, typed inputs/outputs, capability declarations, static graph checks, compatibility versions, and staged activation.

**Exit condition:** Duplicate events are safe, child runs are constrained, workflow upgrades are reversible, and operational traces demonstrate correct approval, cancellation, and replay behavior.

### Later verticals — Choose after platform evidence

- Incident response and release management after independent approvals and idempotent connectors.
- Customer support after account-scoped identity, retrieval ACLs, and action verification.
- Governed analytics after connector-level data policy and query cost controls.
- Multimodal/browser experiences after the artifact, content, session, and isolation contracts are stable.
- Public extension ecosystem only after signed provenance, sandboxing, capability grants, revocation, and conformance gates.

## Cross-scenario product requirements

Any scenario promoted from restricted use to a public product capability must define:

1. **Actor and authority:** Who requested it, what identity was verified, what resources that identity may access, and which actions require another approver?
2. **Data contract:** What data enters the model, where it is stored, how it is classified, which provider receives it, how long it persists, and how it can be deleted/exported?
3. **Effect contract:** Which operations are read-only, reversible, idempotent, externally visible, financial, or safety critical? What happens if execution stops mid-action?
4. **Execution contract:** Deadlines, quotas, cancellation, retries, worker loss, concurrency, backpressure, provider failure, and expected delivery semantics.
5. **Evidence contract:** How are sources, tool calls, inputs, outputs, model/provider versions, approvals, and postconditions captured and redacted?
6. **Quality contract:** What measurable outcome defines success, which safety failures are release blockers, and how is stochastic variance handled?
7. **Operational contract:** Readiness, dashboards, alerts, retention, support ownership, rollback, and incident response.

If a scenario cannot answer these questions, keep it experimental or local-only and make the limitation visible to users.

## Product claims to avoid for now

Until the corresponding acceptance evidence exists, do not describe the product as:

- A fully autonomous agent platform that safely self-assembles arbitrary agents or tools.
- A production-grade multi-tenant SaaS runtime.
- A durable workflow system that guarantees restart-safe execution and exactly-once side effects.
- A secure public plugin/MCP marketplace.
- A fully citation-verified research system.
- A safe autonomous deployment, incident mitigation, refund, or account-management agent.
- A full multimodal or browser/computer-use harness.

Use precise claims instead: configurable graph workflows, model/tool adapters, local run traces and checkpoints, limited configured swarm patterns, and a developing API/SDK with an explicitly restricted deployment profile.

## Decision summary

The product already has a credible foundation for **developer-assistance and controlled internal workflow prototypes**. The strongest near-term use cases are local repository investigation, limited PR review, source-assisted research prototypes, and simple configured graph workflows. The best next investment is not a larger catalog of agents: it is the shared safety and operations foundation in 0.2, followed by reliable event integrations and real scoped delegation in 0.3. High-impact domains should remain out of scope until those controls are implemented and tested in deployment.
