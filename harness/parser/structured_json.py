"""Bounded extraction of one unambiguous JSON object from model text."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any


class StructuredJSONError(ValueError):
    """A model response cannot be safely interpreted as one JSON object."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredJSONError("duplicate_keys", "JSON contains duplicate object keys")
        result[key] = value
    return result


def _balanced_object_candidates(text: str) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str]] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append((start, index + 1, text[start : index + 1]))
                start = None
    return candidates


def parse_json_object_response(
    response: Any,
    *,
    required_keys: set[str],
    max_bytes: int,
    exact_keys: bool = True,
) -> dict[str, Any]:
    """Parse one complete object, allowing fences or surrounding prose.

    Candidate extraction is deliberately conservative: it does not repair
    truncation, mine nested objects out of malformed envelopes, or choose
    between different valid objects.
    """
    text = str(response)
    try:
        response_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise StructuredJSONError("malformed", "JSON response contains invalid Unicode") from exc
    if response_bytes > max_bytes:
        raise StructuredJSONError("oversized", f"JSON response exceeds the {max_bytes // 1000} KB limit")
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise StructuredJSONError("empty", "model returned an empty response")

    candidates: dict[tuple[int, int], str] = {(0, len(text)): text}
    for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL):
        fenced = match.group(1)
        trimmed = fenced.strip()
        if trimmed:
            start = match.start(1) + len(fenced) - len(fenced.lstrip())
            candidates[(start, start + len(trimmed))] = trimmed
    for start, end, candidate in _balanced_object_candidates(text):
        candidates[(start, end)] = candidate

    def reject_non_json_constant(value: str) -> None:
        raise StructuredJSONError("malformed", f"JSON contains invalid constant {value}")

    decoder = json.JSONDecoder(
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=reject_non_json_constant,
    )
    valid: list[dict[str, Any]] = []
    for candidate in candidates.values():
        try:
            parsed, end = decoder.raw_decode(candidate)
        except (json.JSONDecodeError, ValueError, RecursionError):
            continue
        if candidate[end:].strip() or not isinstance(parsed, dict):
            continue
        keys = set(parsed)
        if not required_keys.issubset(keys) or (exact_keys and keys != required_keys):
            continue
        valid.append(parsed)

    if not valid:
        raise StructuredJSONError("malformed", "response did not contain a complete object with the required keys")
    if len(valid) != 1:
        raise StructuredJSONError("ambiguous", "response contained multiple matching JSON objects")
    return valid[0]
