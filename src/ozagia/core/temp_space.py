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

``.<lease>`` (``.ozagia-lease``)
    An optional advisory ``flock`` file for a live cooperative owner.  It is
    not a record; the lock dies with the process.

``CACHEDIR.TAG``
    Written only for a ``cache`` entry.  It carries the conventional cache
    directory signature so other tools (and this module's audit) recognize the
    tree as a disposable cache rather than an untracked legacy artifact.

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
``engine``, ``cache`` and ``other`` entries.  ``<state dir>/tmp/agent/`` is
reserved for agent run roots (wired by a later slice).  ``adopt`` may
additionally track existing paths under the resolved ``TMPDIR``, ``/tmp`` and
``/var/tmp``.

Safety
------

Paths are resolved with :meth:`Path.resolve` (``strict=False``) and must stay
inside an allowed root.  Cleanup only considers ``owned`` entries whose marker
id matches, refuses symlinks, filesystem roots, the repository root, the home
directory and any allowed root itself, and never follows a symlink out of an
entry.  Deletion is a dry-run plan unless ``apply=True``.  A plan records the
entry directory's ``(st_dev, st_ino)`` and rechecks it (and the marker) just
before deletion, so a path replaced by a symlink, another inode or a
non-directory is kept rather than followed.  That narrows but does not close
the name-resolution race; ``openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`` is
the documented future direction.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.config import state_dir, state_path

VERSION = 1
MARKER_NAME = ".ozagia-tmp.json"
LEASE_NAME = ".ozagia-lease"
REGISTRY_DIRNAME = "registry"
TMP_DIRNAME = "tmp"
WORKSPACES_DIRNAME = "workspaces"
AGENT_DIRNAME = "agent"

KINDS = ("scratch", "agent", "check", "eval", "engine", "cache", "other")
STATES = ("active", "released", "expired", "missing")
DEFAULT_KIND = "other"
DEFAULT_SCRATCH_KIND = "scratch"

CACHEDIR_TAG_NAME = "CACHEDIR.TAG"
CACHEDIR_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
CACHEDIR_TAG_CONTENT = (
    f"{CACHEDIR_TAG_SIGNATURE}\n"
    "# This file is a cache directory tag created by ozagia.\n"
    "# For information about cache directory tags, see:\n"
    "#\thttps://bford.info/cachedir/\n"
)

MIN_TTL_S = 60
MAX_TTL_S = 90 * 24 * 60 * 60
MAX_REASON = 200

MAX_WALK_ENTRIES = 200_000
MAX_WALK_SECONDS = 5.0
MAX_WALK_DEPTH = 64

#: ``st_blocks`` is measured in 512-byte units on POSIX.
ALLOCATION_BLOCK_SIZE = 512
#: ``allocation`` value when the platform reports usable block counts.
ALLOCATION_BLOCKS = "blocks"
#: ``allocation`` value when allocated space fell back to ``st_size``.
ALLOCATION_APPARENT_FALLBACK = "apparent-fallback"
#: POSIX reports meaningful ``st_blocks``; zero is a valid fully sparse file,
#: not evidence that block accounting is unavailable.  A non-POSIX platform
#: that reports zero for every non-empty file falls back to apparent size.
TRUST_ST_BLOCKS = os.name == "posix"

DEFAULT_LIST_SORT = "created"
LIST_SORT_KEYS = ("created", "allocated", "apparent", "files", "inodes", "age")

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


class TempBudgetError(TempSpaceError):
    """A create or garbage-collection request would exceed a budget."""


class TempLeaseHeld(TempSpaceError):
    """A cooperative owner already holds the entry's lease."""


#: Environment overrides used when ``create`` is not given an explicit budget.
ENV_MAX_TOTAL_BYTES = "OZAGIA_TMP_MAX_BYTES"
ENV_MAX_INODES = "OZAGIA_TMP_MAX_INODES"
ENV_MIN_FREE = "OZAGIA_TMP_MIN_FREE"


@dataclass(frozen=True)
class TempBudget:
    """Explicit limits for managed temporary space.

    A ``None`` field is unset.  ``create`` merges unset fields from the
    environment (``OZAGIA_TMP_MAX_BYTES``, ``OZAGIA_TMP_MAX_INODES`` and
    ``OZAGIA_TMP_MIN_FREE``) when no value was supplied by the caller.
    """

    max_total_bytes: int | None = None
    max_inodes: int | None = None
    max_session_bytes: int | None = None
    max_session_inodes: int | None = None
    min_free_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_total_bytes", "max_inodes", "max_session_bytes",
                     "max_session_inodes", "min_free_bytes"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TempSpaceError(f"{name} must be an integer or None")
            if value < 0:
                raise TempSpaceError(f"{name} must not be negative")


@dataclass(frozen=True)
class _Lease:
    """An open lease descriptor; closing it releases the ``flock``."""

    path: Path
    descriptor: int
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX hosts
            fcntl = None  # type: ignore[assignment]
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(self.descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(self.descriptor)
            except OSError:
                pass
            object.__setattr__(self, "_closed", True)

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "_Lease":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort final release
        try:
            self.close()
        except Exception:  # noqa: BLE001 - a destructor must not raise
            pass


# --------------------------------------------------------------------------- #
# leases
# --------------------------------------------------------------------------- #


def lease_path(root: str | Path, entry_id: str) -> Path:
    """The lease file for one registry entry."""
    root_path = Path(root).resolve()
    record = _read_record(root_path, _validate_id(entry_id))
    return Path(str(record["path"])) / LEASE_NAME


def _lease_held_path(path: str | Path) -> bool:
    """Probe an entry directory's lease without creating or deleting anything."""
    lease = Path(path) / LEASE_NAME
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX hosts
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lease, flags)
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable or symlinked lease is not evidence that it is held.
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def lease_held(root: str | Path, entry_id: str) -> bool:
    """Whether a cooperative owner currently holds the entry's lease."""
    root_path = Path(root).resolve()
    record = _read_record(root_path, _validate_id(entry_id))
    return _lease_held_path(Path(str(record["path"])))


def acquire_lease(root: str | Path, entry_id: str) -> _Lease:
    """Take the entry's crash-safe lease and return its context manager.

    The lock is an advisory exclusive ``flock`` on ``<entry>/.ozagia-lease``.
    It dies with the process, so a crash cannot leave a permanent lease.
    """
    path = lease_path(root, entry_id)
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - non-POSIX hosts
        raise TempSpaceError(
            "file leases need a POSIX flock implementation") from exc
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise TempSpaceError(f"cannot open lease {path}: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise TempLeaseHeld(
                    f"lease is already held for {entry_id}: {path}") from exc
            raise TempSpaceError(
                f"cannot take lease {path}: {exc}") from exc
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return _Lease(path=path, descriptor=descriptor)


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #


def _env_int(name: str) -> int | None:
    text = os.environ.get(name)
    if text is None or not text.strip():
        return None
    try:
        value = int(text.strip())
    except ValueError as exc:
        raise TempSpaceError(f"{name} must be an integer, got {text!r}") from exc
    if value < 0:
        raise TempSpaceError(f"{name} must not be negative")
    return value


def _budget_with_env(budget: TempBudget | None) -> TempBudget:
    """Return ``budget`` with any unset field filled from the environment."""
    if budget is None:
        budget = TempBudget()
    elif not isinstance(budget, TempBudget):
        raise TempSpaceError("budget must be a TempBudget or None")
    values = {
        "max_total_bytes": budget.max_total_bytes,
        "max_inodes": budget.max_inodes,
        "max_session_bytes": budget.max_session_bytes,
        "max_session_inodes": budget.max_session_inodes,
        "min_free_bytes": budget.min_free_bytes,
    }
    overrides = {
        "max_total_bytes": ENV_MAX_TOTAL_BYTES,
        "max_inodes": ENV_MAX_INODES,
        "min_free_bytes": ENV_MIN_FREE,
    }
    for field, env_name in overrides.items():
        if values[field] is None:
            values[field] = _env_int(env_name)
    return TempBudget(**values)


def _budget_payload(budget: TempBudget | None) -> dict[str, Any] | None:
    if budget is None:
        return None
    return {
        "max_total_bytes": budget.max_total_bytes,
        "max_inodes": budget.max_inodes,
        "max_session_bytes": budget.max_session_bytes,
        "max_session_inodes": budget.max_session_inodes,
        "min_free_bytes": budget.min_free_bytes,
    }


def _session_identity(entry: Mapping[str, Any]) -> tuple[str, str] | None:
    session = entry.get("session")
    if not isinstance(session, Mapping):
        return None
    harness = str(session.get("harness") or "")
    ident = str(session.get("id") or "")
    if not harness or not ident:
        return None
    return harness, ident


def _usage_totals(root: Path, *, exclude_id: str | None = None
                  ) -> tuple[int, int, dict[tuple[str, str], tuple[int, int]]]:
    """Current allocated bytes/inodes and per-session totals.

    Zero-usage (missing) records contribute nothing.  The calculation includes
    every existing registry entry, including released entries whose tree has
    not been deleted yet, because those bytes still occupy the filesystem.
    """
    total_bytes = 0
    total_inodes = 0
    sessions: dict[tuple[str, str], tuple[int, int]] = {}
    for record in _load_records(root):
        if exclude_id is not None and record.get("id") == exclude_id:
            continue
        entry = _entry(root, record)
        size = entry.get("bytes")
        inodes = entry.get("inodes")
        size = size if isinstance(size, int) else 0
        inodes = inodes if isinstance(inodes, int) else 0
        total_bytes += size
        total_inodes += inodes
        key = _session_identity(entry)
        if key is not None:
            old_bytes, old_inodes = sessions.get(key, (0, 0))
            sessions[key] = (old_bytes + size, old_inodes + inodes)
    return total_bytes, total_inodes, sessions


def _free_bytes(path: str | Path) -> int | None:
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        usage = statvfs(path)
    except OSError:
        return None
    return int(usage.f_bavail * usage.f_frsize)


def _enforce_budget(root: Path, budget: TempBudget,
                    session: Mapping[str, Any] | None, *,
                    extra_bytes: int = 0, extra_inodes: int = 0,
                    free_path: str | Path | None = None) -> None:
    if all(getattr(budget, field) is None
           for field in ("max_total_bytes", "max_inodes", "max_session_bytes",
                         "max_session_inodes", "min_free_bytes")):
        return
    total_bytes, total_inodes, sessions = _usage_totals(root)
    projected_bytes = total_bytes + extra_bytes
    projected_inodes = total_inodes + extra_inodes
    if (budget.max_total_bytes is not None
            and projected_bytes > budget.max_total_bytes):
        raise TempBudgetError(
            "temporary-space budget exceeded: total allocated bytes "
            f"{projected_bytes} > {budget.max_total_bytes}")
    if (budget.max_inodes is not None
            and projected_inodes > budget.max_inodes):
        raise TempBudgetError(
            "temporary-space budget exceeded: total inodes "
            f"{projected_inodes} > {budget.max_inodes}")
    if session is not None:
        harness = str(session.get("harness") or "")
        ident = str(session.get("id") or "")
        if harness and ident:
            session_bytes, session_inodes = sessions.get(
                (harness, ident), (0, 0))
            session_bytes += extra_bytes
            session_inodes += extra_inodes
            if (budget.max_session_bytes is not None
                    and session_bytes > budget.max_session_bytes):
                raise TempBudgetError(
                    "temporary-space budget exceeded: session "
                    f"{harness}:{ident} allocated bytes "
                    f"{session_bytes} > {budget.max_session_bytes}")
            if (budget.max_session_inodes is not None
                    and session_inodes > budget.max_session_inodes):
                raise TempBudgetError(
                    "temporary-space budget exceeded: session "
                    f"{harness}:{ident} inodes "
                    f"{session_inodes} > {budget.max_session_inodes}")
    if budget.min_free_bytes is not None and free_path is not None:
        free = _free_bytes(free_path)
        if free is not None and free < budget.min_free_bytes:
            raise TempBudgetError(
                "temporary-space budget exceeded: free bytes "
                f"{free} < minimum {budget.min_free_bytes} at {free_path}")


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


def _allocated_blocks(info: os.stat_result) -> int | None:
    """Allocated bytes from one ``stat`` result, or ``None`` when unavailable.

    POSIX reports ``st_blocks`` in 512-byte units.  A platform without the
    attribute, or a nonsensical negative value, is *unavailable* rather than
    zero, so the caller can fall back to apparent size explicitly.
    """
    blocks = getattr(info, "st_blocks", None)
    if not isinstance(blocks, int) or blocks < 0:
        return None
    return blocks * ALLOCATION_BLOCK_SIZE


def _empty_usage(*, inodes: int = 0) -> dict[str, Any]:
    """A complete zero usage result (also used for non-walked artifact kinds)."""
    return {
        "allocated_bytes": 0,
        "apparent_bytes": 0,
        "files": 0,
        "inodes": inodes,
        "hardlinks": 0,
        "walk": "complete",
        "unknown": False,
        "errors": 0,
        "allocation": ALLOCATION_BLOCKS,
        "cross_device": False,
    }


def _usage_record(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Registry fields derived from one walk, with ``bytes`` = allocated."""
    return {
        "bytes": int(usage["allocated_bytes"]),
        "apparent_bytes": int(usage["apparent_bytes"]),
        "files": int(usage["files"]),
        "inodes": int(usage["inodes"]),
        "hardlinks": int(usage["hardlinks"]),
        "allocation": usage["allocation"],
        "walk": usage["walk"],
        "cross_device": bool(usage.get("cross_device")),
    }


def _cleared_usage_record() -> dict[str, Any]:
    """The registry usage fields for a released, missing or deleted entry."""
    return {
        "bytes": 0,
        "apparent_bytes": 0,
        "files": 0,
        "inodes": 0,
        "hardlinks": 0,
        "allocation": None,
        "walk": "complete",
        "cross_device": False,
    }


def walk_usage(path: str | Path, *, max_entries: int = MAX_WALK_ENTRIES,
               max_seconds: float = MAX_WALK_SECONDS,
               max_depth: int = MAX_WALK_DEPTH) -> dict[str, Any]:
    """Bounded usage walk of a directory tree.

    The walk never follows symlinks and never reads file contents.  It reports:

    ``allocated_bytes``
        ``st_blocks * 512`` summed once per regular-file inode.
    ``apparent_bytes``
        ``st_size`` summed once per regular-file inode.
    ``files``
        Regular-file entries, including every hard link.
    ``inodes``
        Unique ``(st_dev, st_ino)`` values for regular files, directories and
        symlinks visited (the root directory and symlinks count).
    ``hardlinks``
        Duplicate regular-file links; their bytes and inode are not counted
        twice.
    ``allocation``
        ``blocks`` when block counts were usable, or ``apparent-fallback``
        when the platform did not report blocks.  On POSIX a zero block count
        is valid (a fully sparse file); the fallback is only used when the
        platform has no meaningful block accounting, or a non-POSIX platform
        reports zero for every non-empty file.

    A bound that stops the walk, an unreadable entry, or a child on another
    filesystem marks the result ``partial`` and ``unknown`` rather than
    silently reporting a complete total.  A cross-device child is skipped and
    sets ``cross_device``.
    """
    path = Path(path)
    if max_entries < 0 or max_seconds < 0 or max_depth < 0:
        raise TempSpaceError("walk bounds must not be negative")
    result = _empty_usage()
    try:
        root_stat = os.lstat(path)
    except FileNotFoundError:
        return result
    except OSError:
        return {**result, "walk": "partial", "unknown": True, "errors": 1}

    device = root_stat.st_dev
    seen: set[tuple[int, int]] = set()
    allocated = 0
    apparent = 0
    files = 0
    hardlinks = 0
    saw_nonempty = False
    saw_nonzero_blocks = False
    blocks_available = True

    def account_file(info: os.stat_result) -> None:
        nonlocal allocated, apparent, files, hardlinks
        nonlocal saw_nonempty, saw_nonzero_blocks, blocks_available
        blocks = _allocated_blocks(info)
        size = int(info.st_size)
        if blocks is None:
            blocks_available = False
        if size > 0:
            saw_nonempty = True
            if blocks is not None and blocks > 0:
                saw_nonzero_blocks = True
        key = (info.st_dev, info.st_ino)
        files += 1
        if key in seen:
            hardlinks += 1
            return
        seen.add(key)
        apparent += size
        if blocks is not None:
            allocated += blocks

    def finish() -> dict[str, Any]:
        fallback = (not blocks_available
                    or (not TRUST_ST_BLOCKS and saw_nonempty
                        and not saw_nonzero_blocks))
        result.update({
            "allocated_bytes": apparent if fallback else allocated,
            "apparent_bytes": apparent,
            "files": files,
            "inodes": len(seen),
            "hardlinks": hardlinks,
            "allocation": (ALLOCATION_APPARENT_FALLBACK if fallback
                           else ALLOCATION_BLOCKS),
        })
        return result

    if not stat.S_ISDIR(root_stat.st_mode):
        if stat.S_ISREG(root_stat.st_mode):
            account_file(root_stat)
        elif stat.S_ISLNK(root_stat.st_mode):
            seen.add((root_stat.st_dev, root_stat.st_ino))
        return finish()

    seen.add((root_stat.st_dev, root_stat.st_ino))
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
                if info.st_dev != device:
                    result["cross_device"] = True
                    result["walk"] = "partial"
                    result["unknown"] = True
                    continue
                if stat.S_ISREG(info.st_mode):
                    account_file(info)
                elif stat.S_ISDIR(info.st_mode):
                    seen.add((info.st_dev, info.st_ino))
                    if depth + 1 > max_depth:
                        result["walk"] = "partial"
                    else:
                        stack.append((Path(entry.path), depth + 1))
                elif stat.S_ISLNK(info.st_mode):
                    seen.add((info.st_dev, info.st_ino))
                # Special files are visited but do not count as files, bytes
                # or inodes; symlinks are counted as inodes but never followed.
            if stopped:
                break
    return finish()


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
           run_id: str | None = None,
           budget: TempBudget | None = None) -> dict[str, Any]:
    """Create one managed temporary directory and register it.

    ``budget`` is checked against the existing registry (and, for the session
    limits, the same ``{harness, id}``) before the record is written.  An unset
    budget field is filled from ``OZAGIA_TMP_MAX_BYTES``,
    ``OZAGIA_TMP_MAX_INODES`` and ``OZAGIA_TMP_MIN_FREE``.  A refusal raises
    :class:`TempBudgetError` and removes the directory this call created.
    """
    root_path = Path(root).resolve()
    kind = _validate_kind(kind)
    reason = _validate_reason(reason)
    session_value = _normalise_session(session)
    ttl_s = _validate_ttl(ttl_s)
    model = _validate_optional_label(model, "model")
    run_id = _validate_optional_label(run_id, "run_id")
    budget = _budget_with_env(budget)
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
        "pinned": False,
        "pinned_until": None,
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

    wrote_tag = False
    preexisting = os.path.lexists(target)
    try:
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        if kind == "cache":
            wrote_tag = True
            _write_cachedir_tag(target)
        _write_json(target / MARKER_NAME, marker)
        usage = walk_usage(target)
        _enforce_budget(
            root_path, budget, session_value,
            extra_bytes=int(usage["allocated_bytes"]),
            extra_inodes=int(usage["inodes"]), free_path=target)
        record.update(_usage_record(usage))
        record.update({"tmpfs": tmpfs_for(target)})
        _write_json(_record_path(root_path, entry_id), record)
    except BaseException:
        if wrote_tag:
            try:
                (target / CACHEDIR_TAG_NAME).unlink()
            except OSError:
                pass
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
        "pinned": False,
        "pinned_until": None,
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
    record.update(_usage_record(usage))
    record.update({"tmpfs": tmpfs_for(target)})
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
    """Move ``last_used_at`` to now without changing state or usage.

    A touch is the use-recency signal for LRU collection.  It never clears a
    pin and never removes or releases a lease.
    """
    root_path = Path(root).resolve()
    record = _read_record(root_path, _validate_id(entry_id))
    record["last_used_at"] = int(time.time())
    _write_json(_record_path(root_path, entry_id), record)
    return _entry(root_path, record)


def pin(root: str | Path, entry_id: str,
        until: int | float | None = None) -> dict[str, Any]:
    """Pin an entry so ``clean`` and ``gc`` never remove it.

    ``until`` is optional metadata (an epoch second); the pin itself lasts
    until :func:`unpin` is called, so a stale ``pinned_until`` does not make an
    entry deletable.  Re-registering or touching an entry preserves its pin.
    """
    root_path = Path(root).resolve()
    entry_id = _validate_id(entry_id)
    record = _read_record(root_path, entry_id)
    if until is not None:
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise TempSpaceError("pin until must be an epoch second or None")
        if until < 0:
            raise TempSpaceError("pin until must not be negative")
        until = int(until)
    record["pinned"] = True
    record["pinned_until"] = until
    _write_json(_record_path(root_path, entry_id), record)
    return _entry(root_path, record)


def unpin(root: str | Path, entry_id: str) -> dict[str, Any]:
    """Remove an entry's pin."""
    root_path = Path(root).resolve()
    entry_id = _validate_id(entry_id)
    record = _read_record(root_path, entry_id)
    record["pinned"] = False
    record["pinned_until"] = None
    _write_json(_record_path(root_path, entry_id), record)
    return _entry(root_path, record)


def list_entries(root: str | Path, *, kind: str | None = None,
                 session: str | None = None, owned: bool | None = None,
                 include_all: bool = False,
                 sort: str = DEFAULT_LIST_SORT) -> list[dict[str, Any]]:
    """Entries with their current usage.

    The default order is created-at ascending (then id), the historical
    order.  ``sort`` also accepts ``allocated``, ``apparent``, ``files`` and
    ``inodes`` (largest first) or ``age`` (least recently used first).
    """
    root_path = Path(root).resolve()
    if sort not in LIST_SORT_KEYS:
        raise TempSpaceError(
            "sort must be one of " + ", ".join(LIST_SORT_KEYS))
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
    result.sort(key=lambda item: _list_sort_key(item, sort))
    return result


def _list_sort_key(entry: Mapping[str, Any],
                   sort: str) -> tuple[Any, ...]:
    created = int(entry.get("created_at") or 0)
    ident = str(entry.get("id") or "")
    if sort == "created":
        return (created, ident)
    if sort == "age":
        used = int(entry.get("last_used_at") or 0)
        return (used, created, ident)
    field = {"allocated": "bytes", "apparent": "apparent_bytes",
             "files": "files", "inodes": "inodes"}[sort]
    value = entry.get(field)
    if not isinstance(value, int):
        value = 0
    return (-value, created, ident)


def status(root: str | Path, *, top: int | None = None) -> dict[str, Any]:
    """Totals by kind/session/ownership plus capacity for each filesystem.

    ``bytes`` in every bucket is allocated space.  ``apparent_bytes`` is the
    sum of ``st_size``.  ``top`` (when not ``None``) adds the ``N`` largest
    entries by allocated bytes.
    """
    if top is not None and top < 0:
        raise TempSpaceError("top must not be negative")
    root_path = Path(root).resolve()
    errors: list[str] = []
    entries = [_entry(root_path, record)
               for record in _load_records(root_path, errors=errors)]

    def amount(entry: Mapping[str, Any], field: str) -> int:
        value = entry.get(field)
        return value if isinstance(value, int) else 0

    by_kind: dict[str, dict[str, int]] = {}
    by_session: dict[str, dict[str, int]] = {}
    by_ownership = {
        "owned": {"entries": 0, "bytes": 0, "apparent_bytes": 0,
                  "files": 0, "inodes": 0},
        "unowned": {"entries": 0, "bytes": 0, "apparent_bytes": 0,
                    "files": 0, "inodes": 0},
    }
    totals = {"entries": len(entries), "bytes": 0, "apparent_bytes": 0,
              "files": 0, "inodes": 0, "hardlinks": 0,
              "pinned": 0, "leased": 0,
              "partial": 0, "unknown": 0, "cross_device": 0}
    filesystems: dict[int, dict[str, Any]] = {}
    for entry in entries:
        size = amount(entry, "bytes")
        apparent = amount(entry, "apparent_bytes")
        count = amount(entry, "files")
        inodes = amount(entry, "inodes")
        totals["bytes"] += size
        totals["apparent_bytes"] += apparent
        totals["files"] += count
        totals["inodes"] += inodes
        totals["hardlinks"] += amount(entry, "hardlinks")
        if entry.get("pinned"):
            totals["pinned"] += 1
        if entry.get("leased"):
            totals["leased"] += 1
        if entry["walk"] == "partial":
            totals["partial"] += 1
        if entry.get("unknown"):
            totals["unknown"] += 1
        if entry.get("cross_device"):
            totals["cross_device"] += 1
        bucket = by_kind.setdefault(
            entry["kind"], {"entries": 0, "bytes": 0, "apparent_bytes": 0,
                            "files": 0, "inodes": 0,
                            "owned": 0, "unowned": 0})
        bucket["entries"] += 1
        bucket["bytes"] += size
        bucket["apparent_bytes"] += apparent
        bucket["files"] += count
        bucket["inodes"] += inodes
        bucket["owned" if entry["owned"] else "unowned"] += 1
        label = _session_label(entry)
        session_bucket = by_session.setdefault(
            label, {"entries": 0, "bytes": 0, "apparent_bytes": 0,
                    "files": 0, "inodes": 0, "harness": None})
        session_bucket["entries"] += 1
        session_bucket["bytes"] += size
        session_bucket["apparent_bytes"] += apparent
        session_bucket["files"] += count
        session_bucket["inodes"] += inodes
        if entry["session"] is not None:
            session_bucket["harness"] = entry["session"].get("harness")
        ownership = by_ownership["owned" if entry["owned"] else "unowned"]
        ownership["entries"] += 1
        ownership["bytes"] += size
        ownership["apparent_bytes"] += apparent
        ownership["files"] += count
        ownership["inodes"] += inodes
        if entry["state"] == "missing":
            continue
        path = Path(entry["path"])
        statvfs = getattr(os, "statvfs", None)
        if statvfs is None:
            continue
        try:
            device = os.stat(path).st_dev
            usage = statvfs(path)
        except OSError:
            continue
        if device not in filesystems:
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
            used = max(0, total - free)
            percent = round(used * 100.0 / total, 1) if total else None
            inode_total = int(getattr(usage, "f_files", 0) or 0)
            inode_free = int(getattr(usage, "f_ffree", 0) or 0)
            inode_used = max(0, inode_total - inode_free)
            inode_percent = (round(inode_used * 100.0 / inode_total, 1)
                             if inode_total else None)
            is_tmpfs = entry.get("tmpfs")
            warning = None
            if total and free * 10 < total:
                warning = f"free space below 10% ({free} of {total} bytes)"
            elif is_tmpfs and percent is not None and percent > 80.0:
                warning = f"tmpfs above 80% used ({percent}%)"
            inode_warning = None
            if inode_total and inode_free * 10 < inode_total:
                inode_warning = (
                    "free inodes below 10% "
                    f"({inode_free} of {inode_total} inodes)")
            filesystems[device] = {
                "device": device,
                "path": str(path),
                "total_bytes": total,
                "free_bytes": free,
                "used_bytes": used,
                "percent": percent,
                "total_inodes": inode_total,
                "free_inodes": inode_free,
                "inode_percent": inode_percent,
                "tmpfs": is_tmpfs,
                "warning": warning,
                "inode_warning": inode_warning,
            }
    warnings = []
    for filesystem in filesystems.values():
        if filesystem["warning"]:
            warnings.append(filesystem["warning"])
        if filesystem["inode_warning"]:
            warnings.append(filesystem["inode_warning"])
    payload = {
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
    if top is not None:
        ranked = sorted(entries, key=lambda item: _list_sort_key(item,
                                                                 "allocated"))
        rows = []
        for entry in ranked[:top]:
            rows.append({
                "id": entry["id"],
                "kind": entry["kind"],
                "session": entry["session"],
                "session_label": _session_label(entry),
                "reason": entry["reason"],
                "path": entry["path"],
                "state": entry["state"],
                "bytes": amount(entry, "bytes"),
                "allocated_bytes": amount(entry, "bytes"),
                "apparent_bytes": amount(entry, "apparent_bytes"),
                "files": amount(entry, "files"),
                "inodes": amount(entry, "inodes"),
            })
        payload["top"] = rows
    return payload


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
        if stat.S_ISDIR(info.st_mode) and _has_cachedir_tag(path):
            # A tagged cache is not a legacy artifact.  Only the tag file was
            # read; the cache tree itself is not walked.
            seen.add(rendered)
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
        "apparent_bytes": sum(item["apparent_bytes"] for item in artifacts),
        "files": sum(item["files"] for item in artifacts),
        "inodes": sum(item["inodes"] for item in artifacts),
        "hardlinks": sum(item["hardlinks"] for item in artifacts),
        "partial": sum(1 for item in artifacts if item["walk"] == "partial"),
        "unknown": sum(1 for item in artifacts if item["unknown"]),
        "cross_device": sum(1 for item in artifacts
                            if item.get("cross_device")),
    }
    return {
        "root": str(root_path),
        "scan_roots": [str(path) for path in scan_roots],
        "patterns": list(AUDIT_PATTERNS),
        "excluded_roots": [str(path) for path in _audit_excluded_roots()],
        "count": len(artifacts),
        "bytes": totals["bytes"],
        "apparent_bytes": totals["apparent_bytes"],
        "files": totals["files"],
        "inodes": totals["inodes"],
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
        if entry.get("leased"):
            raise TempPathError(
                f"refusing to delete {entry['path']}: leased")
        safe, why = _delete_guard(root_path, entry)
        if not safe:
            if why == "missing":
                record["state"] = "missing"
                record["last_used_at"] = int(time.time())
                _write_json(_record_path(root_path, entry_id), record)
                return _entry(root_path, record)
            raise TempPathError(f"refusing to delete {entry['path']}: {why}")
        _delete_tree(_guarded_delete_path(entry))
        record.update(_cleared_usage_record())
        record.update({"state": "released", "tmpfs": None,
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
    identities: dict[str, tuple[int, int] | None] = {}
    for entry in entries:
        guards[entry["id"]] = _delete_guard(root_path, entry)
        identities[entry["id"]] = _path_identity(entry["path"])

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
            if (entry["id"] not in selected or not safe
                    or entry.get("pinned") or entry.get("leased")):
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
        if entry.get("pinned"):
            kept.append(_clean_row(entry, action="keep", why="pinned"))
            continue
        if entry.get("leased"):
            kept.append(_clean_row(entry, action="keep", why="leased"))
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
        fresh, changed = _clean_verify(
            root_path, entry, identities[entry["id"]])
        if changed is not None:
            kept.append(_clean_row(entry, action="keep", why=changed))
            if changed == "missing":
                record = dict(_load_single(root_path, entry["id"]))
                record["state"] = "missing"
                _write_json(_record_path(root_path, entry["id"]), record)
            continue
        assert fresh is not None
        try:
            _delete_tree(_guarded_delete_path(fresh))
        except OSError as exc:
            kept.append(_clean_row(entry, action="keep",
                                   why=f"delete-failed: {exc.strerror or exc}"))
            continue
        record = dict(_load_single(root_path, entry["id"]))
        record.update(_cleared_usage_record())
        record.update({"state": "released", "tmpfs": None,
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
            "apparent_bytes": sum(row["apparent_bytes"]
                                  for row in candidates),
            "files": sum(row["files"] for row in candidates),
            "inodes": sum(row["inodes"] for row in candidates),
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
        "apparent_bytes": sum(row["apparent_bytes"] for row in candidates),
        "files": sum(row["files"] for row in candidates),
        "inodes": sum(row["inodes"] for row in candidates),
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


def _gc_sort_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """Released/expired, then cache, then LRU, then largest."""
    state = str(entry.get("state") or "active")
    state_rank = 0 if state in ("released", "expired") else 1
    cache_rank = 0 if entry.get("kind") == "cache" else 1
    used = entry.get("last_used_at")
    used = used if isinstance(used, int) else 0
    size = entry.get("bytes")
    size = size if isinstance(size, int) else 0
    inodes = entry.get("inodes")
    inodes = inodes if isinstance(inodes, int) else 0
    return (state_rank, cache_rank, used, -size, -inodes, str(entry.get("id") or ""))


def _gc_block_reason(entry: Mapping[str, Any], root: Path) -> str | None:
    if entry.get("pinned"):
        return "pinned"
    if entry.get("leased"):
        return "leased"
    safe, why = _delete_guard(root, entry)
    return None if safe else why


def _gc_verify(root: Path, entry: Mapping[str, Any], *,
               identity: tuple[int, int] | None = None,
               check_identity: bool = False
               ) -> tuple[dict[str, Any] | None, str | None]:
    """Re-read one candidate immediately before planning or deleting it."""
    try:
        record = _load_single(root, str(entry["id"]))
    except TempSpaceError:
        return None, "unreadable-record"
    fresh = _entry(root, record)
    if check_identity:
        problem = _identity_recheck(fresh["path"], identity)
        if problem is not None:
            return fresh, problem
    reason = _gc_block_reason(fresh, root)
    return fresh, reason


def _gc_min_free(root: Path, entries: Iterable[Mapping[str, Any]]) -> int | None:
    """The lowest free-byte count among the managed/entry filesystems."""
    candidates: list[Path] = [root]
    for entry in entries:
        if entry.get("state") != "missing":
            candidates.append(Path(str(entry.get("path") or "")))
    values = [value for value in (_free_bytes(path) for path in candidates)
              if value is not None]
    return min(values) if values else None


def gc(root: str | Path, *, budget: TempBudget | None = None,
       apply: bool = False,
       older_than_s: int | float | None = None) -> dict[str, Any]:
    """Plan (and with ``apply=True`` perform) budget-driven garbage collection.

    Candidates are ordered released/expired first, then ``cache`` kind, then
    least-recently-used, then largest.  A budget stops the selection once the
    total allocated bytes/inodes (and any per-session or minimum-free limit)
    are satisfied.  Only owned, marker-matched, unpinned, unleased entries are
    ever deleted; unowned, mismatched, symlinked, missing and protected paths
    are reported as kept.
    """
    root_path = Path(root).resolve()
    if older_than_s is not None and older_than_s < 0:
        raise TempSpaceError("older_than_s must not be negative")
    if budget is not None and not isinstance(budget, TempBudget):
        raise TempSpaceError("budget must be a TempBudget or None")
    now = int(time.time())
    records = _load_records(root_path)
    entries = [_entry(root_path, record) for record in records]
    by_id = {entry["id"]: entry for entry in entries}
    ordered = sorted(entries, key=_gc_sort_key)
    has_budget = budget is not None and any(
        getattr(budget, field) is not None
        for field in ("max_total_bytes", "max_inodes", "max_session_bytes",
                      "max_session_inodes", "min_free_bytes"))

    total_bytes = sum(value for value in
                      (entry.get("bytes") for entry in entries)
                      if isinstance(value, int))
    total_inodes = sum(value for value in
                       (entry.get("inodes") for entry in entries)
                       if isinstance(value, int))
    session_totals: dict[tuple[str, str], list[int]] = {}
    for entry in entries:
        key = _session_identity(entry)
        if key is None:
            continue
        bucket = session_totals.setdefault(key, [0, 0])
        size = entry.get("bytes")
        inodes = entry.get("inodes")
        bucket[0] += size if isinstance(size, int) else 0
        bucket[1] += inodes if isinstance(inodes, int) else 0

    free_start = (_gc_min_free(root_path, entries)
                  if budget is not None and budget.min_free_bytes is not None
                  else None)
    min_free_deficit = 0
    if (free_start is not None and budget is not None
            and budget.min_free_bytes is not None):
        min_free_deficit = max(0, budget.min_free_bytes - free_start)

    blocked: dict[str, str] = {}
    too_recent: set[str] = set()
    for entry in entries:
        reason = _gc_block_reason(entry, root_path)
        if reason is not None:
            blocked[entry["id"]] = reason
        if (older_than_s is not None
                and int(entry.get("last_used_at") or 0)
                > now - int(older_than_s)):
            too_recent.add(entry["id"])

    selected: list[str] = []
    selected_set: set[str] = set()
    reclaimed = 0
    if not has_budget:
        for entry in ordered:
            entry_id = entry["id"]
            if entry_id in too_recent or entry_id in blocked:
                continue
            if older_than_s is None and entry.get("state") not in (
                    "released", "expired"):
                continue
            selected.append(entry_id)
            selected_set.add(entry_id)
    else:
        max_total = budget.max_total_bytes if budget is not None else None
        max_inodes = budget.max_inodes if budget is not None else None
        max_session_bytes = (budget.max_session_bytes
                             if budget is not None else None)
        max_session_inodes = (budget.max_session_inodes
                              if budget is not None else None)
        for entry in ordered:
            entry_id = entry["id"]
            if entry_id in too_recent or entry_id in blocked:
                continue
            need = ((max_total is not None and total_bytes > max_total)
                    or (max_inodes is not None
                        and total_inodes > max_inodes)
                    or (min_free_deficit > 0 and reclaimed < min_free_deficit))
            key = _session_identity(entry)
            if (key is not None and not need
                    and (max_session_bytes is not None
                         or max_session_inodes is not None)):
                session_bytes, session_inodes = session_totals.get(key, [0, 0])
                need = ((max_session_bytes is not None
                         and session_bytes > max_session_bytes)
                        or (max_session_inodes is not None
                            and session_inodes > max_session_inodes))
            if not need:
                continue
            selected.append(entry_id)
            selected_set.add(entry_id)
            size = entry.get("bytes")
            inodes = entry.get("inodes")
            size = size if isinstance(size, int) else 0
            inodes = inodes if isinstance(inodes, int) else 0
            total_bytes -= size
            total_inodes -= inodes
            reclaimed += size
            if key is not None:
                bucket = session_totals.setdefault(key, [0, 0])
                bucket[0] -= size
                bucket[1] -= inodes

    deleted: list[str] = []
    errors: list[str] = []
    failure_reasons: dict[str, str] = {}
    delete_order: list[str] = list(selected)
    identities: dict[str, tuple[int, int] | None] = {
        entry_id: _path_identity(by_id[entry_id]["path"])
        for entry_id in selected if entry_id in by_id}
    if apply:
        for entry_id in list(selected):
            entry = by_id.get(entry_id)
            if entry is None:
                continue
            fresh, reason = _gc_verify(
                root_path, entry, identity=identities.get(entry_id),
                check_identity=True)
            if reason is not None:
                failure_reasons[entry_id] = reason
                continue
            assert fresh is not None
            try:
                _delete_tree(_guarded_delete_path(fresh))
            except OSError as exc:
                detail = exc.strerror or str(exc)
                failure_reasons[entry_id] = f"delete-failed: {detail}"
                errors.append(f"{entry_id}: {detail}")
                continue
            record = dict(_load_single(root_path, entry_id))
            record.update(_cleared_usage_record())
            record.update({"state": "released", "tmpfs": None,
                           "last_used_at": int(time.time())})
            _write_json(_record_path(root_path, entry_id), record)
            deleted.append(entry_id)
        # A budget may still be violated after the planned deletions (for
        # example because free space was lower than the estimate).  Continue
        # in the same order, never touching a blocked entry.
        if (budget is not None and budget.min_free_bytes is not None):
            free_now = _gc_min_free(root_path, entries)
            if free_now is not None and free_now < budget.min_free_bytes:
                for entry in ordered:
                    entry_id = entry["id"]
                    if entry_id in selected_set or entry_id in deleted:
                        continue
                    if entry_id in too_recent:
                        continue
                    reason = _gc_block_reason(entry, root_path)
                    if reason is not None:
                        failure_reasons.setdefault(entry_id, reason)
                        continue
                    fresh, reason = _gc_verify(
                        root_path, entry,
                        identity=_path_identity(entry["path"]),
                        check_identity=True)
                    if reason is not None:
                        failure_reasons[entry_id] = reason
                        continue
                    assert fresh is not None
                    try:
                        _delete_tree(_guarded_delete_path(fresh))
                    except OSError as exc:
                        detail = exc.strerror or str(exc)
                        failure_reasons[entry_id] = f"delete-failed: {detail}"
                        errors.append(f"{entry_id}: {detail}")
                        continue
                    record = dict(_load_single(root_path, entry_id))
                    record.update(_cleared_usage_record())
                    record.update({"state": "released", "tmpfs": None,
                                   "last_used_at": int(time.time())})
                    _write_json(_record_path(root_path, entry_id), record)
                    deleted.append(entry_id)
                    delete_order.append(entry_id)
                    free_now = _gc_min_free(root_path, entries)
                    if (free_now is None
                            or free_now >= budget.min_free_bytes):
                        break

    deleted_set = set(deleted) if apply else selected_set
    if apply:
        candidates = [_clean_row(by_id[entry_id], action="deleted",
                                 why="owned-marker-matched")
                      for entry_id in delete_order
                      if entry_id in deleted_set and entry_id in by_id]
    else:
        candidates = [_clean_row(by_id[entry_id], action="delete",
                                 why="eligible")
                      for entry_id in selected if entry_id in by_id]
    kept: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = entry["id"]
        if entry_id in deleted_set:
            continue
        if entry_id in failure_reasons:
            why = failure_reasons[entry_id]
        elif entry_id in blocked:
            why = blocked[entry_id]
        elif entry_id in too_recent:
            why = "too-recent"
        else:
            why = "not-selected"
        kept.append(_clean_row(entry, action="keep", why=why))

    candidate_bytes = sum(row["bytes"] for row in candidates)
    candidate_inodes = sum(row["inodes"] for row in candidates)
    before_bytes = sum(value for value in
                       (entry.get("bytes") for entry in entries)
                       if isinstance(value, int))
    before_inodes = sum(value for value in
                        (entry.get("inodes") for entry in entries)
                        if isinstance(value, int))
    return {
        "root": str(root_path),
        "dry_run": not apply,
        "budget": _budget_payload(budget),
        "older_than_s": (int(older_than_s)
                         if older_than_s is not None else None),
        "min_free": {
            "minimum": (budget.min_free_bytes
                        if budget is not None else None),
            "current": free_start,
            "violated": bool(min_free_deficit),
        },
        "totals": {
            "candidates": len(candidates),
            "deleted": len(deleted),
            "kept": len(kept),
            "bytes": candidate_bytes,
            "apparent_bytes": sum(row["apparent_bytes"]
                                  for row in candidates),
            "files": sum(row["files"] for row in candidates),
            "inodes": candidate_inodes,
            "errors": len(errors),
            "before_bytes": before_bytes,
            "before_inodes": before_inodes,
            "after_bytes": (max(0, before_bytes - candidate_bytes)
                            if apply else before_bytes),
            "after_inodes": (max(0, before_inodes - candidate_inodes)
                             if apply else before_inodes),
        },
        "candidates": candidates,
        "kept": kept,
        "deleted": deleted,
        "errors": errors,
    }



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
    entry["pinned"] = bool(entry.get("pinned", False))
    entry["pinned_until"] = entry.get("pinned_until")
    entry.setdefault("leased", False)
    path = Path(str(entry["path"]))
    state = str(entry.get("state", "active"))
    existing = _safe_lstat(path)
    if state == "active" and (existing is None
                              or not stat.S_ISDIR(existing.st_mode)):
        state = "missing"
    entry["state"] = state
    if existing is None or not stat.S_ISDIR(existing.st_mode):
        entry.update(_cleared_usage_record())
        entry.update({"unknown": False, "errors": 0, "tmpfs": None,
                      "marker": False, "leased": False})
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
        entry.update(_cleared_usage_record())
        entry.update({"unknown": False, "errors": 0, "tmpfs": None,
                      "marker": False, "leased": False})
        return entry
    usage = walk_usage(path)
    entry.update({
        "bytes": int(usage["allocated_bytes"]),
        "apparent_bytes": int(usage["apparent_bytes"]),
        "files": int(usage["files"]),
        "inodes": int(usage["inodes"]),
        "hardlinks": int(usage["hardlinks"]),
        "allocation": usage["allocation"],
        "walk": usage["walk"],
        "unknown": bool(usage["unknown"]),
        "errors": int(usage["errors"]),
        "cross_device": bool(usage.get("cross_device")),
    })
    entry["tmpfs"] = tmpfs_for(path)
    entry["marker"] = _marker_matches(path, str(entry["id"]))
    entry["leased"] = _lease_held_path(path)
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
        usage = _empty_usage(inodes=1)
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
        remaining = max(0.0, deadline - time.monotonic())
        usage = walk_usage(path, max_seconds=remaining)
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
        remaining = max(0.0, deadline - time.monotonic())
        usage = walk_usage(path, max_seconds=remaining)
    else:
        kind = "other"
        usage = _empty_usage()
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
        "bytes": usage["allocated_bytes"],
        "apparent_bytes": usage["apparent_bytes"],
        "files": usage["files"],
        "inodes": usage["inodes"],
        "hardlinks": usage["hardlinks"],
        "allocation": usage["allocation"],
        "cross_device": bool(usage.get("cross_device")),
        "walk": usage["walk"],
        "unknown": usage["unknown"],
        "errors": usage["errors"],
        "mtime": mtime,
        "st_dev": int(info.st_dev),
        "st_ino": int(info.st_ino),
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


def _clean_verify(root: Path, entry: Mapping[str, Any],
                  identity: tuple[int, int] | None
                  ) -> tuple[dict[str, Any] | None, str | None]:
    """Re-check one planned candidate immediately before deletion.

    The registry record, pin, lease, marker id and the planned directory
    identity are all re-read so a path swapped between plan and apply is kept.
    """
    try:
        record = _load_single(root, str(entry["id"]))
    except TempSpaceError:
        return None, "unreadable-record"
    fresh = _entry(root, record)
    if fresh.get("pinned"):
        return fresh, "pinned"
    if fresh.get("leased"):
        return fresh, "leased"
    problem = _identity_recheck(fresh["path"], identity)
    if problem is not None:
        return fresh, problem
    safe, why = _delete_guard(root, fresh)
    if not safe:
        return fresh, why
    return fresh, None


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
        "apparent_bytes": (entry["apparent_bytes"]
                           if isinstance(entry.get("apparent_bytes"), int)
                           else 0),
        "files": entry["files"] if isinstance(entry["files"], int) else 0,
        "inodes": entry["inodes"] if isinstance(entry.get("inodes"), int) else 0,
        "hardlinks": (entry["hardlinks"]
                      if isinstance(entry.get("hardlinks"), int) else 0),
        "allocation": entry.get("allocation"),
        "marker": bool(entry.get("marker")),
        "pinned": bool(entry.get("pinned")),
        "leased": bool(entry.get("leased")),
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
    owner = item.get("registered_entry")
    if owner is not None and lease_held(root, str(owner)):
        return "keep", "leased"
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
        if _has_cachedir_tag(path):
            return "keep", "cache-tag"
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
    expected_dev = row.get("st_dev")
    expected_ino = row.get("st_ino")
    if isinstance(expected_dev, int) and isinstance(expected_ino, int):
        if (int(info.st_dev), int(info.st_ino)) != (expected_dev, expected_ino):
            return "changed"
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
        "apparent_bytes": int(item.get("apparent_bytes") or 0),
        "files": int(item.get("files") or 0),
        "inodes": int(item.get("inodes") or 0),
        "hardlinks": int(item.get("hardlinks") or 0),
        "age_s": int(item.get("age_s") or 0),
        "mtime": item.get("mtime"),
        "st_dev": item.get("st_dev"),
        "st_ino": item.get("st_ino"),
        "registered": bool(item.get("registered")),
        "registered_ancestor": item.get("registered_ancestor"),
        "action": action,
        "why": why,
    }


def _path_identity(path: str | Path) -> tuple[int, int] | None:
    """The ``(st_dev, st_ino)`` of a directory, or ``None`` if it is not one.

    This is recorded at plan time so a deletion can tell that the planned
    directory was replaced before it is removed.
    """
    info = _safe_lstat(Path(path))
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return (int(info.st_dev), int(info.st_ino))


def _identity_recheck(path: str | Path, expected: tuple[int, int] | None) -> str | None:
    """``changed`` when the path is no longer the planned directory."""
    info = _safe_lstat(Path(path))
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "changed"
    if expected is None:
        return "changed"
    if (int(info.st_dev), int(info.st_ino)) != expected:
        return "changed"
    return None


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


def _write_cachedir_tag(path: Path) -> None:
    """Write a conventional ``CACHEDIR.TAG`` (mode 0644) into a cache entry."""
    tag = Path(path) / CACHEDIR_TAG_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(tag, flags, 0o644)
    try:
        os.fchmod(descriptor, 0o644)
        data = CACHEDIR_TAG_CONTENT.encode("utf-8")
        while data:
            written = os.write(descriptor, data)
            if written <= 0:
                raise TempSpaceError(f"cannot write cache tag {tag}")
            data = data[written:]
    finally:
        os.close(descriptor)


def _has_cachedir_tag(path: str | Path) -> bool:
    """Whether a directory carries the conventional cache tag.

    Only the tag file is inspected; the directory is not walked and a
    symlinked tag is not followed.
    """
    tag = Path(path) / CACHEDIR_TAG_NAME
    try:
        info = os.lstat(tag)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(tag, flags)
    except OSError:
        return False
    try:
        data = os.read(descriptor, len(CACHEDIR_TAG_SIGNATURE))
    except OSError:
        return False
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    return data == CACHEDIR_TAG_SIGNATURE.encode("utf-8")


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
    "AGENT_DIRNAME", "ALLOCATION_APPARENT_FALLBACK", "ALLOCATION_BLOCKS",
    "ALLOCATION_BLOCK_SIZE", "AUDIT_MAX_ARTIFACTS", "AUDIT_MAX_SECONDS",
    "AUDIT_PATTERNS", "CACHEDIR_TAG_CONTENT", "CACHEDIR_TAG_NAME",
    "CACHEDIR_TAG_SIGNATURE", "DEFAULT_KIND", "DEFAULT_LIST_SORT",
    "DEFAULT_SCRATCH_KIND", "ENV_MAX_INODES", "ENV_MAX_TOTAL_BYTES",
    "ENV_MIN_FREE", "KINDS", "LEASE_NAME", "LEGACY_CACHE_PATTERNS",
    "LEGACY_DEFAULT_OLDER_THAN_S", "LEGACY_DIR_PATTERNS",
    "LEGACY_FILE_PATTERNS", "LEGACY_SOCKET_TIMEOUT_S", "LIST_SORT_KEYS",
    "MARKER_NAME", "MAX_REASON", "MAX_TTL_S", "MAX_WALK_DEPTH",
    "MAX_WALK_ENTRIES", "MAX_WALK_SECONDS", "MIN_TTL_S", "REGISTRY_DIRNAME",
    "STATES", "TMP_DIRNAME", "TempBudget", "TempBudgetError",
    "TempLeaseHeld", "TempMarkerError", "TempNotFound",
    "TempPathError", "TempSpaceError", "VERSION", "acquire_lease", "adopt",
    "agent_root", "allowed_temp_roots", "audit", "clean", "clean_legacy",
    "create", "default_kind_root", "default_scratch_root",
    "ensure_default_root", "gc", "get", "lease_held", "lease_path",
    "list_entries", "make_session", "pin", "prune_registry", "registry_dir",
    "release", "status", "suggest_path", "tmpfs_for", "touch", "unpin",
    "walk_usage", "workspaces_root",
]
