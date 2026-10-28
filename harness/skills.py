"""Dynamic Skills Architecture and Skill Authoring Engine for Agent Harness.

Provides:
- Dynamic discovery of SKILL.md packages across `.harness/skills/`, `.agents/skills/`, and `.claude/skills/`.
- YAML frontmatter parsing (name, description, tags, version).
- Skill-wala-Skill authoring tool (`skill.create`) allowing agents to create reusable skills dynamically.
- Skill inspection tools (`skill.list`, `skill.view`) and CLI slash command support (`/skills`, `/skill`).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass
class SkillMetadata:
    name: str
    description: str
    version: str = "0.1.0"
    tags: list[str] = field(default_factory=list)
    path: str = ""
    source_dir: str = ""
    scope: str = "workspace"  # "workspace" or "global"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Skill:
    metadata: SkillMetadata
    instructions: str
    scripts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "instructions": self.instructions,
            "scripts": self.scripts,
        }


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter enclosed in --- delimiters."""
    pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
    match = re.match(pattern, content, re.DOTALL)
    if not match:
        return {}, content.strip()

    raw_yaml, body = match.group(1), match.group(2)
    frontmatter: dict[str, Any] = {}
    if yaml:
        try:
            loaded = yaml.safe_load(raw_yaml)
            if isinstance(loaded, dict):
                frontmatter = loaded
        except Exception:  # noqa: BLE001
            frontmatter = {}
    else:
        # Fallback simple key-value parser
        for line in raw_yaml.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                frontmatter[k.strip()] = v.strip().strip("'\"")

    return frontmatter, body.strip()


def _safe_home_dir() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None


