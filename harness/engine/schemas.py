"""Typed runtime schemas shared by all graphs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from harness._compat import BaseModel, ConfigDict, Field

Confidence = Literal["verified", "source_linked", "inferred_from_adjacent_code", "unverified"]
GateDecision = Literal["PASS", "CONCERNS", "FAIL", "NEEDS_HUMAN"]
RunStatus = Literal["queued", "running", "needs_human", "failed", "done"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    file: str | None = None
    line: int | None = None
    command_run: str | None = None
    output_observed: str | None = None
    source_url: str | None = None
    source_id: str | None = None
    supporting_quote: str | None = None
    confidence: Confidence


class GateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: GateDecision
    reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    score: float | None = None


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int
    timestamp: str
    node: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    graph: str
    graph_digest: str | None = None
    status: RunStatus
    input: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    current_node: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
    tenant_id: str = "local"
    user_id: str = "local"
    roles: list[str] = Field(default_factory=lambda: ["user"])


class RunCreateRequest(BaseModel):
    graph: str
    input: dict[str, Any] = Field(default_factory=dict)
    wait: bool = False


class ResumeRequest(BaseModel):
    grant_id: str | None = Field(None, min_length=32, max_length=128)
    wait: bool = False


class ApprovalRequest(BaseModel):
    decision: Literal["approve"] = "approve"


class ModelSmokeRequest(BaseModel):
    graph: str = "brainstorm"
    node: str = "generate"
    prompt: str = "Reply with ok."


class ModelSmokeResponse(BaseModel):
    graph: str
    node: str
    response_excerpt: str


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolInvokeResponse(BaseModel):
    tool: str
    result: dict[str, Any]


class NodeResult(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)
    next_node: str | None = None
    status: RunStatus | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
