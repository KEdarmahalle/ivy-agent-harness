"""Local secret loading for developer-run harness commands."""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

from harness.config import deep_get

DEFAULT_SECRET_STORE = Path(".harness/secrets.json")


class SecretStoreError(RuntimeError):
    """Raised when local secret storage cannot complete the requested action."""


def configured_secret_store(config: dict[str, Any] | None = None) -> Path:
    raw_path = (
        os.environ.get("AGENT_HARNESS_SECRET_STORE")
        or deep_get(config or {}, "secrets.store_path")
        or DEFAULT_SECRET_STORE
    )
    return Path(raw_path)


def _load_dotenv_file(path: Path, target: dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k:
                target[k] = v
    except OSError:
        pass


def _safe_home_dir() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError):
        return None


def load_secrets(config: dict[str, Any] | None = None, path: str | Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}

    home = _safe_home_dir()
    if home:
        # 1. Check Global User Secret Store (~/.ivy/secrets.json)
        global_store_path = home / ".ivy" / "secrets.json"
        if global_store_path.exists() and global_store_path.is_file():
            try:
                g_store = _read_store(global_store_path)
                values.update(_decode_values(g_store))
            except Exception:  # noqa: BLE001, S110
                pass

        # 2. Check Global User .env (~/.ivy/.env)
        global_env_path = home / ".ivy" / ".env"
        if global_env_path.exists() and global_env_path.is_file():
            _load_dotenv_file(global_env_path, values)

    # 3. Check Workspace .env
    local_env_path = Path.cwd() / ".env"
    if local_env_path.exists() and local_env_path.is_file():
        _load_dotenv_file(local_env_path, values)

    # 4. Check Configured Secret Store (path or config)
    target_store = Path(path) if path else configured_secret_store(config)
    if target_store.exists() and target_store.is_file():
        store = _read_store(target_store)
        values.update(_decode_values(store))

    for name, value in values.items():
        os.environ.setdefault(name, value)
    return values


def set_secret(name: str, value: str, path: str | Path | None = None) -> None:
    _validate_name(name)
    store_path = Path(path) if path else configured_secret_store()
    values = _decode_values(_read_store(store_path))
    values[name] = value
    _write_values(store_path, values)


def remove_secret(name: str, path: str | Path | None = None) -> bool:
    _validate_name(name)
    store_path = Path(path) if path else configured_secret_store()
    values = _decode_values(_read_store(store_path))
    existed = name in values
    values.pop(name, None)
    _write_values(store_path, values)
    os.environ.pop(name, None)
    return existed


def list_secret_names(path: str | Path | None = None) -> list[str]:
    store_path = Path(path) if path else configured_secret_store()
    return sorted(_decode_values(_read_store(store_path)))


def mask_secret_value(value: str) -> str:
    """Mask a secret value for safe logging / display (e.g. sk-***1234)."""
    if not value or len(value) < 6:
        return "******"
    prefix = value[:3]
    suffix = value[-4:]
    return f"{prefix}***{suffix}"


def redact_secrets(text: str, custom_secrets: dict[str, str] | None = None) -> str:
    """Scan string and redact all known secret tokens."""
    if not text:
        return ""
    all_secrets = load_secrets()
    if custom_secrets:
        all_secrets.update(custom_secrets)

    redacted = str(text)
    for name, val in all_secrets.items():
        if isinstance(val, str) and len(val) >= 6 and val in redacted:
            redacted = redacted.replace(val, f"[REDACTED_{name.upper()}]")
    return redacted


def _read_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "backend": _backend(), "values": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SecretStoreError(f"secret store is not valid JSON: {path}") from exc
    if not isinstance(loaded, dict):
        raise SecretStoreError(f"secret store root must be an object: {path}")
    return loaded


def _write_values(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backend = _backend()
    encoded = {
        name: _protect(value) if backend == "windows-dpapi" else value
        for name, value in sorted(values.items())
    }
    payload = {"version": 1, "backend": backend, "values": encoded}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _decode_values(store: dict[str, Any]) -> dict[str, str]:
    raw_values = store.get("values", {})
    if not isinstance(raw_values, dict):
        raise SecretStoreError("secret store values must be an object")
    backend = store.get("backend") or _backend()
    decoded: dict[str, str] = {}
    for name, raw_value in raw_values.items():
        if not isinstance(name, str) or not isinstance(raw_value, str):
            continue
        decoded[name] = _decode_value(raw_value, backend)
    return decoded


def _decode_value(raw_value: str, backend: str) -> str:
    if backend != "windows-dpapi":
        return raw_value
    try:
        return _unprotect(raw_value)
    except (binascii.Error, UnicodeDecodeError, SecretStoreError):
        return raw_value


def _validate_name(name: str) -> None:
    if not name or not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise SecretStoreError("secret names must look like environment variable names")


def _backend() -> str:
    forced = os.environ.get("AGENT_HARNESS_SECRETS_BACKEND")
    if forced:
        return forced
    return "windows-dpapi" if os.name == "nt" else "local-file"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(value: str) -> str:
    if os.name != "nt":
        return value
    data = value.encode("utf-8")
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStoreError("Windows DPAPI failed to protect the secret")
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect(value: str) -> str:
    if os.name != "nt":
        return value
    encrypted = base64.b64decode(value.encode("ascii"))
    in_buffer = ctypes.create_string_buffer(encrypted)
    in_blob = _DataBlob(len(encrypted), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStoreError("Windows DPAPI failed to read the secret")
    try:
        plaintext = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return plaintext.decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)
