"""Opt-in, reversible Git hooks for the worktree commit and push policy.

Nothing here installs a hook implicitly. ``ozagia hooks install`` and
:func:`install` are the only entry points; ``check`` and ``policy_check`` never
call them. An existing hook without the ozagia marker is preserved unless the
caller explicitly passes ``force``.

The installed shell wrapper runs the deterministic policy through the Python
interpreter that installed ozagia (or an explicit ``--python`` /
``$OZAGIA_HOOK_PYTHON``). It prints nothing on success, prints the redacted
policy report on failure, and exits with the policy's status. Findings never
include a secret value: the worktree CLI redacts them before printing.
"""
from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

SCHEMA = 1
#: The same marker as :data:`ozagia.core.security.HOOK_MARKER`, so old and new
#: installs recognise each other.
MARKER = "# ozagia security gate"
ENV_PYTHON = "OZAGIA_HOOK_PYTHON"
HOOK_NAMES = {"commit": "pre-commit", "push": "pre-push"}
KINDS = tuple(HOOK_NAMES)
_KIND_ALIASES = {
    "commit": "commit", "pre-commit": "commit",
    "push": "push", "pre-push": "push",
}


class HooksError(RuntimeError):
    """The Git hook operation cannot be completed."""


def _normalise_kind(kind: str) -> str:
    try:
        return _KIND_ALIASES[kind]
    except (KeyError, TypeError):
        raise HooksError(
            f"hook kind must be commit or push, got {kind!r}") from None


def _normalise_kinds(kinds: Sequence[str] | None) -> list[str]:
    if kinds is None:
        return list(KINDS)
    if isinstance(kinds, str):
        kinds = [kinds]
    ordered: list[str] = []
    for kind in kinds:
        normal = _normalise_kind(kind)
        if normal not in ordered:
            ordered.append(normal)
    if not ordered:
        raise HooksError("choose at least one hook: commit or push")
    return ordered


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=15, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HooksError(f"cannot run git: {exc}") from exc


def _git_text(root: Path, args: Sequence[str]) -> str:
    proc = _git(root, args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise HooksError(
            f"git {' '.join(args)} failed"
            + (f": {detail}" if detail else ""))
    return proc.stdout.strip()


def hook_path(root: str | Path, kind: str) -> Path:
    """The hook file Git would run for ``kind`` in ``root``.

    Linked worktrees share the common Git directory's hooks, so the path is
    resolved with ``git rev-parse --git-path hooks`` rather than by appending
    ``.git/hooks`` to the worktree root.
    """
    normal = _normalise_kind(kind)
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        raise HooksError(f"repository root does not exist: {root_p}")
    output = _git_text(root_p, ["rev-parse", "--git-path", "hooks"])
    path = Path(output).expanduser()
    if not path.is_absolute():
        path = root_p / path
    return path.resolve() / HOOK_NAMES[normal]


def python_command(value: str | None = None) -> str:
    """The interpreter a hook should run, as an absolute path when possible."""
    candidate = (value or os.environ.get(ENV_PYTHON) or sys.executable).strip()
    if not candidate:
        raise HooksError("the configured Python interpreter is empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise HooksError("the configured Python interpreter contains control "
                         "characters")
    if os.path.isabs(candidate):
        if not os.path.isfile(candidate):
            raise HooksError(
                f"the configured Python interpreter does not exist: {candidate}")
        return candidate
    if os.sep in candidate:
        resolved = Path(candidate).expanduser().resolve()
        if not resolved.is_file():
            raise HooksError(
                f"the configured Python interpreter does not exist: {resolved}")
        return str(resolved)
    found = shutil.which(candidate)
    if not found:
        raise HooksError(
            f"the configured Python interpreter is not on PATH: {candidate}")
    return str(Path(found).resolve())


def script(kind: str, python: str | None = None) -> str:
    """The small POSIX shell wrapper installed as a hook."""
    normal = _normalise_kind(kind)
    interpreter = shlex.quote(python_command(python))
    return (
        "#!/bin/sh\n"
        f"{MARKER}: {normal}\n"
        "# Installed by `ozagia hooks install`; remove with "
        "`ozagia hooks uninstall`.\n"
        "# Runs the deterministic worktree policy; it never installs or "
        "edits anything.\n"
        "set -u\n"
        "status=0\n"
        f"output=$({interpreter} -m ozagia.worktree_cli policy "
        f"--for {normal} 2>&1) || status=$?\n"
        'if [ "$status" -ne 0 ]; then\n'
        '    printf \'%s\\n\' "$output" >&2\n'
        f"    printf 'ozagia: {normal} policy check failed (exit %s)\\n' "
        '"$status" >&2\n'
        '    exit "$status"\n'
        "fi\n"
    )


def _atomic_write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o755)
            os.replace(temp_path, path)
        except BaseException:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise HooksError(f"cannot write hook {path}: {exc}") from exc


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HooksError(f"cannot read hook {path}: {exc}") from exc


def _executable(path: Path) -> bool:
    try:
        return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP
                                           | stat.S_IXOTH))
    except OSError:
        return False


def _entry(kind: str, path: Path, status: str, *, reason: str = "",
           exists: bool | None = None) -> dict[str, Any]:
    item = {
        "kind": kind,
        "hook": HOOK_NAMES[kind],
        "path": str(path),
        "status": status,
    }
    if exists is not None:
        item["exists"] = exists
    if reason:
        item["reason"] = reason
    return item


