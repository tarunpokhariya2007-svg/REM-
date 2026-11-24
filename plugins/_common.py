"""Small shared helpers for the reconstructed REM plugin suite."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def config(namespace: str) -> dict:
    try:
        from memory.config_manager import get_plugin_config
        return get_plugin_config(namespace)
    except Exception:
        return {}


def save_config(namespace: str, values: dict) -> None:
    from memory.config_manager import save_plugin_config
    save_plugin_config(namespace, values)


def route(player, name: str, parameters: dict) -> str:
    catalog_fn = getattr(player, "tool_catalog", None)
    run_fn = getattr(player, "run_tool", None)
    if not callable(catalog_fn) or not callable(run_fn):
        return f"The existing {name} capability is unavailable in this session."
    catalog = catalog_fn()
    if not any(isinstance(t, dict) and t.get("name") == name for t in catalog):
        return f"The existing {name} tool is not installed or enabled."
    return str(run_fn(name, parameters or {}))


def log(player, text: str) -> None:
    fn = getattr(player, "write_log", None) if player is not None else None
    if callable(fn):
        try:
            fn("REM: " + str(text)[:240])
        except Exception:
            pass


def data_file(name: str) -> Path:
    from memory.config_manager import BASE_DIR
    directory = BASE_DIR / "data" / "plugins"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def read_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except FileNotFoundError:
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def schema(properties: dict, required=()) -> dict:
    return {"type": "OBJECT", "properties": properties, "required": list(required)}


def string(description: str) -> dict:
    return {"type": "STRING", "description": description}


def plugin(name: str, description: str, properties: dict, required=()) -> dict:
    return {"name": name, "description": description,
            "parameters": schema(properties, required)}


def safe_run(fn):
    def run(parameters, player=None, session_memory=None):
        try:
            return str(fn(parameters if isinstance(parameters, dict) else {}, player, session_memory))
        except Exception as exc:
            return f"{fn.__module__.split('.')[-1]} failed safely: {type(exc).__name__}: {exc}"
    run.__name__ = "run"
    return run
