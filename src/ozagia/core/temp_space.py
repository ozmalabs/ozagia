"""Managed temporary space: provenance, usage accounting and safe cleanup.

Models and tools often use ``/tmp`` as scratch space.  It is frequently a
RAM-backed ``tmpfs``, it is not durable, and nothing records which process
created a path, why, for which session, or how much space it uses.  This module
keeps a small registry of managed temporary entries so a caller can create,
adopt, inspect, release and clean them deliberately.

Layout
------

Each entry is a directory plus two records:

``.<marker>`` (``.ozagia-tmp.json``)
    A 0600 JSON marker inside the directory.  It holds only identity and
    provenance (version, id, kind, session, reason, root, created_at, pid,
    owned).  It proves ownership after a restart.

``<state dir>/tmp/registry/<id>.json``
    The authoritative record.  One file per entry means concurrent creates do
    not need a global lock.  Registry directories are 0700 and files 0600.

Both are written atomically (same-directory ``mkstemp``, ``fsync``,
``os.replace``).  Neither contains file contents or environment/secret
material; ``reason`` is a short caller-supplied label and callers must not put
prompts, source text or secrets in it.

Roots
-----

``<state dir>/tmp/workspaces/`` holds ``scratch``, ``check``, ``eval``,
``engine`` and ``other`` entries.  ``<state dir>/tmp/agent/`` is reserved for
agent run roots (wired by a later slice).  ``adopt`` may additionally track
existing paths under the resolved ``TMPDIR``, ``/tmp`` and ``/var/tmp``.

Safety
------

Paths are resolved with :meth:`Path.resolve` (``strict=False``) and must stay
inside an allowed root.  Cleanup only considers ``owned`` entries whose marker
id matches, refuses symlinks, filesystem roots, the repository root, the home
directory and any allowed root itself, and never follows a symlink out of an
entry.  Deletion is a dry-run plan unless ``apply=True``.

The module uses the standard library only.  It does not import the target
project or execute target code.  The read-only views do not open sockets or read
file contents.  Legacy cleanup may open a ``.lock`` file only to test ``flock``
and may attempt a short Unix-socket connect to tell a live daemon from a stale
socket; it never sends or reads application data.
"""

from __future__ import annotations

import errno
import hashlib
import json
import fnmatch
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.config import state_dir, state_path

VERSION = 1
MARKER_NAME = ".ozagia-tmp.json"
REGISTRY_DIRNAME = "registry"
TMP_DIRNAME = "tmp"
WORKSPACES_DIRNAME = "workspaces"
AGENT_DIRNAME = "agent"

KINDS = ("scratch", "agent", "check", "eval", "engine", "other")
STATES = ("active", "released", "expired", "missing")
DEFAULT_KIND = "other"
DEFAULT_SCRATCH_KIND = "scratch"

MIN_TTL_S = 60
MAX_TTL_S = 90 * 24 * 60 * 60
MAX_REASON = 200

MAX_WALK_ENTRIES = 200_000
MAX_WALK_SECONDS = 5.0
MAX_WALK_DEPTH = 64

AUDIT_MAX_ARTIFACTS = 5_000
AUDIT_MAX_SECONDS = 5.0
AUDIT_PATTERNS = (
    "ozagia_agent/*",
    "ozagia-eval-*.sock.lock",
    "ozagia-eval-*.sock",
    "oz-idle-*.sock.lock",
    "oz-jobidle-*.sock.lock",
    "ozagia_check_*",
    "ozagia-native-*",
    "ozagia_eval/*",
    "ozagia_index_bench/*",
    "ozagia_iac_bench/*",
)
_AUDIT_NESTED_PARENTS = {
    "ozagia_agent": "*",
    "ozagia_eval": "*",
    "ozagia_index_bench": "*",
    "ozagia_iac_bench": "*",
}
_AUDIT_ROOT_PATTERNS = tuple(
    pattern for pattern in AUDIT_PATTERNS if "/" not in pattern)

LEGACY_DEFAULT_OLDER_THAN_S = 24 * 60 * 60
LEGACY_SOCKET_TIMEOUT_S = 0.25
LEGACY_FILE_PATTERNS = {
    "ozagia-eval-*.sock.lock": "lock",
    "ozagia-eval-*.sock": "socket",
    "oz-idle-*.sock.lock": "lock",
    "oz-jobidle-*.sock.lock": "lock",
}
LEGACY_DIR_PATTERNS = (
    "ozagia_agent/*",
    "ozagia_eval/*",
    "ozagia_check_*",
    "ozagia-native-*",
)
LEGACY_CACHE_PATTERNS = (
    "ozagia_index_bench/*",
    "ozagia_iac_bench/*",
)

