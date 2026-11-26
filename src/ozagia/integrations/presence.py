"""Read-only detection of ozagia's integration configuration files.

Doctor uses this to say whether the plugin, hooks and MCP entries ozagia would
write are present.  It never writes, creates a directory or runs an installer.
Presence is not evidence that the host loads or trusts the integration: the
returned note says so, and the host rows stay separate.
"""
from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

#: Do not read an arbitrarily large settings file.
MAX_CONFIG_BYTES = 1_000_000
#: Bound the paths named in one section.
MAX_PATHS = 12

NOTE = (
    "Configuration presence only; it does not show that the host loads, "
    "trusts or runs the integration."
)


def _path_state(path: Path) -> dict[str, Any]:
    try:
        present = path.is_file()
    except OSError as exc:
        return {"path": str(path), "state": "unknown", "error": str(exc)}
    return {
        "path": str(path),
        "state": "present" if present else "absent",
        "error": None,
    }


def _text(path: Path) -> tuple[str | None, str | None]:
    """Return ``(text, error)``; ``text is None`` and no error means absent."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{path}: {exc}"
    if not stat.S_ISREG(info.st_mode):
        return None, f"{path}: not a regular file"
    if info.st_size > MAX_CONFIG_BYTES:
        return None, f"{path}: larger than {MAX_CONFIG_BYTES} bytes"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path}: {exc}"


def _json_text(path: Path) -> tuple[Any | None, str | None, str | None]:
    """Return ``(data, state, error)`` for a JSON config file."""
    text, error = _text(path)
    if text is None:
        if error is not None:
            return None, "unknown", error
        return None, "absent", None
    if not text.strip():
        return None, "absent", None
    try:
        return json.loads(text), "present", None
    except (json.JSONDecodeError, TypeError) as exc:
        return None, "unknown", f"{path}: not valid JSON ({exc})"


def _merge(parts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    states = {item.get("state") for item in parts.values()}
    if "present" in states:
        state = "present"
    elif "unknown" in states:
        state = "unknown"
    else:
        state = "absent"
    found: list[str] = []
    errors: list[str] = []
    for item in parts.values():
        if item.get("state") == "present":
            path = item.get("path")
            if path:
                found.append(str(path))
            found.extend(str(value) for value in item.get("found") or [])
        error = item.get("error")
        if error:
            errors.append(str(error))
        errors.extend(str(value) for value in item.get("errors") or [])
    found = list(dict.fromkeys(found))[:MAX_PATHS]
    errors = list(dict.fromkeys(errors))[:MAX_PATHS]
    return {
        "state": state,
        "found": found,
        "errors": errors,
        "parts": parts,
    }

def _command_handlers(data: Any, suffix: str) -> bool:
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                command = (handler.get("command")
                           if isinstance(handler, dict) else None)
                if (isinstance(command, str)
                        and command.rstrip().endswith(suffix)):
                    return True
    return False


def _opencode_state(root: Path) -> dict[str, Any]:
    from ozagia.integrations.opencode import install as install_mod

    parts = {
        "server_plugin": _path_state(root / install_mod.PLUGIN_REL),
        "tui_brand": _path_state(root / install_mod.TUI_BRAND_REL),
        "tui_profile": _path_state(root / install_mod.TUI_PROFILE_REL),
    }
    return _merge(parts)


def _claude_state(root: Path) -> dict[str, Any]:
    from ozagia.integrations.claude_code import install as install_mod

    parts: dict[str, dict[str, Any]] = {}
    for name in ("local", "project"):
        path = install_mod.settings_path(root, name)
        data, state, error = _json_text(path)
        if state == "present" and not _command_handlers(
                data, install_mod.HOOK_SUFFIX):
            state = "absent"
        parts[name] = {
            "path": str(path), "state": state, "error": error,
        }
    return _merge(parts)


def _codex_state(root: Path, environ: dict[str, str]) -> dict[str, Any]:
    from ozagia.integrations.codex import install as install_mod

    home_dir = Path(environ.get("HOME") or Path.home())
    home = Path(environ.get("CODEX_HOME") or (home_dir / ".codex"))
    scopes: dict[str, Any] = {}
    for scope in ("project", "user"):
        paths = install_mod.paths(root, scope, home=home)
        parts: dict[str, dict[str, Any]] = {}

        hooks, state, error = _json_text(paths["hooks"])
        if state == "present" and not _command_handlers(
                hooks, install_mod.HOOK_SUFFIX):
            state = "absent"
        parts["hooks"] = {
            "path": str(paths["hooks"]), "state": state, "error": error}

        text, error = _text(paths["config"])
        if text is None:
            state = "unknown" if error else "absent"
        else:
            try:
                parsed = tomllib.loads(text)
            except (tomllib.TOMLDecodeError, ValueError) as exc:
                state = "unknown"
                error = f"{paths['config']}: not valid TOML ({exc})"
            else:
                servers = parsed.get("mcp_servers")
                server = (servers.get(install_mod.MCP_NAME)
                          if isinstance(servers, dict) else None)
                state = "present" if isinstance(server, dict) else "absent"
        parts["mcp"] = {
            "path": str(paths["config"]), "state": state, "error": error}

        text, error = _text(paths["agents"])
        if text is None:
            state = "unknown" if error else "absent"
        else:
            state = ("present"
                     if install_mod.BEGIN in text and install_mod.END in text
                     else "absent")
        parts["agents"] = {
            "path": str(paths["agents"]), "state": state, "error": error}
        scopes[scope] = _merge(parts)
    merged = _merge(scopes)
    return {
        "state": merged["state"],
        "scopes": scopes,
        "found": merged["found"],
        "errors": merged["errors"],
    }


def _kiro_hook_file(data: Any, suffix: str) -> bool:
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        action = hook.get("action") if isinstance(hook, dict) else None
        command = action.get("command") if isinstance(action, dict) else None
        if isinstance(command, str) and command.rstrip().endswith(suffix):
            return True
    return False


def _kiro_profile(data: Any, suffix: str) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if isinstance(hooks, list):
        return _kiro_hook_file({"hooks": hooks}, suffix)
    if isinstance(hooks, dict):
        for group in hooks.values():
            if isinstance(group, list) and _kiro_hook_file(
                    {"hooks": group}, suffix):
                return True
            if isinstance(group, dict) and _kiro_hook_file(
                    {"hooks": [group]}, suffix):
                return True
    return False


def _kiro_state(root: Path, environ: dict[str, str]) -> dict[str, Any]:
    from ozagia.integrations.kiro import install as install_mod

    home_dir = Path(environ.get("HOME") or Path.home())
    home = home_dir / ".kiro"
    scopes: dict[str, Any] = {}
    for scope in ("project", "user"):
        paths = install_mod.paths(root, scope, home=home)
        parts: dict[str, dict[str, Any]] = {}

        data, state, error = _json_text(paths["hooks"])
        if state == "present" and not _kiro_hook_file(
                data, install_mod.HOOK_SUFFIX):
            state = "absent"
        parts["hooks"] = {
            "path": str(paths["hooks"]), "state": state, "error": error}

        data, state, error = _json_text(paths["agent"])
        if state == "present" and not _kiro_profile(
                data, install_mod.HOOK_SUFFIX):
            state = "absent"
        parts["agent"] = {
            "path": str(paths["agent"]), "state": state, "error": error}

        data, state, error = _json_text(paths["mcp"])
        if state == "present":
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            state = ("present"
                     if isinstance(servers, dict)
                     and install_mod.MCP_NAME in servers
                     else "absent")
        parts["mcp"] = {
            "path": str(paths["mcp"]), "state": state, "error": error}
        scopes[scope] = _merge(parts)
    merged = _merge(scopes)
    return {
        "state": merged["state"],
        "scopes": scopes,
        "found": merged["found"],
        "errors": merged["errors"],
    }


def config_presence(
    root: str | Path, *, environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Report the integration config ozagia would write, without writing."""
    root_path = Path(root)
    env = dict(os.environ if environ is None else environ)
    return {
        "note": NOTE,
        "opencode": _opencode_state(root_path),
        "claude": _claude_state(root_path),
        "codex": _codex_state(root_path, env),
        "kiro": _kiro_state(root_path, env),
    }


__all__ = ["MAX_CONFIG_BYTES", "NOTE", "config_presence"]
