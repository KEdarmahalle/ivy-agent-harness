# Ivy Agent Harness: Configuration and Integration Guide

This is the developer-facing guide to configuring and embedding the harness as it
exists in this repository. It covers the CLI, Python SDK, REST API, model routing,
tools, declarative workflows, MCP, storage, secrets, and the operational limits
that matter when integrating the runtime into another application.

## 1. What the harness provides

The runtime composes these parts:

1. **Model registry** resolves a graph/node to a named provider/model route and
   supports explicitly configured retry and fallback routes.
2. **Tool registry** combines built-in tools, enabled capability packs, MCP tools,
   and plugin tools, then applies allowlists and execution policy.
3. **Agent loop** builds context, asks the selected model for a response, executes
   allowed tool calls, and returns text, structured tool-call records, and stream
   events. The CLI and SDK expose the interactive loop; graph runs are handled by
   the graph runner.
4. **Graph runtime** runs built-in or validated declarative workflows and stores
   run state and trace events through the configured store.
5. **Adapters** expose CLI commands, a Python SDK, REST/SSE/WebSocket routes, an
   OpenAI-compatible API, MCP server/client support, and optional Langfuse tracing.

The project is configurable, but not every declaration in `config.yaml` implies
that an enforcement path is active. Read the implementation notes in the
[configuration reference](#3-configuration-reference) and security section
before treating a setting as an access-control boundary.

## 2. Install and run

Python 3.11 or newer is required. From a checkout:

```powershell
uv sync --extra dev
.venv\Scripts\Activate.ps1
ivy models
ivy tools
```

For a first model request, configure the provider key in the local secret store
(see [Secrets](#6-secrets-and-provider-credentials)), then call a configured
route:

```powershell
ivy chat --graph gemini --node flash --once "Reply exactly: READY"
```

To start an interactive session, omit `--once`. To run the API locally:

```powershell
ivy serve --host 127.0.0.1 --port 8000
```

The CLI accepts global `--config PATH`, `--secret-store PATH`, `--base-url URL`,
and `--api-key VALUE` options before the command. Avoid passing secrets through
command-line arguments because process listings and shell history may expose
them.

## 3. Configuration reference

The checked-in [`config.yaml`](../config.yaml) is the complete sample. YAML is
loaded with `yaml.safe_load`; JSON-compatible YAML is also accepted. Use a
separate deployment config rather than editing defaults in library code.

### 3.1 Config discovery and precedence

When no non-default explicit config path is supplied, `load_config()` deep-merges
the discovered files in this order (later values override earlier values):

1. Package-bundled `harness/config.yaml`, when installed.
2. Source-checkout root `config.yaml`.
3. User `~/.ivy/config.yaml`.
4. User `~/.harness/config.yaml`.
5. Workspace `.harness/config.yaml`.
6. Workspace root `config.yaml`.

Maps merge recursively; lists and scalar values are replaced as a whole. Passing a
non-default explicit path via `--config` or `config_path=` loads that one file
without merging discovered files, and a missing explicit file raises an error.
This distinction is useful for isolated deployment configs and tests.

### 3.2 Top-level keys

| Key | Purpose | Main consumers |
| --- | --- | --- |
| `app` | Display name and version | CLI and terminal UI |
| `database.sqlite_path` | SQLite run/checkpoint store path | Graph API and runner |
| `auth` | API credentials and principal defaults | REST/API middleware |
| `api` | Bind host, CORS origins, callback host allowlist | REST/API middleware |
| `secrets.store_path` | Local secret-store path | CLI secret commands and secret loader |
| `models` | Named provider/model routes | Model registry |
| `packs` | Enable/disable built-in capability packs | Tool registry |
| `tools` | Allowlist, protected paths, built-in overrides, sandbox policy | Tool registry and prompt context |
| `graphs` / `workflows` | Graph metadata and declarative workflow manifests | Graph loader |
| `webhooks` | Integration secrets and webhook settings | Integration endpoints |
| `governance` | Request limits, budgets, role-to-tool declarations, approval roles | API and governance components; see caveats below |
| `chat` | Default route, prompts, loop limits, streaming, approval, UI | CLI, SDK-adjacent defaults, and API |
| `mcp_servers` | MCP subprocess connection definitions | MCP registry |
| `observability.langfuse` | Optional Langfuse tracing switch | Observability adapter |

### 3.3 Model routes

Every key under `models` is a route name. A graph node first resolves to
`<graph>.<node>`, then to `<graph>`, then to `default`.

```yaml
models:
  default:
    provider: fallback
    model: deterministic-local
  assistant.fast:
    provider: litellm
    model: gemini/gemini-3.5-flash-lite
    api_key_env: GEMINI_API_KEY
    temperature: 0.2
    max_tokens: 4096
    timeout_seconds: 60
    retry_count: 1
    retry_delay_seconds: 1
    fallbacks: [assistant.backup, default]
```

Supported providers in this version: `fallback`, `litellm`, `nvidia_nim`, and
`nim`. `api_key` inline in the config is rejected; configure `api_key_env` to
name an environment variable populated by the secret loader. Temperature is
bounded to 0–2; token and timeout limits must be positive. Fallback names must
refer to configured routes to work. Keep fallback chains intentional: a
deterministic fallback may complete without exercising the hosted model.

Route settings include `provider`, `model`, `api_key_env`, `api_base`,
`temperature`, `max_tokens`, `timeout_seconds`, `retry_count`,
`retry_delay_seconds`, and `fallbacks`. Use `ivy models` or `GET /models/routes`
to inspect sanitized route metadata; these surfaces report whether a key is
available, not its value.

### 3.4 Tools, packs, and execution

`packs` controls whether groups of built-ins are registered. The sample pack names
are `core`, `filesystem`, `data_analytics`, `web_network`, `shell_sandbox`, and
`skills`. `tools.allowlist` is an additional name-level filter. If the allowlist
is absent or empty, the registry treats it as unrestricted; for deployments,
define an explicit least-privilege list.

`tools.builtins.enabled`, `tools.builtins.disabled`, and
`tools.builtins.definitions` control built-in registration and metadata overrides.
Use the runtime catalog (`ivy tools` or `GET /tools`) to see what actually
registered. Common built-ins include `file.list`, `file.read_text`,
`file.read_lines`, `file.search_text`, `file.write_text`, `file.replace_text`,
`process.run`, `sandbox.run_python`, `sandbox.inspect`, `web.fetch_url`,
`environment.info`, and `tool.catalog`.

The `tools.sandbox` settings are:

| Setting | Meaning |
| --- | --- |
| `provider` | `local` or `docker`; unknown values fail configuration |
| `image`, `memory_limit`, `cpu_limit`, `network_mode` | Docker provider parameters |
| `workspace_roots`, `writable_roots` | Path roots for file tools and provider policy |
| `network_enabled` | Enables the `web.fetch_url` capability |
| `max_read_bytes`, `max_write_bytes`, `max_output_chars` | Tool I/O bounds |
| `default_timeout_seconds` | Maximum tool command/Python execution time |

`tools.protected_paths` augments built-in protected path patterns; it does not
remove the hard-coded baseline protections. The local provider executes commands
as the operating-system user running Ivy. Its workspace roots constrain file
tools, **not arbitrary shell commands**. Docker is optional and must be installed
and configured if selected. A Docker container is not a substitute for a reviewed
isolation architecture.

### 3.5 Chat and agent-loop controls

Important `chat` keys:

- `graph`, `node`: default route selection.
- `agent_name`, `system_prompt`: display name and base instruction.
- `max_history_messages`: retained chat history limit.
- `streaming.enabled`, `live_tokens`, `show_token_rate`: output streaming/UI.
- `agent_loop.enabled`: allow model/tool multi-step loop.
- `agent_loop.max_tool_steps`: loop tool-step cap.
- `agent_loop.max_tokens`: completion token limit.
- `agent_loop.continuation_on_truncation`, `compact_threshold_messages`:
  context continuation and compaction controls.
- `agent_loop.auto_approve_tools`: approval setting read by the CLI and API
  principal setup.
- `agent_loop.tool_calling_mode`: `auto` or the configured tool-call mode.
- `agent_loop.tool_budget_prompt`: instruction presented at the tool limit.
- `ui.*`: terminal presentation settings only.

The sample config currently sets it to `true`, which is suitable only for a
deliberately trusted local setup. The SDK `RunConfig` also has an
`auto_approve_tools` field, but the current SDK invocation loop does not consult
that field when invoking tools. Registry approval enforcement requires a tenant
principal in the current context. Do not rely on the SDK flag alone to block
side-effects; enforce capability boundaries in the host application and process
environment. Never treat UI approval as a substitute for OS-level process
isolation.

### 3.6 API, authentication, and limits

`api.host` defaults to loopback. `AGENT_HARNESS_BIND_HOST` can override it at
runtime. The API refuses a non-loopback bind unless `auth.api_key`,
`AGENT_HARNESS_API_KEY`, or configured `auth.api_keys` credentials are present.
Use per-principal `api_keys` for tenant/user/role assignment:

```yaml
auth:
  api_keys:
    developer:
      key_env: IVY_DEVELOPER_KEY
      tenant_id: example
      user_id: developer-1
      roles: [developer]
      allowed_roots: [C:/work/example]
```

`api.cors_origins` controls browser origins. `api.callback_hosts` is the allowlist
for callback/webhook destinations. `governance.rate_limiting` configures
`max_requests_per_minute` and `max_concurrent_runs` per authenticated principal.
The current API rate limiter is process-local; it is not a distributed quota
service.

The config also contains `governance.budgets` and
`governance.rbac.restricted_tools`. Treat these as configuration declarations,
not as proof of global enforcement across every entry point. Verify the target
path and add an integration test before relying on budget or role restrictions
for a security boundary. API keys are bearer credentials; use TLS termination,
rotation, least privilege, and a secret manager when deploying beyond localhost.

### 3.7 Workflows, webhooks, MCP, and observability

- **Declarative workflows:** manifests can be placed in `workflows/`, `graphs/`,
  or `.harness/workflows/`, or under `workflows`/`graphs` config. A manifest
  requires a name, start node, and non-empty node map. Nodes can declare goals,
  routes, prompts, inputs/outputs, tools, conditions, and human approval gates.
  Every node tool must also be permitted by `tools.allowlist` when that allowlist
  is configured. See [Advanced Use-Case Scenarios](ADVANCED_USE_CASE_SCENARIOS.md)
  for code-investigation and research workflow examples.
- **Webhooks:** `webhooks.enabled` and endpoint definitions configure event
  delivery; integration signing keys are read by their respective adapters. Keep
  secrets outside checked-in YAML and restrict callback hosts.
- **MCP:** define each server under `mcp_servers` with `transport`, `command`,
  `args`, and optional `env`. The current client implementation uses stdio
  subprocesses; do not assume that setting `transport: http` enables remote MCP.
  It discovers tools at runtime. Treat server commands and arguments as trusted
  executable configuration. The sample `utility` server runs
  `python -m harness.mcp_server`.
- **Observability:** `observability.langfuse.enabled` opts in to the Langfuse
  adapter when the package is installed and configured. Trace payloads may
  contain user/task data; apply your data retention and redaction policy before
  enabling a remote sink.

### 3.8 Configurable agent orchestration

`agents.profiles` defines the complete profile roster and each profile's route,
system prompt, tools, skills, metadata, and delegation policy. Set
`agents.include_builtins: false` to avoid adding the compatibility profile
catalogue to a custom orchestration topology. Profile IDs are configuration
identifiers; labels such as `planner`, `reviewer`, or `worker` have no special
runtime meaning.

`orchestration.chat_entry_profile` selects the profile that represents the
conversation. `planner_profile` optionally selects any configured profile to
propose plans. `mode` controls whether delegation is disabled (`off`), a single
specialist can be selected (`single`), profile-defined delegation and task DAGs
are available (`self_directed`), or a validated plan is required before tool
execution (`required`).

`planning_strategy` makes model autonomy explicit:

- `deterministic` is the default. It disables the model-facing `agent.plan`
  operation. A host can execute a preconstructed `TaskPlan` through the
  coordinator API or use the explicit fan-out API; the scheduler obeys that
  supplied plan and configured limits.
- `model_directed` opts in to LLM-proposed task DAGs. The runtime validates the
  complete proposal before launching tasks. `required` mode requires this
  strategy and fails during startup otherwise.

Model responses may include Markdown fences or short explanatory text around
the JSON. The parser accepts those common wrappers only when it finds exactly
one complete object with the required plan envelope. It rejects duplicate keys,
multiple different plans, invalid JSON constants, and responses over 64 KB. It
never guesses missing braces or tasks. `planner_retries` (default `1`, maximum
`2`) retries a format failure with the original request and a stricter format
reminder; oversized responses are rejected immediately. The plan still must
pass normal authorization and DAG validation before any child agent starts.
`planner_json_mode` defaults to `true` and asks the configured planner route for
JSON output using the provider's OpenAI-compatible `response_format` option.
Set it to `false` for endpoints that reject this option. This mode constrains
format only; the harness still applies its own strict parser and authorization
checks. The planner uses the route on `planner_profile`, independently of the
chat route selected on the CLI. The sample `assistant` profile is now routed to
`gemini.flash`; backend specialist profiles remain routed to NIM. Configure the
planner profile's route explicitly when you want planning to use a different
provider than the chat route. A bounded failure reports the planner route,
finish reason, and response byte count without exposing model text.
The same bounded object parser is used by dynamic graph routing and research
report synthesis. Router parse failures stop routing safely; research reports
accept claims only after their independent source-quote validation, and malformed
model text is not copied into the report summary.

`max_depth`, `max_total_agents`, `max_parallel_agents`, `max_plan_tasks`,
`max_total_tool_calls`, `timeout_seconds`, `failure_policy`, and
`result_bytes_per_task` bound one run. Each profile's `delegation` policy
controls whether it may create child work, its target IDs/tags, child count,
parallel children, depth, and budget. Target authorization never grants tools:
each child continues to use only its configured tools intersected with the
global allowlist and current approval policy. Child tasks receive only explicit
input references, not the full parent conversation.

Independent tasks execute concurrently within the configured limits; dependent
tasks wait for their declared inputs. `continue_independent` allows unaffected
branches to complete after a task failure, while `fail_fast` blocks pending
branches. A child timeout marks that child timed out and does not cancel
independent siblings. This is in-process orchestration, not a durable worker
queue; process restart recovery and exactly-once external side effects are not
provided by this feature.

## 4. Python SDK integration

Install the package in the host application environment, then create one harness
per workspace/configuration. Pass `workspace_root` explicitly so file tools,
skills, project detection, and memory bind to the intended project.

```python
from harness.sdk import AgentHarness, RunConfig

harness = AgentHarness.from_project(
    workspace_root="/srv/workspaces/acme",
    config_path="/etc/ivy/acme.yaml",
)

result = harness.run(
    "Inspect the project and summarize its test setup.",
    config=RunConfig(
        graph="gemini",
        node="flash",
        max_tool_steps=8,
        auto_approve_tools=False,
        stream=False,
    ),
)
print(result.final_output)
print(result.tool_calls)
```

For a deterministic multi-agent flow, the host can supply the plan directly; no
planning model call is made. Each task still passes the same profile-target,
dependency, context-reference, and budget validation:

```python
plan = {
    "schema_version": 1,
    "goal": "Review a change",
    "tasks": [
        {"id": "security", "profile": "security-reviewer",
         "task": "Review the change for security issues", "depends_on": [], "inputs": []},
        {"id": "tests", "profile": "test-reviewer",
         "task": "Review test coverage", "depends_on": [], "inputs": []},
        {"id": "summary", "profile": "reviewer",
         "task": "Combine both reports", "depends_on": ["security", "tests"],
         "inputs": ["security.result", "tests.result"]},
    ],
}
result = harness.execute_plan(plan)
```

To let the configured model create the DAG from the current request, explicitly
set `orchestration.planning_strategy: model_directed` and call
`harness.plan(prompt)` or enable the model-facing `agent.plan` operation for the
configured chat profile. This changes who proposes the task graph; all plans
still pass the same deterministic runtime validation and capability checks.

`AgentHarness` also exposes `stream(prompt, config)` for synchronous typed events
and async variants (`arun` / `astream`) for async applications. `AgentResult`
contains `final_output`, `steps`, `tool_calls`, `duration_s`, `success`, `error`,
and metadata. `RunConfig` supports graph/node or route, token/step limits,
temperature, approval mode, system prompt, tool filter, streaming, and context
overrides.

For application-owned tools, add a reviewed Python module under
`.harness/plugins/` or `plugins/` in the workspace. Plugin modules are imported
as Python code at startup/discovery, so only load trusted code. A minimal example:

```python
from harness.plugins import tool


@tool(
    name="orders.lookup",
    description="Read one order by its public identifier.",
    category="orders",
)
def lookup_order(order_id: str) -> dict:
    """Return a restricted order summary."""
    # Validate authorization and identifier inside the host integration too.
    return {"order_id": order_id, "status": "example"}
```

The current plugin discovery directories are `.harness/plugins/`, `plugins/`,
and `~/.ivy/plugins/`. Plugin tool schemas are inferred from Python annotations;
they are not a replacement for runtime validation. Ensure the tool is also
exposed by the active allowlist and workflow node. Mark mutations and approval
requirements accurately, and enforce tenant authorization inside application
tools. Do not give an agent a general-purpose host capability when a narrower
application operation will do.

## 5. REST and OpenAI-compatible integration

Start the API with `ivy serve`. The current API exposes health, graph/model/tool
catalogs, run submission/status/result/trace/resume, model smoke, tool invocation,
SSE/WebSocket agent interfaces, webhooks, and an OpenAI-compatible `/v1` router.
Check the live OpenAPI document at `/docs` on a local instance for exact request
schemas and paths; request models can change independently of the CLI.

Authenticate using `X-API-Key` or `Authorization: Bearer <key>`. Send credentials
only over TLS when connecting remotely. A caller must not be able to set its own
approval privilege; the API derives approval from server configuration. Run
ownership is tenant-scoped, and approval grants are separate, short-lived,
single-use credentials bound to a paused run.

The API process and SQLite store are not a horizontally scalable deployment by
themselves. For production, add a durable worker/queue, shared database suitable
for concurrent service instances, distributed rate limiting, identity integration,
and operational monitoring before scaling out.

## 6. Secrets and provider credentials

Do not put model keys, API keys, or webhook signing secrets in source-controlled
`config.yaml`. Configure the route's `api_key_env`, then set that named secret in
the local store:

```powershell
ivy secrets set GEMINI_API_KEY
ivy secrets list
```

The CLI prompts for secret values without echoing. On Windows the local store uses
DPAPI for the current user; on other platforms the implementation stores local
file values, so restrict file access and prefer a production secret manager.
`secrets.store_path` or `AGENT_HARNESS_SECRET_STORE` selects the store path.
`load_secrets()` also imports user/workspace dotenv files and process variables;
never share those files or logs. Secret names can also be provided directly as
environment variables in container/service deployments.

Useful operational commands:

```powershell
ivy secrets list       # names only; does not print secret values
ivy secrets path
ivy secrets remove GEMINI_API_KEY
```

## 7. Deployment profiles

### Local development

- Keep `api.host` on `127.0.0.1`.
- Use `provider: local` only for trusted prompts and workspaces.
- Keep network disabled unless a task needs `web.fetch_url`.
- Use a disposable git worktree for autonomous code changes.
- Set a finite `max_tool_steps`, command timeout, and I/O bounds.

### Team-hosted service

- Configure API credentials before binding to a non-loopback interface.
- Set `auto_approve_tools: false` unless a separately reviewed policy authorizes
  the exact capabilities.
- Use least-privilege tool allowlists and role policies; test enforcement at every
  enabled entry point (CLI, SDK, REST, MCP).
- Run tools in an isolated worker/container/VM with scoped files, credentials,
  network egress, CPU, memory, process, and time limits. Local mode is host
  execution.
- Use an external secret manager and rotate keys; do not rely on a shared API key
  as a complete identity system.
- Use durable shared storage/queue and distributed rate/cost accounting before
  deploying multiple replicas.
- Define retention, audit, redaction, backups, health checks, and incident
  response for traces, run data, and webhook payloads.

The project includes a security review and scenario analyses, but those documents
are not a security certification. Validate the exact release configuration and
deployment environment before exposing the service publicly.

## 8. Troubleshooting

| Symptom | Checks |
| --- | --- |
| Route not found | `ivy models`; check `<graph>.<node>`, graph-level route, then `default` fallback resolution. |
| Missing provider key | Confirm `api_key_env` spelling and set that secret name; `ivy secrets list` reveals names only. |
| Model call stalls/fails | Check provider/model availability, timeout, retry/fallback route chain, network policy, and provider logs. |
| Tool is not offered | Check pack enabled status, `tools.allowlist`, built-in enabled/disabled overrides, and per-node tool list. |
| File tool cannot access path | Add a narrow workspace root; ensure the path is not protected and is under writable roots for mutations. |
| Command runs on host | `tools.sandbox.provider: local` is host-user execution; choose and configure an isolation provider. |
| Workflow not discovered | Validate YAML/JSON schema; check its directory and that all routes/targets/tools are declared. |
| Remote API refuses startup | Configure valid API credentials before non-loopback bind; confirm runtime `AGENT_HARNESS_BIND_HOST`. |
| API returns 401/429 | Check API key principal config and process-local request/concurrency limits. |
| No Langfuse traces | Confirm enabled config, dependency installed, and Langfuse credentials set in the runtime environment. |

## 9. Documentation map

- [Advanced use-case scenarios](ADVANCED_USE_CASE_SCENARIOS.md): capability
  coverage and proposed workflow behavior.
- [Advanced scenario implementation plan](ADVANCED_SCENARIO_IMPLEMENTATION_PLAN.md):
  planned acceptance criteria for selected workflows.
- [Self-directing hierarchical orchestration plan](SELF_DIRECTING_HIERARCHICAL_ORCHESTRATION_PLAN.md):
  proposed next layer for model-selected teams, bounded delegation hierarchies,
  plan validation, and execution.
- [Production readiness review](PRODUCTION_READINESS_REVIEW.md): security and
  reliability findings; re-check against the current commit before acting on any
  historical finding.
- [Dynamic harness evolution and gap analysis](DYNAMIC_HARNESS_EVOLUTION_AND_GAP_ANALYSIS.md):
  ecosystem research and proposed future capabilities, not a configuration spec.
- [Gemini harness stress test](GEMINI_HARNESS_STRESS_TEST.md): repeatable local
  route and tool-loop exercises.
- [Leadership demo pitch](GENAI_LEADERSHIP_DEMO_PITCH.md): presentation material,
  not an engineering contract.

# Configurable specialist profiles

Define trusted specialists under `agents.profiles` in `config.yaml`. Each profile requires an existing model `route`, a bounded `system_prompt`, and a list of `tools`. Tool names must be registered and included in the global `tools.allowlist`; the profile only narrows that set. Optional `skills` must name installed skills, and `max_tool_turns` is from 1 through 10.

```yaml
agents:
  profiles:
    researcher:
      description: Collect evidence and summarize uncertainty.
      route: nim.nemotron
      system_prompt: You are a careful research specialist.
      tools: [web.fetch_url]
      skills: []
      max_tool_turns: 3
      can_handoff_to: [reviewer]
```

The normal chat loop exposes `agent.delegate` and appends its structured result to the conversation for the primary agent to continue. In Python, `AgentHarness` uses the same configured profiles; `harness.swarm(task, agents=[...])` and the CLI `swarm` command require registered names. Unknown names fail closed. Specialists cannot choose their own route, tools, or skills. Approval-gated or mutating tools require an active auto-approval principal and remain subject to the global tool policy. Skills supply context only and do not grant tools. Profiles are bounded to 100 entries; each profile allows at most 50 tools, 10 skills, 50 unique handoff targets, 10 tool turns, 32 KB of skill instructions per invocation, and 16 KB of serialized arguments per tool call.
