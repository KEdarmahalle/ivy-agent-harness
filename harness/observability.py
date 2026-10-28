"""Trace sink facade for optional external observability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.config import deep_get
from harness.engine.schemas import TraceEvent


@dataclass
class Observability:
    enabled: bool = False
    sink: Any = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> Observability:
        enabled = bool(deep_get(config, "observability.langfuse.enabled", False))
        if not enabled:
            return cls(enabled=False)
        try:
            from langfuse import get_client
        except ModuleNotFoundError:
            return cls(enabled=False)
        return cls(enabled=True, sink=get_client())

    def record(self, event: TraceEvent) -> None:
        if not self.enabled or self.sink is None:
            return
        try:
            self.sink.update_current_trace(
                name="agent-harness-run",
                metadata={
                    "run_id": event.run_id,
                    "seq": event.seq,
                    "node": event.node,
                    "event_type": event.event_type,
                    "payload": event.payload,
                },
            )
        except (AttributeError, RuntimeError, ValueError):
            self.enabled = False
