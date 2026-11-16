"""Install or remove ozagia's Claude Code hooks in a project's settings.

The hooks go into `<workdir>/.claude/settings.local.json` (personal, not
shared) or, with `settings="project"`, `<workdir>/.claude/settings.json`
(shared, may be committed). Existing keys and other hooks are kept. ozagia's
entries are the command handlers whose command runs this package's `hook`
subcommand; installing again replaces them, so there are never duplicates.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from ozagia.integrations.claude_code.hook import HOOK_TIMEOUT_S

MODULE = "ozagia.integrations.claude_code"
HOOK_SUFFIX = f"-m {MODULE} hook"
SETTINGS_FILES = {"local": "settings.local.json", "project": "settings.json"}
MATCHERS = {
    "PostToolUse": "Read|Grep|Glob|Edit|MultiEdit|Write|Bash",
    "PreToolUse": "Bash|Read|Edit|MultiEdit|Write",
    "SessionStart": "startup|resume|clear|compact",
    "SessionEnd": "",
}


class SettingsError(ValueError):
    """A settings file exists but is not a JSON object with a usable `hooks`."""


def settings_path(workdir: str | Path, settings: str = "local") -> Path:
    if settings not in SETTINGS_FILES:
        raise ValueError(f"settings must be one of {sorted(SETTINGS_FILES)}, not {settings!r}")
    return Path(workdir) / ".claude" / SETTINGS_FILES[settings]


def hook_command(python: str = sys.executable) -> str:
    return f"{shlex.quote(python)} {HOOK_SUFFIX}"


def is_ozagia_handler(handler: Any) -> bool:
    return (isinstance(handler, dict) and handler.get("type") == "command"
            and isinstance(handler.get("command"), str)
            and handler["command"].rstrip().endswith(HOOK_SUFFIX))


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: not a JSON object")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise SettingsError(f"{path}: `hooks` is not an object")
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _strip(data: dict[str, Any]) -> bool:
    """Remove ozagia's handlers from `data` in place. True if any were removed."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [h for h in handlers if not is_ozagia_handler(h)]
            if len(kept) == len(handlers):
                kept_groups.append(group)
                continue
            removed = True
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if removed and not hooks:
        del data["hooks"]
    return removed


def install(workdir: str | Path, *, python: str = sys.executable,
            settings: str = "local") -> Path:
    """Add ozagia's hooks to the project's Claude Code settings. Idempotent."""
    path = settings_path(workdir, settings)
    data = _load(path)
    _strip(data)
    hooks = data.setdefault("hooks", {})
    for event, matcher in MATCHERS.items():
        hooks.setdefault(event, []).append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": hook_command(python),
                       "timeout": HOOK_TIMEOUT_S}],
        })
    _save(path, data)
    return path


def uninstall(workdir: str | Path, *, settings: str | None = None) -> list[Path]:
    """Remove only ozagia's hooks. `settings=None` cleans both files.

    Returns the files that changed.
    """
    names = list(SETTINGS_FILES) if settings is None else [settings]
    changed = []
    for name in names:
        path = settings_path(workdir, name)
        if not path.exists():
            continue
        data = _load(path)
        if _strip(data):
            _save(path, data)
            changed.append(path)
    return changed
