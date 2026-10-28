"""Deterministic gate decisions shared by verification-oriented graphs."""

from __future__ import annotations

from pathlib import Path

from harness.engine.schemas import Evidence, GateVerdict


def score_evidence(evidence: list[Evidence]) -> float:
    if not evidence:
        return 0.0
    weights = {
        "verified": 1.0,
        "source_linked": 0.5,
        "inferred_from_adjacent_code": 0.55,
        "unverified": 0.0,
    }
    return round(sum(weights[item.confidence] for item in evidence) / len(evidence), 3)


def score_and_gate(evidence: list[Evidence]) -> GateVerdict:
    score = score_evidence(evidence)
    if not evidence:
        return GateVerdict(
            decision="NEEDS_HUMAN",
            reason="no evidence was produced",
            evidence=evidence,
            score=score,
        )
    if any(item.confidence == "unverified" for item in evidence):
        return GateVerdict(
            decision="NEEDS_HUMAN",
            reason="unverified claim present",
            evidence=evidence,
            score=score,
        )
    if all(item.confidence == "verified" for item in evidence):
        return GateVerdict(
            decision="PASS",
            reason="all claims verified",
            evidence=evidence,
            score=score,
        )
    return GateVerdict(
        decision="CONCERNS",
        reason="some evidence is inferred or source-linked; semantic support needs human review",
        evidence=evidence,
        score=score,
    )


def normalize_path(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def is_protected_path(path: str, protected_patterns: list[str]) -> bool:
    normalized = normalize_path(path).lower()
    parts = set(Path(normalized).parts)
    for pattern in protected_patterns:
        candidate = normalize_path(pattern).lower()
        if not candidate:
            continue
        if candidate.endswith("/") and (normalized.startswith(candidate) or f"/{candidate}" in normalized):
            return True
        if normalized == candidate or normalized.endswith(f"/{candidate}"):
            return True
        if candidate in parts:
            return True
    return False
