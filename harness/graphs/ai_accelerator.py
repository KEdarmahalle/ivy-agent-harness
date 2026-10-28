"""AI Accelerator & Optimization Pipeline Graph with Dynamic Agentic Routing.

Demonstrates an end-to-end multi-stage AI model optimization workflow where:
1. Latency & memory are benchmarked.
2. AI dynamic supervisor analyzes metrics and dynamically chooses optimization paths (quantization, pruning, distillation).
3. If accuracy degrades, the graph autonomously backtracks and tries alternative calibration.
4. Human approval gate protects production deployment.
"""

from __future__ import annotations

from typing import Any

from harness.engine.agentic_router import AgenticGraphRouter
from harness.engine.graph import GraphSpec, NodeSpec
from harness.engine.schemas import NodeResult, RunSnapshot


def build_graph() -> GraphSpec:
    return GraphSpec(
        name="ai-accelerator",
        start_node="profile_baseline",
        nodes={
            "profile_baseline": NodeSpec("profile_baseline", profile_baseline),
            "agentic_triage": NodeSpec("agentic_triage", agentic_triage),
            "quantize_weights": NodeSpec("quantize_weights", quantize_weights),
            "prune_layers": NodeSpec("prune_layers", prune_layers),
            "benchmark_verify": NodeSpec("benchmark_verify", benchmark_verify),
            "production_gate": NodeSpec("production_gate", production_gate),
            "deploy_artifact": NodeSpec("deploy_artifact", deploy_artifact),
        },
    )


def profile_baseline(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 1: Benchmark baseline model latency, throughput, and memory."""
    context.trace("node_started", {"node": "profile_baseline"})
    model_name = str(snapshot.input.get("model", "deepseek-r1-distill-70b"))
    target_latency_ms = float(snapshot.input.get("target_latency_ms", 35.0))

    # Simulated/real benchmark metrics
    baseline_metrics = {
        "model": model_name,
        "latency_ms": 78.4,
        "memory_gb": 42.0,
        "throughput_tokens_sec": 45.2,
        "accuracy_score": 0.942,
        "target_latency_ms": target_latency_ms,
    }
    context.trace("benchmark_completed", baseline_metrics)

    return NodeResult(
        updates={
            "baseline": baseline_metrics,
            "current_metrics": baseline_metrics,
            "target_latency_ms": target_latency_ms,
        },
        next_node="agentic_triage",
    )


def agentic_triage(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 2: AI Cognitive Router dynamically decides optimization strategy."""
    context.trace("node_started", {"node": "agentic_triage"})
    router = AgenticGraphRouter(models=context.models)

    nodes_catalog = {
        "quantize_weights": {
            "description": "Quantize model weights to INT8/FP8 to reduce latency and memory footprint.",
            "tools": ["process.run", "sandbox.run_python"],
        },
        "prune_layers": {
            "description": "Prune redundant attention heads and sparse MLP weights for compute acceleration.",
            "tools": ["process.run"],
        },
        "benchmark_verify": {
            "description": "Evaluate latency and accuracy against validation benchmark suite.",
            "tools": ["process.run"],
        },
    }

    visited = snapshot.state.setdefault("_visited_history", [])
    visited.append("agentic_triage")

    decision = router.decide_next_node(
        graph_name="ai-accelerator",
        current_node="agentic_triage",
        nodes_catalog=nodes_catalog,
        goal="Achieve target latency with minimal accuracy degradation.",
        state=snapshot.state,
        visited_nodes=visited,
    )

    updates = {
        "triage_decision": {
            "action": decision.action,
            "target_node": decision.next_node,
            "rationale": decision.rationale,
        }
    }
    updates.update(decision.state_updates)

    # Fallback to quantize_weights if router returned None
    next_node = decision.next_node or "quantize_weights"
    return NodeResult(updates=updates, next_node=next_node)


def quantize_weights(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 3: Perform post-training quantization."""
    context.trace("node_started", {"node": "quantize_weights"})
    precision = snapshot.state.get("retry_precision", "INT8")

    # Update metrics after quantization
    new_metrics = dict(snapshot.state.get("current_metrics", {}))
    new_metrics["latency_ms"] = 32.1 if precision == "INT8" else 42.5
    new_metrics["memory_gb"] = 16.5
    new_metrics["accuracy_score"] = 0.938
    new_metrics["quantization_precision"] = precision

    context.trace("quantization_completed", new_metrics)
    return NodeResult(
        updates={"current_metrics": new_metrics, "quantized": True},
        next_node="benchmark_verify",
    )


def prune_layers(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 4: Prune sparse weights."""
    context.trace("node_started", {"node": "prune_layers"})
    new_metrics = dict(snapshot.state.get("current_metrics", {}))
    new_metrics["latency_ms"] = 28.4
    new_metrics["accuracy_score"] = 0.925

    context.trace("pruning_completed", new_metrics)
    return NodeResult(
        updates={"current_metrics": new_metrics, "pruned": True},
        next_node="benchmark_verify",
    )


def benchmark_verify(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 5: Verify accuracy and target latency, with self-healing backtracking."""
    context.trace("node_started", {"node": "benchmark_verify"})
    current = snapshot.state.get("current_metrics", {})
    target_lat = snapshot.state.get("target_latency_ms", 35.0)

    acc = current.get("accuracy_score", 0.0)
    lat = current.get("latency_ms", 999.0)

    if acc < 0.93 and not snapshot.state.get("retried_with_fp8"):
        # Autonomous Backtrack: Accuracy degraded too much, loop back to quantize with FP8
        context.trace("backtrack_triggered", {"reason": "Accuracy degradation > threshold", "action": "Retry FP8"})
        return NodeResult(
            updates={"retried_with_fp8": True, "retry_precision": "FP8"},
            next_node="quantize_weights",
        )

    if lat <= target_lat and acc >= 0.93:
        context.trace("benchmarks_passed", {"latency_ms": lat, "accuracy_score": acc})
        return NodeResult(
            updates={"verification_status": "passed", "final_metrics": current},
            next_node="production_gate",
        )

    return NodeResult(
        updates={"verification_status": "suboptimal", "final_metrics": current},
        next_node="production_gate",
    )


def production_gate(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 6: Human-in-the-Loop approval gate before deployment."""
    context.trace("node_started", {"node": "production_gate"})
    context.trace("gate_triggered", {"gate": "human_approval_required"})
    return NodeResult(
        updates={"required_approval": "production_deployment_token", "resume_node": "deploy_artifact"},
        status="needs_human",
        next_node="deploy_artifact",
    )


def deploy_artifact(context: Any, snapshot: RunSnapshot) -> NodeResult:
    """Step 7: Deploy optimized artifact."""
    context.trace("node_started", {"node": "deploy_artifact"})
    metrics = snapshot.state.get("final_metrics", {})
    deploy_result = {
        "status": "deployed",
        "endpoint": "https://models.internal.corp/v1/models/deepseek-r1-optimized",
        "metrics": metrics,
    }
    context.trace("deployment_successful", deploy_result)
    return NodeResult(
        updates={"deployment": deploy_result},
        result=deploy_result,
        status="done",
        next_node=None,
    )
