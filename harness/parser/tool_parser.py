"""Tool call parsing and invocation extractors."""

from __future__ import annotations

import json
import re
from typing import Any

from harness.parser.json_repair import repair_truncated_json, sanitize_json_string


def parse_tool_calls(reply: str) -> list[dict[str, Any]]:
    """Extract one or multiple tool calls from model output in any format (single, list, comma-separated, truncated)."""
    tool_calls: list[dict[str, Any]] = []
    cleaned = reply.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()

    repaired = repair_truncated_json(cleaned)
    candidate_texts = [cleaned, repaired, f"[{cleaned.rstrip(',')}]", f"[{repaired.rstrip(',')}]"]
    for candidate in candidate_texts:
        for sanitized in [candidate, sanitize_json_string(candidate)]:
            try:
                parsed = json.loads(sanitized)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and isinstance(item.get("tool"), str):
                            tool_calls.append({
                                "tool": item["tool"],
                                "args": item.get("args", {}) if isinstance(item.get("args"), dict) else {},
                            })
                    if tool_calls:
                        return tool_calls
                elif isinstance(parsed, dict) and isinstance(parsed.get("tool"), str):
                    return [{
                        "tool": parsed["tool"],
                        "args": parsed.get("args", {}) if isinstance(parsed.get("args"), dict) else {},
                    }]
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

    pos = 0
    while pos < len(reply):
        start = reply.find("{", pos)
        if start == -1:
            break
        brace_count = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(reply)):
            char = reply[i]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        end = i
                        break
        if end != -1:
            chunk = reply[start : end + 1]
            for candidate in [chunk, sanitize_json_string(chunk), repair_truncated_json(chunk)]:
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict) and isinstance(obj.get("tool"), str):
                        args = obj.get("args", {})
                        tool_calls.append({
                            "tool": obj["tool"],
                            "args": args if isinstance(args, dict) else {},
                        })
                        break
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
            pos = end + 1
        else:
            pos = start + 1

    return tool_calls


def parse_tool_call(reply: str) -> dict[str, Any] | None:
    calls = parse_tool_calls(reply)
    return calls[0] if calls else None


def execute_chat_tool(tools: Any, tool_call: dict[str, Any]) -> dict[str, Any]:
    try:
        result = tools.invoke(tool_call["tool"], **tool_call["args"])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "tool": tool_call.get("tool"), "error": f"{type(exc).__name__}: {exc}"}
    # Tool handlers may return structured failures without raising (for example,
    # rejected plans or unavailable integrations). Preserve that status so
    # policy gates cannot mistake a failed operation for an authorized success.
    ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
    observation = {"ok": ok, "tool": tool_call["tool"], "result": result}
    if not ok and isinstance(result, dict) and result.get("error"):
        observation["error"] = str(result["error"])[:1000]
    return observation


def summarize_tool_observations(observations: list[dict[str, Any]]) -> str:
    if not observations:
        return "I could not gather project context before the tool budget was exhausted."
    lines = ["I inspected the project with local tools. Summary:"]
    for observation in observations:
        if not observation.get("ok"):
            lines.append(f"- Tool error: {observation.get('error')}")
            continue
        tool_name = observation.get("tool")
        result = observation.get("result", {})
        if tool_name == "file.list" and isinstance(result, dict):
            path = result.get("path", ".")
            entries = result.get("entries", [])
            names = [
                f"{entry.get('name')}/" if entry.get("is_dir") else str(entry.get("name"))
                for entry in entries[:20]
                if isinstance(entry, dict)
            ]
            lines.append(f"- {path}: {', '.join(names)}")
        elif tool_name == "file.search_text" and isinstance(result, dict):
            matches = result.get("matches", [])
            refs = [
                f"{match.get('file')}:{match.get('line')}"
                for match in matches[:10]
                if isinstance(match, dict)
            ]
            lines.append(f"- Search matches: {', '.join(refs)}")
        else:
            lines.append(f"- {tool_name}: {json.dumps(result, default=str)[:1000]}")
    return "\n".join(lines)