def install(root: str | Path, kinds: Sequence[str] | None = None, *,
            force: bool = False, python: str | None = None) -> dict[str, Any]:
    """Install the requested hooks. Never called implicitly."""
    root_p = Path(root).expanduser().resolve()
    interpreter = python_command(python)
    entries: list[dict[str, Any]] = []
    changed = False
    skipped = False
    for kind in _normalise_kinds(kinds):
        path = hook_path(root_p, kind)
        wanted = script(kind, interpreter)
        if path.exists():
            existing = _read(path)
            ours = MARKER in existing
            if not ours and not force:
                skipped = True
                entries.append(_entry(
                    kind, path, "skipped",
                    reason=("an existing hook was not installed by ozagia; "
                            "review it and pass --force to replace it")))
                continue
            if ours and existing == wanted:
                if _executable(path):
                    entries.append(_entry(kind, path, "unchanged"))
                    continue
                try:
                    path.chmod(0o755)
                except OSError as exc:
                    raise HooksError(
                        f"cannot make hook executable {path}: {exc}") from exc
                changed = True
                entries.append(_entry(
                    kind, path, "updated",
                    reason="executable bit restored"))
                continue
            status_name = "updated" if ours else "overwritten"
        else:
            status_name = "installed"
        _atomic_write(path, wanted)
        changed = True
        entries.append(_entry(kind, path, status_name))
    installed_kinds = [item["kind"] for item in entries
                       if item["status"] in {"installed", "updated",
                                             "overwritten"}]
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "root": str(root_p),
        "python": interpreter,
        "force": bool(force),
        "ok": not skipped,
        "changed": changed,
        "entries": entries,
        "installed": not skipped,
        "installed_kinds": installed_kinds,
        "skipped": [item["kind"] for item in entries
                    if item["status"] == "skipped"],
    }
    if len(entries) == 1:
        report["kind"] = entries[0]["kind"]
        report["path"] = entries[0]["path"]
    return report


def uninstall(root: str | Path,
              kinds: Sequence[str] | None = None) -> dict[str, Any]:
    """Remove only hooks carrying the ozagia marker."""
    root_p = Path(root).expanduser().resolve()
    entries: list[dict[str, Any]] = []
    removed: list[str] = []
    skipped = False
    for kind in _normalise_kinds(kinds):
        path = hook_path(root_p, kind)
        if not path.exists():
            entries.append(_entry(kind, path, "absent", exists=False))
            continue
        existing = _read(path)
        if MARKER not in existing:
            skipped = True
            entries.append(_entry(
                kind, path, "skipped", exists=True,
                reason="the hook was not installed by ozagia; it was kept"))
            continue
        try:
            path.unlink()
        except OSError as exc:
            raise HooksError(f"cannot remove hook {path}: {exc}") from exc
        removed.append(kind)
        entries.append(_entry(kind, path, "removed", exists=False))
    removed_kinds = list(removed)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "root": str(root_p),
        "ok": not skipped,
        "changed": bool(removed),
        "entries": entries,
        "removed": bool(removed),
        "removed_kinds": removed_kinds,
        "skipped": [item["kind"] for item in entries
                    if item["status"] == "skipped"],
    }
    if len(entries) == 1:
        report["kind"] = entries[0]["kind"]
        report["path"] = entries[0]["path"]
    return report


def status(root: str | Path,
           kinds: Sequence[str] | None = None) -> dict[str, Any]:
    """Report whether each hook exists, is ours, and is executable."""
    root_p = Path(root).expanduser().resolve()
    entries: list[dict[str, Any]] = []
    for kind in _normalise_kinds(kinds):
        path = hook_path(root_p, kind)
        if not path.exists():
            entry = _entry(kind, path, "absent", exists=False)
            entry.update({"ozagia": False, "foreign": False,
                          "installed": False, "executable": False})
            entries.append(entry)
            continue
        existing = _read(path)
        ours = MARKER in existing
        entries.append({
            **_entry(kind, path, "installed" if ours else "foreign",
                     exists=True),
            "ozagia": ours,
            "foreign": not ours,
            "installed": ours,
            "executable": _executable(path),
        })
    installed_kinds = [item["kind"] for item in entries
                       if item.get("installed")]
    foreign = [item["kind"] for item in entries if item.get("foreign")]
    return {
        "schema": SCHEMA,
        "root": str(root_p),
        "entries": entries,
        "installed": bool(installed_kinds),
        "installed_kinds": installed_kinds,
        "foreign": foreign,
        "hooks": {item["kind"]: item for item in entries},
    }


def format_install(report: dict[str, Any]) -> str:
    lines = []
    for item in report.get("entries") or []:
        line = f"{item['status']}: {item['hook']} {item['path']}"
        if item.get("reason"):
            line += f" ({item['reason']})"
        lines.append(line)
    if report.get("changed"):
        lines.append(f"python: {report.get('python')}")
    return "\n".join(lines) or "no hooks changed"


def format_uninstall(report: dict[str, Any]) -> str:
    lines = []
    for item in report.get("entries") or []:
        line = f"{item['status']}: {item['hook']} {item['path']}"
        if item.get("reason"):
            line += f" ({item['reason']})"
        lines.append(line)
    return "\n".join(lines) or "no hooks changed"


def format_status(report: dict[str, Any]) -> str:
    lines = []
    for item in report.get("entries") or []:
        line = f"{item['hook']}: {item['status']} ({item['path']})"
        if item.get("ozagia"):
            line += "  executable" if item.get("executable") else "  not executable"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "ENV_PYTHON",
    "HOOK_NAMES",
    "HooksError",
    "KINDS",
    "MARKER",
    "SCHEMA",
    "format_install",
    "format_status",
    "format_uninstall",
    "hook_path",
    "install",
    "python_command",
    "script",
    "status",
    "uninstall",
]
