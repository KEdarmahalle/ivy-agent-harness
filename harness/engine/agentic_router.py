"""Agentic Graph Router: Autonomous AI-driven Dynamic Node Navigation & Decision Engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from harness.events import AgenticRouteDecisionEvent, global_event_bus
from harness.parser.json_repair import repair_truncated_json
from harness.registries.model_registry import ModelRegistry


@dataclass
class AgenticRouteDecision:
    """Outcome of an AI dynamic graph node decision."""

    action: str  # "advance", "skip", "backtrack", "finish"
    next_node: str | None = None
    rationale: str = ""
    state_updates: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0


class AgenticGraphRouter:
    """Cognitive graph supervisor that reasons over workflow goals, state, and candidate nodes."""

    def __init__(
        self,
        models: ModelRegistry | None = None,
        default_route: str = "default",
        max_loop_count: int = 5,
    ) -> None:
        self.models = models or ModelRegistry()
        self.default_route = default_route
        self.max_loop_count = max_loop_count

    def decide_next_node(
        self,
        graph_name: str,
        current_node: str,
        nodes_catalog: dict[str, dict[str, Any]],
        goal: str,
        state: dict[str, Any],
        visited_nodes: list[str],
        route_override: str | None = None,
        allowed_candidates: list[str] | None = None,
    ) -> AgenticRouteDecision:
        """Dynamically evaluate state and select the next graph node or declare completion."""
        # 1. Candidate nodes filter
        candidates = {
            k: v for k, v in nodes_catalog.items()
            if not allowed_candidates or k in allowed_candidates
        }

        # 2. Check for infinite loop circuit breaker
        node_occurrences = visited_nodes.count(current_node)
        if node_occurrences >= self.max_loop_count:
            # Circuit breaker: Force termination or progression away from loop
            alternate_nodes = [k for k in candidates if k != current_node and visited_nodes.count(k) < 2]
            target = alternate_nodes[0] if alternate_nodes else None
            decision = AgenticRouteDecision(
                action="finish" if not target else "advance",
                next_node=target,
                rationale=f"Circuit breaker triggered: Node '{current_node}' executed {node_occurrences} times.",
                state_updates={"circuit_breaker_triggered": True},
            )
            self._emit_event(graph_name, current_node, decision, len(visited_nodes))
            return decision

        # 3. Assemble dynamic routing prompt
        candidates_desc = []
        for name, meta in candidates.items():
            desc = meta.get("description") or meta.get("goal") or f"Node {name}"
            tools = ", ".join(meta.get("tools", [])) or "None"
            candidates_desc.append(f"- **{name}**: {desc} (Tools: {tools})")
        candidates_str = "\n".join(candidates_desc)

        visited_str = " -> ".join(visited_nodes) if visited_nodes else current_node
        clean_state = {k: v for k, v in state.items() if not str(k).startswith("_") and len(str(v)) < 1000}
        state_summary = json.dumps(clean_state, indent=2, default=str)

        system_prompt = (
            "You are the Autonomous Graph Supervisor & Dynamic Workflow Router.\n"
            "Your job is to evaluate the current execution progress and determine the optimal next node in the graph.\n\n"
            "You have 4 possible routing actions:\n"
            "1. 'advance': Proceed to the next logical downstream node.\n"
            "2. 'skip': Jump past unneeded intermediate nodes directly to a later node.\n"
            "3. 'backtrack': Loop back to an earlier node to fix regressions, re-test, or refine.\n"
            "4. 'finish': Conclude the workflow immediately if the goal is completely satisfied.\n\n"
            "OUTPUT PROTOCOL:\n"
            "You MUST respond ONLY with a single valid JSON object in this exact schema:\n"
            "{\n"
            '  "action": "advance" | "skip" | "backtrack" | "finish",\n'
            '  "next_node": "<node_name_from_candidates>" or null if action is finish,\n'
            '  "rationale": "<Clear engineering explanation for this routing decision>",\n'
            '  "state_updates": {"key": "value"}\n'
            "}"
        )

        user_prompt = (
            f"# OVERARCHING WORKFLOW GOAL\n{goal}\n\n"
            f"# EXECUTION TRAJECTORY\nVisited: {visited_str}\nCurrent Node: {current_node}\n\n"
            f"# ACCUMULATED STATE & OBSERVATIONS\n{state_summary}\n\n"
            f"# CANDIDATE GRAPH NODES\n{candidates_str}\n\n"
            "Decide the next step based on the findings so far."
        )

        # 4. Invoke LLM completion
        route = route_override or self.default_route
        graph_part, node_part = (route.split(".", 1)) if "." in route else ("chat", route)

        try:
            res_obj = self.models.complete(
                graph=graph_part,
                node=node_part,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1024,
                stream=False,
            )
            raw_text = getattr(res_obj, "content", str(res_obj))
            decision = self._parse_decision(raw_text, candidates)
        except Exception as exc:  # noqa: BLE001 - routing failures must fail closed.
            decision = AgenticRouteDecision(
                action="finish",
                rationale="Dynamic route selection failed; workflow stopped safely.",
                state_updates={"routing_error": type(exc).__name__},
            )

        self._emit_event(graph_name, current_node, decision, len(visited_nodes))
        return decision

    def _parse_decision(self, raw_text: str, candidates: dict[str, Any]) -> AgenticRouteDecision:
        clean = raw_text.strip()
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            json_str = match.group(0)
        else:
            json_str = repair_truncated_json(clean)

        parse_error: str | None = None
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            data = {"action": "finish", "next_node": None, "rationale": "Invalid route response."}
            parse_error = "invalid_json"
        if not isinstance(data, dict):
            data = {"action": "finish", "next_node": None, "rationale": "Invalid router response."}
            parse_error = "invalid_response"

        action = str(data.get("action", "advance")).lower()
        if action not in {"advance", "skip", "backtrack", "finish"}:
            action = "finish"
            parse_error = "invalid_action"

        next_node = data.get("next_node")
        if next_node and next_node not in candidates:
            # Match case-insensitively or fuzzy
            for c_name in candidates:
                if c_name.lower() == str(next_node).lower():
                    next_node = c_name
                    break
            else:
                next_node = None
                action = "finish"
                parse_error = "unknown_target"

        if action == "finish":
            next_node = None
        elif not next_node and not parse_error:
            parse_error = "missing_target"

        updates = data.get("state_updates", {})
        if not isinstance(updates, dict) or len(updates) > 64:
            updates = {}
        safe_updates: dict[str, Any] = {}
        total_update_bytes = 0
        reserved = {"required_approval", "resume_node", "approvals", "approved"}
        for key, value in updates.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", key):
                continue
            if key.startswith(("_", "approved_")) or key in reserved:
                continue
            try:
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
                if len(encoded) > 4_000:
                    continue
            except (TypeError, ValueError):
                continue
            total_update_bytes += len(key.encode("utf-8")) + len(encoded.encode("utf-8"))
            if total_update_bytes > 8_000:
                break
            safe_updates[key] = value

        if parse_error:
            safe_updates["routing_error"] = parse_error

        return AgenticRouteDecision(
            action=action,
            next_node=next_node,
            rationale=(data.get("rationale", "")[:2_000] if isinstance(data.get("rationale", ""), str) else ""),
            state_updates=safe_updates,
        )

    def _emit_event(
        self,
        graph_name: str,
        from_node: str,
        decision: AgenticRouteDecision,
        step_index: int,
    ) -> None:
        event = AgenticRouteDecisionEvent(
            event_id=f"route_{from_node}_{step_index}",
            event_type="agentic_route_decision",
            run_id=graph_name,
            from_node=from_node,
            to_node=decision.next_node,
            action=decision.action,
            rationale=decision.rationale,
            step_index=step_index,
            state_updates=decision.state_updates,
        )
        global_event_bus.emit(event)
