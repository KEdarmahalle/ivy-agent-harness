"""Repository investigation with bounded inputs and safe reproduction."""

from __future__ import annotations

from typing import Any

from harness.engine.graph import GraphSpec, NodeSpec
from harness.engine.schemas import Evidence, GateVerdict, NodeResult, RunSnapshot
from harness.gates.verification_gate import score_and_gate

MAX_QUERY_CHARS = 2_000
MAX_REPO_PATH_CHARS = 4_096
MAX_SEARCH_RESULTS = 100
MAX_COMMAND_CHARS = 4_000
MAX_TIMEOUT_SECONDS = 300


def build_graph() -> GraphSpec:
    return GraphSpec(name="code-investigate", start_node="locate", nodes={
        "locate": NodeSpec("locate", locate),
        "trace": NodeSpec("trace", trace),
        "protected_guard": NodeSpec("protected_guard", protected_guard),
        "reproduce": NodeSpec("reproduce", reproduce),
        "score": NodeSpec("score", score),
        "gate": NodeSpec("gate", gate),
    })


def _validate_input(snapshot: RunSnapshot) -> tuple[str, str, int, str | None, int]:
    raw_query = snapshot.input.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip() or len(raw_query) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be a non-empty string of at most {MAX_QUERY_CHARS} characters")
    query = raw_query.strip()
    raw_repo = snapshot.input.get("repo_path", ".")
    if not isinstance(raw_repo, str) or not raw_repo.strip() or len(raw_repo) > MAX_REPO_PATH_CHARS:
        raise ValueError("repo_path must be a non-empty path string within the supported length")
    raw_limit = snapshot.input.get("max_search_results", 20)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 1 <= raw_limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"max_search_results must be an integer between 1 and {MAX_SEARCH_RESULTS}")
    command = snapshot.input.get("test_command", snapshot.input.get("reproduction_command"))
    if command is not None and (not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS):
        raise ValueError(f"reproduction command must be a non-empty string of at most {MAX_COMMAND_CHARS} characters")
    raw_timeout = snapshot.input.get("timeout_seconds", 120)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or not 1 <= raw_timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}")
    return query, raw_repo.strip(), raw_limit, command.strip() if command else None, raw_timeout


def _repo(snapshot: RunSnapshot) -> str:
    return str(snapshot.input.get("repo_path") or ".")


def _evidence(snapshot: RunSnapshot) -> list[Evidence]:
    return [Evidence.model_validate(item) for item in snapshot.state.get("evidence", [])]


def _append_evidence(snapshot: RunSnapshot, *items: Evidence) -> list[dict[str, Any]]:
    evidence = [item.model_dump(mode="json") for item in _evidence(snapshot)]
    evidence.extend(item.model_dump(mode="json") for item in items)
    return evidence


def locate(context: Any, snapshot: RunSnapshot) -> NodeResult:
    query, repo, max_results, _, _ = _validate_input(snapshot)
    result = context.tools.invoke("file.search_text", root=repo, query=query, max_results=max_results)
    matches = result.get("matches", result.get("results", []))
    context.trace("tool_called", {"tool": "file.search_text", "result_count": len(matches), "truncated": bool(result.get("truncated"))})
    # Only consider protected names when there is no readable match. Their bytes
    # are never returned by the search tool and never enter graph evidence.
    protected = result.get("protected_files", [])
    candidates = sorted({item["file"] for item in matches})
    updates: dict[str, Any] = {"search": {"count": len(matches), "truncated": bool(result.get("truncated"))}, "candidate_files": candidates, "candidate_matches": matches, "protected_candidates_uninspected": bool(protected and not matches)}
    if matches:
        first = matches[0]
        updates["evidence"] = _append_evidence(snapshot, Evidence(
            claim=f"A searchable code line matched the investigation query at {first['file']}:{first['line']}",
            file=first["file"], line=first["line"], output_observed=str(first.get("text", first.get("snippet", "")))[:500], confidence="verified",
        ))
    else:
        updates["evidence"] = _append_evidence(snapshot, Evidence(
            claim="No readable repository lines matched the investigation query", output_observed="Search completed with zero readable matches", confidence="unverified",
        ))
    return NodeResult(updates=updates, next_node="trace")


