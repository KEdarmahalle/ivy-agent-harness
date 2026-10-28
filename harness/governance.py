"""Enterprise governance, multi-tenancy isolation, RBAC, cost budgets, and rate limiting."""

from __future__ import annotations

import hmac
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


class GovernanceError(PermissionError):
    """Raised when a tenant, RBAC policy, or sandbox isolation is violated."""


class BudgetExceededError(RuntimeError):
    """Raised when a token or dollar spend budget is exceeded."""


class RateLimitExceededError(RuntimeError):
    """Raised when request rate or concurrency limit is exceeded."""


class RateLimiter:
    """Sliding-window rate limiter per tenant or user."""

    def __init__(self, max_requests_per_minute: int = 60, max_concurrent: int = 10) -> None:
        self.max_rpm = max_requests_per_minute
        self.max_concurrent = max_concurrent
        self._timestamps: list[float] = []
        self._active_count = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            if len(self._timestamps) >= self.max_rpm:
                raise RateLimitExceededError(f"Rate limit exceeded: max {self.max_rpm} requests/min")
            if self._active_count >= self.max_concurrent:
                raise RateLimitExceededError(f"Concurrency limit exceeded: max {self.max_concurrent} concurrent runs")
            self._timestamps.append(now)
            self._active_count += 1

    def release(self) -> None:
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1


# Standard model pricing estimates per 1,000 tokens (USD)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "default": {"prompt": 0.0015, "completion": 0.002},
    "llama-70b": {"prompt": 0.0007, "completion": 0.0009},
    "qwen-coder": {"prompt": 0.0006, "completion": 0.0008},
    "gpt-4o": {"prompt": 0.0050, "completion": 0.0150},
    "claude-3-5-sonnet": {"prompt": 0.0030, "completion": 0.0150},
}


@dataclass
class CostBudget:
    """Enforces strict token and USD limits on agent runs."""
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    used_tokens: int = 0
    used_cost_usd: float = 0.0

    def record_usage(self, prompt_tokens: int, completion_tokens: int, model: str = "default") -> None:
        total = prompt_tokens + completion_tokens
        self.used_tokens += total

        pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])
        cost = (prompt_tokens / 1000.0 * pricing["prompt"]) + (completion_tokens / 1000.0 * pricing["completion"])
        self.used_cost_usd += cost

        if self.max_tokens and self.used_tokens > self.max_tokens:
            raise BudgetExceededError(
                f"Token budget exceeded: used {self.used_tokens} tokens (limit {self.max_tokens})"
            )
        if self.max_cost_usd and self.used_cost_usd > self.max_cost_usd:
            raise BudgetExceededError(
                f"Cost budget exceeded: spent ${self.used_cost_usd:.4f} USD (limit ${self.max_cost_usd:.4f})"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "max_tokens": self.max_tokens,
            "used_cost_usd": round(self.used_cost_usd, 6),
            "max_cost_usd": self.max_cost_usd,
        }


@dataclass
class TenantContext:
    """Multi-tenancy context specifying tenant identity, workspace, and roles."""
    tenant_id: str
    user_id: str
    roles: list[str] = field(default_factory=lambda: ["user"])
    budget: CostBudget = field(default_factory=CostBudget)
    allowed_roots: list[str] = field(default_factory=list)
    auto_approve_tools: bool = False

    def check_tool_permission(self, tool_name: str, restricted_tools: dict[str, list[str]] | None = None) -> None:
        """Check if tenant's roles permit invoking a given tool."""
        restrictions = restricted_tools or {
            "process.run": ["admin", "developer"],
            "sandbox.run_python": ["admin", "developer", "data_scientist"],
            "data.sql_query": ["admin", "developer", "data_analyst", "user"],
        }

        required_roles = restrictions.get(tool_name)
        if required_roles and not any(role in required_roles for role in self.roles):
            raise GovernanceError(
                f"Tenant {self.tenant_id!r} (user {self.user_id!r}, roles {self.roles}) is not authorized "
                f"to invoke restricted tool {tool_name!r}. Required roles: {required_roles}"
            )


_CURRENT_TENANT: ContextVar[TenantContext | None] = ContextVar("harness_tenant", default=None)


def current_tenant() -> TenantContext | None:
    return _CURRENT_TENANT.get()


def set_current_tenant(context: TenantContext | None) -> Any:
    return _CURRENT_TENANT.set(context)


def reset_current_tenant(token: Any) -> None:
    _CURRENT_TENANT.reset(token)


def has_auth_credentials(config: dict[str, Any]) -> bool:
    auth = config.get("auth", {}) or {}
    return bool(auth.get("api_key") or os.environ.get("AGENT_HARNESS_API_KEY") or auth.get("api_keys"))


def authenticate_api_key(config: dict[str, Any], token: str | None) -> TenantContext | None:
    """Resolve a configured key to a principal; ``None`` means unauthenticated."""
    auth = config.get("auth", {}) or {}
    records = auth.get("api_keys", {}) or {}
    if isinstance(records, dict):
        iterable = records.items()
    elif isinstance(records, list):
        iterable = ((str(item.get("id", index)), item) for index, item in enumerate(records) if isinstance(item, dict))
    else:
        iterable = ()
    for key_id, record in iterable:
        if not isinstance(record, dict):
            continue
        env_name = str(record.get("key_env", ""))
        expected = os.environ.get(env_name) if env_name else None
        if token and expected and hmac.compare_digest(token, expected):
            return TenantContext(
                tenant_id=str(record.get("tenant_id", "default")),
                user_id=str(record.get("user_id", key_id)),
                roles=[str(role) for role in record.get("roles", ["user"])],
                allowed_roots=[str(root) for root in record.get("allowed_roots", [])],
                auto_approve_tools=bool(record.get("auto_approve_tools", False)),
            )

    legacy_key = auth.get("api_key") or os.environ.get("AGENT_HARNESS_API_KEY")
    if token and legacy_key and hmac.compare_digest(token, str(legacy_key)):
        return TenantContext(
            tenant_id=str(auth.get("tenant_id", "default")),
            user_id=str(auth.get("user_id", "api-key")),
            roles=[str(role) for role in auth.get("roles", ["admin"])],
            auto_approve_tools=bool(auth.get("auto_approve_tools", False)),
        )
    return None
