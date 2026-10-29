"""Thin CLI client and interactive terminal interface for Agent Harness."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from typing import Any

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
os.environ.setdefault("LITELLM_LOG", "ERROR")

from harness.config import deep_get, load_config
from harness.context import apply_observation_masking
from harness.detector import detect_project
from harness.features import FeatureRegistry
from harness.governance import has_auth_credentials
from harness.memory import MemoryStore, compact_history
from harness.parser import (
    execute_chat_tool,
    parse_tool_calls,
    repair_truncated_json,
    summarize_tool_observations,
)
from harness.prompts import build_harness_context, build_smart_system_prompt
from harness.registries.mcp_registry import MCPRegistry
from harness.registries.model_registry import ModelConfigurationError, ModelRegistry
from harness.registries.tool_registry import ToolRegistry
from harness.secrets import (
    configured_secret_store,
    list_secret_names,
    load_secrets,
    remove_secret,
    set_secret,
)
from harness.skills import SkillRegistry

_VERIFICATION_COMMAND_PATTERN = re.compile(
    r"(?i)(\bpytest\b|\bunittest\b|\btox\b|\bnox\b|\bruff\b|\bmypy\b|\bpyright\b|"
    r"\bcargo\s+test\b|\bnpm\s+test\b|\bpnpm\s+test\b|\byarn\s+test\b|"
    r"\bgo\s+test\b|\bdotnet\s+test\b|\bmvn\s+test\b|\bgradle\s+test\b|\bmake\s+test\b)"
)


def verification_command(tool_call: dict[str, Any]) -> str | None:
    """Return a bounded shell/Python tool payload when it appears to run checks."""
    tool_name = tool_call.get("tool")
    if tool_name == "process.run":
        candidate = tool_call.get("args", {}).get("command", "")
    elif tool_name == "sandbox.run_python":
        candidate = tool_call.get("args", {}).get("code", "")
    else:
        return None
    if not isinstance(candidate, str) or not _VERIFICATION_COMMAND_PATTERN.search(candidate):
        return None
    return candidate[:240].replace("\r", " ").replace("\n", " ")


def verification_status_notice(
    runs: list[dict[str, Any]],
    *,
    last_mutation_step: int,
    tool_budget_exhausted: bool,
    max_tool_steps: int,
) -> str | None:
    """Produce deterministic user-visible status from observed tool results.

    This does not infer correctness from model claims. It reports only command
    outcomes and whether a later mutating tool made that verification stale.
    """
    if not runs and not tool_budget_exhausted:
        return None
    prefix = "Harness verification evidence: "
    if not runs:
        message = "no test/check command completed successfully during this turn."
    else:
        latest = runs[-1]
        command = latest.get("command", "verification command")
        exit_code = latest.get("exit_code")
        if exit_code is None:
            message = f"`{command}` could not be executed; no exit code was returned."
        elif exit_code != 0:
            message = f"`{command}` exited with code {exit_code}; checks did not pass."
        elif latest.get("step", 0) < last_mutation_step:
            message = (
                f"`{command}` exited with code 0, but a later file/command tool may have changed code; "
                "verification is stale and must be rerun."
            )
        else:
            message = f"`{command}` exited with code 0; this is the latest observed check result."
    if tool_budget_exhausted:
        message += f" Tool-step budget ({max_tool_steps}) was exhausted; review remaining work manually."
    return prefix + message

try:
    from rich import box
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
except ModuleNotFoundError:  # pragma: no cover - exercised when optional deps are absent.
    box = None  # type: ignore[assignment]
    Console = None  # type: ignore[assignment]
    Group = None  # type: ignore[assignment]
    Markdown = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Rule = None  # type: ignore[assignment]
    Syntax = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


def request(
    method: str,
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc


def parse_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def parse_arg_pair(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected key=value")
    key, raw = value.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError("argument key cannot be empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return key, parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Harness CLI")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--secret-store", default=None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for X-API-Key; defaults to AGENT_HARNESS_API_KEY",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose tool and subagent execution logs")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="Start an interactive model chat")
    chat.add_argument("--graph", default=None)
    chat.add_argument("--node", default=None)
    chat.add_argument("--system", default=None)
    chat.add_argument("--once", default=None, help="Send one prompt and exit")
    chat.add_argument("--verbose", "-v", action="store_true", help="Enable maximum verbosity showing all tool calls, inputs, outputs, and subagent traces")
    chat.add_argument("--quiet", "-q", action="store_true", help="Pure chatbot mode: hide tool logs")

    swarm = sub.add_parser("swarm", help="Run multi-agent swarm tasks (fanout / handoff)")
    swarm.add_argument("--pattern", choices=["fanout", "handoff"], default="fanout", help="Swarm coordination pattern")
    swarm.add_argument("--agents", default=None, help="Comma-separated agent names")
    swarm.add_argument("--task", default="Inspect workspace and summarize status", help="Task description")

    run = sub.add_parser("run", help="Create a run")
    run.add_argument("--graph", required=True)
    run.add_argument("--input-json", type=parse_json, required=True)
    run.add_argument("--wait", action="store_true")

    status = sub.add_parser("status", help="Get run status")
    status.add_argument("run_id")

    trace = sub.add_parser("trace", help="Get run trace")
    trace.add_argument("run_id")

    result = sub.add_parser("result", help="Get run result")
    result.add_argument("run_id")

    resume = sub.add_parser("resume", help="Resume a paused run")
    resume.add_argument("run_id")
    resume.add_argument("--approval", action="append", default=[])
    resume.add_argument("--wait", action="store_true")

    sub.add_parser("models", help="List configured model routes")

    sub.add_parser("tools", help="List configured tool routes")

    tool = sub.add_parser("tool", help="Invoke a configured tool")
    tool.add_argument("tool_name")
    tool.add_argument("--args-json", type=parse_json, default={})
    tool.add_argument(
        "--arg",
        action="append",
        type=parse_arg_pair,
        default=[],
        help="Tool argument as key=value; JSON values are accepted",
    )

    smoke = sub.add_parser("model-smoke", help="Run a model smoke request through the API")
    smoke.add_argument("--graph", default="brainstorm")
    smoke.add_argument("--node", default="generate")
    smoke.add_argument("--prompt", default="Reply with ok.")

    serve = sub.add_parser("serve", help="Start the FastAPI development server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    secrets = sub.add_parser("secrets", help="Manage local CLI secrets")
    secret_sub = secrets.add_subparsers(dest="secret_command", required=True)
    secret_set = secret_sub.add_parser("set", help="Store a secret locally")
    secret_set.add_argument("name")
    secret_set.add_argument("--value", default=None, help="Secret value; omit to prompt securely")
    secret_remove = secret_sub.add_parser("remove", help="Remove a stored secret")
    secret_remove.add_argument("name")
    secret_sub.add_parser("list", help="List stored secret names")
    secret_sub.add_parser("path", help="Print the configured secret store path")

    sub.add_parser("mcp-server", help="Run Ivy as a Model Context Protocol (MCP) stdio server for Cursor / Claude Desktop")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = build_parser().parse_args(argv)
    if args.command == "mcp-server":
        from harness.mcp_server import main as mcp_main
        mcp_main()
        return 0
    if args.secret_store:
        load_secrets(path=args.secret_store)
    else:
        config_for_secrets = load_config(args.config)
        load_secrets(config_for_secrets)
    args.api_key = args.api_key or os.environ.get("AGENT_HARNESS_API_KEY")
    if args.command in {None, "chat"}:
        return run_chat(args)
    if args.command == "run":
        output = request(
            "POST",
            args.base_url,
            "/runs",
            {"graph": args.graph, "input": args.input_json, "wait": args.wait},
            api_key=args.api_key,
        )
    elif args.command == "status":
        output = request("GET", args.base_url, f"/runs/{args.run_id}", api_key=args.api_key)
    elif args.command == "trace":
        output = request("GET", args.base_url, f"/runs/{args.run_id}/trace", api_key=args.api_key)
    elif args.command == "result":
        output = request("GET", args.base_url, f"/runs/{args.run_id}/result", api_key=args.api_key)
    elif args.command == "resume":
        output = request(
            "POST",
            args.base_url,
            f"/runs/{args.run_id}/resume",
            {"approvals": args.approval, "wait": args.wait},
            api_key=args.api_key,
        )
    elif args.command == "models":
        output = request("GET", args.base_url, "/models/routes", api_key=args.api_key)
    elif args.command == "tools":
        output = request("GET", args.base_url, "/tools", api_key=args.api_key)
    elif args.command == "tool":
        tool_args = dict(args.args_json)
        tool_args.update(dict(args.arg))
        output = request(
            "POST",
            args.base_url,
            f"/tools/{args.tool_name}",
            {"arguments": tool_args},
            api_key=args.api_key,
        )
    elif args.command == "model-smoke":
        output = request(
            "POST",
            args.base_url,
            "/models/smoke",
            {"graph": args.graph, "node": args.node, "prompt": args.prompt},
            api_key=args.api_key,
        )
    elif args.command == "serve":
        import uvicorn

        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = args.host.lower() == "localhost"
        config = load_config(args.config)
        if not loopback and not has_auth_credentials(config):
            raise SystemExit("Refusing non-loopback API bind without configured API credentials")
        os.environ["AGENT_HARNESS_CONFIG"] = args.config
        os.environ["AGENT_HARNESS_BIND_HOST"] = args.host
        uvicorn.run(
            "harness.api.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0
    elif args.command == "secrets":
        return run_secrets(args)
    elif args.command == "swarm":
        return run_swarm(args)
    else:  # pragma: no cover - argparse prevents this.
        raise SystemExit(f"unknown command: {args.command}")
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def run_swarm(args: argparse.Namespace) -> int:
    from harness.swarm import SwarmCoordinator

    config = load_config(args.config)
    load_secrets(config, path=args.secret_store)
    tools = ToolRegistry.from_config(config)
    models = ModelRegistry.from_config(config)
    skills = SkillRegistry(workspace_root=".")
    ui_console = Console() if Console is not None else None

    coord = SwarmCoordinator.from_default_config(models=models, tools=tools, config=config, skills=skills)
    raw_agents = getattr(args, "agents", None)
    agent_names = [a.strip() for a in raw_agents.split(",") if a.strip()] if raw_agents else []
    if not agent_names:
        entry = coord.chat_entry_profile
        if not entry:
            raise ValueError("--agents is required when orchestration.chat_entry_profile is not configured")
        agent_names = [entry]

    if ui_console:
        ui_console.print(f"\n[bold #a855f7]⚡ MULTI-AGENT SWARM EXECUTION ({args.pattern.upper()})[/bold #a855f7]")
        ui_console.print(f"  [dim]Agents:[/dim] [cyan]{', '.join(agent_names)}[/cyan]")
        ui_console.print(f"  [dim]Task:[/dim] [white]{args.task}[/white]\n")

    def handle_swarm_event(event: dict[str, Any]) -> None:
        etype = event.get("type")
        agent = event.get("agent", "subagent")
        if etype == "subagent_started":
            idx_str = f" [{event.get('index')}/{event.get('total')}]" if "index" in event else (f" [Hop {event.get('hop')}]" if "hop" in event else "")
            if ui_console:
                ui_console.print(f"\n[bold #38bdf8]⚡ Running Subagent{idx_str}:[/bold #38bdf8] [bold yellow]{agent}[/bold yellow]")
            else:
                print(f"\n⚡ Running Subagent{idx_str}: {agent}")
        elif etype == "subagent_tool":
            t_name = event.get("tool", "tool")
            t_args = event.get("args", {})
            t_res = event.get("result")
            res_summary = summarize_tool_result_data(t_name, t_res)
            args_summary = summarize_tool_args(t_name, t_args)
            if ui_console:
                ui_console.print(f"  [bold cyan]●[/bold cyan] [bold yellow]{t_name}[/bold yellow] [dim white]{args_summary}[/dim white] ➔ [bold green]✓ {res_summary}[/bold green]")
            else:
                print(f"  ● {t_name} {args_summary} -> {res_summary}")
        elif etype == "subagent_completed":
            tools_cnt = event.get("tools_count", 0)
            if ui_console and Text:
                line = Text()
                line.append("✓ ", style="bold green")
                line.append(f"Subagent [{agent}] finished ", style="dim")
                line.append(f"({tools_cnt} tool actions executed)", style="dim cyan")
                ui_console.print(line)
            else:
                print(f"✓ Subagent [{agent}] finished ({tools_cnt} tool actions executed)")

    if args.pattern == "handoff":
        res = asyncio.run(coord.run_handoff(entry_agent=agent_names[0], task=args.task, on_event=handle_swarm_event))
        if ui_console:
            ui_console.print(f"\n[bold white on green] ✓ FINAL RESULT [/bold white on green]\n{res.get('final_output')}\n")
        else:
            print(json.dumps(res, indent=2, default=str))
    else:
        res = asyncio.run(coord.run_fan_out(agent_names=agent_names, task=args.task, synthesizer_agent=coord.planner_profile, on_event=handle_swarm_event))
        if ui_console:
            ui_console.print(f"\n[bold white on green] ✓ SWARM SYNTHESIS [/bold white on green]\n{res.get('synthesis')}\n")
        else:
            print(json.dumps(res, indent=2, default=str))
    return 0


def summarize_tool_args(tool_name: str, args: dict[str, Any]) -> str:
    if not args:
        return ""
    if tool_name in {"file.list", "file.read_text", "file.read_lines", "file.write_text", "file.replace_text"}:
        path = args.get("path") or args.get("file") or "."
        parts = [str(path)]
        if "max_entries" in args:
            parts.append(f"max={args['max_entries']}")
        if "start" in args or "end" in args:
            parts.append(f"L{args.get('start', 1)}-{args.get('end', 'end')}")
        return " ".join(parts)
    if tool_name == "file.search_text":
        return f"query={args.get('query', '')!r} root={args.get('root', '.')}"
    if tool_name == "process.run":
        cmd = str(args.get("command", ""))
        return f"{cmd[:45]}..." if len(cmd) > 48 else cmd
    if tool_name == "sandbox.run_python":
        code = str(args.get("code", "")).strip().replace("\n", " ")
        return f"{code[:50]}..." if len(code) > 53 else code
    if tool_name == "web.fetch_url":
        return str(args.get("url", ""))
    return ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])


def summarize_tool_result_data(tool_name: str, result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)[:50]
    if tool_name == "file.list":
        entries = result.get("entries", [])
        return f"{len(entries)} items"
    if tool_name == "file.search_text":
        matches = result.get("matches", [])
        return f"{len(matches)} matches"
    if tool_name in {"file.read_text", "file.read_lines"}:
        if "lines" in result:
            return f"{len(result.get('lines', []))} lines"
        content = str(result.get("content", ""))
        return f"{len(content):,} chars"
    if tool_name == "file.write_text":
        return f"wrote {result.get('bytes_written', 0):,} bytes"
    if tool_name == "file.replace_text":
        return "replaced text"
    if tool_name == "process.run":
        code = result.get("exit_code", 0)
        return f"exit {code}"
    if tool_name == "sandbox.run_python":
        code = result.get("exit_code", 0)
        stdout = str(result.get("stdout", "")).strip()
        line_count = len(stdout.splitlines()) if stdout else 0
        return f"exit {code} ({line_count} lines out)"
    if tool_name == "web.fetch_url":
        return f"{len(str(result.get('content', ''))):,} bytes"
    return "ok"


def run_chat(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    load_secrets(config, path=args.secret_store)
    graph = getattr(args, "graph", None) or deep_get(config, "chat.graph", "nim")
    node = getattr(args, "node", None) or deep_get(config, "chat.node", "nemotron")
    configured_entry = deep_get(config, "orchestration.chat_entry_profile")
    profile_data = deep_get(config, f"agents.profiles.{configured_entry}", {}) if configured_entry else {}
    if configured_entry and isinstance(profile_data, dict) and not getattr(args, "graph", None) and not getattr(args, "node", None):
        route_parts = str(profile_data.get("route", f"{graph}.{node}")).split(".", 1)
        graph = route_parts[0]
        node = route_parts[1] if len(route_parts) > 1 else "default"
    system_prompt = getattr(args, "system", None) or deep_get(
        config,
        "chat.system_prompt",
        "You are Ivy, an expert autonomous software engineering agent running inside Agent Harness.",
    )
    if configured_entry and isinstance(profile_data, dict) and not getattr(args, "system", None):
        system_prompt = str(profile_data.get("system_prompt", system_prompt))
    max_history_messages = int(deep_get(config, "chat.max_history_messages", 30))
    loop_enabled = bool(deep_get(config, "chat.agent_loop.enabled", True))
    max_tool_steps = int(
        deep_get(config, "chat.agent_loop.max_tool_steps", deep_get(config, "chat.max_tool_steps", 10))
    )
    max_tokens = int(deep_get(config, "chat.agent_loop.max_tokens", 4096))
    continuation_on_truncation = bool(deep_get(config, "chat.agent_loop.continuation_on_truncation", True))
    compact_threshold_messages = int(deep_get(config, "chat.agent_loop.compact_threshold_messages", 30))
    auto_approve_tools = bool(deep_get(config, "chat.agent_loop.auto_approve_tools", False))
    tool_observation_prompt = str(
        deep_get(
            config,
            "chat.agent_loop.tool_observation_prompt",
            "Now answer the user's request using this tool result.",
        )
    )
    tool_budget_prompt = str(
        deep_get(
            config,
            "chat.agent_loop.tool_budget_prompt",
            "The tool budget is exhausted. Do not request another tool. "
            "Answer the original user request using the tool results already provided.",
        )
    )
    memory = MemoryStore(workspace_root=".")
    skills = SkillRegistry(workspace_root=".")
    mcp = MCPRegistry.from_config(config)
    registry = ModelRegistry.from_config(config)
    tools = ToolRegistry.from_config(config)
    from harness.swarm import SwarmCoordinator
    coordinator = (
        SwarmCoordinator.from_default_config(models=registry, tools=tools, config=config, skills=skills)
        if isinstance(getattr(registry, "routes", None), dict)
        else None
    )
    def dispatch_profile(agent: str, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        if coordinator is None:
            return {"ok": False, "status": "unavailable", "agent": agent, "error": "model registry unavailable"}
        import asyncio
        import contextvars
        import threading
        inherited_context = contextvars.copy_context()
        result: list[dict[str, Any]] = []
        error: list[Exception] = []
        def run() -> None:
            try:
                result.append(asyncio.run(coordinator.run_agent(agent, prompt, context, requester=configured_entry)))
            except Exception as exc:  # noqa: BLE001 - surfaced by the delegate tool boundary
                error.append(exc)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            run()
        else:
            worker = threading.Thread(target=lambda: inherited_context.run(run), daemon=True)
            worker.start()
            worker.join()
        if error:
            raise error[0]
        return result[0]
    tools.delegate_dispatcher = dispatch_profile
    def dispatch_plan(prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        if coordinator is None or not configured_entry:
            return {"ok": False, "status": "unavailable", "error": "configured planning entrypoint is unavailable"}
        import asyncio
        import contextvars
        import threading

        from harness.events import current_event_run_id
        run_id = current_event_run_id()
        inherited_context = contextvars.copy_context()
        result: list[dict[str, Any]] = []
        error: list[Exception] = []
        def run() -> None:
            try:
                result.append(asyncio.run(coordinator.plan(configured_entry, prompt, context, run_id=run_id)))
            except Exception as exc:  # noqa: BLE001
                error.append(exc)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            run()
        else:
            worker = threading.Thread(target=lambda: inherited_context.run(run), daemon=True)
            worker.start()
            worker.join()
        if error:
            raise error[0]
        return result[0] if result else {"ok": False, "status": "failed", "error": "planner produced no result"}
    tools.plan_dispatcher = dispatch_plan
    features = FeatureRegistry()
    route = registry.route_for(graph, node)
    ui = ChatUI(
        config=config,
        graph=graph,
        node=node,
        model=route.model,
        provider=route.provider,
        tools=tools,
        models=registry,
        memory=memory,
        features=features,
        skills=skills,
        mcp=mcp,
    )
    if getattr(args, "verbose", False):
        ui.tool_display = "verbose"
        ui.show_tool_args = True
        ui.show_tool_result_excerpt = True
    elif getattr(args, "quiet", False):
        ui.tool_display = "hidden"

    harness_context = build_harness_context(config)
    memory_context = memory.render_system_memory_context()
    skills_context = skills.render_skills_prompt_catalog()
    project_profile = detect_project(".")
    project_context = project_profile.render_context_summary()
    chat_tool_catalog = tools.catalog()
    if coordinator is not None and "agent.delegate" in chat_tool_catalog:
        profile_names = ", ".join(f"{name}: {spec.description or spec.system_prompt[:120]}" for name, spec in coordinator.agents.items())
        chat_tool_catalog["agent.delegate"]["description"] += f" Registered profiles: {profile_names}. Select only one of these names."
    if coordinator is not None and configured_entry and configured_entry in coordinator.agents:
        entry = coordinator.agents[configured_entry]
        permitted = set(entry.tools)
        if coordinator.mode == "single" and "agent.delegate" in tools.names() and (not tools.allowlist or "agent.delegate" in tools.allowlist):
            permitted.add("agent.delegate")
        elif coordinator.mode in {"self_directed", "required"} and entry.delegation.get("enabled", False):
            permitted.update(coordinator._orchestration_tools(entry))
        chat_tool_catalog = {name: meta for name, meta in chat_tool_catalog.items() if name in permitted}
        if "agent.delegate" in chat_tool_catalog:
            cards = coordinator.eligible_profiles(configured_entry) if coordinator.mode in {"self_directed", "required"} else [{"id": name, "description": spec.description[:300]} for name, spec in coordinator.agents.items()]
            chat_tool_catalog["agent.delegate"]["description"] += " Eligible profiles: " + str(cards)[:3000]
        if "agent.plan" in chat_tool_catalog:
            chat_tool_catalog["agent.plan"]["description"] += " Eligible profiles: " + str(coordinator.eligible_profiles(configured_entry))[:3000]
    chat_allowed_tools = set(chat_tool_catalog)
    smart_system_prompt = build_smart_system_prompt(
        base_prompt=system_prompt,
        tools_catalog=chat_tool_catalog,
        harness_context=harness_context,
        memory_context=memory_context,
        skills_context=skills_context,
        project_context=project_context,
        config=config,
    )
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": smart_system_prompt,
        }
    ]

    session_state = {"always_approve": auto_approve_tools}

    def complete(prompt: str) -> str:
        start_time = time.perf_counter()
        plan_authorized = coordinator is None or coordinator.mode != "required"
        messages.append({"role": "user", "content": prompt})
        observations: list[dict[str, Any]] = []
        verification_runs: list[dict[str, Any]] = []
        step_count = 0
        last_mutation_step = 0

        for step in range(1, max_tool_steps + 1):
            if len(messages) > compact_threshold_messages:
                compacted = compact_history(messages)
                messages.clear()
                messages.extend(compacted)

            raw_window = [messages[0], *messages[-max_history_messages:]]
            window = apply_observation_masking(raw_window, keep_recent=2)
            current_max_tokens = int(deep_get(config, "chat.agent_loop.max_tokens", max_tokens))
            is_streaming = bool(deep_get(config, "chat.streaming.enabled", True))
            tool_mode = str(deep_get(config, "chat.agent_loop.tool_calling_mode", "auto")).lower()
            openai_tools = tools.to_openai_tools(sorted(chat_allowed_tools)) if loop_enabled and tool_mode in {"auto", "native"} else None

            def handle_failover(from_r: str, to_r: str, err: Exception) -> None:
                if ui.console:
                    ui.console.print(
                        f"[dim yellow]⚡ Failover: Route '{from_r}' encountered {type(err).__name__}, seamlessly switching to '{to_r}'...[/dim yellow]"
                    )
                else:
                    print(f"Failover: Route '{from_r}' failed ({err}), switching to '{to_r}'...")

            with ui.status(f"Thinking ({graph}.{node})..."):
                try:
                    reply_obj = registry.complete(
                        graph,
                        node,
                        window,
                        max_tokens=current_max_tokens,
                        stream=is_streaming,
                        tools=openai_tools,
                        on_failover=handle_failover,
                    )
                except ModelConfigurationError as exc:
                    raise SystemExit(str(exc)) from exc

            reply = str(reply_obj)
            messages.append({"role": "assistant", "content": reply})

            # Dual-Mode Tool Extraction: Native Tool Calling + Prompt JSON Parsing
            tool_calls: list[dict[str, Any]] = []
            if loop_enabled:
                native_calls = getattr(reply_obj, "tool_calls", None)
                if native_calls:
                    for tc in native_calls:
                        tool_calls.append({"tool": tc.name, "args": tc.arguments})
                else:
                    tool_calls = parse_tool_calls(reply)

            if (
                not tool_calls
                and loop_enabled
                and continuation_on_truncation
                and re.search(r'\{\s*["\']tool["\']\s*:', reply)
            ):
                repaired = repair_truncated_json(reply)
                tool_calls = parse_tool_calls(repaired)
                if not tool_calls and step < max_tool_steps:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response appeared to be truncated mid-tool call. "
                                "Please output the complete, valid JSON tool call."
                            ),
                        }
                    )
                    continue

            if not tool_calls:
                notice = verification_status_notice(
                    verification_runs,
                    last_mutation_step=last_mutation_step,
                    tool_budget_exhausted=False,
                    max_tool_steps=max_tool_steps,
                )
                if notice:
                    reply = f"{reply.rstrip()}\n\n{notice}"
                duration = time.perf_counter() - start_time
                ui.turn_summary(step_count=step_count, duration=duration)
                return reply

            step_observations: list[dict[str, Any]] = []
            for tool_call in tool_calls:
                step_count += 1
                tool_name = tool_call.get("tool", "unknown")
                tool_spec = tools.catalog().get(tool_name, {})
                requires_approval = bool(tool_spec.get("requires_approval") or tool_spec.get("mutates_state"))

                if tool_name not in chat_allowed_tools:
                    observation = {"ok": False, "tool": tool_name, "error": "tool is outside the configured chat profile scope"}
                    observations.append(observation)
                    step_observations.append(observation)
                    ui.tool_result(observation, duration_ms=0.0)
                    continue

                if coordinator is not None and coordinator.mode == "required" and not plan_authorized and tool_name != "agent.plan":
                    observation = {"ok": False, "tool": tool_name, "error": "a validated orchestration plan is required before tool execution"}
                    observations.append(observation)
                    step_observations.append(observation)
                    ui.tool_result(observation, duration_ms=0.0)
                    continue

                ui.tool_call(tool_call, step=step_count, max_steps=max_tool_steps, spec=tool_spec)

                # Human-in-the-loop permission check if tool requires approval
                if requires_approval and not session_state["always_approve"]:
                    approved, always = ui.request_tool_approval(tool_call, spec=tool_spec)
                    if always:
                        session_state["always_approve"] = True
                    if not approved:
                        observation = {
                            "ok": False,
                            "tool": tool_name,
                            "error": "Tool execution was declined by user.",
                        }
                        check_command = verification_command(tool_call)
                        if check_command is not None:
                            verification_runs.append(
                                {"step": step_count, "command": check_command, "exit_code": None}
                            )
                        observations.append(observation)
                        step_observations.append(observation)
                        ui.tool_result(observation, duration_ms=0.0)
                        continue

                tool_start = time.perf_counter()
                with ui.status(f"Executing {tool_name}..."):
                    observation = execute_chat_tool(tools, tool_call)
                if tool_name == "agent.plan" and observation.get("ok"):
                    plan_authorized = True
                tool_elapsed_ms = (time.perf_counter() - tool_start) * 1000.0

                check_command = verification_command(tool_call)
                if check_command is not None:
                    result_data = observation.get("result") if observation.get("ok") else None
                    exit_code = result_data.get("exit_code") if isinstance(result_data, dict) else None
                    verification_runs.append(
                        {
                            "step": step_count,
                            "command": check_command,
                            "exit_code": exit_code if isinstance(exit_code, int) else None,
                        }
                    )
                elif observation.get("ok") and tool_name in {
                    "file.write_text",
                    "file.replace_text",
                    "process.run",
                    "sandbox.run_python",
                }:
                    # Conservatively invalidate a prior check after any tool
                    # capable of changing workspace state.
                    last_mutation_step = step_count

                observations.append(observation)
                step_observations.append(observation)
                ui.tool_result(observation, duration_ms=tool_elapsed_ms)

            obs_data = (
                step_observations
                if len(step_observations) > 1
                else step_observations[0]
                if step_observations
                else {}
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool result(s):\n"
                        f"{json.dumps(obs_data, indent=2, default=str)}\n\n"
                        f"{tool_observation_prompt}"
                    ),
                }
            )

        messages.append(
            {
                "role": "user",
                "content": tool_budget_prompt,
            }
        )
        try:
            current_max_tokens = int(deep_get(config, "chat.agent_loop.max_tokens", max_tokens))
            is_streaming = bool(deep_get(config, "chat.streaming.enabled", True))
            with ui.status("Finalizing summary response..."):
                final_reply = registry.complete(
                    graph,
                    node,
                    [messages[0], *messages[-max_history_messages:]],
                    max_tokens=current_max_tokens,
                    stream=is_streaming,
                )
        except ModelConfigurationError as exc:
            raise SystemExit(str(exc)) from exc

        duration = time.perf_counter() - start_time
        ui.turn_summary(step_count=step_count, duration=duration)
        notice = verification_status_notice(
            verification_runs,
            last_mutation_step=last_mutation_step,
            tool_budget_exhausted=True,
            max_tool_steps=max_tool_steps,
        )
        if not parse_tool_calls(final_reply):
            if notice:
                final_reply = f"{str(final_reply).rstrip()}\n\n{notice}"
            messages.append({"role": "assistant", "content": str(final_reply)})
            return final_reply
        summary = summarize_tool_observations(observations)
        if notice:
            summary = f"{summary}\n\n{notice}"
        return summary

    once = getattr(args, "once", None)
    if once:
        ui.assistant(complete(once))
        return 0

    ui.header()
    while True:
        try:
            prompt = ui.prompt().strip()
        except (EOFError, KeyboardInterrupt):
            ui.newline()
            ui.goodbye()
            return 0
        if not prompt:
            continue
        command = prompt.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            ui.goodbye()
            return 0
        if command == "/help":
            ui.help()
            continue
        if command in {"/skills", "/skill"} or command.startswith("/skill "):
            ui.handle_skill_command(prompt)
            continue
        if command in {"/mcp"} or command.startswith("/mcp "):
            ui.handle_mcp_command(prompt)
            continue
        if command == "/context":
            ui.context(harness_context)
            continue
        if command == "/project":
            profile = detect_project(".")
            if ui.console:
                ui.console.print(f"[bold cyan]📁 Project Stack Profile:[/bold cyan] {profile.render_context_summary()}")
                ui.console.print(f"  [dim]Primary Domain:[/dim] [green]{profile.primary_domain}[/green]")
                ui.console.print(f"  [dim]Languages:[/dim] {', '.join(profile.languages) or 'None'}")
                ui.console.print(f"  [dim]Frameworks:[/dim] {', '.join(profile.frameworks) or 'None'}")
                ui.console.print(f"  [dim]Databases:[/dim] {', '.join(profile.database_types) or 'None'}")
                ui.console.print(f"  [dim]Recommended Packs:[/dim] [yellow]{', '.join(profile.recommended_packs)}[/yellow]")
            else:
                print(profile.render_context_summary())
            continue
        if command in {"/graphs", "/workflows"}:
            from harness.engine.graph_loader import list_graphs
            available_graphs = list_graphs(config=config)
            if ui.console:
                ui.console.print(f"[bold cyan]📊 Configured Workflows & Graphs ({len(available_graphs)}):[/bold cyan]")
                for g_name in available_graphs:
                    ui.console.print(f"  • [green]{g_name}[/green]")
            else:
                print("Available Graphs: " + ", ".join(available_graphs))
            continue
        if command == "/tools":
            ui.show_tools()
            continue
        if command in {"/models", "/routes"}:
            ui.show_models()
            continue
        if command.startswith(("/route", "/model")):
            switched = ui.handle_route_command(prompt)
            if switched:
                graph, node, _, _ = switched
                route = registry.route_for(graph, node)
            continue
        if command in {"/features", "/feature"} or command.startswith("/feature "):
            ui.handle_feature_command(prompt)
            continue
        if command == "/clear":
            ui.clear()
            continue
        if command.startswith("/config"):
            ui.handle_config_command(prompt)
            continue
        if command.startswith("/memory"):
            ui.handle_memory_command(prompt)
            continue
        if command.startswith("/session"):
            ui.handle_session_command(prompt, messages)
            continue
        if command == "/compact":
            compacted = compact_history(messages)
            messages.clear()
            messages.extend(compacted)
            ui.compact_notice(len(messages))
            continue
        if command.startswith("/verbose"):
            parts = prompt.strip().split()
            mode = parts[1].lower() if len(parts) > 1 else ("compact" if ui.tool_display == "verbose" else "verbose")
            if mode in {"on", "true", "full", "verbose"}:
                ui.tool_display = "verbose"
                ui.show_tool_args = True
                ui.show_tool_result_excerpt = True
                msg = "[green]✓ Verbose tool inspection ENABLED (full arguments, outputs, and subagent traces shown).[/green]"
            elif mode in {"off", "compact"}:
                ui.tool_display = "compact"
                msg = "[yellow]✓ Compact tool mode ENABLED (single-line tool badges).[/yellow]"
            elif mode in {"hidden", "quiet", "mute", "chatbot"}:
                ui.tool_display = "hidden"
                msg = "[magenta]✓ Pure Chatbot mode ENABLED (tool lines hidden from display).[/magenta]"
            else:
                msg = f"[dim]Unknown mode: {mode}. Use /verbose on, /verbose compact, or /verbose hidden.[/dim]"
            if ui.console:
                ui.console.print(msg)
            else:
                print(msg)
            continue
        if command.startswith(("/subagents", "/swarm")):
            parts = prompt.strip().split(maxsplit=1)
            sub_task = parts[1] if len(parts) > 1 else "Inspect workspace and summarize status"
            if ui.console:
                ui.console.print(f"\n[bold #a855f7]Spawning configured swarm for subtask:[/bold #a855f7] [cyan]{sub_task}[/cyan]")
            selected = [name for name in ("analyst", "reviewer") if coordinator and name in coordinator.agents]
            if not coordinator or not selected:
                res = {"status": "failed", "error": "No configured swarm profiles are available."}
            else:
                res = asyncio.run(coordinator.run_fan_out(selected, sub_task, synthesizer_agent=coordinator.planner_profile))
            if ui.console:
                for a_name, a_out in res.get("findings", {}).items():
                    ui.console.print(f"  [bold cyan]● Subagent [{a_name}]:[/bold cyan] {a_out}")
                ui.console.print(f"[bold green]✓ Swarm Synthesis:[/bold green]\n{res.get('synthesis')}\n")
            else:
                print(json.dumps(res, indent=2, default=str))
            continue
        if command == "/auto":
            session_state["always_approve"] = not session_state["always_approve"]
            ui.auto_approve_notice(session_state["always_approve"])
            continue
        if prompt.startswith("/"):
            cmd_name = prompt.split()[0].lower()
            custom_prompt = memory.get_custom_command(cmd_name)
            if custom_prompt:
                extra_args = prompt[len(cmd_name):].strip()
                full_prompt = f"{custom_prompt}\n\nAdditional user input: {extra_args}" if extra_args else custom_prompt
                try:
                    ui.assistant(complete(full_prompt))
                except KeyboardInterrupt:
                    ui.interrupted()
                continue

        try:
            ui.assistant(complete(prompt))
        except KeyboardInterrupt:
            ui.interrupted()
            continue
        except Exception as exc:  # noqa: BLE001
            if ui.console:
                ui.console.print(f"[bold red]✗ Model or Execution Error:[/bold red] [yellow]{exc}[/yellow]")
                ui.console.print("[dim]Tip: Switch models using `/route <name>` or inspect `/models`.[/dim]")
            else:
                print(f"Error: {exc}")
            continue


def run_secrets(args: argparse.Namespace) -> int:
    store_path = args.secret_store or str(configured_secret_store(load_config(args.config)))
    if args.secret_command == "set":
        value = args.value
        if value is None:
            value = getpass.getpass(f"{args.name}: ")
        set_secret(args.name, value, path=store_path)
        print(f"stored {args.name} in {store_path}")
        return 0
    if args.secret_command == "remove":
        removed = remove_secret(args.name, path=store_path)
        print(f"removed {args.name}" if removed else f"{args.name} was not stored")
        return 0
    if args.secret_command == "list":
        for name in list_secret_names(path=store_path):
            print(name)
        return 0
    if args.secret_command == "path":
        print(store_path)
        return 0
    raise SystemExit(f"unknown secrets command: {args.secret_command}")


def configure_output() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


class ChatUI:
    """Polished terminal user interface inspired by Codex and Claude Code."""

    def __init__(
        self,
        config: dict[str, Any],
        graph: str,
        node: str,
        model: str,
        provider: str = "nim",
        tools: ToolRegistry | None = None,
        models: ModelRegistry | None = None,
        memory: MemoryStore | None = None,
        features: FeatureRegistry | None = None,
        skills: SkillRegistry | None = None,
        mcp: MCPRegistry | None = None,
    ) -> None:
        self.config = config
        self.graph = graph
        self.node = node
        self.model = model
        self.provider = provider
        self.tools = tools
        self.models = models
        self.memory = memory
        self.features = features or FeatureRegistry()
        self.skills = skills or SkillRegistry(workspace_root=".")
        self.mcp = mcp or MCPRegistry({})
        self.style = str(deep_get(config, "chat.ui.style", "rich"))
        self.tool_display = str(deep_get(config, "chat.ui.tool_display", "compact"))
        self.show_tool_args = bool(deep_get(config, "chat.ui.show_tool_args", True))
        self.show_tool_result_excerpt = bool(deep_get(config, "chat.ui.show_tool_result_excerpt", True))
        self.tool_result_excerpt_chars = int(deep_get(config, "chat.ui.tool_result_excerpt_chars", 1200))
        self.show_step_timing = bool(deep_get(config, "chat.ui.show_step_timing", True))
        self.spinner = str(deep_get(config, "chat.ui.spinner", "dots"))
        self.console = Console() if Console is not None and self.style == "rich" else None
        self._pending_tool: dict[str, Any] | None = None

    def header(self) -> None:
        if self.console and Panel and Table and Text:
            header_table = Table.grid(expand=True, padding=(0, 2))
            header_table.add_column(ratio=3)
            header_table.add_column(ratio=3)

            col1 = Text()
            col1.append("Route:    ", style="bold #94a3b8")
            col1.append(f"{self.graph}.{self.node}\n", style="bold cyan")
            col1.append("Model:    ", style="bold #94a3b8")
            col1.append(f"{self.model}\n", style="bold green")
            col1.append("Provider: ", style="bold #94a3b8")
            col1.append(f"{self.provider}", style="bold magenta")

            col2 = Text()
            col2.append("Loop:     ", style="bold #94a3b8")
            col2.append(f"{self._loop_summary()}\n", style="bold yellow")
            col2.append("Commands: ", style="bold #94a3b8")
            col2.append("/help  /skills  /mcp  /route  /features  /auto  /tools", style="dim cyan")

            header_table.add_row(col1, col2)

            app_name = str(deep_get(self.config, "app.name", "Ivy Agent Harness"))
            app_version = str(deep_get(self.config, "app.version", "0.1.0"))
            banner = Panel(
                header_table,
                title=f"[bold white on #1e3a8a]  ⚡ {app_name.upper()}  [/bold white on #1e3a8a] [bold #38bdf8]v{app_version}[/bold #38bdf8]",
                title_align="left",
                border_style="#3b82f6",
                box=box.ROUNDED if box else None,
                padding=(1, 2),
            )
            self.console.print(banner)
            return

        app_name = str(deep_get(self.config, "app.name", "Ivy Agent Harness"))
        app_version = str(deep_get(self.config, "app.version", "0.1.0"))
        print("=" * 72)
        print(f"{app_name} v{app_version}")
        print(f"Route: {self.graph}.{self.node}")
        print(f"Model: {self.model} ({self.provider})")
        print(f"Loop: {self._loop_summary()}")
        print("Commands: /help, /route, /features, /auto, /tools, /models, /memory, /session, /compact, /config, /clear, /exit")
        print("=" * 72)

    def prompt(self) -> str:
        if self.console:
            return self.console.input("\n[bold #f59e0b]👤 You[/bold #f59e0b] [bold #38bdf8]❯[/bold #38bdf8] ")
        return input("\nYou> ")

    def status(self, label: str):
        if self.console:
            return self.console.status(f"[bold #38bdf8]{label}[/bold #38bdf8]", spinner=self.spinner)
        print(f"... {label}")
        return nullcontext()

    def interrupted(self) -> None:
        if self.console and Text:
            self.console.print("\n[bold yellow]⚡ Interrupted by user[/bold yellow]\n")
        else:
            print("\n⚡ Interrupted by user\n")

    def auto_approve_notice(self, enabled: bool) -> None:
        state = "[green]ENABLED (autonomous mode - all tools auto-approved)[/green]" if enabled else "[yellow]DISABLED (manual approval for state-mutating tools)[/yellow]"
        if self.console:
            self.console.print(f"\n⚡ Auto-approval {state}\n")
        else:
            print(f"\nAuto-approval {state}\n")

    def compact_notice(self, message_count: int) -> None:
        if self.console:
            self.console.print(f"[green]✓ Conversation history compacted into working memory ({message_count} messages active).[/green]")
        else:
            print(f"Conversation compacted ({message_count} messages).")

    def tool_call(
        self,
        tool_call: dict[str, Any],
        step: int = 1,
        max_steps: int = 6,
        spec: dict[str, Any] | None = None,
    ) -> None:
        tool_name = tool_call.get("tool", "unknown")
        category = (spec or {}).get("category", "filesystem")
        self._pending_tool = {
            "tool_call": tool_call,
            "step": step,
            "max_steps": max_steps,
            "category": category,
        }

        if self.tool_display == "verbose":
            args_text = json.dumps(tool_call.get("args", {}), indent=2, default=str)
            if self.console and Panel and Syntax:
                title_text = f"[bold yellow]⚙ TOOL CALL [{step}/{max_steps}]:[/bold yellow] [bold white]{tool_name}[/bold white]"
                body = Syntax(args_text, "json", theme="monokai", word_wrap=True) if self.show_tool_args else Text("Arguments omitted", style="dim")
                self.console.print(
                    Panel(
                        body,
                        title=title_text,
                        title_align="left",
                        border_style="#eab308",
                        box=box.ROUNDED if box else None,
                        padding=(0, 1),
                    )
                )
                return
            print(f"\ntool> {tool_name} [{step}/{max_steps}]")
            if self.show_tool_args:
                print(args_text)

    def request_tool_approval(self, tool_call: dict[str, Any], spec: dict[str, Any] | None = None) -> tuple[bool, bool]:
        tool_name = tool_call.get("tool", "unknown")
        args_text = json.dumps(tool_call.get("args", {}), indent=2, default=str)
        if self.console and Panel:
            warning_text = Text()
            warning_text.append(f"Tool: {tool_name}\n", style="bold yellow")
            warning_text.append(f"Arguments: {args_text}\n", style="dim")
            warning_text.append("This tool may mutate state or run external commands.\n", style="bold red")

            self.console.print(
                Panel(
                    warning_text,
                    title="[bold red]⚠️  TOOL EXECUTION APPROVAL REQUIRED[/bold red]",
                    border_style="red",
                    box=box.ROUNDED if box else None,
                )
            )
            choice = self.console.input("[bold yellow]Approve execution? [y (once) / a (always for session) / n (decline)]: [/bold yellow]").strip().lower()
        else:
            print(f"\n⚠️  TOOL APPROVAL: {tool_name} with args {args_text}")
            choice = input("Approve execution? [y (once) / a (always for session) / n (decline)]: ").strip().lower()

        if choice in {"a", "always"}:
            return True, True
        if choice in {"y", "yes"}:
            return True, False
        return False, False

    def tool_result(self, observation: dict[str, Any], duration_ms: float = 0.0) -> None:
        ok = bool(observation.get("ok"))
        pending = self._pending_tool or {}
        tool_call = pending.get("tool_call", {})
        tool_name = observation.get("tool") or tool_call.get("tool", "unknown")
        args = tool_call.get("args", {})
        args_str = summarize_tool_args(tool_name, args)
        category = pending.get("category", "filesystem")

        category_icon = {
            "filesystem": "📁",
            "shell": "🐚",
            "web": "🌐",
            "sandbox": "🛡️",
            "tooling": "🛠️",
            "environment": "⚙️",
        }.get(category, "⚡")

        # Pure Chatbot mode (suppresses tool output)
        if self.tool_display in {"hidden", "quiet", "mute", "chatbot"}:
            return

        # Check for Subagent Execution
        result_data = observation.get("result", {})
        if isinstance(result_data, dict) and "delegated_to" in result_data:
            sub_agent = result_data["delegated_to"]
            sub_tools = result_data.get("tools_called", [])
            sub_obs = result_data.get("observation", "")
            if self.console and Panel and Text:
                sub_text = Text()
                sub_text.append(f"🤖 Persona: {sub_agent}\n", style="bold cyan")
                if sub_tools:
                    sub_text.append("Inner Subagent Tool Actions:\n", style="bold yellow")
                    for st in sub_tools:
                        sub_text.append(f"  ● {st.get('tool')}({st.get('args', {})}) ➔ {st.get('result')}\n", style="green")
                sub_text.append(f"Observation: {sub_obs}\n", style="white")
                self.console.print(
                    Panel(
                        sub_text,
                        title=f"[bold #a855f7]⚡ SUBAGENT EXECUTION [{sub_agent}][/bold #a855f7]",
                        border_style="#a855f7",
                        box=box.ROUNDED if box else None,
                        padding=(0, 1),
                    )
                )
                return

        # Compact Claude Code / Codex mode
        if self.tool_display == "compact":
            timing_str = f" ({duration_ms:.1f}ms)" if self.show_step_timing and duration_ms > 0 else ""
            if ok:
                res_str = summarize_tool_result_data(tool_name, observation.get("result"))
                if self.console and Text:
                    line = Text()
                    line.append("  ● ", style="bold #38bdf8")
                    line.append(f"{category_icon} {tool_name}", style="bold yellow")
                    if args_str:
                        line.append(f" {args_str}", style="dim white")
                    line.append(f" ➔ ✓ {res_str}", style="bold green")
                    line.append(f"{timing_str}", style="dim")
                    self.console.print(line)
                else:
                    print(f"tool> {tool_name} {args_str} -> ok ({res_str}){timing_str}")
            else:
                err_msg = str(observation.get("error", "Failed"))[:60]
                if self.console and Text:
                    line = Text()
                    line.append("  ● ", style="bold red")
                    line.append(f"❌ {tool_name}", style="bold red")
                    if args_str:
                        line.append(f" {args_str}", style="dim white")
                    line.append(f" ➔ ✗ {err_msg}", style="bold red")
                    line.append(f"{timing_str}", style="dim")
                    self.console.print(line)
                else:
                    print(f"tool> {tool_name} {args_str} -> error ({err_msg}){timing_str}")
            return

        # Verbose Panel mode
        timing_str = f" in {duration_ms:.1f}ms" if self.show_step_timing and duration_ms > 0 else ""
        title = f"[bold green]✓ SUCCESS{timing_str}[/bold green]" if ok else f"[bold red]✗ FAILED{timing_str}[/bold red]"
        border = "#22c55e" if ok else "#ef4444"
        text = json.dumps(observation, indent=2, default=str)
        if len(text) > self.tool_result_excerpt_chars:
            text = text[: self.tool_result_excerpt_chars] + "\n... (truncated)"

        if self.console and Panel and Syntax:
            body = Syntax(text, "json", theme="monokai", word_wrap=True) if self.show_tool_result_excerpt else Text("Result hidden", style="dim")
            self.console.print(
                Panel(
                    body,
                    title=title,
                    title_align="left",
                    border_style=border,
                    box=box.ROUNDED if box else None,
                    padding=(0, 1),
                )
            )
            return

        if self.show_tool_result_excerpt:
            status_text = "tool result: ok" if ok else "tool result: failed"
            print(f"{status_text}{timing_str}")
            print(text)

    def turn_summary(self, step_count: int, duration: float) -> None:
        if not self.console or not self.show_step_timing:
            return
        summary = f"[dim cyan]⚡ Turn completed · {step_count} tool step(s) · {duration:.2f}s[/dim cyan]"
        self.console.print(summary)

    def assistant(self, reply: str) -> None:
        agent_name = str(deep_get(self.config, "chat.agent_name", "ivy")).lower()
        if self.console and Panel and Markdown:
            md = Markdown(reply.strip())
            self.console.print(
                Panel(
                    md,
                    title=f"[bold white on #0284c7]  ⚡ {agent_name.upper()}  [/bold white on #0284c7]",
                    title_align="left",
                    border_style="#38bdf8",
                    box=box.ROUNDED if box else None,
                    padding=(1, 2),
                )
            )
            return

        print(f"\n{agent_name.upper()}>")
        print(reply.strip())
        print()

    def help(self) -> None:
        if self.console and Table and Panel:
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("Command", style="bold green", width=16)
            table.add_column("Description", style="white")

            table.add_row("/help", "Show this interactive command reference")
            table.add_row("/skills", "List all installed skills, view details, or author new skills")
            table.add_row("/mcp", "List and manage connected Model Context Protocol (MCP) servers")
            table.add_row("/route [name]", "Show or switch active model route (/route nim.llama-70b)")
            table.add_row("/features", "View and manage dynamic feature flags (/feature enable/disable/toggle)")
            table.add_row("/auto", "Toggle autonomous execution mode (auto-approve state-mutating tools)")
            table.add_row("/tools", "List all registered tools, categories, and permission gates")
            table.add_row("/models", "List configured model routes, providers, and parameters")
            table.add_row("/memory", "View and manage long-term workspace memory (/memory add/remove/clear)")
            table.add_row("/session", "Save or resume conversation sessions (/session save/load/list)")
            table.add_row("/compact", "Condense conversation history into working memory ledger")
            table.add_row("/context", "Display the local project context provided to Ivy")
            table.add_row("/project", "Inspect auto-detected client stack, domain, and recommended packs")
            table.add_row("/graphs", "List available declarative workflows and graph state machines")
            table.add_row("/config", "View or modify configuration parameters (/config set <key> <val>)")
            table.add_row("/clear", "Clear the terminal screen and redraw the header")
            table.add_row("/exit, /quit", "Gracefully terminate the chat session")

            self.console.print(
                Panel(
                    table,
                    title="[bold cyan]Command Reference[/bold cyan]",
                    border_style="cyan",
                    box=box.ROUNDED if box else None,
                )
            )
            return

        print("\nCommands:")
        print("  /help          Show this help")
        print("  /skills        List and inspect installed specialized skills")
        print("  /mcp           List and inspect connected MCP servers")
        print("  /route [name]  Show or switch active model route")
        print("  /features      View and manage dynamic feature flags")
        print("  /auto          Toggle autonomous mode")
        print("  /tools         List all registered tools")
        print("  /models        List configured model routes")
        print("  /memory        Manage workspace memories")
        print("  /session       Save/load conversation sessions")
        print("  /compact       Compact conversation history")
        print("  /context       Show local project context sent to the model")
        print("  /config        Show/modify configuration")
        print("  /clear    Clear terminal")
        print("  /exit     Quit chat\n")

    def show_tools(self) -> None:
        if not self.tools:
            print("No tool registry loaded.")
            return
        catalog = self.tools.catalog()
        if self.console and Table and Panel:
            table = Table(box=box.ROUNDED if box else box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("Tool Name", style="bold green")
            table.add_column("Category", style="cyan")
            table.add_column("Approval", style="yellow")
            table.add_column("Mutates", style="magenta")
            table.add_column("Description", style="white")

            for name, meta in catalog.items():
                appr = "Required" if meta.get("requires_approval") else "Auto"
                mut = "Yes" if meta.get("mutates_state") else "No"
                table.add_row(name, meta.get("category", ""), appr, mut, meta.get("description", ""))

            self.console.print(
                Panel(table, title="[bold cyan]Registered Tools Catalog[/bold cyan]", border_style="cyan")
            )
            return

        print("\nRegistered Tools:")
        for name, meta in catalog.items():
            print(f"  - {name}: {meta.get('description', '')}")

    def show_models(self) -> None:
        if not self.models:
            print("No model registry loaded.")
            return
        routes = self.models.routes_metadata()
        if self.console and Table and Panel:
            table = Table(box=box.ROUNDED if box else box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("Route Name", style="bold green")
            table.add_column("Provider", style="cyan")
            table.add_column("Model Identifier", style="white")
            table.add_column("API Key Env", style="yellow")
            table.add_column("Key Set", style="magenta")

            for name, meta in routes.items():
                has_key = "[green]✓ Yes[/green]" if meta.get("has_api_key") else "[red]✗ No[/red]"
                table.add_row(name, meta.get("provider", ""), meta.get("model", ""), meta.get("api_key_env") or "None", has_key)

            self.console.print(
                Panel(table, title="[bold cyan]Configured Model Routes[/bold cyan]", border_style="cyan")
            )
            return

        print("\nConfigured Model Routes:")
        for name, meta in routes.items():
            print(f"  - {name}: {meta.get('model', '')} ({meta.get('provider', '')})")

    def handle_memory_command(self, prompt: str) -> None:
        if not self.memory:
            print("Memory store unavailable.")
            return
        parts = prompt.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "list"

        if action in {"list", "show"}:
            memories = self.memory.list_all()
            guidelines = self.memory.load_project_guidelines()
            if self.console and Table and Panel:
                table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
                table.add_column("#ID", style="bold yellow", width=6)
                table.add_column("Category", style="cyan", width=12)
                table.add_column("Memory / Rule", style="white")

                for m in memories:
                    table.add_row(str(m.id), m.category, m.text)

                guide_text = f"\n[bold magenta]Project Guidelines Ingested:[/bold magenta]\n{guidelines}" if guidelines else ""
                self.console.print(
                    Panel(
                        table,
                        title="[bold cyan]Long-Term Workspace Memory (.harness/memory.json)[/bold cyan]",
                        subtitle=f"[dim]Total: {len(memories)} entries. Use `/memory add <text>` or `/memory remove <id>`[/dim]",
                        border_style="cyan",
                    )
                )
                if guide_text:
                    self.console.print(Panel(guide_text, border_style="magenta", box=box.ROUNDED if box else None))
                return

            print(f"Memories ({len(memories)}):")
            for m in memories:
                print(f"  #{m.id} [{m.category}] {m.text}")
            return

        if action == "add" and len(parts) > 2:
            entry = self.memory.add(parts[2])
            if self.console:
                self.console.print(f"[green]✓ Saved memory #{entry.id}: {entry.text}[/green]")
            else:
                print(f"Saved memory #{entry.id}")
            return

        if action in {"remove", "delete"} and len(parts) > 2:
            try:
                mem_id = int(parts[2])
                removed = self.memory.remove(mem_id)
                if self.console:
                    msg = f"[green]✓ Removed memory #{mem_id}[/green]" if removed else f"[yellow]Memory #{mem_id} not found[/yellow]"
                    self.console.print(msg)
                else:
                    print(f"Removed #{mem_id}" if removed else f"#{mem_id} not found")
            except ValueError:
                print("Invalid memory ID integer.")
            return

        if action == "clear":
            self.memory.clear()
            if self.console:
                self.console.print("[green]✓ Cleared all long-term memories.[/green]")
            else:
                print("Cleared all memories.")
            return

        print("Usage: /memory [list | add <text> | remove <id> | clear]")

    def handle_session_command(self, prompt: str, messages: list[dict[str, Any]]) -> None:
        if not self.memory:
            print("Session manager unavailable.")
            return
        parts = prompt.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "list"

        if action in {"list", "show"}:
            sessions = self.memory.list_sessions()
            if self.console and Table and Panel:
                table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
                table.add_column("Saved Session", style="bold green")

                for s in sessions:
                    table.add_row(s)

                self.console.print(
                    Panel(
                        table,
                        title="[bold cyan]Saved Chat Sessions (.harness/sessions/)[/bold cyan]",
                        subtitle="[dim]Use `/session save <name>` or `/session load <name>`[/dim]",
                        border_style="cyan",
                    )
                )
                return
            print("Saved sessions:", ", ".join(sessions) if sessions else "None")
            return

        if action == "save" and len(parts) > 2:
            name = parts[2]
            path = self.memory.save_session(name, messages)
            if self.console:
                self.console.print(f"[green]✓ Session saved to {path}[/green]")
            else:
                print(f"Session saved to {path}")
            return

        if action == "load" and len(parts) > 2:
            name = parts[2]
            loaded = self.memory.load_session(name)
            if loaded:
                messages.clear()
                messages.extend(loaded)
                if self.console:
                    self.console.print(f"[green]✓ Loaded session '{name}' ({len(messages)} messages).[/green]")
                else:
                    print(f"Loaded session '{name}'")
            else:
                if self.console:
                    self.console.print(f"[red]✗ Session '{name}' not found.[/red]")
                else:
                    print(f"Session '{name}' not found.")
            return

        print("Usage: /session [list | save <name> | load <name>]")

    def show_features(self) -> None:
        features_list = self.features.snapshot(self.config)
        if self.console and Table and Panel:
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("Category", style="cyan", width=18)
            table.add_column("Feature Key", style="bold green")
            table.add_column("Status", style="bold white", width=12)
            table.add_column("Description", style="white")

            for feat in features_list:
                val = feat["value"]
                if isinstance(val, bool):
                    status_text = "[green]✓ Enabled[/green]" if val else "[yellow]✗ Disabled[/yellow]"
                else:
                    status_text = f"[bold cyan]{val}[/bold cyan]"
                table.add_row(feat["category"], feat["key"], status_text, feat["description"])

            self.console.print(
                Panel(
                    table,
                    title="[bold cyan]⚡ Dynamic Feature Flags & Capabilities[/bold cyan]",
                    subtitle="[dim]Use `/feature enable <key>` or `/feature disable <key>` or `/feature toggle <key>`[/dim]",
                    border_style="cyan",
                )
            )
            return

        print("\nDynamic Feature Flags:")
        for feat in features_list:
            print(f"  [{feat['category']}] {feat['key']}: {feat['value']} - {feat['description']}")

    def handle_feature_command(self, prompt: str) -> None:
        parts = prompt.split()
        if len(parts) == 1:
            self.show_features()
            return

        action = parts[1].lower()
        if action in {"list", "show"}:
            self.show_features()
            return

        if action in {"enable", "on"} and len(parts) > 2:
            key = parts[2]
            try:
                self.features.set_value(key, True, self.config)
                if self.console:
                    self.console.print(f"[green]✓ Feature '{key}' enabled.[/green]")
                else:
                    print(f"Feature '{key}' enabled.")
            except (KeyError, ValueError, TypeError) as exc:
                print(f"Error: {exc}")
            return

        if action in {"disable", "off"} and len(parts) > 2:
            key = parts[2]
            try:
                self.features.set_value(key, False, self.config)
                if self.console:
                    self.console.print(f"[yellow]✗ Feature '{key}' disabled.[/yellow]")
                else:
                    print(f"Feature '{key}' disabled.")
            except (KeyError, ValueError, TypeError) as exc:
                print(f"Error: {exc}")
            return

        if action == "toggle" and len(parts) > 2:
            key = parts[2]
            try:
                new_state = self.features.toggle(key, self.config)
                state_str = "enabled" if new_state else "disabled"
                if self.console:
                    self.console.print(f"[green]✓ Feature '{key}' toggled {state_str}.[/green]")
                else:
                    print(f"Feature '{key}' toggled {state_str}.")
            except (KeyError, ValueError, TypeError) as exc:
                print(f"Error: {exc}")
            return

        if action == "set" and len(parts) > 3:
            key, val = parts[2], parts[3]
            try:
                self.features.set_value(key, val, self.config)
                if self.console:
                    self.console.print(f"[green]✓ Feature '{key}' set to {val}.[/green]")
                else:
                    print(f"Feature '{key}' set to {val}.")
            except (KeyError, ValueError, TypeError) as exc:
                print(f"Error: {exc}")
            return

        print("Usage: /feature [list | enable <key> | disable <key> | toggle <key> | set <key> <val>]")

    def handle_route_command(self, prompt: str) -> tuple[str, str, str, str] | None:
        parts = prompt.split()
        if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() in {"list", "show"}):
            self.show_models()
            return None

        route_target = parts[1]
        if not self.models:
            print("Model registry unavailable.")
            return None

        if "." in route_target:
            g, n = route_target.split(".", 1)
        else:
            g, n = "nim", route_target
            if f"{g}.{n}" not in self.models.routes and route_target in self.models.routes:
                g, n = route_target, "example"

        try:
            route = self.models.route_for(g, n)
            self.graph = g
            self.node = n
            self.model = route.model
            self.provider = route.provider
            if self.console:
                self.console.print(f"[bold green]✓ Switched active route to [cyan]{g}.{n}[/cyan] ({route.model})[/bold green]")
            else:
                print(f"Switched active route to {g}.{n} ({route.model})")
            return g, n, route.model, route.provider
        except ModelConfigurationError as exc:
            if self.console:
                self.console.print(f"[red]✗ Route '{route_target}' not found: {exc}[/red]")
            else:
                print(f"Route '{route_target}' not found.")
            return None

    def show_skills(self) -> None:
        skills = self.skills.list_skills()
        if self.console and Table and Panel:
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("Skill Name", style="bold green", width=24)
            table.add_column("Scope", style="magenta", width=11)
            table.add_column("Version", style="cyan", width=8)
            table.add_column("Tags", style="yellow", width=16)
            table.add_column("Description", style="white")

            for s in skills:
                m = s.metadata
                tags_str = ", ".join(m.tags) if m.tags else "general"
                scope_style = "[bold magenta]global[/bold magenta]" if m.scope == "global" else "[green]workspace[/green]"
                table.add_row(m.name, scope_style, m.version, tags_str, m.description)

            if not skills:
                table.add_row("(none)", "-", "-", "-", "No skills found in workspace or ~/.ivy/skills/")

            self.console.print(
                Panel(
                    table,
                    title="[bold cyan]⚡ Installed Specialized Skills (Global & Workspace)[/bold cyan]",
                    subtitle="[dim]Use `/skill view <name>` to view instructions or `/skill create [--global] <name>` to author[/dim]",
                    border_style="cyan",
                )
            )
            return

        print("\nInstalled Specialized Skills:")
        for s in skills:
            m = s.metadata
            print(f"  - [{m.scope}] {m.name} (v{m.version}): {m.description}")

    def handle_skill_command(self, prompt: str) -> None:
        parts = prompt.split()
        if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() in {"list", "show"}):
            self.show_skills()
            return

        action = parts[1].lower()
        if action == "view" and len(parts) > 2:
            skill_name = parts[2]
            skill = self.skills.get_skill(skill_name)
            if not skill:
                if self.console:
                    self.console.print(f"[red]✗ Skill '{skill_name}' not found.[/red]")
                else:
                    print(f"Skill '{skill_name}' not found.")
                return
            m = skill.metadata
            if self.console and Panel and Markdown:
                self.console.print(
                    Panel(
                        Markdown(skill.instructions),
                        title=f"[bold cyan]Skill: {m.name} [{m.scope}] (v{m.version})[/bold cyan]",
                        subtitle=f"[dim]Path: {m.path}[/dim]",
                        border_style="cyan",
                    )
                )
            else:
                print(f"\n--- Skill: {m.name} [{m.scope}] ({m.path}) ---")
                print(skill.instructions)
            return

        if action in {"create", "new"} and len(parts) > 2:
            is_global = False
            remaining = parts[2:]
            if remaining and remaining[0].lower() in {"--global", "-g"}:
                is_global = True
                remaining = remaining[1:]

            if not remaining:
                print("Error: skill name is required.")
                return

            skill_name = remaining[0]
            desc = " ".join(remaining[1:]) if len(remaining) > 1 else "Specialized agent skill"
            instructions = (
                f"# {skill_name}\n\n"
                f"## Overview\n{desc}\n\n"
                "## Workflows\n"
                "1. Step one\n"
                "2. Step two\n"
            )
            created = self.skills.create_skill(
                name=skill_name,
                description=desc,
                instructions=instructions,
                global_scope=is_global,
            )
            scope_badge = "global (~/.ivy/skills)" if is_global else "workspace"
            if self.console:
                self.console.print(
                    f"[bold green]✓ Created new {scope_badge} skill '[cyan]{created.metadata.name}[/cyan]' at [dim]{created.metadata.path}[/dim][/bold green]"
                )
            else:
                print(f"Created new {scope_badge} skill '{created.metadata.name}' at {created.metadata.path}")
            return

        print("Usage: /skill [list | view <name> | create [--global] <name> [description]]")

    def show_mcp(self) -> None:
        servers = self.mcp.names()
        if self.console and Table and Panel:
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
            table.add_column("MCP Server", style="bold green", width=18)
            table.add_column("Transport", style="cyan", width=12)
            table.add_column("Command / Target", style="white")

            for name in servers:
                cfg = self.mcp.get_server_config(name)
                target = f"{cfg.command} {' '.join(cfg.args)}" if cfg.command else (cfg.url or "N/A")
                table.add_row(name, cfg.transport, target)

            if not servers:
                table.add_row("(none)", "stdio", "No external MCP servers configured in config.yaml")

            self.console.print(
                Panel(
                    table,
                    title="[bold cyan]🔌 Model Context Protocol (MCP) Servers[/bold cyan]",
                    subtitle="[dim]Configure servers in `config.yaml` under `mcp_servers`[/dim]",
                    border_style="cyan",
                )
            )
            return

        print("\nConfigured MCP Servers:")
        for name in servers:
            cfg = self.mcp.get_server_config(name)
            print(f"  - {name} ({cfg.transport}): {cfg.command}")
        if not servers:
            print("  (no MCP servers configured)")

    def handle_mcp_command(self, prompt: str) -> None:
        parts = prompt.split()
        if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() in {"list", "show"}):
            self.show_mcp()
            return

        action = parts[1].lower()
        if action == "tools":
            tools = self.mcp.discover_tools()
            if self.console and Table and Panel:
                table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
                table.add_column("MCP Tool", style="bold green")
                table.add_column("Server", style="cyan")
                table.add_column("Description", style="white")

                for t in tools:
                    table.add_row(t.full_name, t.server_name, t.description)
                if not tools:
                    table.add_row("(none)", "-", "No MCP tools discovered")

                self.console.print(
                    Panel(
                        table,
                        title="[bold cyan]🔌 Discovered MCP Tools[/bold cyan]",
                        border_style="cyan",
                    )
                )
            else:
                for t in tools:
                    print(f"  - {t.full_name}: {t.description}")
            return

        print("Usage: /mcp [list | tools]")

    def handle_config_command(self, prompt: str) -> None:
        parts = prompt.split()
        if len(parts) == 1:
            loop_enabled = deep_get(self.config, "chat.agent_loop.enabled", True)
            steps = deep_get(self.config, "chat.agent_loop.max_tool_steps", 10)
            max_tok = deep_get(self.config, "chat.agent_loop.max_tokens", 4096)
            streaming = deep_get(self.config, "chat.streaming.enabled", True)
            auto_appr = deep_get(self.config, "chat.agent_loop.auto_approve_tools", False)
            ui_style = deep_get(self.config, "chat.ui.style", "rich")
            show_args = deep_get(self.config, "chat.ui.show_tool_args", True)

            if self.console and Table and Panel:
                table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
                table.add_column("Config Key / Feature", style="bold green")
                table.add_column("Current Value", style="white")

                table.add_row("chat.agent_loop.enabled", str(loop_enabled))
                table.add_row("chat.agent_loop.max_tool_steps", str(steps))
                table.add_row("chat.agent_loop.max_tokens", str(max_tok))
                table.add_row("chat.streaming.enabled", str(streaming))
                table.add_row("chat.agent_loop.auto_approve_tools", str(auto_appr))
                table.add_row("chat.ui.style", str(ui_style))
                table.add_row("chat.ui.tool_display", str(self.tool_display))
                table.add_row("chat.ui.show_tool_args", str(show_args))
                table.add_row("chat.ui.tool_result_excerpt_chars", str(self.tool_result_excerpt_chars))

                self.console.print(
                    Panel(
                        table,
                        title="[bold cyan]Agent Loop & UI Configuration[/bold cyan]",
                        subtitle="[dim]Use `/config set <key> <value>` or `/features` for feature flags[/dim]",
                        border_style="cyan",
                    )
                )
                return

            print("Config settings:")
            print(f"  chat.agent_loop.max_tool_steps = {steps}")
            print(f"  chat.agent_loop.max_tokens = {max_tok}")
            print(f"  chat.streaming.enabled = {streaming}")
            print(f"  chat.agent_loop.enabled = {loop_enabled}")
            print(f"  chat.ui.tool_display = {self.tool_display}")
            return

        if len(parts) >= 4 and parts[1] == "set":
            key, val = parts[2], parts[3]
            # Check if key is registered in features
            if self.features.get_feature(key):
                try:
                    self.features.set_value(key, val, self.config)
                    if key == "ui.tool_display":
                        self.tool_display = str(val)
                    elif key == "ui.show_tool_args":
                        self.show_tool_args = val.lower() in {"true", "1", "yes"}
                    if self.console:
                        self.console.print(f"[green]✓ Updated {key} to {val}[/green]")
                    else:
                        print(f"Updated {key} to {val}")
                    return
                except (ValueError, TypeError) as exc:
                    print(f"Error: {exc}")
                    return

            # Generic dot-path setting
            if key == "max_tool_steps":
                try:
                    self.config.setdefault("chat", {}).setdefault("agent_loop", {})["max_tool_steps"] = int(val)
                    if self.console:
                        self.console.print(f"[green]✓ Updated max_tool_steps to {val}[/green]")
                    else:
                        print(f"Updated max_tool_steps to {val}")
                except ValueError:
                    print("Invalid integer value.")
            elif key == "max_tokens":
                try:
                    self.config.setdefault("chat", {}).setdefault("agent_loop", {})["max_tokens"] = int(val)
                    if self.console:
                        self.console.print(f"[green]✓ Updated max_tokens to {val}[/green]")
                    else:
                        print(f"Updated max_tokens to {val}")
                except ValueError:
                    print("Invalid integer value.")
            elif key == "tool_display":
                if val.lower() in {"compact", "verbose"}:
                    self.tool_display = val.lower()
                    self.config.setdefault("chat", {}).setdefault("ui", {})["tool_display"] = self.tool_display
                    if self.console:
                        self.console.print(f"[green]✓ Updated tool_display to {self.tool_display}[/green]")
                    else:
                        print(f"Updated tool_display to {self.tool_display}")
            elif key == "show_tool_args":
                parsed_bool = val.lower() in {"true", "1", "yes"}
                self.show_tool_args = parsed_bool
                self.config.setdefault("chat", {}).setdefault("ui", {})["show_tool_args"] = parsed_bool
                if self.console:
                    self.console.print(f"[green]✓ Updated show_tool_args to {parsed_bool}[/green]")
                else:
                    print(f"Updated show_tool_args to {parsed_bool}")
            else:
                # Arbitrary dot-path
                target = self.config
                parts_path = key.split(".")
                for p in parts_path[:-1]:
                    if p not in target or not isinstance(target[p], dict):
                        target[p] = {}
                    target = target[p]
                target[parts_path[-1]] = val
                if self.console:
                    self.console.print(f"[green]✓ Updated config key '{key}' to {val}[/green]")
                else:
                    print(f"Updated config key '{key}' to {val}")

    def clear(self) -> None:
        if self.console:
            self.console.clear()
            self.header()
        else:
            os.system("cls" if os.name == "nt" else "clear")
            self.header()

    def goodbye(self) -> None:
        app_name = str(deep_get(self.config, "app.name", "Ivy Agent Harness"))
        if self.console and Text:
            goodbye_text = Text(f"\n⚡ Thank you for using {app_name}. Goodbye!\n", style="bold #38bdf8")
            self.console.print(goodbye_text)
        else:
            print(f"\n⚡ Goodbye from {app_name}!\n")

    def context(self, harness_context: str) -> None:
        if self.console and Panel:
            self.console.print(
                Panel(
                    harness_context,
                    title="[bold magenta]Local Harness Context[/bold magenta]",
                    border_style="magenta",
                    box=box.ROUNDED if box else None,
                )
            )
            return
        print(harness_context)

    def newline(self) -> None:
        if self.console:
            self.console.print()
        else:
            print()

    def _loop_summary(self) -> str:
        enabled = bool(deep_get(self.config, "chat.agent_loop.enabled", True))
        steps = deep_get(self.config, "chat.agent_loop.max_tool_steps", deep_get(self.config, "chat.max_tool_steps", 6))
        return f"{'enabled' if enabled else 'disabled'} (max_steps={steps})"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

