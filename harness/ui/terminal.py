"""Terminal UI and Rich console rendering for Agent Harness."""

from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from typing import Any

from harness.config import deep_get

try:
    from rich import box
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
except ModuleNotFoundError:  # pragma: no cover
    box = None  # type: ignore[assignment]
    Console = None  # type: ignore[assignment]
    Group = None  # type: ignore[assignment]
    Markdown = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Rule = None  # type: ignore[assignment]
    Syntax = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


class UI:
    """Rich terminal renderer with fallback to standard ANSI/plain output."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.console = Console() if Console else None
        self._streaming_active = False

    def header(self, route: str | None = None) -> None:
        app_name = str(deep_get(self.config, "app.name", "IVY AGENT HARNESS")).upper()
        version = str(deep_get(self.config, "app.version", "v0.1.0"))
        active_route = route or f"{deep_get(self.config, 'chat.graph', 'nim')}.{deep_get(self.config, 'chat.node', 'nemotron')}"
        models = deep_get(self.config, "models", {}) or {}
        model_cfg = models.get(active_route, {})
        model_name = model_cfg.get("model", "nvidia/nemotron-3-super-120b-a12b")
        provider = model_cfg.get("provider", "nvidia_nim")

        if self.console and Panel and Table:
            table = Table.grid(expand=True, padding=(0, 2))
            table.add_column(justify="left", ratio=1)
            table.add_column(justify="left", ratio=1)

            table.add_row(
                f"[bold cyan]Route:[/bold cyan]    [white]{active_route}[/white]",
                f"[bold green]Loop:[/bold green]     [white]{self._loop_summary()}[/white]",
            )
            table.add_row(
                f"[bold cyan]Model:[/bold cyan]    [white]{model_name}[/white]",
                "[bold yellow]Commands:[/bold yellow] [dim]/help  /skills  /mcp  /route  /features  /auto  /tools[/dim]",
            )
            table.add_row(
                f"[bold cyan]Provider:[/bold cyan] [white]{provider}[/white]",
                "",
            )

            panel = Panel(
                table,
                title=f"[bold #38bdf8]⚡ {app_name}[/bold #38bdf8]   [dim]{version}[/dim]",
                title_align="left",
                border_style="#0284c7",
                box=box.ROUNDED if box else None,
                padding=(1, 2),
            )
            self.console.print(panel)
        else:
            print(f"=== ⚡ {app_name} {version} (Route: {active_route} | Model: {model_name}) ===")
            print("Commands: /help, /skills, /mcp, /route, /features, /auto, /tools, /exit\n")

    def user_prompt(self, user_name: str | None = None) -> str:
        name = user_name or os.getenv("USERNAME", os.getenv("USER", "You"))
        if self.console:
            self.console.print(f"\n[bold #38bdf8]👤 {name}[/bold #38bdf8] [dim]❯[/dim] ", end="")
        else:
            print(f"\n👤 {name} > ", end="")
        sys.stdout.flush()
        try:
            return input()
        except EOFError:
            return "/exit"

    def status(self, message: str = "Thinking...") -> Any:
        if self.console:
            return self.console.status(f"[bold #38bdf8]{message}[/bold #38bdf8]", spinner="dots")
        return nullcontext()

    def assistant(self, reply: str, title: str = "⚡ IVY") -> None:
        if self._streaming_active:
            self._streaming_active = False
            if self.console:
                self.console.print()
            else:
                print()
            return

        if not reply or not reply.strip():
            return

        if self.console and Panel:
            content: Any
            try:
                content = Markdown(reply) if Markdown else reply
            except (TypeError, ValueError):
                content = reply

            panel = Panel(
                content,
                title=f"[bold #38bdf8]{title}[/bold #38bdf8]",
                title_align="left",
                border_style="#0284c7",
                box=box.ROUNDED if box else None,
                padding=(1, 2),
            )
            self.console.print(panel)
        else:
            print(f"\n[{title}]\n{reply}\n")

    def stream_chunk(self, chunk: str) -> None:
        self._streaming_active = True
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def subagent_card(self, agent: str, subtask: str, tool_records: list[dict[str, Any]] | None = None) -> None:
        if self.console and Panel:
            lines = [f"[bold cyan]👤 Persona:[/bold cyan] [white]{agent}[/white]"]
            if tool_records:
                lines.append("[dim]Inner Subagent Tool Actions:[/dim]")
                for tr in tool_records:
                    tool = tr.get("tool", "unknown")
                    args = json.dumps(tr.get("args", {}))
                    res = str(tr.get("result", ""))[:80]
                    lines.append(f"  [dim cyan]●[/dim cyan] [green]{tool}({args})[/green] [dim]➔[/dim] {res}")
            lines.append(f"[dim]Observation:[/dim] {subtask}")

            self.console.print(
                Panel(
                    "\n".join(lines),
                    title="[bold #38bdf8]⚡ SUBAGENT EXECUTION [/bold #38bdf8]",
                    border_style="#0284c7",
                    box=box.ROUNDED if box else None,
                )
            )
        else:
            print(f"⚡ [SUBAGENT: {agent}] {subtask}")

    def tool_step(self, tool: str, args_summary: str, duration_ms: float | None = None, result_summary: str | None = None) -> None:
        icon_map = {
            "file": "📁",
            "process": "🐚",
            "sandbox": "🧪",
            "git": "🌿",
            "web": "🌐",
            "mcp": "🔌",
            "skill": "⚡",
            "agent": "🤖",
            "data": "📊",
            "time": "⏰",
            "environment": "🖥️",
        }
        category = tool.split(".")[0] if "." in tool else "tool"
        icon = icon_map.get(category, "●")
        dur_str = f" [dim]({duration_ms:.1f}ms)[/dim]" if duration_ms is not None else ""
        res_str = f" [dim]➔[/dim] [green]✓ {result_summary}[/green]" if result_summary else ""

        if self.console:
            self.console.print(
                f"  [bold #38bdf8]{icon}[/bold #38bdf8] [bold yellow]{tool}[/bold yellow] "
                f"[white]{args_summary}[/white]{res_str}{dur_str}"
            )
        else:
            print(f"  {icon} {tool} {args_summary}{res_str}{dur_str}")

    def loop_completed(self, steps: int, total_duration_s: float) -> None:
        if self.console:
            self.console.print(
                f"[dim cyan]⚡ Turn completed · {steps} tool step(s) · {total_duration_s:.2f}s[/dim cyan]"
            )
        else:
            print(f"⚡ Turn completed · {steps} tool step(s) · {total_duration_s:.2f}s")

    def goodbye(self) -> None:
        app_name = str(deep_get(self.config, "app.name", "Ivy Agent Harness"))
        if self.console and Text:
            self.console.print(Text(f"\n⚡ Thank you for using {app_name}. Goodbye!\n", style="bold #38bdf8"))
        else:
            print(f"\n⚡ Goodbye from {app_name}!\n")

    def _loop_summary(self) -> str:
        enabled = bool(deep_get(self.config, "chat.agent_loop.enabled", True))
        steps = deep_get(self.config, "chat.agent_loop.max_tool_steps", deep_get(self.config, "chat.max_tool_steps", 6))
        return f"{'enabled' if enabled else 'disabled'} (max_steps={steps})"
