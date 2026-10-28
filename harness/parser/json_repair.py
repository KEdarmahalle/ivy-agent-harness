"""JSON string sanitization and automated repair utilities for LLM output."""

from __future__ import annotations

import re


def sanitize_json_string(raw: str) -> str:
    """Sanitize unescaped Windows backslashes in JSON strings (e.g. C:\agent_harness -> C:/agent_harness)."""
    def fix_backslash(match: re.Match[str]) -> str:
        s = match.group(0)
        return re.sub(r'\\(?![\"\\/bfnrtu])', "/", s)

    return re.sub(r'"([^"\\]*(\\.[^"\\]*)*)"', fix_backslash, raw)


def repair_truncated_json(raw: str) -> str:
    """Repair truncated or incomplete JSON strings (e.g. models hitting token limits mid-generation)."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()

    if '"tool"' not in text and "'tool'" not in text:
        return text

    # Remove unescaped trailing colons/commas
    text = re.sub(r"[,:\s]+$", "", text)

    in_quote = False
    escape = False
    for char in text:
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_quote = not in_quote

    if in_quote:
        text += '"'

    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")

    if open_braces > 0:
        text += "}" * open_braces
    if open_brackets > 0:
        text += "]" * open_brackets

    return text
