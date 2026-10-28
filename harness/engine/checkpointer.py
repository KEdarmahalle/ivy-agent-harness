"""Checkpoint store factory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.config import deep_get
from harness.engine.store import SQLiteRunStore


def build_store(config: dict[str, Any]) -> SQLiteRunStore:
    sqlite_path = deep_get(config, "database.sqlite_path", ".harness/runs.sqlite3")
    return SQLiteRunStore(Path(sqlite_path))
