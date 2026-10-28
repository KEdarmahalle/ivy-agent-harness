"""Small compatibility layer for running core tests before dependencies are synced."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, get_args, get_origin

try:  # pragma: no cover - exercised when project dependencies are installed.
    from pydantic import BaseModel, ConfigDict, Field
except ModuleNotFoundError:  # pragma: no cover - intentionally tiny fallback.

    class _FieldInfo:
        def __init__(self, default: Any = None, default_factory: Any = None, **_: Any) -> None:
            self.default = default
            self.default_factory = default_factory

    def Field(default: Any = None, default_factory: Any = None, **kwargs: Any) -> Any:
        return _FieldInfo(default=default, default_factory=default_factory, **kwargs)

    class ConfigDict(dict):
        pass

    class BaseModel:
        model_config: ClassVar[dict[str, Any]] = {}

        def __init__(self, **data: Any) -> None:
            annotations: dict[str, Any] = {}
            for cls in reversed(type(self).mro()):
                annotations.update(getattr(cls, "__annotations__", {}))
            for name, annotation in annotations.items():
                if get_origin(annotation) is ClassVar:
                    continue
                default = getattr(type(self), name, None)
                if name in data:
                    value = data.pop(name)
                elif isinstance(default, _FieldInfo):
                    value = default.default_factory() if default.default_factory else default.default
                elif default is not None:
                    value = default
                else:
                    value = None
                setattr(self, name, self._coerce(annotation, value))
            for name, value in data.items():
                setattr(self, name, value)

        @classmethod
        def model_validate(cls, value: Any) -> Any:
            if isinstance(value, cls):
                return value
            if isinstance(value, dict):
                return cls(**value)
            raise TypeError(f"Cannot validate {type(value).__name__} as {cls.__name__}")

        def model_dump(self, mode: str = "python", **_: Any) -> dict[str, Any]:
            return {
                key: self._dump(value, mode)
                for key, value in self.__dict__.items()
                if not key.startswith("_")
            }

        @classmethod
        def _coerce(cls, annotation: Any, value: Any) -> Any:
            origin = get_origin(annotation)
            args = get_args(annotation)
            if value is None:
                return value
            if origin is list and args:
                return [cls._coerce(args[0], item) for item in value]
            if origin is dict:
                return dict(value)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                return annotation.model_validate(value)
            return value

        @classmethod
        def _dump(cls, value: Any, mode: str) -> Any:
            if isinstance(value, BaseModel):
                return value.model_dump(mode=mode)
            if isinstance(value, list):
                return [cls._dump(item, mode) for item in value]
            if isinstance(value, dict):
                return {key: cls._dump(item, mode) for key, item in value.items()}
            if isinstance(value, (datetime, Path)):
                return str(value) if mode == "json" else value
            if isinstance(value, Enum):
                return value.value
            return value
