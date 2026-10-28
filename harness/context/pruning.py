"""Context optimization, observation masking, and bilateral tool trimming.

Implements frontier agent context management techniques (Claude Code / SWE-agent / OpenHands):
1. Rolling Observation Masking: Masks older tool results in multi-step ReAct loops to prevent context bloat.
2. Bilateral Tool Output Trimming: Preserves critical head error lines and tail summaries with truncation banners.
3. Cache-Aligned Prefix Management: Preserves static invariant system prompt headers for maximum prompt cache hit rate.
"""

from __future__ import annotations

import json
import re
from typing import Any


def trim_tool_output(
    output: str,
    max_chars: int = 4000,
    head_lines: int = 25,
    tail_lines: int = 35,
) -> str:
    """Trim large tool outputs bilaterally (head + truncation banner + tail)."""
    if not output or len(output) <= max_chars:
        return output

    lines = output.splitlines()
    if len(lines) <= (head_lines + tail_lines):
        marker = f"[... {len(output) - max_chars} characters omitted by output policy ...]"
        remaining = max(0, max_chars - len(marker))
        if remaining == 0:
            return marker[:max_chars]
        head_size = (remaining + 1) // 2
        tail_size = remaining - head_size
        return f"{output[:head_size]}{marker}{output[-tail_size:] if tail_size else ''}"

    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    omitted = len(lines) - (head_lines + tail_lines)
    marker = f"\n[... {omitted} lines omitted by sandbox policy ...]\n"
    if len(marker) >= max_chars:
        marker = "[... lines omitted by sandbox policy ...]"
    remaining = max(0, max_chars - len(marker))
    if remaining == 0:
        return marker[:max_chars]
    head_size = (remaining + 1) // 2
    tail_size = remaining - head_size
    return f"{head[:head_size]}{marker}{tail[-tail_size:] if tail_size else ''}"


def mask_observation_payload(raw_obs: Any) -> Any:
    """Convert verbose tool observation into a lightweight receipt token."""
    if isinstance(raw_obs, dict):
        tool_name = raw_obs.get("tool") or raw_obs.get("tool_name", "unknown")
        ok_val = raw_obs.get("ok", True)
        err = raw_obs.get("error")

        masked_entry: dict[str, Any] = {
            "tool": tool_name,
            "status": "completed" if ok_val else "failed",
            "[OBSERVATION_MASKED]": f"Output from earlier turn masked to preserve context window. Status ok={ok_val}.",
        }
        if err:
            masked_entry["error"] = str(err)[:120]
        if "file" in raw_obs:
            masked_entry["file"] = raw_obs["file"]
        return masked_entry

    if isinstance(raw_obs, list):
        return [mask_observation_payload(item) for item in raw_obs]

    text = str(raw_obs)
    return f"[OBSERVATION_MASKED: {len(text)} chars from earlier turn masked]"


def apply_observation_masking(
    messages: list[dict[str, Any]],
    keep_recent: int = 2,
) -> list[dict[str, Any]]:
    """Mask older tool observations in ReAct message history, keeping the last `keep_recent` full."""
    if len(messages) <= 3:
        return messages

    # Identify all indices containing tool observation results
    obs_indices: list[int] = []
    for i, msg in enumerate(messages):
        content = str(msg.get("content", ""))
        role = msg.get("role")
        if role == "tool" or (role == "user" and "Tool result(s):" in content):
            obs_indices.append(i)

    if len(obs_indices) <= keep_recent:
        return messages

    # Older observation indices to mask
    indices_to_mask = set(obs_indices[:-keep_recent])
    optimized: list[dict[str, Any]] = []

    for i, msg in enumerate(messages):
        if i not in indices_to_mask:
            optimized.append(msg)
            continue

        role = msg.get("role")
        content = str(msg.get("content", ""))

        if role == "user" and "Tool result(s):" in content:
            # Parse tool result JSON block
            prefix_match = re.search(r"Tool result\(s\):\s*\n", content)
            suffix_match = re.search(r"\n\n(Synthesize findings.*|Now answer.*|The tool budget.*)", content, re.DOTALL)

            prompt_suffix = suffix_match.group(1) if suffix_match else "Synthesize findings or continue."
            json_part = content[prefix_match.end() : suffix_match.start()].strip() if (prefix_match and suffix_match) else content

            try:
                parsed_json = json.loads(json_part)
                masked_json = mask_observation_payload(parsed_json)
                masked_content = (
                    "Tool result(s):\n"
                    f"{json.dumps(masked_json, indent=2, default=str)}\n\n"
                    f"{prompt_suffix}"
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                masked_content = (
                    "Tool result(s):\n"
                    "{\"status\": \"ok\", \"[OBSERVATION_MASKED]\": \"Earlier tool observation masked.\"}\n\n"
                    f"{prompt_suffix}"
                )

            optimized.append({"role": role, "content": masked_content})

        elif role == "tool":
            try:
                parsed = json.loads(content)
                masked_content = json.dumps(mask_observation_payload(parsed), default=str)
            except (json.JSONDecodeError, TypeError, ValueError):
                masked_content = "{\"status\": \"ok\", \"[OBSERVATION_MASKED]\": \"Earlier tool observation masked.\"}"
            optimized.append({
                "role": role,
                "name": msg.get("name"),
                "content": masked_content,
            })
        else:
            optimized.append(msg)

    return optimized