_ID_RE = re.compile(r"^tmp_[0-9a-f]{12}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TempSpaceError(Exception):
    """A temporary-space request is invalid."""


class TempPathError(TempSpaceError):
    """A path is outside the allowed roots or is not safe to touch."""


class TempNotFound(TempSpaceError):
    """No registry entry has the requested id."""


class TempMarkerError(TempPathError):
    """A marker exists but does not identify this entry."""


# --------------------------------------------------------------------------- #
# roots and locations
# --------------------------------------------------------------------------- #


def registry_dir(root: str | Path) -> Path:
    """The registry directory for ``root``."""
    return state_path(root, TMP_DIRNAME, REGISTRY_DIRNAME)


def workspaces_root(root: str | Path) -> Path:
    """The root for non-agent managed entries."""
    return state_path(root, TMP_DIRNAME, WORKSPACES_DIRNAME)


def agent_root(root: str | Path) -> Path:
    """The root reserved for agent run roots."""
    return state_path(root, TMP_DIRNAME, AGENT_DIRNAME)


def default_kind_root(root: str | Path, kind: str = DEFAULT_SCRATCH_KIND) -> Path:
    """The default parent directory for one kind of managed entry."""
    kind = _validate_kind(kind)
    if kind == "agent":
        return agent_root(root)
    return workspaces_root(root) / kind


def default_scratch_root(root: str | Path) -> Path:
    """The default scratch root callers should use instead of ``/tmp``."""
    return default_kind_root(root, DEFAULT_SCRATCH_KIND)


def ensure_default_root(root: str | Path, kind: str = DEFAULT_SCRATCH_KIND) -> Path:
    """Create and return the default root for ``kind`` (mode 0700)."""
    target = default_kind_root(root, kind)
    _ensure_dir(target)
    return target


def allowed_temp_roots(root: str | Path) -> tuple[Path, ...]:
    """Every root under which a temporary path may be managed or adopted."""
    candidates = [workspaces_root(root), agent_root(root), Path("/tmp"),
                  Path("/var/tmp")]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidates.append(Path(tmpdir).expanduser())
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = _absolute(candidate)
        if str(resolved) not in seen:
            result.append(resolved)
            seen.add(str(resolved))
    return tuple(result)


def suggest_path(root: str | Path, kind: str, reason: str,
                 session: Mapping[str, Any] | None = None) -> Path:
    """A non-created path suggestion for a caller that needs one up front."""
    kind = _validate_kind(kind)
    reason = _validate_reason(reason)
    label = _SLUG_RE.sub("-", reason.lower()).strip("-")[:24] or kind
    material = [kind, reason, str(session.get("harness") if session else ""),
                str(session.get("id") if session else "")]
    digest = _short_hash("\x00".join(material))
    base = default_kind_root(root, kind)
    candidate = base / f"{label}-{digest}"
    for suffix in range(1, 100):
        if not os.path.lexists(candidate):
            return candidate
        candidate = base / f"{label}-{digest}-{suffix}"
    raise TempSpaceError("could not suggest an unused temporary path")


# --------------------------------------------------------------------------- #
# records and measurements
# --------------------------------------------------------------------------- #


def make_session(harness: str | None, session_id: str | None) -> dict[str, str] | None:
    """Build a ``{harness, id}`` session, or ``None`` when no id is known.

    A harness name alone is not a session: the id is never invented.
    """
    if not session_id:
        return None
    clean_id = _clean_token(session_id, "session id")
    clean_harness = _clean_token(harness, "harness") if harness else "unknown"
    return {"harness": clean_harness, "id": clean_id}


def walk_usage(path: str | Path, *, max_entries: int = MAX_WALK_ENTRIES,
               max_seconds: float = MAX_WALK_SECONDS,
               max_depth: int = MAX_WALK_DEPTH) -> dict[str, Any]:
    """Bounded usage walk of a directory tree.

    Directory symlinks are never followed.  ``st_size`` is summed for regular
    files and files are counted; file contents are never read.  A bound that
    stops the walk, or an unreadable entry, marks the result ``partial`` and
    ``unknown`` rather than silently reporting zero.
    """
    path = Path(path)
    if max_entries < 0 or max_seconds < 0 or max_depth < 0:
        raise TempSpaceError("walk bounds must not be negative")
    result = {"bytes": 0, "files": 0, "walk": "complete", "unknown": False,
              "errors": 0}
    try:
        root_stat = os.lstat(path)
    except FileNotFoundError:
        return result
    except OSError:
        return {**result, "walk": "partial", "unknown": True, "errors": 1}
    if not stat.S_ISDIR(root_stat.st_mode):
        if stat.S_ISREG(root_stat.st_mode):
            result["bytes"] = root_stat.st_size
            result["files"] = 1
        return result

    deadline = time.monotonic() + max_seconds
    entries = 0
    stack: list[tuple[Path, int]] = [(path, 0)]
    while stack:
        if time.monotonic() > deadline:
            result["walk"] = "partial"
            break
        directory, depth = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError:
            result["errors"] += 1
            result["unknown"] = True
            result["walk"] = "partial"
            continue
        with iterator:
            stopped = False
            for entry in iterator:
                if entries >= max_entries:
                    result["walk"] = "partial"
                    stopped = True
                    break
                entries += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    result["errors"] += 1
                    result["unknown"] = True
                    result["walk"] = "partial"
                    continue
                if stat.S_ISREG(info.st_mode):
                    result["bytes"] += info.st_size
                    result["files"] += 1
                elif stat.S_ISDIR(info.st_mode):
                    if depth + 1 > max_depth:
                        result["walk"] = "partial"
                    else:
                        stack.append((Path(entry.path), depth + 1))
                # Symlinks and special files are counted as visited but never
                # followed and never added to the byte/file totals.
            if stopped:
                break
    return result


def tmpfs_for(path: str | Path, *, mountinfo: str | None = None) -> bool | None:
    """Whether ``path`` is on a tmpfs mount, or ``None`` when unknown.

    Detection reads ``/proc/self/mountinfo`` (falling back to ``/proc/mounts``)
    on Linux.  There is no guessing: a non-Linux host or unreadable mount table
    returns ``None``.
    """
    if not sys.platform.startswith("linux"):
        return None
    tables: list[str] = []
    if mountinfo is not None:
        tables.append(("mountinfo", mountinfo))
    else:
        text = _read_mountinfo()
        if text is not None:
            tables.append(("mountinfo", text))
        text = _read_mounts()
        if text is not None:
            tables.append(("mounts", text))
    if not tables:
        return None
    wanted = _try_resolve(path)
    if wanted is None:
        return None
    for style, text in tables:
        mounts = _parse_mounts(text, style)
        if not mounts:
            continue
        for mount, fstype in _mounts_longest_first(mounts):
            if _on_mount(wanted, mount):
                return fstype == "tmpfs"
    return None


# --------------------------------------------------------------------------- #
# create / adopt
# --------------------------------------------------------------------------- #


def create(root: str | Path, *, kind: str = DEFAULT_SCRATCH_KIND, reason: str,
           session: Mapping[str, Any] | str | None = None,
           ttl_s: int | None = None, path: str | Path | None = None,
           pid: int | None = None, model: str | None = None,
           run_id: str | None = None) -> dict[str, Any]:
    """Create one managed temporary directory and register it."""
    root_path = Path(root).resolve()
    kind = _validate_kind(kind)
    reason = _validate_reason(reason)
    session_value = _normalise_session(session)
    ttl_s = _validate_ttl(ttl_s)
    model = _validate_optional_label(model, "model")
    run_id = _validate_optional_label(run_id, "run_id")
    if pid is None:
        pid = os.getpid()
    if not isinstance(pid, int) or pid < 0:
        raise TempSpaceError(f"pid must be a non-negative integer, got {pid!r}")

    _ensure_dir(registry_dir(root_path))
    # A default target is created here, so it is ours to remove if the record
    # write fails; a caller-supplied path may already exist and is left alone.
    created_target = False
    if path is None:
        target, entry_id = _create_default_target(root_path, kind)
        created_target = True
    else:
        raw = _lexical_path(path)
        _refuse_symlink(raw)
        target = _try_resolve(raw)
        if target is None:
            raise TempPathError(
                f"cannot resolve path (symlink loop?): {raw}")
        entry_id = _new_id(root_path)
        _check_allowed(root_path, target)
        _check_repo_guard(root_path, target)
        if _is_protected(root_path, target, allowed_temp_roots(root_path)):
            raise TempPathError(
                f"refusing to create at a protected path: {target}")
        _assert_creatable(target)

    now = int(time.time())
    record = {
        "version": VERSION,
        "id": entry_id,
        "path": str(target),
        "kind": kind,
        "session": session_value,
        "reason": reason,
        "model": model,
        "run_id": run_id,
        "root": str(root_path),
        "created_at": now,
        "last_used_at": now,
        "ttl_s": ttl_s,
        "expires_at": now + ttl_s if ttl_s is not None else None,
        "pid": pid,
        "owned": True,
        "state": "active",
    }
    marker = {
        "version": VERSION,
        "id": entry_id,
        "kind": kind,
        "session": session_value,
        "reason": reason,
        "root": str(root_path),
        "created_at": now,
        "pid": pid,
        "owned": True,
    }

    try:
        preexisting = os.path.lexists(target)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        _write_json(target / MARKER_NAME, marker)
        usage = walk_usage(target)
        record.update({"bytes": usage["bytes"], "files": usage["files"],
                       "walk": usage["walk"], "tmpfs": tmpfs_for(target)})
        _write_json(_record_path(root_path, entry_id), record)
    except BaseException:
        _remove_marker_and_empty_dir(target, entry_id,
                                     remove_dir=created_target or not preexisting)
        raise
    return _entry(root_path, record)


def adopt(root: str | Path, path: str | Path, *, reason: str,
          session: Mapping[str, Any] | str | None = None, kind: str = DEFAULT_KIND,
          ttl_s: int | None = None, owned: bool = False,
          mark: bool = False, model: str | None = None,
          run_id: str | None = None) -> dict[str, Any]:
    """Track an existing temporary path.

    ``owned`` is false by default: the entry is reported and sized but cleanup
    never deletes it.  A marker is only written when both ``owned`` and ``mark``
    are true, and never over an existing marker with another id.
    """
    root_path = Path(root).resolve()
    kind = _validate_kind(kind)
    reason = _validate_reason(reason)
    session_value = _normalise_session(session)
    ttl_s = _validate_ttl(ttl_s)
    model = _validate_optional_label(model, "model")
    run_id = _validate_optional_label(run_id, "run_id")
    raw = _lexical_path(path)
    _refuse_symlink(raw)
    target = _try_resolve(raw)
    if target is None:
        raise TempPathError(f"cannot resolve path (symlink loop?): {raw}")
    _check_allowed(root_path, target)
    _check_repo_guard(root_path, target)
    try:
        info = os.lstat(raw)
    except FileNotFoundError as exc:
        raise TempPathError(f"path does not exist: {target}") from exc
    except OSError as exc:
        raise TempPathError(f"cannot inspect {target}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TempPathError(f"refusing to adopt a symlink: {target}")
    if not stat.S_ISDIR(info.st_mode):
        raise TempPathError(f"refusing to adopt a non-directory: {target}")
    if mark and not owned:
        raise TempSpaceError("mark needs owned=True")
    if mark and _is_protected(root_path, target,
                              allowed_temp_roots(root_path)):
        raise TempPathError(
            f"refusing to mark a protected path: {target}")

    _ensure_dir(registry_dir(root_path))
    for record in _load_records(root_path):
        if record.get("path") == str(target) and record.get("state") == "active":
            raise TempSpaceError(
                f"path is already tracked as {record.get('id')}: {target}")

    entry_id = _new_id(root_path)
    if mark:
        marker_path = target / MARKER_NAME
        if os.path.lexists(marker_path):
            existing = _read_marker(target)
            if existing is None or existing.get("id") != entry_id:
                raise TempMarkerError(
                    f"refusing to overwrite an existing marker in {target}")

    now = int(time.time())
    record = {
        "version": VERSION,
        "id": entry_id,
        "path": str(target),
        "kind": kind,
        "session": session_value,
        "reason": reason,
        "model": model,
        "run_id": run_id,
        "root": str(root_path),
        "created_at": now,
        "last_used_at": now,
        "ttl_s": ttl_s,
        "expires_at": now + ttl_s if ttl_s is not None else None,
        "pid": os.getpid(),
        "owned": bool(owned),
        "state": "active",
    }
    if mark:
        marker = {
            "version": VERSION,
            "id": entry_id,
            "kind": kind,
            "session": session_value,
            "reason": reason,
            "root": str(root_path),
            "created_at": now,
            "pid": os.getpid(),
            "owned": True,
        }
        _write_json(target / MARKER_NAME, marker)
    usage = walk_usage(target)
    record.update({"bytes": usage["bytes"], "files": usage["files"],
                   "walk": usage["walk"], "tmpfs": tmpfs_for(target)})
    try:
        _write_json(_record_path(root_path, entry_id), record)
    except BaseException:
        if mark:
            try:
                (target / MARKER_NAME).unlink()
            except OSError:
                pass
        raise
    return _entry(root_path, record)


# --------------------------------------------------------------------------- #
# read views
# --------------------------------------------------------------------------- #


def get(root: str | Path, entry_id: str) -> dict[str, Any]:
    """One entry, with its current state and usage."""
    root_path = Path(root).resolve()
    record = _read_record(root_path, _validate_id(entry_id))
    return _entry(root_path, record)


def touch(root: str | Path, entry_id: str) -> dict[str, Any]:
    """Move ``last_used_at`` to now without changing state or usage."""
    root_path = Path(root).resolve()
    record = _read_record(root_path, _validate_id(entry_id))
    record["last_used_at"] = int(time.time())
    _write_json(_record_path(root_path, entry_id), record)
    return _entry(root_path, record)


def list_entries(root: str | Path, *, kind: str | None = None,
                 session: str | None = None, owned: bool | None = None,
                 include_all: bool = False) -> list[dict[str, Any]]:
    """Entries, newest first is not assumed: sorted by ``created_at`` then id."""
    root_path = Path(root).resolve()
    if kind is not None:
        kind = _validate_kind(kind)
    result: list[dict[str, Any]] = []
    errors: list[str] = []
    for record in _load_records(root_path, errors=errors):
        entry = _entry(root_path, record)
        if not include_all and entry["state"] not in ("active", "missing"):
            continue
        if kind is not None and entry["kind"] != kind:
            continue
        if owned is not None and bool(entry["owned"]) is not owned:
            continue
        if session is not None and not _session_matches(entry, session):
            continue
        result.append(entry)
    result.sort(key=lambda item: (item["created_at"], item["id"]))
    return result


def status(root: str | Path) -> dict[str, Any]:
    """Totals by kind/session/ownership plus capacity for each filesystem."""
    root_path = Path(root).resolve()
    errors: list[str] = []
    entries = [_entry(root_path, record)
               for record in _load_records(root_path, errors=errors)]
    by_kind: dict[str, dict[str, int]] = {}
    by_session: dict[str, dict[str, int]] = {}
    by_ownership = {
        "owned": {"entries": 0, "bytes": 0, "files": 0},
        "unowned": {"entries": 0, "bytes": 0, "files": 0},
    }
    totals = {"entries": len(entries), "bytes": 0, "files": 0,
              "partial": 0, "unknown": 0}
    filesystems: dict[int, dict[str, Any]] = {}
    for entry in entries:
        size = entry["bytes"] if isinstance(entry["bytes"], int) else 0
        count = entry["files"] if isinstance(entry["files"], int) else 0
        totals["bytes"] += size
        totals["files"] += count
        if entry["walk"] == "partial":
            totals["partial"] += 1
        if entry.get("unknown"):
            totals["unknown"] += 1
        bucket = by_kind.setdefault(
            entry["kind"], {"entries": 0, "bytes": 0, "files": 0,
                            "owned": 0, "unowned": 0})
        bucket["entries"] += 1
        bucket["bytes"] += size
        bucket["files"] += count
        bucket["owned" if entry["owned"] else "unowned"] += 1
        label = _session_label(entry)
        session_bucket = by_session.setdefault(
            label, {"entries": 0, "bytes": 0, "files": 0, "harness": None})
        session_bucket["entries"] += 1
        session_bucket["bytes"] += size
        session_bucket["files"] += count
        if entry["session"] is not None:
            session_bucket["harness"] = entry["session"].get("harness")
        ownership = by_ownership["owned" if entry["owned"] else "unowned"]
        ownership["entries"] += 1
        ownership["bytes"] += size
        ownership["files"] += count
        if entry["state"] == "missing":
            continue
        path = Path(entry["path"])
        try:
            device = os.stat(path).st_dev
            usage = os.statvfs(path)
        except OSError:
            continue
        if device not in filesystems:
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
            used = max(0, total - free)
            percent = round(used * 100.0 / total, 1) if total else None
            is_tmpfs = entry.get("tmpfs")
            warning = None
            if total and free * 10 < total:
                warning = f"free space below 10% ({free} of {total} bytes)"
            elif is_tmpfs and percent is not None and percent > 80.0:
                warning = f"tmpfs above 80% used ({percent}%)"
            filesystems[device] = {
                "device": device,
                "path": str(path),
                "total_bytes": total,
                "free_bytes": free,
                "used_bytes": used,
                "percent": percent,
                "tmpfs": is_tmpfs,
                "warning": warning,
            }
    warnings = [fs["warning"] for fs in filesystems.values() if fs["warning"]]
    return {
        "root": str(root_path),
        "registry": str(registry_dir(root_path)),
        "totals": totals,
        "by_kind": {key: by_kind[key] for key in sorted(by_kind)},
        "by_session": {key: by_session[key] for key in sorted(by_session)},
        "by_ownership": by_ownership,
        "filesystems": [filesystems[key] for key in sorted(filesystems)],
        "warnings": sorted(warnings),
        "registry_errors": errors,
        "limits": {"max_entries": MAX_WALK_ENTRIES,
                   "max_seconds": MAX_WALK_SECONDS,
                   "max_depth": MAX_WALK_DEPTH},
    }


# --------------------------------------------------------------------------- #
# audit: untracked legacy artifacts (read-only)
# --------------------------------------------------------------------------- #


def audit(root: str | Path, *, roots: Iterable[str | Path] | None = None,
          max_artifacts: int = AUDIT_MAX_ARTIFACTS,
          max_seconds: float = AUDIT_MAX_SECONDS,
          now: int | None = None) -> dict[str, Any]:
    """Scan allowed temp roots for conservative legacy ozagia artifacts.

    This is read-only: it never creates, rewrites or deletes an entry.  It
    matches only the built-in name patterns, never follows symlinks and skips
    ``/tmp/opencode`` and ``~/.local/share/opencode``.  Each artifact records
    whether its exact path is registered, or whether a registered ancestor
    covers it, so a leak is surfaced without claiming ownership.
    """
    root_path = Path(root).resolve()
    if max_artifacts < 0 or max_seconds < 0:
        raise TempSpaceError("audit bounds must not be negative")
    current = int(time.time()) if now is None else int(now)
    scan_roots = _audit_scan_roots(
        allowed_temp_roots(root_path) if roots is None else roots)
    registry = {str(record.get("path") or ""): record
                for record in _load_records(root_path)}
    registry.pop("", None)
    deadline = time.monotonic() + max_seconds
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    warnings: list[str] = []
    truncated = False

    def add(path: Path, pattern: str) -> None:
        nonlocal truncated
        if len(artifacts) >= max_artifacts:
            truncated = True
            return
        rendered = str(path)
        if rendered in seen or _audit_excluded(path):
            return
        try:
            info = os.lstat(path)
        except OSError as exc:
            warnings.append(f"cannot inspect {path}: {exc.strerror or exc}")
            return
        seen.add(rendered)
        artifacts.append(_audit_artifact(path, pattern, info, registry,
                                         current, deadline))

    for scan_root in scan_roots:
        if _audit_excluded(scan_root):
            continue
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            info = os.lstat(scan_root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(
                f"cannot inspect scan root {scan_root}: {exc.strerror or exc}")
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        try:
            iterator = os.scandir(scan_root)
        except OSError as exc:
            warnings.append(
                f"cannot scan {scan_root}: {exc.strerror or exc}")
            continue
        with iterator:
            for entry in iterator:
                if time.monotonic() > deadline or len(artifacts) >= max_artifacts:
                    truncated = True
                    break
                for pattern in _AUDIT_ROOT_PATTERNS:
                    if fnmatch.fnmatchcase(entry.name, pattern):
                        add(Path(entry.path), pattern)
                child_pattern = _AUDIT_NESTED_PARENTS.get(entry.name)
                if child_pattern is None:
                    continue
                if not _is_dir_no_follow(entry):
                    continue
                try:
                    children = os.scandir(entry.path)
                except OSError as exc:
                    warnings.append(
                        f"cannot scan {entry.path}: {exc.strerror or exc}")
                    continue
                with children:
                    for child in children:
                        if (time.monotonic() > deadline
                                or len(artifacts) >= max_artifacts):
                            truncated = True
                            break
                        if fnmatch.fnmatchcase(child.name, child_pattern):
                            add(Path(child.path), f"{entry.name}/*")

    def _age(item: dict[str, Any]) -> int:
        return int(item["age_s"])

    oldest = max(artifacts, key=lambda item: (_age(item), item["path"])) \
        if artifacts else None
    newest = min(artifacts, key=lambda item: (_age(item), item["path"])) \
        if artifacts else None
    totals = {
        "artifacts": len(artifacts),
        "registered": sum(1 for item in artifacts if item["registered"]),
        "covered": sum(1 for item in artifacts
                       if not item["registered"]
                       and item["registered_ancestor"]),
        "untracked": sum(1 for item in artifacts
                         if not item["registered"]
                         and not item["registered_ancestor"]),
        "bytes": sum(item["bytes"] for item in artifacts),
        "files": sum(item["files"] for item in artifacts),
        "partial": sum(1 for item in artifacts if item["walk"] == "partial"),
        "unknown": sum(1 for item in artifacts if item["unknown"]),
    }
    return {
        "root": str(root_path),
        "scan_roots": [str(path) for path in scan_roots],
        "patterns": list(AUDIT_PATTERNS),
        "excluded_roots": [str(path) for path in _audit_excluded_roots()],
        "count": len(artifacts),
        "bytes": totals["bytes"],
        "files": totals["files"],
        "totals": totals,
        "age": {
            "oldest_s": oldest["age_s"] if oldest else None,
            "oldest_path": oldest["path"] if oldest else None,
            "newest_s": newest["age_s"] if newest else None,
            "newest_path": newest["path"] if newest else None,
        },
        "artifacts": artifacts,
        "warnings": sorted(warnings),
        "truncated": truncated,
        "limits": {"max_artifacts": max_artifacts,
                   "max_seconds": max_seconds},
    }


# --------------------------------------------------------------------------- #
# release / clean / legacy cleanup / prune
# --------------------------------------------------------------------------- #


def release(root: str | Path, entry_id: str, *, delete: bool = False) -> dict[str, Any]:
    """Mark an entry released, optionally deleting an owned marked tree."""
    root_path = Path(root).resolve()
    record = _read_record(root_path, _validate_id(entry_id))
    entry = _entry(root_path, record)
    if delete:
        if not entry["owned"]:
            raise TempPathError(
                f"refusing to delete unowned entry {entry_id}")
        safe, why = _delete_guard(root_path, entry)
        if not safe:
            if why == "missing":
                record["state"] = "missing"
                record["last_used_at"] = int(time.time())
                _write_json(_record_path(root_path, entry_id), record)
                return _entry(root_path, record)
            raise TempPathError(f"refusing to delete {entry['path']}: {why}")
        _delete_tree(_guarded_delete_path(entry))
        record.update({"state": "released", "bytes": 0, "files": 0,
                       "walk": "complete", "tmpfs": None,
                       "last_used_at": int(time.time())})
    else:
        record["state"] = "released"
        record["last_used_at"] = int(time.time())
    _write_json(_record_path(root_path, entry_id), record)
    return _entry(root_path, record)


def clean(root: str | Path, *, dry_run: bool = True, expired: bool = False,
          older_than_s: int | float | None = None,
          max_total_bytes: int | None = None,
          ids: Iterable[str] | None = None,
          apply: bool = False) -> dict[str, Any]:
    """Plan (and with ``apply=True`` perform) safe removal of managed entries.

    Deletion is always a dry run unless ``apply`` is true.  Only ``owned``
    entries with a matching marker are eligible; unowned paths and every
    protected path are reported as kept with a reason.
    """
    root_path = Path(root).resolve()
    if older_than_s is not None and older_than_s < 0:
        raise TempSpaceError("older_than_s must not be negative")
    if max_total_bytes is not None and max_total_bytes < 0:
        raise TempSpaceError("max_total_bytes must not be negative")
    now = int(time.time())
    requested: list[str] | None = None
    if ids is not None:
        requested = [_validate_id(item) for item in ids]

    records = _load_records(root_path)
    entries = [_entry(root_path, record) for record in records]
    by_id = {entry["id"]: entry for entry in entries}
    if requested is not None:
        missing_ids = [item for item in requested if item not in by_id]
        if missing_ids:
            raise TempNotFound(
                f"unknown temporary entr{'y' if len(missing_ids) == 1 else 'ies'}: "
                + ", ".join(missing_ids))
        base_pool = [by_id[item] for item in requested]
    else:
        base_pool = entries

    selected: set[str] = set()
    has_filter = bool(expired or older_than_s is not None or requested is not None)
    if requested is not None:
        selected.update(requested)
    else:
        for entry in entries:
            if not has_filter and _default_cleanable(entry, now):
                selected.add(entry["id"])
            if expired and _is_expired(entry, now):
                selected.add(entry["id"])
            if (older_than_s is not None
                    and entry["last_used_at"] <= now - older_than_s):
                selected.add(entry["id"])

    guards: dict[str, tuple[bool, str]] = {}
    for entry in entries:
        guards[entry["id"]] = _delete_guard(root_path, entry)

    if max_total_bytes is not None:
        total = sum(entry["bytes"] for entry in entries
                    if isinstance(entry["bytes"], int))
        deficit = max(0, total - max_total_bytes)
        if not has_filter:
            selected = {entry["id"] for entry in base_pool}
        order = sorted(base_pool, key=lambda item: (item["last_used_at"],
                                                    item["id"]))
        capped: set[str] = set()
        for entry in order:
            if deficit <= 0:
                break
            safe, _ = guards[entry["id"]]
            if entry["id"] not in selected or not safe:
                continue
            capped.add(entry["id"])
            deficit -= entry["bytes"] if isinstance(entry["bytes"], int) else 0
        selected = capped

    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    deleted: list[str] = []
    for entry in entries:
        if entry["id"] not in selected:
            kept.append(_clean_row(entry, action="keep", why="not-selected"))
            continue
        safe, why = guards[entry["id"]]
        if not safe:
            kept.append(_clean_row(entry, action="keep", why=why))
            if apply and why == "missing":
                record = dict(_load_single(root_path, entry["id"]))
                record["state"] = "missing"
                _write_json(_record_path(root_path, entry["id"]), record)
            continue
        if not apply:
            candidates.append(_clean_row(entry, action="delete",
                                         why="eligible"))
            continue
        try:
            _delete_tree(_guarded_delete_path(entry))
        except OSError as exc:
            kept.append(_clean_row(entry, action="keep",
                                   why=f"delete-failed: {exc.strerror or exc}"))
            continue
        record = dict(_load_single(root_path, entry["id"]))
        record.update({"state": "released", "bytes": 0, "files": 0,
                       "walk": "complete", "tmpfs": None,
                       "last_used_at": int(time.time())})
        _write_json(_record_path(root_path, entry["id"]), record)
        candidates.append(_clean_row(entry, action="deleted",
                                     why="owned-marker-matched"))
        deleted.append(entry["id"])

    payload = {
        "root": str(root_path),
        "dry_run": not apply,
        "totals": {
            "candidates": sum(1 for row in candidates),
            "deleted": len(deleted),
            "kept": len(kept),
            "bytes": sum(row["bytes"] for row in candidates),
            "files": sum(row["files"] for row in candidates),
        },
        "candidates": candidates,
        "kept": kept,
        "deleted": deleted,
    }
    return payload


# --------------------------------------------------------------------------- #
# legacy cleanup
# --------------------------------------------------------------------------- #


def clean_legacy(root: str | Path, *,
                 roots: Iterable[str | Path] | None = None,
                 apply: bool = False,
                 older_than_s: int | float = LEGACY_DEFAULT_OLDER_THAN_S,
                 include_caches: bool = False,
                 max_artifacts: int = AUDIT_MAX_ARTIFACTS) -> dict[str, Any]:
    """Plan (and with ``apply=True`` perform) cleanup of legacy artifacts.

    Only paths that :func:`audit` already reports by a known ozagia name
    pattern are considered, so this never glob-deletes arbitrary temp files.
    Registered paths and paths under a registered ancestor are left to
    :func:`clean`.  Symlinks, protected paths, live sockets and held lock files
    are always kept.  Benchmark cache roots are only eligible with
    ``include_caches``.  This is a migration cleanup: it never writes a
    registry record, because the artifacts are deleted rather than adopted.
    """
    root_path = Path(root).resolve()
    if older_than_s < 0:
        raise TempSpaceError("older_than_s must not be negative")
    if max_artifacts < 0:
        raise TempSpaceError("max_artifacts must not be negative")
    older = int(older_than_s)
    report = audit(root_path, roots=roots, max_artifacts=max_artifacts)
    scan_roots = tuple(Path(item) for item in report["scan_roots"])

    rows: list[dict[str, Any]] = []
    for item in report["artifacts"]:
        action, why = _legacy_decision(
            item, root=root_path, scan_roots=scan_roots,
            older_than_s=older, include_caches=include_caches)
        rows.append(_legacy_row(item, action=action, why=why))
    rows.sort(key=lambda row: row["path"])

    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    deleted: list[str] = []
    errors: list[str] = []
    now = int(time.time())
    if apply:
        for row in rows:
            if row["action"] != "delete":
                kept.append(row)
                continue
            blocked = _legacy_recheck(
                row, root=root_path, scan_roots=scan_roots,
                older_than_s=older, include_caches=include_caches, now=now)
            if blocked is not None:
                row["action"] = "keep"
                row["why"] = blocked
                kept.append(row)
                continue
            try:
                _legacy_remove(
                    Path(row["path"]),
                    directory=_legacy_directory_pattern(row["pattern"]))
            except OSError as exc:
                detail = exc.strerror or str(exc)
                row["action"] = "keep"
                row["why"] = f"delete-failed: {detail}"
                errors.append(f"{row['path']}: {detail}")
                kept.append(row)
                continue
            row["action"] = "deleted"
            row["why"] = "eligible"
            candidates.append(row)
            deleted.append(row["path"])
    else:
        candidates = [row for row in rows if row["action"] == "delete"]
        kept = [row for row in rows if row["action"] == "keep"]

    truncated = bool(report.get("truncated"))
    totals = {
        "artifacts": len(rows),
        "candidates": len(candidates),
        "deleted": len(deleted),
        "kept": len(kept),
        "bytes": sum(row["bytes"] for row in candidates),
        "files": sum(row["files"] for row in candidates),
        "errors": len(errors),
        "truncated": truncated,
    }
    return {
        "root": str(root_path),
        "dry_run": not apply,
        "older_than_s": older,
        "include_caches": bool(include_caches),
        "registry_written": False,
        "scan_roots": report["scan_roots"],
        "excluded_roots": report["excluded_roots"],
        "patterns": list(AUDIT_PATTERNS),
        "totals": totals,
        "artifacts": rows,
        "candidates": candidates,
        "kept": kept,
        "deleted": deleted,
        "errors": errors,
        "warnings": report["warnings"],
        "truncated": truncated,
    }


def prune_registry(root: str | Path, *, apply: bool = False) -> dict[str, Any]:
    """Mark expired/missing records and drop finished ones from the registry.

    Active entries are never removed; a TTL expiry is the only transition that
    changes an active record.  A record whose path still exists is kept so
    ``clean`` can still find and delete it.
    """
    root_path = Path(root).resolve()
    now = int(time.time())
    marked_expired: list[str] = []
    marked_missing: list[str] = []
    removed: list[str] = []
    kept: list[str] = []
    for record in _load_records(root_path):
        entry = _entry(root_path, record)
        entry_id = entry["id"]
        state = record.get("state", "active")
        if state == "active":
            if _is_expired(entry, now):
                state = "expired"
                marked_expired.append(entry_id)
            elif entry["state"] == "missing":
                state = "missing"
                marked_missing.append(entry_id)
        if state in ("expired", "released", "missing"):
            if not os.path.lexists(Path(record["path"])):
                removed.append(entry_id)
                if apply:
                    try:
                        _record_path(root_path, entry_id).unlink()
                    except FileNotFoundError:
                        pass
                continue
        if state != record.get("state"):
            record["state"] = state
            if apply:
                _write_json(_record_path(root_path, entry_id), record)
        kept.append(entry_id)
    return {
        "root": str(root_path),
        "dry_run": not apply,
        "marked_expired": sorted(marked_expired),
        "marked_missing": sorted(marked_missing),
        "removed": sorted(removed),
        "kept": sorted(kept),
    }


def gc(root: str | Path, *, apply: bool = False) -> dict[str, Any]:
    """Alias for :func:`prune_registry`."""
    return prune_registry(root, apply=apply)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _validate_kind(kind: str) -> str:
    if kind not in KINDS:
        raise TempSpaceError(
            f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    return kind


def _validate_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise TempSpaceError("reason must be a string")
    reason = reason.strip()
    if not reason:
        raise TempSpaceError("reason must not be empty")
    if len(reason) > MAX_REASON:
        raise TempSpaceError(f"reason must be at most {MAX_REASON} characters")
    if _CONTROL_RE.search(reason):
        raise TempSpaceError("reason must not contain control characters")
    return reason


def _validate_optional_label(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _clean_token(value, label)


def _validate_ttl(ttl_s: int | None) -> int | None:
    if ttl_s is None:
        return None
    if not isinstance(ttl_s, int) or isinstance(ttl_s, bool):
        raise TempSpaceError("ttl_s must be an integer number of seconds")
    if ttl_s < MIN_TTL_S or ttl_s > MAX_TTL_S:
        raise TempSpaceError(
            f"ttl_s must be between {MIN_TTL_S} and {MAX_TTL_S} seconds")
    return ttl_s


def _validate_id(entry_id: str) -> str:
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
        raise TempNotFound(f"unknown temporary entry {entry_id!r}")
    return entry_id


def _clean_token(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TempSpaceError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise TempSpaceError(f"{label} must not be empty")
    if len(value) > MAX_REASON:
        raise TempSpaceError(f"{label} must be at most {MAX_REASON} characters")
    if _CONTROL_RE.search(value):
        raise TempSpaceError(f"{label} must not contain control characters")
    return value


def _normalise_session(session: Mapping[str, Any] | str | None
                       ) -> dict[str, str] | None:
    if session is None:
        return None
    if isinstance(session, Mapping):
        return make_session(session.get("harness"), session.get("id"))
    return make_session(None, str(session))


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _try_resolve(path: str | Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _lexical_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _refuse_symlink(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TempPathError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TempPathError(f"refusing a symlink path: {path}")


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _check_allowed(root: Path, target: Path) -> None:
    roots = allowed_temp_roots(root)
    if not any(_is_within(target, candidate) for candidate in roots):
        rendered = ", ".join(str(candidate) for candidate in roots)
        raise TempPathError(
            f"path is not under an allowed temp root: {target} (allowed: {rendered})")


def _check_repo_guard(root: Path, target: Path) -> None:
    if _is_within(target, root) and not _is_within(target, state_dir(root)):
        raise TempPathError(
            "refusing to write into the repository outside its state directory: "
            f"{target}")


def _assert_creatable(target: Path) -> None:
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        if not target.parent.is_dir():
            raise TempPathError(
                f"parent directory does not exist: {target.parent}") from None
        return
    except OSError as exc:
        raise TempPathError(f"cannot inspect {target}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TempPathError(f"refusing to create over a symlink: {target}")
    if not stat.S_ISDIR(info.st_mode):
        raise TempPathError(f"path exists and is not a directory: {target}")
    with os.scandir(target) as iterator:
        if any(True for _ in iterator):
            raise TempPathError(f"path is not empty: {target}")


def _create_default_target(root: Path, kind: str) -> tuple[Path, str]:
    base = default_kind_root(root, kind)
    _ensure_dir(base)
    for _ in range(32):
        entry_id = "tmp_" + secrets.token_hex(6)
        target = base / entry_id
        if _record_path(root, entry_id).exists():
            continue
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return target, entry_id
    raise TempSpaceError("could not allocate a unique temporary entry")


def _new_id(root: Path) -> str:
    for _ in range(32):
        entry_id = "tmp_" + secrets.token_hex(6)
        if not _record_path(root, entry_id).exists():
            return entry_id
    raise TempSpaceError("could not allocate a unique temporary entry id")


def _record_path(root: Path, entry_id: str) -> Path:
    return registry_dir(root) / f"{entry_id}.json"


def _entry(root: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    entry = dict(record)
    session = entry.get("session")
    entry["session"] = dict(session) if isinstance(session, Mapping) else None
    entry["session_source"] = "caller" if entry["session"] else "unknown"
    entry.setdefault("model", None)
    entry.setdefault("run_id", None)
    entry.setdefault("expires_at", None)
    entry["expires_at"] = _expires_at(entry)
    path = Path(str(entry["path"]))
    state = str(entry.get("state", "active"))
    existing = _safe_lstat(path)
    if state == "active" and (existing is None
                              or not stat.S_ISDIR(existing.st_mode)):
        state = "missing"
    entry["state"] = state
    if existing is None or not stat.S_ISDIR(existing.st_mode):
        entry.update({"bytes": 0, "files": 0, "walk": "complete",
                      "unknown": False, "errors": 0, "tmpfs": None,
                      "marker": False})
        return entry
    resolved = _try_resolve(path)
    allowed = allowed_temp_roots(root)
    view_safe = (
        resolved is not None
        and any(_is_within(resolved, candidate) for candidate in allowed)
        and not (_is_within(resolved, root)
                 and not _is_within(resolved, state_dir(root)))
        and not _is_protected(root, resolved, allowed)
    )
    if not view_safe:
        entry.update({"bytes": 0, "files": 0, "walk": "complete",
                      "unknown": False, "errors": 0, "tmpfs": None,
                      "marker": False})
        return entry
    usage = walk_usage(path)
    entry.update(usage)
    entry["tmpfs"] = tmpfs_for(path)
    entry["marker"] = _marker_matches(path, str(entry["id"]))
    return entry


def _expires_at(entry: Mapping[str, Any]) -> int | None:
    if entry.get("expires_at") is not None:
        return int(entry["expires_at"])
    ttl = entry.get("ttl_s")
    created = entry.get("created_at")
    if isinstance(ttl, int) and isinstance(created, int):
        return created + ttl
    return None


def _is_expired(entry: Mapping[str, Any], now: int) -> bool:
    expires = _expires_at(entry)
    return expires is not None and expires <= now


def _default_cleanable(entry: Mapping[str, Any], now: int) -> bool:
    state = entry.get("state")
    if state in ("released", "expired"):
        return True
    if state == "missing":
        return True
    return _is_expired(entry, now)


def _audit_excluded_roots() -> tuple[Path, ...]:
    candidates = [Path("/tmp/opencode"),
                  Path.home() / ".local" / "share" / "opencode"]
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = _absolute(candidate)
        if str(resolved) not in seen:
            result.append(resolved)
            seen.add(str(resolved))
    return tuple(result)


def _audit_excluded(path: str | Path) -> bool:
    target = _try_resolve(path)
    if target is None:
        return True
    return any(target == root or _is_within(target, root)
               for root in _audit_excluded_roots())


def _audit_scan_roots(roots: Iterable[str | Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in roots:
        resolved = _try_resolve(raw)
        if resolved is None:
            continue
        if str(resolved) not in seen:
            result.append(resolved)
            seen.add(str(resolved))
    return tuple(sorted(result, key=str))


def _is_dir_no_follow(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _audit_artifact(path: Path, pattern: str, info: os.stat_result,
                    registry: Mapping[str, Mapping[str, Any]],
                    now: int, deadline: float) -> dict[str, Any]:
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
        usage = {"bytes": 0, "files": 0, "walk": "complete",
                 "unknown": False, "errors": 0}
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
        remaining = max(0.0, deadline - time.monotonic())
        usage = walk_usage(path, max_seconds=remaining)
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
        usage = {"bytes": info.st_size, "files": 1, "walk": "complete",
                 "unknown": False, "errors": 0}
    else:
        kind = "other"
        usage = {"bytes": 0, "files": 0, "walk": "complete",
                 "unknown": False, "errors": 0}
    rendered = str(path)
    exact = registry.get(rendered)
    ancestor: Mapping[str, Any] | None = None
    ancestor_path = ""
    for registered_path, record in registry.items():
        if registered_path == rendered:
            continue
        if len(registered_path) <= len(ancestor_path):
            continue
        if _is_within(path, Path(registered_path)):
            ancestor = record
            ancestor_path = registered_path
    owner = exact if exact is not None else ancestor
    mtime = int(info.st_mtime)
    return {
        "path": rendered,
        "pattern": pattern,
        "kind": kind,
        "bytes": usage["bytes"],
        "files": usage["files"],
        "walk": usage["walk"],
        "unknown": usage["unknown"],
        "errors": usage["errors"],
        "mtime": mtime,
        "age_s": max(0, now - mtime),
        "registered": exact is not None,
        "registered_entry": exact.get("id") if exact is not None else None,
        "registered_ancestor": (ancestor.get("id")
                                if ancestor is not None else None),
        "owned": bool(owner.get("owned")) if owner is not None else False,
    }


def _session_matches(entry: Mapping[str, Any], wanted: str) -> bool:
    session = entry.get("session")
    if not isinstance(session, Mapping):
        return wanted in ("", "unknown", "null")
    harness = str(session.get("harness") or "")
    ident = str(session.get("id") or "")
    return wanted in (ident, f"{harness}:{ident}")


def _session_label(entry: Mapping[str, Any]) -> str:
    session = entry.get("session")
    if not isinstance(session, Mapping):
        return "unknown"
    return f"{session.get('harness')}:{session.get('id')}"


def _clean_row(entry: Mapping[str, Any], *, action: str, why: str
               ) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "path": entry["path"],
        "kind": entry["kind"],
        "state": entry["state"],
        "owned": bool(entry["owned"]),
        "session": entry["session"],
        "reason": entry["reason"],
        "model": entry.get("model"),
        "run_id": entry.get("run_id"),
        "bytes": entry["bytes"] if isinstance(entry["bytes"], int) else 0,
        "files": entry["files"] if isinstance(entry["files"], int) else 0,
        "marker": bool(entry.get("marker")),
        "action": action,
        "why": why,
    }


def _legacy_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _legacy_directory_pattern(pattern: str) -> bool:
    return pattern in LEGACY_DIR_PATTERNS or pattern in LEGACY_CACHE_PATTERNS


def _legacy_remove(path: Path, *, directory: bool) -> None:
    """Remove one planned artifact without following a symlink."""
    if directory:
        _delete_tree(path)
    else:
        os.unlink(path)


def _legacy_protected(root: Path, path: Path,
                      scan_roots: Iterable[Path]) -> bool:
    allowed = allowed_temp_roots(root)
    if _is_protected(root, path, allowed):
        return True
    managed = [Path(state_dir(root)), state_path(root, TMP_DIRNAME),
               registry_dir(root), workspaces_root(root), agent_root(root)]
    managed.extend(default_kind_root(root, kind) for kind in KINDS)
    for item in managed:
        if path == item or _is_within(path, item) or _is_within(item, path):
            return True
    return any(path == scan_root for scan_root in scan_roots)


def _legacy_lock_state(path: Path) -> str:
    """``free``, ``held`` or a conservative keep reason for a lock file."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return "lock-check-unavailable"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "lock-unreadable"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return "held"
            return "lock-error"
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        return "free"
    finally:
        os.close(descriptor)


def _legacy_socket_state(path: Path) -> str:
    """``stale``, ``live`` or a conservative keep reason for a socket path."""
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return "socket-unknown"
    client.settimeout(LEGACY_SOCKET_TIMEOUT_S)
    try:
        client.connect(str(path))
        return "live"
    except socket.timeout:
        return "socket-timeout"
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK):
            return "stale"
        return "socket-unknown"
    finally:
        client.close()


def _legacy_decision(item: Mapping[str, Any], *, root: Path,
                     scan_roots: Iterable[Path], older_than_s: int,
                     include_caches: bool) -> tuple[str, str]:
    pattern = str(item.get("pattern") or "")
    path = Path(str(item.get("path") or ""))
    if item.get("registered"):
        return "keep", "registered"
    if item.get("registered_ancestor"):
        return "keep", "registered-ancestor"
    if not any(_is_within(path, scan_root) for scan_root in scan_roots):
        return "keep", "outside-scan-root"
    kind = item.get("kind")
    if kind == "symlink":
        return "keep", "symlink"
    age = int(item.get("age_s") or 0)
    file_kind = LEGACY_FILE_PATTERNS.get(pattern)
    if file_kind is not None:
        info = _safe_lstat(path)
        if info is None:
            return "keep", "missing"
        if stat.S_ISLNK(info.st_mode):
            return "keep", "symlink"
        if file_kind == "lock":
            if not stat.S_ISREG(info.st_mode):
                return "keep", "not-a-regular-file"
        # A bound Unix socket is a socket node, not a regular file.  Plain
        # regular files are accepted as well so a stale placeholder is still
        # handled; other special files (FIFOs, devices) are kept.
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
            return "keep", "not-a-socket"
        if _legacy_protected(root, path, scan_roots):
            return "keep", "protected-path"
        if file_kind == "lock":
            state = _legacy_lock_state(path)
            if state != "free":
                return "keep", "lock-held" if state == "held" else state
        else:
            state = _legacy_socket_state(path)
            if state != "stale":
                return "keep", "socket-live" if state == "live" else state
        if age < older_than_s:
            return "keep", "too-recent"
        return "delete", "eligible"
    if pattern in LEGACY_DIR_PATTERNS or pattern in LEGACY_CACHE_PATTERNS:
        if pattern in LEGACY_CACHE_PATTERNS and not include_caches:
            return "keep", "cache-disabled"
        if kind != "directory":
            return "keep", "not-a-directory"
        if age < older_than_s:
            return "keep", "too-recent"
        if _legacy_protected(root, path, scan_roots):
            return "keep", "protected-path"
        return "delete", "eligible"
    return "keep", "unknown-pattern"


def _legacy_recheck(row: Mapping[str, Any], *, root: Path,
                    scan_roots: Iterable[Path], older_than_s: int,
                    include_caches: bool, now: int) -> str | None:
    """Re-check a planned artifact immediately before deleting it."""
    path = Path(str(row["path"]))
    info = _safe_lstat(path)
    if info is None:
        return "missing"
    fresh = {
        "path": str(path),
        "pattern": row["pattern"],
        "kind": _legacy_kind(info.st_mode),
        "age_s": max(0, now - int(info.st_mtime)),
        "registered": False,
        "registered_ancestor": None,
    }
    action, why = _legacy_decision(
        fresh, root=root, scan_roots=scan_roots,
        older_than_s=older_than_s, include_caches=include_caches)
    return None if action == "delete" else why


def _legacy_row(item: Mapping[str, Any], *, action: str,
                why: str) -> dict[str, Any]:
    return {
        "path": str(item.get("path")),
        "pattern": item.get("pattern"),
        "kind": item.get("kind"),
        "bytes": int(item.get("bytes") or 0),
        "files": int(item.get("files") or 0),
        "age_s": int(item.get("age_s") or 0),
        "mtime": item.get("mtime"),
        "registered": bool(item.get("registered")),
        "registered_ancestor": item.get("registered_ancestor"),
        "action": action,
        "why": why,
    }


def _delete_guard(root: Path, entry: Mapping[str, Any]) -> tuple[bool, str]:
    if not entry.get("owned"):
        return False, "unowned"
    raw = Path(str(entry["path"]))
    path = _try_resolve(raw)
    if path is None:
        return False, "unresolvable-path"
    info = _safe_lstat(raw)
    if info is None:
        return False, "missing"
    if stat.S_ISLNK(info.st_mode):
        return False, "symlink"
    if not stat.S_ISDIR(info.st_mode):
        return False, "not-a-directory"
    allowed = allowed_temp_roots(root)
    if not any(_is_within(path, candidate) for candidate in allowed):
        return False, "outside-allowed-root"
    if _is_within(path, root) and not _is_within(path, state_dir(root)):
        return False, "repository-path"
    if _is_protected(root, path, allowed):
        return False, "protected-path"
    marker = _read_marker(path)
    if marker is None:
        return False, "marker-missing"
    if marker.get("id") != entry["id"]:
        return False, "marker-mismatch"
    return True, "ok"


def _guarded_delete_path(entry: Mapping[str, Any]) -> Path:
    raw = Path(str(entry["path"]))
    return _try_resolve(raw) or raw


def _is_protected(root: Path, path: Path,
                  allowed: Iterable[Path] = ()) -> bool:
    protected = {
        Path(path.anchor),
        root,
        Path.home().resolve(),
        Path(state_dir(root)),
        state_path(root, TMP_DIRNAME),
        registry_dir(root),
        workspaces_root(root),
        agent_root(root),
        *[default_kind_root(root, kind) for kind in KINDS],
        *allowed,
    }
    return any(path == item for item in protected)


def _delete_tree(path: Path) -> None:
    """Remove a tree without ever following a symlink."""
    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(path)
        return
    with os.scandir(path) as iterator:
        children = list(iterator)
    for child in children:
        child_path = Path(child.path)
        child_info = child.stat(follow_symlinks=False)
        if stat.S_ISDIR(child_info.st_mode):
            _delete_tree(child_path)
        else:
            os.unlink(child_path)
    os.rmdir(path)


def _remove_marker_and_empty_dir(path: Path, entry_id: str, *,
                                 remove_dir: bool = True) -> None:
    marker = path / MARKER_NAME
    try:
        existing = _read_marker(path)
        if existing is not None and existing.get("id") == entry_id:
            marker.unlink()
    except OSError:
        pass
    if not remove_dir:
        return
    try:
        path.rmdir()
    except OSError:
        pass


def _safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _marker_matches(path: Path, entry_id: str) -> bool:
    marker = _read_marker(path)
    return bool(marker and marker.get("id") == entry_id)


def _read_marker(path: Path) -> dict[str, Any] | None:
    marker_path = Path(path) / MARKER_NAME
    try:
        info = os.lstat(marker_path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise TempSpaceError(f"not a directory: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise TempSpaceError(f"cannot secure directory {path}: {exc}") from exc


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _load_records(root: Path, *, errors: list[str] | None = None
                  ) -> list[dict[str, Any]]:
    directory = registry_dir(root)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        if errors is not None:
            errors.append(f"registry unreadable: {exc}")
        return []
    records: list[dict[str, Any]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = directory / name
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            if errors is not None:
                errors.append(f"registry record {name}: {exc}")
            continue
        if not isinstance(record, dict):
            if errors is not None:
                errors.append(f"registry record {name}: not an object")
            continue
        if not _ID_RE.match(str(record.get("id") or "")):
            if errors is not None:
                errors.append(f"registry record {name}: invalid id")
            continue
        records.append(record)
    return records


def _read_record(root: Path, entry_id: str) -> dict[str, Any]:
    path = _record_path(root, entry_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError as exc:
        raise TempNotFound(f"unknown temporary entry {entry_id!r}") from exc
    except (OSError, ValueError) as exc:
        raise TempSpaceError(
            f"unreadable temporary registry record {entry_id}: {exc}") from exc
    if not isinstance(record, dict) or record.get("id") != entry_id:
        raise TempSpaceError(
            f"invalid temporary registry record {entry_id}")
    return record


def _load_single(root: Path, entry_id: str) -> dict[str, Any]:
    return _read_record(root, entry_id)


def _read_mountinfo() -> str | None:
    return _read_proc("/proc/self/mountinfo")


def _read_mounts() -> str | None:
    return _read_proc("/proc/mounts")


def _read_proc(name: str) -> str | None:
    try:
        return Path(name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse_mounts(text: str, style: str) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if style == "mountinfo":
            if "-" not in fields:
                continue
            separator = fields.index("-")
            if separator < 6 or len(fields) <= separator + 1:
                continue
            mount = _unescape(fields[4])
            fstype = fields[separator + 1]
        else:
            if len(fields) < 3:
                continue
            mount = _unescape(fields[1])
            fstype = fields[2]
        mounts.append((mount, fstype))
    return mounts


def _mounts_longest_first(mounts: list[tuple[str, str]]
                          ) -> list[tuple[str, str]]:
    return sorted(mounts, key=lambda item: len(item[0]), reverse=True)


def _on_mount(path: Path, mount: str) -> bool:
    rendered = path.as_posix()
    if mount == "/":
        return True
    return rendered == mount or rendered.startswith(mount.rstrip("/") + "/")


def _unescape(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))
    return re.sub(r"\\([0-7]{3})", replace, value)


__all__ = [
    "AGENT_DIRNAME", "AUDIT_MAX_ARTIFACTS", "AUDIT_MAX_SECONDS",
    "AUDIT_PATTERNS", "DEFAULT_KIND", "DEFAULT_SCRATCH_KIND", "KINDS",
    "LEGACY_CACHE_PATTERNS", "LEGACY_DEFAULT_OLDER_THAN_S",
    "LEGACY_DIR_PATTERNS", "LEGACY_FILE_PATTERNS", "LEGACY_SOCKET_TIMEOUT_S",
    "MARKER_NAME", "MAX_REASON", "MAX_TTL_S", "MAX_WALK_DEPTH",
    "MAX_WALK_ENTRIES", "MAX_WALK_SECONDS", "MIN_TTL_S", "REGISTRY_DIRNAME",
    "STATES", "TMP_DIRNAME", "TempMarkerError", "TempNotFound",
    "TempPathError", "TempSpaceError", "VERSION", "adopt", "agent_root",
    "allowed_temp_roots", "audit", "clean", "clean_legacy", "create",
    "default_kind_root", "default_scratch_root", "ensure_default_root", "gc",
    "get", "list_entries", "make_session", "prune_registry", "registry_dir",
    "release", "status", "suggest_path", "tmpfs_for", "touch", "walk_usage",
    "workspaces_root",
]
