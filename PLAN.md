# Ivy Universal Agentic Harness — Master Architecture & Domain-Agnostic Roadmap

> **Vision**: A lightweight, modular, enterprise-grade **Universal Agentic Harness** that can be seamlessly attached to **ANY client project or domain** (e.g., Enterprise Workflow Automation, Financial Analysis, Customer Support, DevOps, Data Pipelines, Legal Document Processing, and Software Engineering).

---

## 🏛️ Core Architectural Invariants

1. **Zero Domain Lock-in**: The harness must not assume it is running on a code repository. Tools, memory, and reasoning graphs must be completely modular and capability-driven.
2. **Attach-Anywhere Architecture**: Must support being embedded via:
   - **Python SDK / Library**: `from harness import AgentHarness, RunConfig`
   - **REST & SSE Streaming API**: FastAPI microservice for web/mobile/cloud backends.
   - **WebSocket Real-Time Gateway**: Bidirectional streaming for interactive frontends.
   - **CLI & TUI**: Developer and operations terminal interface.
   - **Model Context Protocol (MCP)**: Acts as both an MCP Client and an MCP Server.
3. **Multi-Tier Resolution (Global ↔ Project ↔ Tenant)**:
   - **Tenant / User Level**: Global user preferences and API credentials.
   - **Client Project Level**: Local project rules, domain packs, custom tools, and workflows.
   - **Runtime Overrides**: Per-request dynamic context and tool permissions.
4. **Declarative & Zero-Code Customization**: Workflows, security boundaries, model routing, and domain packs are configured via YAML/JSON with zero Python code changes required.

---

## 🗺️ Re-Planned Universal Roadmap

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        UNIVERSAL AGENTIC HARNESS BLUEPRINT                             │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  Phase 1: Resilient Multi-Model & Tool Calling Foundation (COMPLETED)                 │
│  ├── Native OpenAI/NIM Function Calling Schema Reflection                             │
│  ├── Declarative Model Route Failover Chains (429/503 Auto-Healing)                   │
│  └── Dual-Mode Fallback (Native JSON Schema + Adaptive Prompt Tool Parser)            │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  Phase 2: Universal Capability Packs & Dynamic Plugin Engine (NEXT)                   │
│  ├── Domain Capability Packs (core, filesystem, data_analytics, web, shell, mcp)     │
│  ├── Zero-Code Custom Python Plugin Auto-Discovery (`plugins/` directory)             │
│  ├── Universal Input/Output Schema Reflection & Validation                            │
│  └── Client Project Auto-Detection & Environment Adaptation                           │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  Phase 3: Pluggable Multi-Agent Graphs, Swarms & Workflow DAGs                         │
│  ├── Declarative YAML/JSON Graph & DAG Definition Engine                              │
│  ├── Multi-Agent Orchestrator-Worker & Swarm Delegation Patterns                      │
│  ├── Dynamic State Machine with Conditional Transition Routing                        │
│  └── Universal Human-in-the-Loop Approval & Intervention Gates                        │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  Phase 4: Real-Time Event Bus, Multi-Backend Storage & State Time-Travel              │
│  ├── Universal Event Bus (SSE, WebSockets, Pub/Sub, Webhooks)                         │
│  ├── Pluggable State Storage (In-Memory, SQLite, PostgreSQL, Redis)                   │
│  ├── State Checkpointing, Snapshotting & Time-Travel / Resume                         │
│  └── Multi-Tier Memory (Short-Term, Working Memory, Vector/Episodic Store)            │
├────────────────────────────────────────────────────────────────────────────────────────┤
│  Phase 5: Enterprise Governance, Multi-Tenancy & Observability                         │
│  ├── Multi-Tenant Isolation (Tenant ID, Project ID, User ID)                          │
│  ├── Fine-Grained Policy Engine & Tool RBAC (Allow/Deny/Approve rules)                │
│  ├── Token, Rate Limit & Cost Budget Guardrails                                       │
│  └── Full OpenTelemetry, Langfuse & Prometheus Metric Exporters                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 📦 Phase 2 Specification: Universal Capability Packs & Plugin Engine

### 2.1 Domain Capability Packs
Instead of hardcoded tools, tools are grouped into modular, enableable/disableable **Packs**:
```yaml
packs:
  core:
    enabled: true        # context, environment, timing, state
  data_analytics:
    enabled: true        # SQL query, CSV/JSON transforms, statistics
  web_network:
    enabled: true        # HTTP fetch, webhook dispatch, API clients
  filesystem:
    enabled: true        # File reading, writing, searching
  shell_sandbox:
    enabled: false       # Shell execution (disabled in secure environments)
  custom_mcp:
    enabled: true        # Discovered MCP servers (Postgres, Slack, Jira, etc.)
```

### 2.2 Dynamic Plugin Auto-Discovery (`plugins/`)
- Any client project can drop a `.py` file into `.harness/plugins/` or configure an import path `my_client_app.tools`.
- Tools decorated with `@tool` or subclasses of `BaseTool` are automatically discovered, typed, and registered into the LLM schema.

### 2.3 Client Project Auto-Detection
- When attached to a project, Ivy scans project markers (`package.json`, `pyproject.toml`, `Cargo.toml`, `docker-compose.yml`, `sql/`, `data/`) and automatically suggests or activates the appropriate domain packs and skills.

---

## 🔄 Phase 3 Specification: Pluggable Multi-Agent Graphs & Workflows

### 3.1 Declarative Workflow DAGs
Client projects define multi-step business logic in pure YAML:
```yaml
graphs:
  customer_ticket_triage:
    entry: intake
    nodes:
      intake:
        route: nim.llama-70b
        system_prompt: "Classify ticket urgency and category."
        next: [route_by_urgency]
      investigate_data:
        route: nim.qwen-coder
        tools: ["mcp.postgres.query", "data.transform"]
        next: draft_response
      human_review:
        gate: approval_required
        next: finalize
```

### 3.2 Multi-Agent Delegation
- Primary Orchestrator agent can spawn specialized sub-agents with scoped permissions and tools.

---

## 📡 Phase 4 Specification: Real-Time Event Bus & State Persistence

### 4.1 Universal Event Stream
Emits standard CloudEvents / JSON events:
- `run.created`, `agent.thought`, `tool.start`, `tool.result`, `gate.awaiting_approval`, `run.completed`.
- Consumed easily by React UIs, Slack bots, or internal enterprise dashboards.

### 4.2 Pluggable Storage & Time-Travel
- Runs can be paused, inspected, rolled back to step $N$, modified, and resumed.

---

## 🛡️ Phase 5 Specification: Governance, Multi-Tenancy & Budgets

- Per-tenant quotas (max $ per day, max token rate).
- Role-based tool access control (e.g. `viewer` role cannot invoke mutative tools).
- OpenTelemetry tracing with spans for LLM calls, tool executions, and graph transitions.
