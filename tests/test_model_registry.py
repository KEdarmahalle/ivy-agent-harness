from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from harness.registries.model_registry import (
    DEFAULT_NVIDIA_NIM_API_BASE,
    ModelConfigurationError,
    ModelRegistry,
)


def completion_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class ModelRegistryTests(unittest.TestCase):
    def test_nvidia_nim_route_invokes_litellm_with_prefixed_model_and_secret_env(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "brainstorm.generate": {
                        "provider": "nvidia_nim",
                        "model": "nvidia/nemotron-3-ultra-550b-a55b",
                        "api_key_env": "NVIDIA_NIM_API_KEY",
                        "temperature": 0,
                        "max_tokens": 16,
                        "timeout_seconds": 30,
                    }
                }
            }
        )
        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=completion_response("ok")) as completion,
        ):
            text = registry.complete(
                "brainstorm",
                "generate",
                [{"role": "user", "content": "Reply with ok."}],
            )

        self.assertEqual(text, "ok")
        completion.assert_called_once()
        kwargs = completion.call_args.kwargs
        self.assertEqual(kwargs["model"], "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(kwargs["api_key"], "test-secret")
        self.assertEqual(kwargs["api_base"], DEFAULT_NVIDIA_NIM_API_BASE)
        self.assertEqual(kwargs["temperature"], 0)
        self.assertEqual(kwargs["max_tokens"], 16)
        self.assertEqual(kwargs["timeout"], 30)

    def test_nvidia_nim_route_honors_api_base_env_override(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "brainstorm.generate": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        with (
            patch.dict(
                os.environ,
                {
                    "NVIDIA_NIM_API_KEY": "test-secret",
                    "NVIDIA_NIM_API_BASE": "https://nim.example.test/v1",
                },
                clear=False,
            ),
            patch("litellm.completion", return_value=completion_response("ok")) as completion,
        ):
            registry.complete(
                "brainstorm",
                "generate",
                [{"role": "user", "content": "Reply with ok."}],
            )
        self.assertEqual(completion.call_args.kwargs["api_base"], "https://nim.example.test/v1")

    def test_nvidia_nim_gpt_oss_120b_route_invokes_litellm(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.gpt-oss-120b": {
                        "provider": "nvidia_nim",
                        "model": "openai/gpt-oss-120b",
                    }
                }
            }
        )
        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=completion_response("ok")) as completion,
        ):
            registry.complete(
                "nim",
                "gpt-oss-120b",
                [{"role": "user", "content": "Reply with ok."}],
            )
        self.assertEqual(completion.call_args.kwargs["model"], "nvidia_nim/openai/gpt-oss-120b")

    def test_nvidia_nim_route_requires_api_key_before_network_call(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "brainstorm.generate": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("litellm.completion") as completion,
            self.assertRaises(ModelConfigurationError),
        ):
            registry.complete(
                "brainstorm",
                "generate",
                [{"role": "user", "content": "Reply with ok."}],
            )
        completion.assert_not_called()

    def test_routes_metadata_is_sanitized(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                        "api_key_env": "NVIDIA_NIM_API_KEY",
                    }
                }
            }
        )
        with patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False):
            metadata = registry.routes_metadata()["nim.example"]
        self.assertEqual(metadata["provider"], "nvidia_nim")
        self.assertEqual(metadata["api_key_env"], "NVIDIA_NIM_API_KEY")
        self.assertTrue(metadata["has_api_key"])
        self.assertNotIn("api_key", metadata)
        self.assertNotIn("test-secret", str(metadata))

    def test_inline_api_key_is_rejected(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            ModelRegistry.from_config(
                {
                    "models": {
                        "nim.example": {
                            "provider": "nvidia_nim",
                            "model": "meta/llama-3.1-8b-instruct",
                            "api_key": "do-not-store-secrets",
                        }
                    }
                }
            )

    def test_invalid_route_settings_are_rejected(self) -> None:
        invalid_configs = [
            {"provider": "unknown", "model": "x"},
            {"provider": "nvidia_nim", "model": ""},
            {"provider": "nvidia_nim", "model": "x", "temperature": 3},
            {"provider": "nvidia_nim", "model": "x", "max_tokens": 0},
            {"provider": "nvidia_nim", "model": "x", "timeout_seconds": -1},
        ]
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(ModelConfigurationError):
                ModelRegistry.from_config({"models": {"bad.route": config}})

    def test_complete_supports_dynamic_max_tokens_override(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=completion_response("ok")) as completion,
        ):
            registry.complete(
                "nim",
                "example",
                [{"role": "user", "content": "Reply with ok."}],
                max_tokens=8192,
            )
            self.assertEqual(completion.call_args.kwargs["max_tokens"], 8192)

    def test_complete_supports_streaming(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        chunks_mock = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="world!"))]),
        ]
        collected: list[str] = []
        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=iter(chunks_mock)) as completion,
        ):
            res = registry.complete(
                "nim",
                "example",
                [{"role": "user", "content": "Hi"}],
                stream=True,
                on_chunk=collected.append,
            )
        self.assertEqual(res, "Hello world!")
        self.assertEqual(collected, ["Hello ", "world!"])
        self.assertTrue(completion.call_args.kwargs["stream"])

    def test_native_tool_calling_extraction(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        mock_tool_call = SimpleNamespace(
            id="call_123",
            function=SimpleNamespace(
                name="file.read_text",
                arguments='{"file": "main.py"}',
            ),
        )
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Reading file",
                        tool_calls=[mock_tool_call],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=mock_response),
        ):
            res = registry.complete(
                "nim",
                "example",
                [{"role": "user", "content": "Read main.py"}],
                tools=[{"type": "function", "function": {"name": "file.read_text"}}],
            )

        self.assertEqual(res.content, "Reading file")
        self.assertEqual(len(res.tool_calls), 1)
        self.assertEqual(res.tool_calls[0].name, "file.read_text")
        self.assertEqual(res.tool_calls[0].arguments, {"file": "main.py"})

    def test_streaming_native_tool_calls_accumulation(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.example": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                    }
                }
            }
        )
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="Executing ",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_abc",
                                    function=SimpleNamespace(name="process.", arguments='{"comm'),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="command",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(name="run", arguments='and": "ls"}'),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            ),
        ]
        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", return_value=iter(chunks)),
        ):
            res = registry.complete(
                "nim",
                "example",
                [{"role": "user", "content": "Run ls"}],
                stream=True,
            )

        self.assertEqual(res.content, "Executing command")
        self.assertEqual(len(res.tool_calls), 1)
        self.assertEqual(res.tool_calls[0].name, "process.run")
        self.assertEqual(res.tool_calls[0].arguments, {"command": "ls"})

    def test_failover_chain_on_error(self) -> None:
        registry = ModelRegistry.from_config(
            {
                "models": {
                    "nim.primary": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.1-8b-instruct",
                        "fallbacks": ["nim.secondary", "default"],
                        "retry_count": 0,
                    },
                    "nim.secondary": {
                        "provider": "nvidia_nim",
                        "model": "meta/llama-3.3-70b-instruct",
                        "retry_count": 0,
                    },
                    "default": {
                        "provider": "fallback",
                        "model": "deterministic-local",
                    },
                }
            }
        )

        def mock_completion(**kwargs: Any) -> Any:
            if "8b" in kwargs.get("model", ""):
                raise RuntimeError("Service temporarily overloaded (503)")
            return completion_response("Secondary route succeeded!")

        failover_events: list[tuple[str, str]] = []

        def on_failover(from_r: str, to_r: str, exc: Exception) -> None:
            failover_events.append((from_r, to_r))

        with (
            patch.dict(os.environ, {"NVIDIA_NIM_API_KEY": "test-secret"}, clear=False),
            patch("litellm.completion", side_effect=mock_completion),
        ):
            res = registry.complete(
                "nim",
                "primary",
                [{"role": "user", "content": "Hello"}],
                on_failover=on_failover,
            )

        self.assertEqual(res.content, "Secondary route succeeded!")
        self.assertEqual(res.route_used, "nim.secondary")
        self.assertEqual(len(failover_events), 1)
        self.assertEqual(failover_events[0], ("nim.primary", "nim.secondary"))


if __name__ == "__main__":
    unittest.main()