class SkillRegistry:
    """Manages skill discovery, loading, authoring, and prompt rendering across global and local tiers."""

    DEFAULT_SKILL_DIRS: ClassVar[list[str]] = [
        ".harness/skills",
        ".agents/skills",
        ".claude/skills",
        "builtin/skills",
    ]

    def __init__(
        self,
        workspace_root: Path | str = ".",
        skill_dirs: list[str] | None = None,
        include_global: bool = True,
        global_skill_dirs: list[Path] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.skill_dirs = skill_dirs or list(self.DEFAULT_SKILL_DIRS)
        self.include_global = include_global
        self.global_skill_dirs = global_skill_dirs or self._default_global_dirs()
        self._skills: dict[str, Skill] = {}
        self.discover_skills()

    @classmethod
    def _default_global_dirs(cls) -> list[Path]:
        home = _safe_home_dir()
        if not home:
            return []
        return [
            home / ".ivy" / "skills",
            home / ".harness" / "skills",
            home / ".agents" / "skills",
            home / ".claude" / "skills",
            home / ".gemini" / "antigravity-cli" / "skills",
            home / ".gemini" / "antigravity-cli" / "builtin" / "skills",
        ]

    def discover_skills(self) -> dict[str, Skill]:
        """Scan global user directories and local workspace for SKILL.md packages."""
        self._skills.clear()

        # 1. First scan Global User Dirs (lower precedence, overridden by local)
        if self.include_global:
            for g_dir in self.global_skill_dirs:
                if not g_dir.exists() or not g_dir.is_dir():
                    continue
                for skill_dir in sorted(g_dir.iterdir()):
                    if not skill_dir.is_dir():
                        continue
                    self._load_single_skill(skill_dir, scope="global")

        # 2. Then scan Local Workspace Dirs (highest precedence)
        for rel_dir in self.skill_dirs:
            base = self.workspace_root / rel_dir
            if not base.exists() or not base.is_dir():
                continue

            for skill_dir in sorted(base.iterdir()):
                if not skill_dir.is_dir():
                    continue
                self._load_single_skill(skill_dir, scope="workspace")

        return self._skills

    def _load_single_skill(self, skill_dir: Path, scope: str = "workspace") -> None:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists() or not skill_file.is_file():
            return

        try:
            content = skill_file.read_text(encoding="utf-8", errors="ignore")
            frontmatter, body = parse_frontmatter(content)
            name = str(frontmatter.get("name") or skill_dir.name).strip()
            desc = str(frontmatter.get("description") or "Specialized skill").strip()
            ver = str(frontmatter.get("version") or "0.1.0").strip()
            tags = frontmatter.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            # Check for scripts
            scripts_dir = skill_dir / "scripts"
            scripts: list[str] = []
            if scripts_dir.exists() and scripts_dir.is_dir():
                scripts = [s.name for s in scripts_dir.iterdir() if s.is_file()]

            display_path = (
                skill_file.relative_to(self.workspace_root).as_posix()
                if skill_file.is_relative_to(self.workspace_root)
                else str(skill_file)
            )
            display_dir = (
                skill_dir.relative_to(self.workspace_root).as_posix()
                if skill_dir.is_relative_to(self.workspace_root)
                else str(skill_dir)
            )

            meta = SkillMetadata(
                name=name,
                description=desc,
                version=ver,
                tags=list(tags) if isinstance(tags, list) else [],
                path=display_path,
                source_dir=display_dir,
                scope=scope,
            )
            self._skills[name] = Skill(
                metadata=meta,
                instructions=body,
                scripts=scripts,
            )
        except OSError:
            pass

    def get_skill(self, name: str) -> Skill | None:
        """Lookup skill by name (case-insensitive)."""
        clean_name = name.strip().lower()
        for k, v in self._skills.items():
            if k.lower() == clean_name:
                return v
        return None

    def list_skills(self) -> list[Skill]:
        """Return all discovered skills."""
        return list(self._skills.values())

    def create_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        tags: list[str] | None = None,
        version: str = "0.1.0",
        target_dir: str = ".harness/skills",
        global_scope: bool = False,
    ) -> Skill:
        """Author and persist a new skill package to disk (The Skill-wala-Skill)."""
        clean_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower())
        if not clean_name:
            raise ValueError("Skill name cannot be empty")

        if global_scope:
            skill_dir = Path.home() / ".ivy" / "skills" / clean_name
            scope_val = "global"
        else:
            skill_dir = self.workspace_root / target_dir / clean_name
            scope_val = "workspace"

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"

        tag_list = tags or ["custom", "harness-skill"]
        tags_yaml = ", ".join(f"'{t}'" for t in tag_list)
        yaml_header = (
            f"---\n"
            f"name: '{clean_name}'\n"
            f"description: '{description.strip()}'\n"
            f"version: '{version.strip()}'\n"
            f"tags: [{tags_yaml}]\n"
            f"---\n\n"
        )
        full_content = yaml_header + instructions.strip() + "\n"
        skill_file.write_text(full_content, encoding="utf-8")

        display_path = (
            skill_file.relative_to(self.workspace_root).as_posix()
            if skill_file.is_relative_to(self.workspace_root)
            else str(skill_file)
        )
        display_dir = (
            skill_dir.relative_to(self.workspace_root).as_posix()
            if skill_dir.is_relative_to(self.workspace_root)
            else str(skill_dir)
        )

        meta = SkillMetadata(
            name=clean_name,
            description=description.strip(),
            version=version.strip(),
            tags=tag_list,
            path=display_path,
            source_dir=display_dir,
            scope=scope_val,
        )
        new_skill = Skill(metadata=meta, instructions=instructions.strip(), scripts=[])
        self._skills[clean_name] = new_skill
        return new_skill

    def render_skills_prompt_catalog(self) -> str:
        """Render a compact catalog for prompt injection."""
        if not self._skills:
            return ""

        lines = ["# AVAILABLE SPECIALIZED SKILLS"]
        lines.append("The workspace and global registry provide the following specialized skills that you can inspect via `skill.view`:")
        for skill in self._skills.values():
            meta = skill.metadata
            scope_badge = f"[{meta.scope}]"
            lines.append(f"- **{meta.name}** {scope_badge} (`{meta.path}`): {meta.description}")

        lines.append(
            "\nTo inspect the full instructions and steps for any skill, use `skill.view` with the skill name."
        )
        return "\n".join(lines)
