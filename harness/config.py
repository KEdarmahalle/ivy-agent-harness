"""Configuration loading for the harness service with multi-tier resolution.

Resolution order:
1. Explicit `--config <path>` argument (if specified).
2. Workspace `./config.yaml` or `./.harness/config.yaml` (if present).
3. User global `~/.ivy/config.yaml` or `~/.harness/config.yaml` (if present).
4. Bundled package default configuration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config.yaml")


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                f"{path} is not JSON-compatible and PyYAML is not installed"
            ) from exc
        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override dictionary into base dictionary."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _safe_home_dir() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load configuration with global fallback and deep merging."""
    target_path = Path(path) if path else DEFAULT_CONFIG_PATH

    # 1. Explicit path specified and not default
    if path and str(target_path) != str(DEFAULT_CONFIG_PATH):
        if not target_path.exists() or not target_path.is_file():
            raise FileNotFoundError(f"explicit configuration file not found: {target_path}")
        return _read_config_file(target_path)

    # 2. Collect candidate config files
    module_dir = Path(__file__).resolve().parent
    candidates: list[Path] = [
        module_dir / "config.yaml",  # Wheel-bundled package default
        module_dir.parent / "config.yaml",  # Source-checkout default
    ]
    home = _safe_home_dir()
    if home:
        candidates.extend([
            home / ".ivy" / "config.yaml",  # User global config
            home / ".harness" / "config.yaml",
        ])
    candidates.extend([
        Path.cwd() / ".harness" / "config.yaml",  # Workspace config
        Path.cwd() / "config.yaml",               # Workspace root config
    ])

    merged: dict[str, Any] = {}
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            loaded = _read_config_file(candidate)
            if loaded:
                merged = _deep_merge(merged, loaded)

    return merged


def deep_get(config: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
