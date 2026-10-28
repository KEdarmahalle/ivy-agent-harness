"""Universal real-time typed event bus supporting in-memory subscribers, SSE, and webhooks."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class AgentEvent:
    """Universal agent execution event compatible with CloudEvents."""

    event_id: str
    event_type: str
    run_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    node: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "type": self.event_type,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "node": self.node,
            "payload": self.payload,
        }

    def to_sse_message(self) -> str:
        """Format event as Server-Sent Events (SSE) data frame."""
        return f"event: {self.event_type}\ndata: {json.dumps(self.to_dict(), default=str)}\n\n"


# ==================== Typed Event Specializations ====================


@dataclass
class TokenStreamEvent(AgentEvent):
    """Emitted for real-time LLM token generation."""

    delta: str = ""
    index: int = 0
    role: str = "assistant"

    def __post_init__(self) -> None:
        self.event_type = "token"
        if not self.payload:
            self.payload = {"delta": self.delta, "index": self.index, "role": self.role}


@dataclass
class ToolExecutionEvent(AgentEvent):
    """Emitted on tool invocation start, completion, or failure."""

    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    status: str = "started"  # "started", "completed", "failed"
    duration_s: float = 0.0
    summary: str = ""

    def __post_init__(self) -> None:
        self.event_type = f"tool_{self.status}"
        if not self.payload:
            self.payload = {
                "tool": self.tool_name,
                "args": self.args,
                "result": self.result,
                "status": self.status,
                "duration_s": self.duration_s,
                "summary": self.summary,
            }


@dataclass
class SubagentSwarmEvent(AgentEvent):
    """Emitted on subagent swarm fan-out, hand-off, or completion."""

    agent_name: str = ""
    status: str = "started"  # "started", "completed", "failed"
    task: str = ""
    output: str = ""
    tools_count: int = 0

    def __post_init__(self) -> None:
        self.event_type = f"subagent_{self.status}"
        if not self.payload:
            self.payload = {
                "agent": self.agent_name,
                "status": self.status,
                "task": self.task,
                "output": self.output,
                "tools_count": self.tools_count,
            }


@dataclass
class ApprovalRequiredEvent(AgentEvent):
    """Emitted when a tool or node execution requires Human-in-the-Loop approval."""

    call_id: str = ""
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = "Tool policy requires explicit human approval"

    def __post_init__(self) -> None:
        self.event_type = "approval_required"
        if not self.payload:
            self.payload = {
                "call_id": self.call_id,
                "tool": self.tool_name,
                "args": self.args,
                "reason": self.reason,
            }


@dataclass
class AgentRunEvent(AgentEvent):
    """Emitted on agent turn start, step iteration, or completion."""

    status: str = "started"  # "started", "completed", "failed"
    final_output: str = ""
    steps: int = 0
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        self.event_type = f"run_{self.status}"
        if not self.payload:
            self.payload = {
                "status": self.status,
                "final_output": self.final_output,
                "steps": self.steps,
                "duration_s": self.duration_s,
            }


@dataclass
class AgenticRouteDecisionEvent(AgentEvent):
    """Emitted when AI dynamic router makes an autonomous graph node transition."""

    from_node: str = ""
    to_node: str | None = None
    action: str = "advance"  # "advance", "skip", "backtrack", "finish"
    rationale: str = ""
    step_index: int = 0
    state_updates: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.event_type = "agentic_route_decision"
        if not self.payload:
            self.payload = {
                "from_node": self.from_node,
                "to_node": self.to_node,
                "action": self.action,
                "rationale": self.rationale,
                "step_index": self.step_index,
                "state_updates": self.state_updates,
            }


EventListener = Callable[[AgentEvent], None]
AsyncEventListener = Callable[[AgentEvent], Any]


class EventBus:
    """Central event broker for agent lifecycle and real-time streaming with bounded memory."""

    def __init__(self, max_history: int = 5000) -> None:
        self.max_history = max_history
        self._subscribers: list[EventListener] = []
        self._typed_subscribers: dict[str, list[EventListener]] = {}
        self._async_queues: list[asyncio.Queue[AgentEvent]] = []
        self._history: deque[AgentEvent] = deque(maxlen=max_history)
        self._counter: int = 0

    def subscribe(self, listener: EventListener, event_type: str | None = None) -> None:
        """Register a synchronous listener callback, optionally filtered by event_type."""
        if event_type:
            listeners = self._typed_subscribers.setdefault(event_type, [])
            if listener not in listeners:
                listeners.append(listener)
        else:
            if listener not in self._subscribers:
                self._subscribers.append(listener)

    def unsubscribe(self, listener: EventListener, event_type: str | None = None) -> None:
        if event_type:
            listeners = self._typed_subscribers.get(event_type, [])
            if listener in listeners:
                listeners.remove(listener)
        else:
            if listener in self._subscribers:
                self._subscribers.remove(listener)

    def create_stream(self, max_size: int = 1000) -> asyncio.Queue[AgentEvent]:
        """Create a bounded async queue for SSE or WebSocket streaming."""
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=max_size)
        self._async_queues.append(queue)
        return queue

    def close_stream(self, queue: asyncio.Queue[AgentEvent]) -> None:
        if queue in self._async_queues:
            self._async_queues.remove(queue)

    def publish(
        self,
        event_type: str,
        run_id: str,
        payload: dict[str, Any] | None = None,
        node: str | None = None,
    ) -> AgentEvent:
        """Emit an event to all sync listeners and async streaming queues."""
        self._counter += 1
        event_id = f"evt_{self._counter}_{datetime.now(UTC).timestamp()}"
        event = AgentEvent(
            event_id=event_id,
            event_type=event_type,
            run_id=run_id,
            node=node,
            payload=payload or {},
        )
        self._history.append(event)
        self._dispatch(event)
        return event

    def emit(self, event: AgentEvent) -> AgentEvent:
        """Emit a strongly-typed AgentEvent instance directly."""
        self._history.append(event)
        self._dispatch(event)
        return event

    def _dispatch(self, event: AgentEvent) -> None:
        # Notify global sync listeners
        for listener in list(self._subscribers):
            try:
                listener(event)
            except Exception:  # noqa: BLE001, S110
                pass

        # Notify typed sync listeners
        for listener in list(self._typed_subscribers.get(event.event_type, [])):
            try:
                listener(event)
            except Exception:  # noqa: BLE001, S110
                pass

        # Push to async stream queues (non-blocking)
        for q in list(self._async_queues):
            try:
                q.put_nowait(event)
            except (asyncio.QueueFull, Exception):  # noqa: BLE001, S110
                pass

    def get_history(self, run_id: str | None = None) -> list[AgentEvent]:
        if run_id:
            return [e for e in self._history if e.run_id == run_id]
        return list(self._history)

    def clear(self) -> None:
        self._history.clear()


# Global singleton instance for easy cross-module subscription
global_event_bus = EventBus()

# Set while a tool is executing on behalf of a chat turn. Nested tool events
# inherit this correlation ID, but no principal data is added to shared events.
_CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("harness_event_run_id", default=None)


def current_event_run_id() -> str | None:
    return _CURRENT_RUN_ID.get()


def set_current_event_run_id(run_id: str) -> Token[str | None]:
    return _CURRENT_RUN_ID.set(run_id)


def reset_current_event_run_id(token: Token[str | None]) -> None:
    _CURRENT_RUN_ID.reset(token)
