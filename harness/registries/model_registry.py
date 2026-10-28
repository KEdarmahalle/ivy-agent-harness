"""Config-driven model routing with deterministic, native function calling, and failover chains."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from harness.config import deep_get

DEFAULT_NVIDIA_NIM_API_BASE = "https://integrate.api.nvidia.com/v1"
SUPPORTED_PROVIDERS = {"fallback", "litellm", "nvidia_nim", "nim"}


@dataclass
class ToolCall:
    """Represents a structured function/tool call from a model."""
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "raw_arguments": self.raw_arguments,
        }


class ModelResponse(str):
    """Unified response subclassing str for full JSON/str compatibility with rich metadata."""

    tool_calls: list[ToolCall]
    route_used: str
    finish_reason: str | None

    def __new__(
        cls,
        content: str,
        tool_calls: list[ToolCall] | None = None,
        route_used: str = "",
        finish_reason: str | None = None,
    ) -> Self:
        instance = super().__new__(cls, content)
        instance.tool_calls = tool_calls or []
        instance.route_used = route_used
        instance.finish_reason = finish_reason
        return instance

    @property
    def content(self) -> str:
        return str(self)


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    model: str
    api_key_env: str | None = None
    api_base: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    fallbacks: tuple[str, ...] = ()
    retry_count: int = 1
    retry_delay_seconds: float = 1.0


class ModelConfigurationError(RuntimeError):
    """Raised when a configured model provider cannot be invoked safely."""


class ModelRegistry:
    def __init__(self, routes: dict[str, ModelRoute], default_route: str = "default") -> None:
        self.routes = routes
        self.default_route = default_route

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ModelRegistry:
        raw_routes = deep_get(config, "models", {}) or {}
        routes = {
            name: cls._route_from_mapping(name, value)
            for name, value in raw_routes.items()
            if isinstance(value, dict)
        }
        routes.setdefault("default", ModelRoute(provider="fallback", model="deterministic-local"))
        return cls(routes)

    @classmethod
    def _route_from_mapping(cls, name: str, value: dict[str, Any]) -> ModelRoute:
        if "api_key" in value:
            raise ModelConfigurationError(
                f"model route {name!r} must use api_key_env; inline api_key is forbidden"
            )
        provider = str(value.get("provider", "fallback"))
        if provider not in SUPPORTED_PROVIDERS:
            raise ModelConfigurationError(
                f"model route {name!r} uses unsupported provider {provider!r}"
            )
        model = str(value.get("model", "")).strip()
        if not model:
            raise ModelConfigurationError(f"model route {name!r} requires a non-empty model")
        temperature = value.get("temperature")
        if temperature is not None and not 0 <= float(temperature) <= 2:
            raise ModelConfigurationError(
                f"model route {name!r} temperature must be between 0 and 2"
            )
        max_tokens = value.get("max_tokens")
        if max_tokens is not None and int(max_tokens) <= 0:
            raise ModelConfigurationError(f"model route {name!r} max_tokens must be positive")
        timeout_seconds = value.get("timeout_seconds")
        if timeout_seconds is not None and int(timeout_seconds) <= 0:
            raise ModelConfigurationError(
                f"model route {name!r} timeout_seconds must be positive"
            )

        raw_fallbacks = value.get("fallbacks", [])
        if isinstance(raw_fallbacks, str):
            fallbacks = tuple(f.strip() for f in raw_fallbacks.split(",") if f.strip())
        elif isinstance(raw_fallbacks, (list, tuple)):
            fallbacks = tuple(str(f).strip() for f in raw_fallbacks if str(f).strip())
        else:
            fallbacks = ()

        retry_count = int(value.get("retry_count", 0))
        retry_delay = float(value.get("retry_delay_seconds", 1.0))

        return ModelRoute(
            provider=provider,
            model=model,
            api_key_env=value.get("api_key_env"),
            api_base=value.get("api_base"),
            temperature=float(temperature) if temperature is not None else None,
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            timeout_seconds=int(timeout_seconds) if timeout_seconds is not None else None,
            fallbacks=fallbacks,
            retry_count=max(0, retry_count),
            retry_delay_seconds=max(0.0, retry_delay),
        )

    def route_for(self, graph: str, node: str) -> ModelRoute:
        route_name = self.route_name_for(graph, node)
        return self.routes[route_name]

    def route_name_for(self, graph: str, node: str) -> str:
        target = f"{graph}.{node}"
        if target in self.routes:
            return target
        if graph in self.routes:
            return graph
        if self.default_route in self.routes:
            return self.default_route
        raise ModelConfigurationError(f"No configured route for '{target}' and default route is missing")

    def has_route(self, graph: str, node: str) -> bool:
        return f"{graph}.{node}" in self.routes

    def complete(
        self,
        graph: str,
        node: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
        on_failover: Callable[[str, str, Exception], None] | None = None,
    ) -> ModelResponse:
        """Execute a completion with automatic retry and failover chains."""
        primary_route_name = self.route_name_for(graph, node)
        primary_route = self.routes[primary_route_name]

        # Build failover candidate sequence (explicit fallbacks only)
        candidates: list[tuple[str, ModelRoute]] = [(primary_route_name, primary_route)]
        for fallback_name in primary_route.fallbacks:
            if fallback_name in self.routes and fallback_name != primary_route_name:
                candidates.append((fallback_name, self.routes[fallback_name]))

        last_error: Exception | None = None

        for idx, (route_name, route) in enumerate(candidates):
            effective_max_tokens = max_tokens or route.max_tokens or 4096
            effective_temp = temperature if temperature is not None else route.temperature

            for attempt in range(route.retry_count + 1):
                try:
                    if route.provider == "litellm":
                        res = self._litellm_completion(
                            route=route,
                            model=route.model,
                            messages=messages,
                            api_key=self._api_key_from_env(route),
                            api_base=route.api_base,
                            max_tokens=effective_max_tokens,
                            temperature=effective_temp,
                            stream=stream,
                            tools=tools,
                            on_chunk=on_chunk,
                        )
                        res.route_used = route_name
                        return res

                    if route.provider in {"nvidia_nim", "nim"}:
                        res = self._nvidia_nim_completion(
                            route=route,
                            messages=messages,
                            max_tokens=effective_max_tokens,
                            temperature=effective_temp,
                            stream=stream,
                            tools=tools,
                            on_chunk=on_chunk,
                        )
                        res.route_used = route_name
                        return res

                    # Deterministic fallback provider
                    fallback_text = self._fallback_complete(graph, node, messages)
                    return ModelResponse(content=fallback_text, route_used=route_name)

                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    # If retries remain on this route, backoff and retry
                    if attempt < route.retry_count and route.retry_delay_seconds > 0:
                        time.sleep(route.retry_delay_seconds * (2**attempt))
                        continue
                    break

            # Failover notification if there's a next candidate
            if idx + 1 < len(candidates) and on_failover and last_error:
                next_route_name = candidates[idx + 1][0]
                on_failover(route_name, next_route_name, last_error)

        # If all candidates failed, raise the final error
        if last_error:
            if isinstance(last_error, ModelConfigurationError):
                raise last_error
            raise ModelConfigurationError(f"All route candidates failed for {graph}.{node}: {last_error}") from last_error

        raise ModelConfigurationError(f"No valid completion returned for {graph}.{node}")

    def routes_metadata(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "provider": route.provider,
                "model": route.model,
                "api_base": self._route_api_base(route),
                "api_key_env": route.api_key_env or self._default_key_env(route),
                "has_api_key": self._has_api_key(route),
                "temperature": route.temperature,
                "max_tokens": route.max_tokens or 4096,
                "timeout_seconds": route.timeout_seconds,
                "fallbacks": list(route.fallbacks),
                "retry_count": route.retry_count,
            }
            for name, route in sorted(self.routes.items())
        }

    def _nvidia_nim_completion(
        self,
        route: ModelRoute,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        api_key = self._api_key_from_env(route)
        model = route.model if route.model.startswith("nvidia_nim/") else f"nvidia_nim/{route.model}"
        return self._litellm_completion(
            route=route,
            model=model,
            messages=messages,
            api_key=api_key,
            api_base=self._route_api_base(route),
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            tools=tools,
            on_chunk=on_chunk,
        )

    def _litellm_completion(
        self,
        route: ModelRoute,
        model: str,
        messages: list[dict[str, str]],
        api_key: str | None = None,
        api_base: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        try:
            import litellm
            from litellm import completion
            litellm.suppress_debug_info = True
            litellm.drop_params = True
            os.environ["LITELLM_LOG"] = "ERROR"
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ModelConfigurationError("LiteLLM is configured but not installed") from exc

        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base
        temp = temperature if temperature is not None else route.temperature
        if temp is not None:
            kwargs["temperature"] = temp
        tokens = max_tokens or route.max_tokens or 4096
        kwargs["max_tokens"] = tokens
        if route.timeout_seconds is not None:
            kwargs["timeout"] = route.timeout_seconds
        def _execute_call(pass_tools: bool) -> Any:
            call_kwargs = dict(kwargs)
            if pass_tools and tools:
                call_kwargs["tools"] = tools
                call_kwargs["tool_choice"] = "auto"
            else:
                call_kwargs.pop("tools", None)
                call_kwargs.pop("tool_choice", None)
            return completion(**call_kwargs)

        if stream:
            kwargs["stream"] = True

            def _get_chunks(pass_tools: bool):
                try:
                    gen = _execute_call(pass_tools=pass_tools)
                    yield from gen
                except Exception:
                    # If model rejects native tools schema, retry without native tools schema parameter
                    if pass_tools and tools:
                        yield from _get_chunks(pass_tools=False)
                    else:
                        raise

            chunks: list[str] = []
            streaming_tools: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None

            for chunk in _get_chunks(pass_tools=bool(tools)):
                if not hasattr(chunk, "choices") or not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if hasattr(choice, "finish_reason") and choice.finish_reason:
                    finish_reason = str(choice.finish_reason)

                # 1. Text Content Stream
                delta_content = ""
                if hasattr(choice, "delta") and hasattr(choice.delta, "content") and choice.delta.content:
                    delta_content = choice.delta.content
                elif hasattr(choice, "message") and hasattr(choice.message, "content") and choice.message.content:
                    delta_content = choice.message.content

                if delta_content:
                    chunks.append(delta_content)
                    if on_chunk:
                        on_chunk(delta_content)

                # 2. Native Streaming Tool Calls Accumulator
                delta = getattr(choice, "delta", None)
                if delta and hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        idx = getattr(tc_chunk, "index", 0)
                        if idx not in streaming_tools:
                            streaming_tools[idx] = {
                                "id": getattr(tc_chunk, "id", "") or f"call_{idx}",
                                "name": "",
                                "arguments_parts": [],
                            }
                        fn_delta = getattr(tc_chunk, "function", None)
                        if fn_delta:
                            fn_name = getattr(fn_delta, "name", None)
                            if fn_name:
                                streaming_tools[idx]["name"] += fn_name
                            fn_args = getattr(fn_delta, "arguments", None)
                            if fn_args:
                                streaming_tools[idx]["arguments_parts"].append(fn_args)

            content_text = "".join(chunks)

            # Reconstruct tool calls from stream
            native_tool_calls: list[ToolCall] = []
            for tool_data in streaming_tools.values():
                raw_args = "".join(tool_data["arguments_parts"])
                parsed_args: dict[str, Any] = {}
                if raw_args.strip():
                    try:
                        loaded = json.loads(raw_args)
                        if isinstance(loaded, dict):
                            parsed_args = loaded
                    except json.JSONDecodeError:
                        pass

                native_tool_calls.append(
                    ToolCall(
                        id=tool_data["id"],
                        name=tool_data["name"],
                        arguments=parsed_args,
                        raw_arguments=raw_args,
                    )
                )

            return ModelResponse(
                content=content_text,
                tool_calls=native_tool_calls,
                finish_reason=finish_reason,
            )

        # Non-streaming invocation
        try:
            response = _execute_call(pass_tools=bool(tools))
        except Exception:
            if tools:
                response = _execute_call(pass_tools=False)
            else:
                raise
        content_text = ""
        native_tool_calls = []
        finish_reason = None

        if hasattr(response, "choices") and response.choices:
            choice = response.choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            msg = getattr(choice, "message", None)
            if msg:
                content_text = getattr(msg, "content", "") or ""
                raw_tool_calls = getattr(msg, "tool_calls", None)
                if raw_tool_calls:
                    for raw_tc in raw_tool_calls:
                        tc_id = getattr(raw_tc, "id", "") or "call_0"
                        fn = getattr(raw_tc, "function", None)
                        if not fn:
                            continue
                        fn_name = getattr(fn, "name", "")
                        fn_raw_args = getattr(fn, "arguments", "")
                        parsed_args = {}
                        if isinstance(fn_raw_args, dict):
                            parsed_args = fn_raw_args
                            fn_raw_args = json.dumps(parsed_args)
                        elif isinstance(fn_raw_args, str) and fn_raw_args.strip():
                            try:
                                loaded = json.loads(fn_raw_args)
                                if isinstance(loaded, dict):
                                    parsed_args = loaded
                            except json.JSONDecodeError:
                                pass

                        native_tool_calls.append(
                            ToolCall(
                                id=tc_id,
                                name=fn_name,
                                arguments=parsed_args,
                                raw_arguments=fn_raw_args,
                            )
                        )

        return ModelResponse(
            content=content_text,
            tool_calls=native_tool_calls,
            finish_reason=finish_reason,
        )

    def _api_key_from_env(self, route: ModelRoute) -> str | None:
        api_key_env = route.api_key_env or self._default_key_env(route)
        if not api_key_env:
            return None
        api_key = os.environ.get(api_key_env)
        if not api_key and route.provider in {"nvidia_nim", "nim"}:
            raise ModelConfigurationError(
                f"NVIDIA NIM route for model {route.model!r} requires environment variable "
                f"{api_key_env}"
            )
        return api_key

    def _route_api_base(self, route: ModelRoute) -> str | None:
        if route.provider in {"nvidia_nim", "nim"}:
            return (
                route.api_base
                or os.environ.get("NVIDIA_NIM_API_BASE")
                or DEFAULT_NVIDIA_NIM_API_BASE
            )
        return route.api_base

    def _default_key_env(self, route: ModelRoute) -> str | None:
        if route.provider in {"nvidia_nim", "nim"}:
            return "NVIDIA_NIM_API_KEY"
        return route.api_key_env

    def _has_api_key(self, route: ModelRoute) -> bool:
        key_env = route.api_key_env or self._default_key_env(route)
        return bool(key_env and os.environ.get(key_env))

    def _fallback_complete(self, graph: str, node: str, messages: list[dict[str, str]]) -> str:
        user_text = " ".join(message.get("content", "") for message in messages if message.get("role") == "user")
        if graph == "brainstorm":
            return (
                "1. Clarify the audience and success metric.\n"
                "2. Prototype the smallest useful workflow.\n"
                "3. Add review checkpoints before scaling."
            )
        if graph == "research-report":
            # The deterministic offline provider follows the same structured
            # contract as hosted models so graph validation is exercised locally.
            try:
                payload = json.loads(messages[-1].get("content", "{}"))
                claims = []
                for source in payload.get("sources", []):
                    content = source.get("content", "").strip()
                    if content:
                        excerpt = content[:min(240, len(content))]
                        claims.append({
                            "claim": excerpt,
                            "citations": [{"source_id": source["source_id"], "quote": excerpt}],
                        })
                return json.dumps({
                    "summary": "Offline structured synthesis; source excerpts require human semantic review.",
                    "claims": claims,
                })
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return "{}"
        return f"Deterministic analysis for {node}: {user_text[:500]}"
