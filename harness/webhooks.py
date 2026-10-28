"""Automated Webhook Event Dispatcher with HMAC-SHA256 Signatures."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from harness.events import AgentEvent, global_event_bus


@dataclass
class WebhookSubscription:
    url: str
    secret: str | None = None
    event_filter: list[str] = field(default_factory=list)  # Empty means all events
    max_retries: int = 2
    timeout_seconds: float = 5.0


def compute_hmac_signature(payload_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest for payload verification."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


class WebhookDispatcher:
    """Dispatches real-time agent execution events to external HTTP webhook endpoints."""

    def __init__(self, subscriptions: list[WebhookSubscription] | None = None) -> None:
        self.subscriptions: list[WebhookSubscription] = subscriptions or []
        self._listener_handle: Callable[[AgentEvent], None] | None = None
        self._dispatched_count = 0
        self._failures_count = 0

    def add_subscription(self, subscription: WebhookSubscription) -> None:
        self.subscriptions.append(subscription)

    def start_listening(self) -> None:
        """Attach listener to global_event_bus."""
        if self._listener_handle is None:
            self._listener_handle = self._on_event
            global_event_bus.subscribe(self._listener_handle)

    def stop_listening(self) -> None:
        if self._listener_handle is not None:
            global_event_bus.unsubscribe(self._listener_handle)
            self._listener_handle = None

    def _on_event(self, event: AgentEvent) -> None:
        for sub in self.subscriptions:
            if not sub.event_filter or event.event_type in sub.event_filter:
                self._dispatch_single(sub, event)

    def _dispatch_single(self, sub: WebhookSubscription, event: AgentEvent) -> bool:
        payload_dict = event.to_dict()
        payload_bytes = json.dumps(payload_dict, sort_keys=True, default=str).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Event": event.event_type,
            "X-Harness-Run-Id": event.run_id,
        }
        if sub.secret:
            signature = compute_hmac_signature(payload_bytes, sub.secret)
            headers["X-Harness-Signature"] = f"sha256={signature}"

        req = urllib.request.Request(sub.url, data=payload_bytes, headers=headers, method="POST")

        for _attempt in range(sub.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=sub.timeout_seconds) as response:
                    if 200 <= response.status < 300:
                        self._dispatched_count += 1
                        return True
            except Exception:  # noqa: BLE001, S110
                pass

        self._failures_count += 1
        return False
