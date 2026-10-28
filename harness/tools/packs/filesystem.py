"""Filesystem Tool Pack: list, read, write, replace, and search files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harness.tools.policy import SandboxPolicy


def resolve_in_workspace(path: str | Path, root: str | Path | None, policy: SandboxPolicy) -> Path:
    base = Path(root).resolve() if root else policy.workspace_roots[0]
    candidate = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()

    if policy.is_protected(candidate):
        raise PermissionError(f"Access to protected path {candidate} is forbidden.")

    for ws_root in policy.workspace_roots:
        try:
            candidate.relative_to(ws_root)
            return candidate
        except ValueError:
            continue

    if not policy.is_custom_roots:
        return candidate
    raise PermissionError(f"path {candidate} is outside configured workspace roots")


def list_files(path: str = ".", root: str | None = None, max_entries: int = 100, policy: SandboxPolicy | None = None) -> dict[str, Any]:
    policy = policy or SandboxPolicy(workspace_roots=[Path.cwd()], writable_roots=[Path.cwd()])
    target = resolve_in_workspace(path, root, policy)
    if not target.exists():
        raise FileNotFoundError(f"directory {target} does not exist")
    if not target.is_dir():
        raise NotADirectoryError(f"{target} is not a directory")

    entries: list[dict[str, Any]] = []
    for item in sorted(target.iterdir()):
        if len(entries) >= max_entries:
            break
        # Skip protected files/folders
        if policy.is_protected(item):
            continue
        entries.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size_bytes": item.stat().st_size if item.is_file() else 0,
        })
    return {"path": str(target), "entries": entries, "count": len(entries)}


def read_text(file: str, root: str | None = None, policy: SandboxPolicy | None = None) -> dict[str, Any]:
    policy = policy or SandboxPolicy(workspace_roots=[Path.cwd()], writable_roots=[Path.cwd()])
    target = resolve_in_workspace(file, root, policy)
    if not target.exists():
        raise FileNotFoundError(f"file {target} does not exist")
    if not target.is_file():
        raise IsADirectoryError(f"{target} is a directory")

    content = target.read_text(encoding="utf-8", errors="replace")
    if len(content.encode("utf-8")) > policy.max_read_bytes:
        content = content[:policy.max_read_bytes]
    return {"file": str(target), "content": content, "size_bytes": len(content.encode("utf-8"))}


def read_lines(file: str, start: int = 1, end: int | None = None, root: str | None = None, policy: SandboxPolicy | None = None) -> dict[str, Any]:
    policy = policy or SandboxPolicy(workspace_roots=[Path.cwd()], writable_roots=[Path.cwd()])
    target = resolve_in_workspace(file, root, policy)
    if not target.exists():
        raise FileNotFoundError(f"file {target} does not exist")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start_idx = max(0, start - 1)
    end_idx = end if end is not None else len(lines)
    selected = lines[start_idx:end_idx]
    return {"file": str(target), "lines": selected, "start": start, "end": end_idx, "total_lines": len(lines)}


def write_text(file: str, content: str, root: str | None = None, policy: SandboxPolicy | None = None) -> dict[str, Any]:
    policy = policy or SandboxPolicy(workspace_roots=[Path.cwd()], writable_roots=[Path.cwd()])
    target = resolve_in_workspace(file, root, policy)
    if not policy.is_writable(target):
        raise PermissionError(f"file {target} is outside configured writable roots")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"file": str(target), "bytes_written": len(content.encode("utf-8")), "status": "written"}


def replace_text(file: str, old: str, new: str, root: str | None = None, policy: SandboxPolicy | None = None) -> dict[str, Any]:
    policy = policy or SandboxPolicy(workspace_roots=[Path.cwd()], writable_roots=[Path.cwd()])
    target = resolve_in_workspace(file, root, policy)
    if not policy.is_writable(target):
        raise PermissionError(f"file {target} is outside configured writable roots")
    content = target.read_text(encoding="utf-8")
    if old not in content:
        raise ValueError(f"target string not found in {target}")
    updated = content.replace(old, new)
    target.write_text(updated, encoding="utf-8")
    return {"file": str(target), "status": "replaced"}


def search_text(query: str, root: str | None = None, max_results: int = 50, policy: SandboxPolicy | None = None) -> dict[str, Any]:
    policy = policy or SandboxPolicy(workspace_roots=[Path.cwd()], writable_roots=[Path.cwd()])
    base = Path(root).resolve() if root else policy.workspace_roots[0]
    matches: list[dict[str, Any]] = []
    for current_root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__" and not policy.is_protected(d)]
        for f in files:
            if len(matches) >= max_results:
                break
            fp = Path(current_root) / f
            if policy.is_protected(fp):
                continue
            try:
                content = fp.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(content.splitlines(), start=1):
                    if query in line:
                        matches.append({"file": str(fp.relative_to(base)), "line": i, "content": line.strip()})
                        if len(matches) >= max_results:
                            break
            except (OSError, UnicodeError):
                continue
    return {"query": query, "matches": matches, "total": len(matches)}
