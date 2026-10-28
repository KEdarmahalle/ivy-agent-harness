"""Open-ended brainstorming graph with no verification gate."""

from __future__ import annotations

from typing import Any

from harness.engine.graph import GraphSpec, NodeSpec
from harness.engine.schemas import NodeResult, RunSnapshot


def build_graph() -> GraphSpec:
    return GraphSpec(
        name="brainstorm",
        start_node="generate",
        nodes={
            "generate": NodeSpec("generate", generate),
            "structure": NodeSpec("structure", structure),
            "finalize": NodeSpec("finalize", finalize),
        },
    )


def generate(context: Any, snapshot: RunSnapshot) -> NodeResult:
    prompt = str(snapshot.input.get("prompt") or snapshot.input.get("query") or "")
    raw = context.models.complete(
        "brainstorm",
        "generate",
        [{"role": "user", "content": prompt}],
    )
    ideas = [
        line.split(".", 1)[-1].strip()
        for line in raw.splitlines()
        if line.strip()
    ]
    context.trace("model_called", {"node": "generate", "idea_count": len(ideas)})
    return NodeResult(updates={"raw_ideas": raw, "ideas": ideas}, next_node="structure")


def structure(context: Any, snapshot: RunSnapshot) -> NodeResult:
    ideas = snapshot.state.get("ideas", [])
    structured = [
        {
            "title": idea[:70],
            "rationale": "Fits the prompt and can be explored without verification overhead.",
            "next_step": "Turn this into one concrete experiment or decision.",
        }
        for idea in ideas
    ]
    context.trace("ideas_structured", {"count": len(structured)})
    return NodeResult(updates={"structured_ideas": structured}, next_node="finalize")


def finalize(context: Any, snapshot: RunSnapshot) -> NodeResult:
    result = {
        "prompt": snapshot.input.get("prompt") or snapshot.input.get("query"),
        "ideas": snapshot.state.get("structured_ideas", []),
        "confidence": "not_gated",
    }
    return NodeResult(status="done", result=result)
