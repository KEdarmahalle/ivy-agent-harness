"""Tests for dynamic skills registry and skill-wala-skill authoring engine."""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness.registries.tool_registry import ToolRegistry
from harness.skills import SkillRegistry, parse_frontmatter


def test_parse_frontmatter() -> None:
    content = """---
name: sample-skill
description: A helpful sample skill
version: 1.2.0
tags: [python, test]
---
# Sample Instructions
Step 1: Do something.
"""
    fm, body = parse_frontmatter(content)
    assert fm.get("name") == "sample-skill"
    assert fm.get("description") == "A helpful sample skill"
    assert fm.get("version") == "1.2.0"
    assert "Step 1: Do something." in body


def test_skill_registry_crud() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        registry = SkillRegistry(workspace_root=root, include_global=False)

        # Initially empty
        assert len(registry.list_skills()) == 0

        # Author a new skill (The Skill-wala-Skill)
        created = registry.create_skill(
            name="code-reviewer",
            description="Expert code review and security audit",
            instructions="# Code Review Guide\nPerform static and security analysis.",
            tags=["review", "audit"],
            version="1.0.0",
        )

        assert created.metadata.name == "code-reviewer"
        assert created.metadata.version == "1.0.0"
        assert (root / ".harness" / "skills" / "code-reviewer" / "SKILL.md").exists()

        # Re-discover
        registry.discover_skills()
        found = registry.get_skill("code-reviewer")
        assert found is not None
        assert found.metadata.description == "Expert code review and security audit"
        assert "Perform static and security analysis." in found.instructions

        # Render prompt catalog
        catalog = registry.render_skills_prompt_catalog()
        assert "code-reviewer" in catalog
        assert "Expert code review" in catalog


def test_tool_registry_skill_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        skills = SkillRegistry(workspace_root=root, include_global=False)
        tools = ToolRegistry(skills=skills)

        # 1. Create a skill via tool
        res_create = tools.invoke(
            "skill.create",
            name="deep-debugger",
            description="Deep root-cause debugger",
            instructions="# Debugging Steps\n1. Check logs\n2. Trace stack",
            tags=["debugging"],
        )
        assert res_create.get("ok") is True
        assert res_create.get("created") is True

        # 2. List skills via tool
        res_list = tools.invoke("skill.list")
        assert res_list.get("skills_count") == 1
        assert res_list["skills"][0]["name"] == "deep-debugger"

        # 3. View skill via tool
        res_view = tools.invoke("skill.view", name="deep-debugger")
        assert res_view.get("ok") is True
        assert res_view.get("name") == "deep-debugger"
        assert "Check logs" in res_view.get("instructions", "")

        # 4. View non-existent skill
        res_view_missing = tools.invoke("skill.view", name="non-existent")
        assert res_view_missing.get("ok") is False


def test_global_skill_discovery() -> None:
    with tempfile.TemporaryDirectory() as global_dir, tempfile.TemporaryDirectory() as workspace_dir:
        # Create a skill in a custom global directory
        g_skill = Path(global_dir) / "global-tester"
        g_skill.mkdir(parents=True)
        (g_skill / "SKILL.md").write_text(
            "---\nname: global-tester\ndescription: Global skill for testing\nversion: 1.0.0\n---\nGlobal body",
            encoding="utf-8",
        )

        reg = SkillRegistry(
            workspace_root=workspace_dir,
            include_global=True,
            global_skill_dirs=[Path(global_dir)],
        )

        skill = reg.get_skill("global-tester")
        assert skill is not None
        assert skill.metadata.scope == "global"
        assert skill.metadata.name == "global-tester"
        assert "Global body" in skill.instructions
