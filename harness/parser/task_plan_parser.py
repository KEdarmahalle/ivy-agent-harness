"""Task-plan-specific facade over the shared structured JSON response parser."""

from __future__ import annotations

from typing import Any

from harness.parser.structured_json import StructuredJSONError, parse_json_object_response


class PlanResponseError(StructuredJSONError):
    """A planner response could not be interpreted as one unambiguous plan."""


def parse_task_plan_response(response: Any, *, max_bytes: int = 64_000) -> dict[str, Any]:
    try:
        return parse_json_object_response(
            response,
            required_keys={"schema_version", "goal", "tasks"},
            max_bytes=max_bytes,
        )
    except StructuredJSONError as exc:
        raise PlanResponseError(exc.code, str(exc)) from exc