def trace(context: Any, snapshot: RunSnapshot) -> NodeResult:
    matches = snapshot.state.get("candidate_matches", [])
    if not matches:
        return NodeResult(updates={"hypothesis": "No hypothesis can be formed without readable candidate code"}, next_node="protected_guard")
    first = matches[0]
    start, end = max(1, int(first["line"]) - 3), int(first["line"]) + 3
    lines = context.tools.invoke("file.read_lines", root=_repo(snapshot), file=first["file"], start=start, end=end)
    context.trace("tool_called", {"tool": "file.read_lines", "file": first["file"], "line": first["line"]})
    rendered = "\n".join(f"{row['line']}: {row['text']}" for row in lines["lines"])
    hypothesis = f"The query appears related to {first['file']}:{first['line']}; this is a code-reading hypothesis."
    evidence = _append_evidence(snapshot, Evidence(claim=hypothesis, file=first["file"], line=first["line"], output_observed=rendered[:1000], confidence="inferred_from_adjacent_code"))
    return NodeResult(updates={"trace_context": lines, "hypothesis": hypothesis, "evidence": evidence}, next_node="protected_guard")


def protected_guard(context: Any, snapshot: RunSnapshot) -> NodeResult:
    policy = getattr(context.tools, "sandbox", None)
    # The active policy contains mandatory defaults and configured patterns.
    # Failing closed is essential: a missing policy must not weaken the guard.
    if policy is None or not callable(getattr(policy, "is_protected", None)):
        raise RuntimeError("active sandbox protection policy is unavailable")
    touched = [path for path in snapshot.state.get("candidate_files", []) if policy.is_protected(path)]
    if snapshot.state.get("protected_candidates_uninspected"):
        touched.append("[protected candidates omitted by search policy]")
    if not touched:
        return NodeResult(updates={"protected_paths_touched": []}, next_node="reproduce")
    verdict = GateVerdict(decision="NEEDS_HUMAN", reason="search candidates include paths denied by the active protection policy", evidence=_evidence(snapshot))
    context.trace("gate_decision", {"decision": verdict.decision, "protected_candidate_count": len(touched)})
    # Acknowledging this review pause can only finalize an inconclusive result.
    # It can never grant access or resume at a process/tool node.
    return NodeResult(updates={"protected_paths_touched": len(touched), "required_approval": "protected_path_review", "resume_node": "score"}, status="needs_human", result={"verdict": "Protected paths could not be inspected by the search policy", "confidence": verdict.decision, "reason": verdict.reason, "evidence": [item.model_dump(mode="json") for item in verdict.evidence]})


def reproduce(context: Any, snapshot: RunSnapshot) -> NodeResult:
    _, repo, _, command, timeout = _validate_input(snapshot)
    if not command:
        evidence = _append_evidence(snapshot, Evidence(claim="No reproduction command was provided; runtime behavior remains unverified", confidence="unverified"))
        return NodeResult(updates={"reproduction": {"ran": False}, "evidence": evidence}, next_node="score")
    result = context.tools.invoke("process.run", command=command, cwd=repo, timeout_seconds=timeout)
    context.trace("tool_called", {"tool": "process.run", "exit_code": result.get("exit_code"), "timed_out": bool(result.get("timed_out"))})
    evidence = _append_evidence(snapshot, Evidence(
        claim=f"The supplied reproduction command completed with exit code {result.get('exit_code')}; this establishes execution outcome only, not the hypothesis",
        command_run=command, output_observed=str(result.get("output_excerpt") or "")[:1000], confidence="verified",
    ))
    return NodeResult(updates={"reproduction": result, "evidence": evidence}, next_node="score")


def score(context: Any, snapshot: RunSnapshot) -> NodeResult:
    verdict = score_and_gate(_evidence(snapshot))
    context.trace("score_computed", {"score": verdict.score, "decision": verdict.decision})
    return NodeResult(updates={"score": verdict.score, "preliminary_decision": verdict.decision}, next_node="gate")


def gate(context: Any, snapshot: RunSnapshot) -> NodeResult:
    verdict = score_and_gate(_evidence(snapshot))
    result = {"verdict": snapshot.state.get("hypothesis", "No code hypothesis was produced"), "confidence": verdict.decision, "reason": verdict.reason, "score": verdict.score, "evidence": [item.model_dump(mode="json") for item in verdict.evidence], "reproduction": snapshot.state.get("reproduction")}
    context.trace("gate_decision", {"decision": verdict.decision, "reason": verdict.reason})
    return NodeResult(status="done", result=result)
