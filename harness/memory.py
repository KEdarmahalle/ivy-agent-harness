"""Advanced Memory and Context Management for Agent Harness.

Provides:
- Persistent workspace memory store (.harness/memory.json)
- Automatic AGENTS.md and CLAUDE.md instruction ingestion
- Interactive /memory, /session, and /compact slash command support
- Rolling context compaction to prevent context window overflow
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemoryEntry:
    id: int
    text: str
    category: str = "general"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryStore:
    """Persistent workspace long-term memory store."""

    def __init__(self, workspace_root: Path | str = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.memory_dir = self.workspace_root / ".harness"
        self.memory_file = self.memory_dir / "memory.json"
        self.sessions_dir = self.memory_dir / "sessions"
        self._entries: list[MemoryEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.memory_file.exists():
            return
        try:
            data = json.loads(self.memory_file.read_text(encoding="utf-8"))
            self._entries = [
                MemoryEntry(
                    id=item.get("id", idx + 1),
                    text=item.get("text", ""),
                    category=item.get("category", "general"),
                    created_at=item.get("created_at", time.time()),
                )
                for idx, item in enumerate(data.get("memories", []))
            ]
        except (json.JSONDecodeError, OSError, ValueError):
            self._entries = []

    def save(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "memories": [e.to_dict() for e in self._entries],
        }
        self.memory_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add(self, text: str, category: str = "general") -> MemoryEntry:
        next_id = max((e.id for e in self._entries), default=0) + 1
        entry = MemoryEntry(id=next_id, text=text.strip(), category=category)
        self._entries.append(entry)
        self.save()
        return entry

    def remove(self, memory_id: int) -> bool:
        initial_len = len(self._entries)
        self._entries = [e for e in self._entries if e.id != memory_id]
        if len(self._entries) != initial_len:
            self.save()
            return True
        return False

    def clear(self) -> None:
        self._entries.clear()
        self.save()

    def list_all(self) -> list[MemoryEntry]:
        return list(self._entries)

    def search_entries(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """Search memory entries using keyword relevance scoring and recency weighting."""
        if not self._entries or not query.strip():
            return list(self._entries[:top_k])

        query_tokens = set(query.lower().split())
        scored: list[tuple[float, MemoryEntry]] = []

        now = time.time()
        for entry in self._entries:
            entry_tokens = set(entry.text.lower().split())
            overlap = len(query_tokens.intersection(entry_tokens))
            if overlap > 0 or query.lower() in entry.text.lower():
                token_score = max(overlap, 1.0)
                age_days = (now - entry.created_at) / 86400.0
                recency_boost = max(0.1, 1.0 / (1.0 + age_days * 0.1))
                score = token_score * recency_boost
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_k]]

    def load_project_guidelines(self) -> str:
        """Scan workspace for AGENTS.md, CLAUDE.md, .cursorrules, or .harness/GUIDELINES.md."""
        guidelines: list[str] = []
        standard_files = [
            "AGENTS.md",
            "CLAUDE.md",
            ".cursorrules",
            ".harness/GUIDELINES.md",
        ]
        for filename in standard_files:
            path = self.workspace_root / filename
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore").strip()
                    if content:
                        guidelines.append(f"### Guidelines from {filename}:\n{content[:2000]}")
                except OSError:
                    continue

        # Check .cursor/rules and .harness/rules directories
        for rule_dir in [self.workspace_root / ".cursor" / "rules", self.workspace_root / ".harness" / "rules"]:
            if rule_dir.exists() and rule_dir.is_dir():
                for rule_file in sorted(rule_dir.glob("*.md*")):
                    try:
                        content = rule_file.read_text(encoding="utf-8", errors="ignore").strip()
                        if content:
                            guidelines.append(f"### Rule from {rule_file.name}:\n{content[:1500]}")
                    except OSError:
                        continue

        return "\n\n".join(guidelines)

    def list_custom_commands(self) -> dict[str, str]:
        """List custom slash commands from .harness/commands/ and .claude/commands/."""
        commands: dict[str, str] = {}
        for cmd_dir in [self.workspace_root / ".harness" / "commands", self.workspace_root / ".claude" / "commands"]:
            if cmd_dir.exists() and cmd_dir.is_dir():
                for cmd_file in sorted(cmd_dir.glob("*.md")):
                    try:
                        commands[f"/{cmd_file.stem}"] = cmd_file.read_text(encoding="utf-8", errors="ignore").strip()
                    except OSError:
                        continue
        return commands

    def get_custom_command(self, name: str) -> str | None:
        clean_name = name.lstrip("/").lower()
        commands = self.list_custom_commands()
        return commands.get(f"/{clean_name}")

    def render_system_memory_context(self) -> str:
        """Render formatted memory block for injection into the system prompt."""
        sections: list[str] = []
        project_guide = self.load_project_guidelines()
        if project_guide:
            sections.append(f"# WORKSPACE INSTRUCTIONS & RULES\n{project_guide}")
        if self._entries:
            memory_lines = [f"- [{e.category}] #{e.id}: {e.text}" for e in self._entries]
            sections.append("# LONG-TERM WORKSPACE MEMORY\n" + "\n".join(memory_lines))
        return "\n\n".join(sections)

    def format_context(self, prompt: str = "") -> str:
        """Alias for render_system_memory_context."""
        return self.render_system_memory_context()

    def save_session(self, session_name: str, messages: list[dict[str, Any]]) -> Path:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name)
        session_file = self.sessions_dir / f"{safe_name}.json"
        payload = {
            "session_name": session_name,
            "saved_at": time.time(),
            "messages": messages,
        }
        session_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return session_file

    def load_session(self, session_name: str) -> list[dict[str, Any]] | None:
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_name)
        session_file = self.sessions_dir / f"{safe_name}.json"
        if not session_file.exists():
            return None
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            return data.get("messages", [])
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def list_sessions(self) -> list[str]:
        if not self.sessions_dir.exists():
            return []
        return [f.stem for f in self.sessions_dir.glob("*.json")]


def compact_history(
    messages: list[dict[str, str]],
    keep_recent: int = 4,
) -> list[dict[str, str]]:
    """Condense older conversation history into a structured working memory summary."""
    if len(messages) <= keep_recent + 1:
        return messages

    system_msg = messages[0]
    older_messages = messages[1:-keep_recent]
    recent_messages = messages[-keep_recent:]

    summary_points: list[str] = []
    for msg in older_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "").strip()
        if role == "user":
            summary_points.append(f"User requested: {content[:180]}")
        elif role == "assistant":
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
            summary = lines[0] if lines else "Assistant provided response"
            summary_points.append(f"Assistant action/reply: {summary[:180]}")

    summary_text = (
        "### COMPACTED WORKING MEMORY (Earlier Conversation Summary)\n"
        + "\n".join(f"- {pt}" for pt in summary_points[-12:])
    )

    compacted_msg = {
        "role": "user",
        "content": summary_text,
    }
    compacted_ack = {
        "role": "assistant",
        "content": "Understood. I have retained all key context from our earlier conversation in working memory.",
    }

    return [system_msg, compacted_msg, compacted_ack, *recent_messages]
