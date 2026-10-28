# Ivy Agent Harness

A Python runtime for configurable model routing, tool execution, agent loops, and
graph-based workflows. Integrate it through the Python SDK, REST API, CLI, or MCP.

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --extra dev
.venv\Scripts\Activate.ps1
ivy models
ivy tools
```

From an unactivated PowerShell shell, use the checked-in wrappers:

```powershell
.\ivy.ps1 models
.\ivy.ps1 serve --host 127.0.0.1 --port 8000
```

Configure a model key in the local secret store, then make a one-shot request:

```powershell
ivy secrets set GEMINI_API_KEY
ivy chat --graph gemini --node flash --once "Reply exactly: READY"
```

Start interactive chat or the local API:

```powershell
ivy chat --graph gemini --node flash
ivy serve --host 127.0.0.1 --port 8000
```

## Integrate the harness

The full developer guide covers every configuration section, model routes,
capability packs, tool policy, sandbox providers, workflow manifests, MCP,
secrets, CLI, Python SDK, REST/OpenAI-compatible API, and deployment profiles:

**[Configuration and Integration Guide](docs/CONFIGURATION_AND_INTEGRATION.md)**

Python SDK example:

```python
from harness.sdk import AgentHarness, RunConfig

agent = AgentHarness.from_project(
    workspace_root="/path/to/your/project",
    config_path="/path/to/ivy.yaml",
)
result = agent.run(
    "Summarize this project.",
    config=RunConfig(graph="gemini", node="flash"),
)
print(result.final_output)
```

## Documentation

- [Configuration and Integration Guide](docs/CONFIGURATION_AND_INTEGRATION.md)
- [Advanced Use-Case Scenarios](docs/ADVANCED_USE_CASE_SCENARIOS.md)
- [Scenario Implementation Plan](docs/ADVANCED_SCENARIO_IMPLEMENTATION_PLAN.md)
- [Production Readiness Review](docs/PRODUCTION_READINESS_REVIEW.md)
- [Harness Evolution and Gap Analysis](docs/DYNAMIC_HARNESS_EVOLUTION_AND_GAP_ANALYSIS.md)
- [Gemini Harness Stress Test](docs/GEMINI_HARNESS_STRESS_TEST.md)
- [Leadership Demo Pitch](docs/GENAI_LEADERSHIP_DEMO_PITCH.md)

## Configuration and safety

[`config.yaml`](config.yaml) is the sample deployment configuration. Keep
credentials out of it; use `ivy secrets set NAME` or the deployment's secret
manager. Local sandbox mode runs commands with the current operating-system
user's permissions. Its workspace roots constrain file tools but do not confine
arbitrary shell commands. For untrusted tasks, use a genuinely isolated worker or
container with least-privilege credentials and network policy.

The sample is aimed at local development, not an internet-facing deployment.
Review authentication, approval settings, tool allowlists, sandbox policy,
storage, and deployment-specific controls in the integration guide before
exposing the API to other users.
