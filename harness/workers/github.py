"""Autonomous GitHub Actions and Webhook Background Worker.

Provides:
- Automated PR Code Review with multi-agent fan-out (security_auditor, performance_profiler, ast_architect, tech_lead).
- Autonomous GitHub Issue triage, reproduction planning, and automated comment dispatch.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from harness.sdk import AgentHarness
from harness.swarm import SwarmCoordinator


def post_github_pr_comment(
    pr_number: int,
    comment: str,
    repo: str = "",
    github_token: str | None = None,
) -> dict[str, Any]:
    """Post a markdown comment on a GitHub Pull Request or Issue."""
    token = github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repository = repo or os.environ.get("GITHUB_REPOSITORY", "")

    if not token or not repository:
        # Dry-run or local mode when credentials are not configured
        return {
            "status": "dry_run",
            "pr_number": pr_number,
            "repo": repository,
            "comment_length": len(comment),
        }

    url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
    payload = json.dumps({"body": comment}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Ivy-Agent-Harness-Bot",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return {"status": "posted", "comment_id": res_data.get("id"), "url": res_data.get("html_url")}
    except Exception as exc:  # noqa: BLE001 - convert integration failures to worker results
        return {"status": "error", "error": str(exc), "pr_number": pr_number, "repo": repository}


async def on_pull_request_opened(
    pr_diff: str,
    pr_number: int,
    repo: str = "",
    title: str = "",
    github_token: str | None = None,
    post_comment: bool = True,
) -> dict[str, Any]:
    """Autonomous PR Review Worker executing multi-agent fanout and tech_lead synthesis."""
    swarm = SwarmCoordinator.from_default_config()

    task_prompt = (
        f"Review Pull Request #{pr_number}" + (f": {title}" if title else "") + f"\n\nDiff Content:\n{pr_diff}\n\n"
        "Instructions:\n"
        "1. Identify security vulnerabilities, credentials, or unsafe calls.\n"
        "2. Evaluate runtime complexity, memory allocations, and performance bottlenecks.\n"
        "3. Verify AST structure and software design consistency.\n"
    )

    review = await swarm.run_fan_out(
        agent_names=["security_auditor", "performance_profiler", "ast_architect"],
        task=task_prompt,
        synthesizer_agent="tech_lead",
    )

    synthesis_markdown = (
        f"### 🤖 Ivy Autonomous PR Review (PR #{pr_number})\n\n"
        f"{review.get('synthesis', '')}\n\n"
        "---\n"
        "<details><summary>🔍 Detailed Subagent Analysis</summary>\n\n"
    )
    for agent_name, output in review.get("findings", {}).items():
        synthesis_markdown += f"#### Persona: `{agent_name}`\n{output}\n\n"
    synthesis_markdown += "</details>\n"

    dispatch_res = {}
    if post_comment:
        dispatch_res = post_github_pr_comment(
            pr_number=pr_number,
            comment=synthesis_markdown,
            repo=repo,
            github_token=github_token,
        )

    return {
        "status": "completed",
        "pr_number": pr_number,
        "repo": repo,
        "synthesis": synthesis_markdown,
        "findings": review.get("findings", {}),
        "comment_dispatch": dispatch_res,
    }


async def on_issue_opened(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repo: str = "",
    github_token: str | None = None,
    post_comment: bool = True,
) -> dict[str, Any]:
    """Autonomous Issue Triage Worker: investigates issue context and drafts fix plan."""
    harness = AgentHarness.from_project(workspace_root=".")
    investigation_prompt = (
        f"GitHub Issue #{issue_number}: {issue_title}\n\n"
        f"Description:\n{issue_body}\n\n"
        "Search the codebase, locate relevant files, and propose an exact reproduction and fix plan."
    )
    result = await harness.arun(investigation_prompt)

    comment_body = (
        f"### ⚡ Ivy Autonomous Issue Analysis (Issue #{issue_number})\n\n"
        f"{result.final_output}\n\n"
        f"---\n*Investigated in {result.duration_s:.2f}s across {result.steps} reasoning steps.*"
    )

    dispatch_res = {}
    if post_comment:
        dispatch_res = post_github_pr_comment(
            pr_number=issue_number,
            comment=comment_body,
            repo=repo,
            github_token=github_token,
        )

    return {
        "status": "completed",
        "issue_number": issue_number,
        "analysis": comment_body,
        "comment_dispatch": dispatch_res,
    }
