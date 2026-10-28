"""Domain-neutral graph contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from harness.engine.schemas import NodeResult, RunSnapshot


class RunContext(Protocol):
    config: dict
    tools: object
    models: object
    mcp: object

    def trace(self, event_type: str, payload: dict | None = None, node: str | None = None) -> None:
        ...


NodeHandler = Callable[[RunContext, RunSnapshot], NodeResult | Awaitable[NodeResult]]


@dataclass(frozen=True)
class NodeSpec:
    name: str
    handler: NodeHandler


@dataclass(frozen=True)
class GraphSpec:
    name: str
    start_node: str
    nodes: dict[str, NodeSpec] = field(default_factory=dict)

    def node(self, name: str) -> NodeSpec:
        try:
            return self.nodes[name]
        except KeyError as exc:
            raise KeyError(f"graph {self.name!r} has no node {name!r}") from exc
