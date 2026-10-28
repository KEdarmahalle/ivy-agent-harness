"""Universal client project stack detection and environment adaptation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProjectProfile:
    project_name: str
    workspace_root: str
    primary_domain: str = "generic"
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    has_database: bool = False
    database_types: list[str] = field(default_factory=list)
    has_tests: bool = False
    recommended_packs: list[str] = field(default_factory=list)
    detected_markers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "workspace_root": self.workspace_root,
            "primary_domain": self.primary_domain,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "has_database": self.has_database,
            "database_types": self.database_types,
            "has_tests": self.has_tests,
            "recommended_packs": self.recommended_packs,
            "detected_markers": self.detected_markers,
        }

    def render_context_summary(self) -> str:
        """Render a concise context summary for system prompt injection."""
        lines = [f"Project: {self.project_name} (Domain: {self.primary_domain})"]
        if self.languages:
            lines.append(f"Languages: {', '.join(self.languages)}")
        if self.frameworks:
            lines.append(f"Frameworks/Stack: {', '.join(self.frameworks)}")
        if self.has_database:
            lines.append(f"Database: {', '.join(self.database_types) if self.database_types else 'Detected'}")
        if self.has_tests:
            lines.append("Testing: Test suite detected")
        return " | ".join(lines)

    def to_prompt_context(self) -> str:
        """Alias for render_context_summary."""
        return self.render_context_summary()


def detect_project(workspace_root: str | Path = ".") -> ProjectProfile:
    """Analyze directory structure to automatically detect client project stack and domain."""
    root = Path(workspace_root).resolve()
    if root.is_file():
        root = root.parent
    if not root.exists() or not root.is_dir():
        return ProjectProfile(
            project_name=root.name or "unknown",
            workspace_root=str(root),
            primary_domain="generic",
            recommended_packs=["core", "filesystem", "skills"],
        )

    name = root.name
    languages: set[str] = set()
    frameworks: set[str] = set()
    db_types: set[str] = set()
    markers: list[str] = []
    has_tests = False

    # 1. Python ecosystem
    if (root / "pyproject.toml").exists():
        markers.append("pyproject.toml")
        languages.add("Python")
    if (root / "requirements.txt").exists():
        markers.append("requirements.txt")
        languages.add("Python")
    if (root / "setup.py").exists():
        markers.append("setup.py")
        languages.add("Python")
    if (root / "manage.py").exists():
        markers.append("Django")
        languages.add("Python")
        frameworks.add("Django")

    # 2. JavaScript / TypeScript / Frontend
    if (root / "package.json").exists():
        markers.append("package.json")
        languages.add("JavaScript")
        if (root / "tsconfig.json").exists():
            markers.append("tsconfig.json")
            languages.add("TypeScript")
        # Check frameworks in package.json
        try:
            content = (root / "package.json").read_text(encoding="utf-8", errors="ignore").lower()
            if "next" in content:
                frameworks.add("Next.js")
            elif "react" in content:
                frameworks.add("React")
            elif "vue" in content:
                frameworks.add("Vue")
            elif "express" in content:
                frameworks.add("Express")
            elif "fastify" in content:
                frameworks.add("Fastify")
        except OSError:
            pass

    # 3. Rust / Go / C++
    if (root / "Cargo.toml").exists():
        markers.append("Cargo.toml")
        languages.add("Rust")
    if (root / "go.mod").exists():
        markers.append("go.mod")
        languages.add("Go")
    if (root / "CMakeLists.txt").exists():
        markers.append("CMakeLists.txt")
        languages.add("C/C++")

    # 4. Data / Analytics / SQL
    sql_files = list(root.glob("*.sql")) + list((root / "sql").glob("*.sql") if (root / "sql").exists() else [])
    csv_files = list(root.glob("*.csv")) + list((root / "data").glob("*.csv") if (root / "data").exists() else [])
    sqlite_files = list(root.glob("*.sqlite*")) + list(root.glob("*.db"))
    if (root / "dbt_project.yml").exists():
        markers.append("dbt_project.yml")
        frameworks.add("dbt")
        db_types.add("SQL/Warehouse")
    if sql_files or csv_files or sqlite_files:
        markers.append(f"Data files (sql={len(sql_files)}, csv={len(csv_files)})")
        if sqlite_files:
            db_types.add("SQLite")

    # 5. Tests detection
    if (root / "tests").exists() or (root / "test").exists() or list(root.glob("test_*.py")):
        has_tests = True

    # Determine primary domain
    primary_domain = "software_engineering"
    recommended_packs = ["core", "filesystem", "skills"]

    if db_types or "dbt" in frameworks or len(csv_files) > 0 or len(sql_files) > 0:
        primary_domain = "data_analytics"
        recommended_packs.append("data_analytics")
    elif "React" in frameworks or "Next.js" in frameworks or "Vue" in frameworks:
        primary_domain = "web_frontend"
        recommended_packs.append("web_network")
    elif "FastAPI" in frameworks or "Express" in frameworks or "Django" in frameworks:
        primary_domain = "backend_service"
        recommended_packs.extend(["web_network", "shell_sandbox"])
    else:
        recommended_packs.extend(["data_analytics", "web_network", "shell_sandbox"])

    return ProjectProfile(
        project_name=name,
        workspace_root=str(root),
        primary_domain=primary_domain,
        languages=sorted(languages),
        frameworks=sorted(frameworks),
        has_database=bool(db_types),
        database_types=sorted(db_types),
        has_tests=has_tests,
        recommended_packs=sorted(set(recommended_packs)),
        detected_markers=markers,
    )
