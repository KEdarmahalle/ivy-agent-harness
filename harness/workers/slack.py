"""Autonomous Slack Bot and Webhook Background Worker.

Provides:
- Handling of `@Ivy` Slack app mentions with thread replies.
- Interactive HITL approval actions for tool permissions.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from harness.sdk import AgentHarness


def post_slack_message(
    channel: str,
    text: str,
    thread_ts: str | None = None,
    bot_token: str | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Post a message or thread reply to Slack via Bot API or incoming webhook."""
    token = bot_token or os.environ.get("SLACK_BOT_TOKEN")
    hook = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")

    try:
        if hook:
            payload: dict[str, Any] = {"text": text}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            req = urllib.request.Request(
                hook,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return {"status": "posted_webhook", "code": resp.status}

        if token:
            url = "https://slack.com/api/chat.postMessage"
            payload = {"channel": channel, "text": text}
            if thread_ts:
                payload["thread_ts"] = thread_ts
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                return {"status": "posted_api", "ok": res_data.get("ok"), "ts": res_data.get("ts")}
    except Exception as exc:  # noqa: BLE001 - convert integration failures to worker results
        return {"status": "error", "error": str(exc), "channel": channel}

    return {"status": "dry_run", "channel": channel, "text_len": len(text)}


async def on_slack_mention(
    text: str,
    user: str,
    channel: str,
    thread_ts: str | None = None,
    bot_token: str | None = None,
    reply_in_thread: bool = True,
) -> dict[str, Any]:
    """Process a Slack @mention by executing an agent run and posting back."""
    harness = AgentHarness.from_project(workspace_root=".")
    clean_text = text.replace("<@IVY>", "").replace("<@ivy>", "").strip()

    result = await harness.arun(clean_text)

    reply_text = f"Hi <@{user}>! Here is what I did:\n\n{result.final_output}"
    dispatch_res = {}
    if reply_in_thread:
        dispatch_res = post_slack_message(
            channel=channel,
            text=reply_text,
            thread_ts=thread_ts,
            bot_token=bot_token,
        )

    return {
        "status": "completed",
        "user": user,
        "channel": channel,
        "result": result.to_dict(),
        "slack_dispatch": dispatch_res,
    }
