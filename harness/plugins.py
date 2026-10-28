"""Universal dynamic plugin engine and @tool decorator for client-attached extensions."""

from __future__ import annotations

import importlib.util
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, get_args, get_origin

PluginToolFn = Callable[..., Any]


@dataclass
class PluginToolSpec:
    name: str
    fn: PluginToolFn
    description: str
    category: str = "custom"
    origin: str = "plugin"
    mutates_state: bool = False
    requires_approval: bool = False
    input_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "origin": self.origin,
            "mutates_state": self.mutates_state,
            "requires_approval": self.requires_approval,
            "input_schema": self.input_schema,
        }


def _type_to_json_schema(annotation: Any) -> tuple[str, bool]:
    """Convert a Python type annotation to JSON schema type string with optionality flag."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return "string", True

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Handle Optional[T] / Union[T, None]
    if origin is not None and type(None) in args:
        inner_types = [t for t in args if t is not type(None)]
        if inner_types:
            inner_name, _ = _type_to_json_schema(inner_types[0])
            return inner_name, True
        return "string", True

    if annotation in (str, Path):
        return "string", False
    if annotation in (int,):
        return "integer", False
    if annotation in (float,):
        return "number", False
    if annotation in (bool,):
        return "boolean", False
    if annotation in (list, tuple) or origin in (list, tuple):
        return "array", False
    if annotation in (dict,) or origin in (dict,):
        return "object", False

    return "string", False


def tool(
    name: str | None = None,
    description: str | None = None,
    *,
    category: str = "custom",
    requires_approval: bool = False,
    mutates_state: bool = False,
) -> Callable[[PluginToolFn], PluginToolFn]:
    """Decorator to mark a Python function as an AI agent tool."""

    def decorator(fn: PluginToolFn) -> PluginToolFn:
        fn_name = name or fn.__name__.replace("_", ".")
        fn_doc = description or (inspect.getdoc(fn) or f"Custom tool {fn_name}").split("\n\n")[0].strip()

        # Reflect input schema from signature
        sig = inspect.signature(fn)
        schema: dict[str, Any] = {}

        for param_name, param in sig.parameters.items():
            if param_name in {"self", "cls"}:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            type_name, is_optional = _type_to_json_schema(param.annotation)
            if param.default is not inspect.Parameter.empty:
                is_optional = True

            schema[param_name] = f"{type_name}?" if is_optional else type_name

        spec = PluginToolSpec(
            name=fn_name,
            fn=fn,
            description=fn_doc,
            category=category,
            origin="plugin",
            mutates_state=mutates_state,
            requires_approval=requires_approval,
            input_schema=schema,
        )

        fn.__ivy_tool_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return decorator


class PluginRegistry:
    """Discovers and manages client custom tools from .harness/plugins/ and packages."""

    DEFAULT_PLUGIN_DIRS: ClassVar[list[str]] = [
        ".harness/plugins",
        "~/.ivy/plugins",
        "plugins",
    ]

    def __init__(
        self,
        workspace_root: Path | str = ".",
        plugin_dirs: list[str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.plugin_dirs = plugin_dirs or list(self.DEFAULT_PLUGIN_DIRS)
        self._tools: dict[str, PluginToolSpec] = {}
        self.discover_plugins()

    def discover_plugins(self) -> dict[str, PluginToolSpec]:
        """Scan configured plugin directories for Python modules containing @tool decorators."""
        self._tools.clear()

        for raw_dir in self.plugin_dirs:
            if raw_dir.startswith("~"):
                try:
                    p_dir = Path.home() / raw_dir[2:]
                except (RuntimeError, KeyError):
                    continue
            else:
                p_dir = self.workspace_root / raw_dir

            if not p_dir.exists() or not p_dir.is_dir():
                continue

            for py_file in sorted(p_dir.glob("*.py")):
                if py_file.name.startswith("__"):
                    continue
                self._load_module_tools(py_file)

        return self._tools

    def _load_module_tools(self, file_path: Path) -> None:
        """Dynamically import a Python file and register all @tool functions."""
        module_name = f"ivy_plugin_{re.sub(r'[^a-zA-Z0-9_]', '_', file_path.stem)}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if not spec or not spec.loader:
                return
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            for attr_name in dir(mod):
                attr = getattr(mod, attr_name, None)
                if callable(attr) and hasattr(attr, "__ivy_tool_spec__"):
                    tool_spec: PluginToolSpec = attr.__ivy_tool_spec__
                    self._tools[tool_spec.name] = tool_spec
        except Exception:  # noqa: BLE001, S110
            # Ignore broken plugins gracefully without crashing the whole harness
            pass

    def list_tools(self) -> list[PluginToolSpec]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> PluginToolSpec | None:
        return self._tools.get(name)
