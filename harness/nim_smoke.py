"""Direct NVIDIA NIM model smoke test helper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness.config import load_config
from harness.registries.model_registry import ModelConfigurationError, ModelRegistry
from harness.secrets import load_secrets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test an NVIDIA NIM chat model route")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--graph", default="nim")
    parser.add_argument("--node", default="example")
    parser.add_argument("--model", default=None, help="Override the configured route model")
    parser.add_argument("--prompt", default="Reply with ok.")
    parser.add_argument("--api-key-env", default="NVIDIA_NIM_API_KEY")
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    load_secrets(config)
    route_key = f"{args.graph}.{args.node}"
    if args.model:
        config.setdefault("models", {})[route_key] = {
            "provider": "nvidia_nim",
            "model": args.model,
            "api_key_env": args.api_key_env,
            "api_base": args.api_base,
            "max_tokens": args.max_tokens,
            "temperature": 0,
        }
    registry = ModelRegistry.from_config(config)
    if not registry.has_route(args.graph, args.node):
        print(
            json.dumps(
                {"ok": False, "error": f"model route not configured: {route_key}"},
                indent=2,
            )
        )
        return 2
    try:
        text = registry.complete(
            args.graph,
            args.node,
            [{"role": "user", "content": args.prompt}],
        )
    except ModelConfigurationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps({"ok": True, "response_excerpt": text[:1000]}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
