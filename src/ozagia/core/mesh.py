"""Local session mesh: a filesystem registry, bounded messages and leases.

The mesh lets Ozagia sessions that can already see the same repository state
coordinate without a leader or a network.  It is deliberately local
coordination, not an authorization boundary: leases are advisory and a
cooperative peer can ignore them.  Links only describe a 1:1 peer/sub/super
edge; delegations are separate, bounded records and a link alone never releases
a lease, exposes a mailbox or authorizes a merge.

Layout (all below ``<state dir>/mesh/``)::

    <mesh_id>/mesh.json       scope guard: schema, repo_id, mesh_id
    <mesh_id>/sessions/       one JSON registration per session key
    <mesh_id>/leases/         one JSON record per lease; ``.lock`` serializes
    <mesh_id>/mail/<sk>/      one JSON envelope per delivered message
    <mesh_id>/links/          one JSON record per 1:1 link handshake
    <mesh_id>/delegations/    one JSON record per bounded delegation
    <mesh_id>/audit/          bounded, redacted event records (no bodies)
    <mesh_id>/rate/           reserved for rate limits

Every record carries ``repo_id`` and ``mesh_id``.  A read uses a record only
when both match the current scope; a mismatch is reported, never merged.
Directories are 0700 and files 0600.  Writes use a same-directory
``mkstemp``/``fsync``/``os.replace``; lease, link and delegation mutations
serialize under ``flock`` on ``leases/.lock``.  No path is followed through a
symlink, and nothing outside this one state directory and mesh id is scanned.

Liveness uses ``/proc/<pid>/stat`` start time and the kernel boot id on Linux.
A session is never declared dead just because one signal is old: PID reuse,
missing /proc and contradictory heartbeat/boot evidence report ``stale`` or
``unknown`` instead.  On a non-Linux host ``os.kill`` is only a fallback and a
successful probe reports ``unknown`` rather than live.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import ENV_STATE_DIR, STATE_DIRNAME, load_config_section, state_dir
from ozagia.core import security as SEC
from ozagia.core import worktree as W

SCHEMA = 1
MESH_DIRNAME = "mesh"
MESH_FILE = "mesh.json"
SESSIONS_DIR = "sessions"
LEASES_DIR = "leases"
MAIL_DIR = "mail"
LINKS_DIR = "links"
DELEGATIONS_DIR = "delegations"
AUDIT_DIR = "audit"
RATE_DIR = "rate"
LEASE_LOCK = ".lock"

DEFAULT_MESH_ID = "default"
MESH_ID_ENV = "OZAGIA_MESH_ID"
MESH_ENABLED_ENV = "OZAGIA_MESH"
MESH_ENV_TRUE = frozenset({"1", "true", "yes", "on"})
MESH_ENV_FALSE = frozenset({"0", "false", "no", "off"})
MESH_CONFIG_KEYS = frozenset({"id", "enabled"})
MESH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
SK_RE = re.compile(r"^sk_[0-9a-f]{24}$")
MSG_RE = re.compile(r"^msg_[0-9a-f]{16}$")
LEASE_RE = re.compile(r"^lease_[0-9a-f]{16}$")
LINK_RE = re.compile(r"^lnk_[0-9a-f]{16}$")
DELEGATION_RE = re.compile(r"^dlg_[0-9a-f]{16}$")

EXPOSURES = ("none", "read", "coordinate", "supervise")
COORDINATE = frozenset({"coordinate", "supervise"})
SOURCES = ("cli", "harness", "host", "model", "other")
KINDS = ("hello", "request", "offer", "accept", "reject", "status", "release",
         "note")
RESOURCE_KINDS = ("worktree", "branch", "merge", "path", "grant", "other")
MODES = ("read", "write", "merge")
RELATIONS = ("peer", "sub", "super")
LINK_STATES = ("pending", "active", "declined", "revoked", "expired", "stale")
DELEGATION_ACTIONS = ("lease_release", "mailbox_read", "merge_on_behalf")

HEARTBEAT_S = 15
STALE_S = 90
DEAD_S = 300

SUBJECT_MAX = 120
BODY_MAX = 1024
MESSAGE_TTL_S = 60 * 60
MESSAGE_TTL_MAX_S = 24 * 60 * 60
MAILBOX_MAX_MESSAGES = 100
MAILBOX_MAX_BYTES = 256 * 1024
MAILBOX_MAX_UNREAD = 50

LEASE_TTL_S = 15 * 60
LEASE_TTL_MAX_S = 24 * 60 * 60
LEASE_MAX_RENEWALS = 8
LEASE_MAX_LIFETIME_S = 4 * 60 * 60
LEASE_QUEUE_MAX = 32
LOCK_TIMEOUT_S = 5.0
MAX_REASON = 300
MAX_HISTORY = 16

LINK_TTL_S = 60 * 60
LINK_TTL_MAX_S = 24 * 60 * 60
DELEGATION_TTL_S = 60 * 60
DELEGATION_TTL_MAX_S = 24 * 60 * 60
DELEGATION_MAX_USES = 100
IDEMPOTENCY_MAX = 128

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SECRET_REF = re.compile(r"(?i)\bsecret\s*=")


class MeshError(RuntimeError):
    """A mesh operation failed for a configuration, IO or schema reason."""

    code = "mesh_error"
    exit_code = 2


class MeshConfigError(MeshError):
    code = "mesh_config"


class MeshScopeError(MeshError):
    """A record or directory belongs to another repository or mesh id."""

    code = "scope_mismatch"


class MeshIOError(MeshError):
    code = "mesh_io"


class MeshSchemaError(MeshError):
    code = "mesh_schema"


class MeshLockError(MeshError):
    code = "lock_timeout"


class MeshRefusal(MeshError):
    """A valid refusal: the caller's request was understood and declined."""

    code = "refused"
    exit_code = 1

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _epoch() -> float:
    return time.time()


def _iso(epoch: float | None = None) -> str:
    stamp = datetime.fromtimestamp(_epoch() if epoch is None else epoch,
                                   tz=timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _bounded_text(value: Any, name: str, limit: int, *, allow_empty: bool = False,
                  controls: bool = False) -> str:
    if not isinstance(value, str):
        raise MeshRefusal(f"{name} must be text", code="invalid_value")
    if not allow_empty and not value:
        raise MeshRefusal(f"{name} must not be empty", code="invalid_value")
    if len(value) > limit:
        raise MeshRefusal(f"{name} exceeds {limit} characters",
                          code="too_large")
    if controls and _CONTROL.search(value):
        raise MeshRefusal(f"{name} contains control characters",
                          code="control_character")
    return value


def _hash_id(prefix: str, text: str, length: int) -> str:
    return f"{prefix}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:length]}"


def _redact(text: str) -> tuple[str, list[str]]:
    """Redact with the security patterns and the optional secrets detector."""
    original = str(text)
    redacted = SEC.redact_text(original)
    labels: set[str] = {
        match.group(1) for match in re.finditer(r"<redacted:([^>]+)>", redacted)
    }
    try:  # The secrets layer is optional; the security redactor always runs.
        from ozagia.core import secrets as secrets_module
        capture = secrets_module.detect_text(redacted, max_secrets=8)
        redacted = capture.text
        for item in capture.secrets:
            labels.add(str(item.label))
        if capture.detected and not capture.secrets:
            labels.add("secret")
    except Exception:
        pass
    return redacted, sorted(labels)


def _safe_name(value: Any, name: str) -> str:
    text = _bounded_text(value, name, 128, controls=True)
    if not NAME_RE.fullmatch(text):
        raise MeshRefusal(f"{name} is not a safe session name",
                          code="invalid_identity")
    return text


# --------------------------------------------------------------------------- #
# scope: repository, mesh id and state layout
# --------------------------------------------------------------------------- #
def _git_text(root: Path, args: Sequence[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=10, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def repo_id(root: str | Path) -> str:
    """A stable repository identity: the git common dir, else the real root."""
    root_p = Path(root).resolve()
    base = _git_text(root_p, ["rev-parse", "--path-format=absolute",
                              "--git-common-dir"])
    if not base:
        relative = _git_text(root_p, ["rev-parse", "--git-common-dir"])
        base = str((root_p / relative).resolve()) if relative else None
    if not base:
        base = _git_text(root_p, ["rev-parse", "--show-toplevel"])
    if not base:
        base = str(root_p)
    try:
        base = str(Path(base).resolve())
    except OSError:
        base = str(root_p)
    return _hash_id("r", base, 16)


def _mesh_values(root: str | Path) -> dict[str, Any]:
    """The validated ``[mesh]`` table from either config file (read-only)."""
    section = load_config_section(Path(root).resolve(), "mesh")
    if section.errors:
        first = section.errors[0]
        raise MeshConfigError(f"{first.path}: {first.message}")
    unknown = sorted(set(section.values) - MESH_CONFIG_KEYS)
    if unknown:
        raise MeshConfigError("unknown key(s) in [mesh]: " + ", ".join(unknown))
    return dict(section.values)


def mesh_id(root: str | Path) -> str:
    """``[mesh] id`` from either config file, else ``default``."""
    env = os.environ.get(MESH_ID_ENV)
    if env:
        return validate_mesh_id(env)
    return validate_mesh_id(_mesh_values(root).get("id", DEFAULT_MESH_ID))


def mesh_enabled(root: str | Path) -> bool:
    """Whether the harness should join the mesh: ``[mesh] enabled``, env first.

    ``OZAGIA_MESH=1`` (or true/yes/on) enables it and ``0``/false/no/off
    disables it.  The default is off; an unknown value or a non-boolean
    ``enabled`` is a configuration error the caller may choose to fail open.
    """
    raw = os.environ.get(MESH_ENABLED_ENV)
    if raw is not None and raw.strip():
        flag = raw.strip().lower()
        if flag in MESH_ENV_TRUE:
            return True
        if flag in MESH_ENV_FALSE:
            return False
        raise MeshConfigError(
            f"{MESH_ENABLED_ENV} must be 1/0, true/false, yes/no or on/off")
    value = _mesh_values(root).get("enabled", False)
    if not isinstance(value, bool):
        raise MeshConfigError("[mesh] enabled must be true or false")
    return value


def mesh_configured(root: str | Path) -> bool:
    """Whether a ``[mesh]`` table or mesh environment variable exists.

    Read-only and side-effect free: this never creates the state directory.
    """
    if os.environ.get(MESH_ID_ENV) or os.environ.get(MESH_ENABLED_ENV):
        return True
    return bool(_mesh_values(root))


def validate_mesh_id(value: Any) -> str:
    if not isinstance(value, str) or not MESH_ID_RE.fullmatch(value):
        raise MeshConfigError(
            "mesh id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    return value


def session_key(repo: str, mesh: str, harness: str, session: str) -> str:
    return _hash_id("sk", f"{repo}\0{mesh}\0{harness}\0{session}", 24)


def _ensure_dir(path: Path, *, create: bool = True,
                secure: bool = True) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return False
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
    except OSError as exc:
        raise MeshIOError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise MeshScopeError(f"refusing symlinked mesh path: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise MeshIOError(f"not a directory: {path}")
    if secure:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise MeshIOError(f"cannot secure directory {path}: {exc}") from exc
    return True


def _layout(root: str | Path, mid: str | None = None) -> tuple[Path, str, str]:
    root_p = Path(root).resolve()
    repo = repo_id(root_p)
    mesh = mid or mesh_id(root_p)
    base = state_dir(root_p) / MESH_DIRNAME / mesh
    return base, repo, mesh


def _scope_guard(path: Path, repo: str, mesh: str, *, create: bool) -> None:
    if not path.exists():
        if not create:
            return
        _write_json(path, {"schema": SCHEMA, "repo_id": repo,
                           "mesh_id": mesh})
        return
    record = _read_json(path)
    if (record.get("repo_id"), record.get("mesh_id")) != (repo, mesh):
        raise MeshScopeError(
            "mesh state belongs to another repository or mesh id "
            f"({record.get('repo_id')!r}/{record.get('mesh_id')!r}); "
            f"this scope is {repo!r}/{mesh!r}")
    if record.get("schema") != SCHEMA:
        raise MeshSchemaError(f"{path}: unsupported mesh schema")


def _raw_state_path(root: Path) -> Path:
    """The state path before ``config.state_dir`` resolves a symlink."""
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        candidate = Path(env)
        return candidate if candidate.is_absolute() else root / candidate
    return root / STATE_DIRNAME


def _open_base(root: str | Path, *, create: bool,
               mid: str | None = None) -> Path | None:
    root_p = Path(root).resolve()
    base, repo, mesh = _layout(root_p, mid)
    raw_state = _raw_state_path(root_p)
    if raw_state.is_symlink():
        raise MeshScopeError(f"refusing symlinked state directory: {raw_state}")
    state = state_dir(root_p)
    if not state.exists():
        if not create:
            return None
        _ensure_dir(state, create=True, secure=False)
    else:
        _ensure_dir(state, create=False, secure=False)
    mesh_root = state / MESH_DIRNAME
    if not _ensure_dir(mesh_root, create=create, secure=create):
        return None
    if not _ensure_dir(base, create=create, secure=create):
        return None
    if create:
        for name in (SESSIONS_DIR, LEASES_DIR, MAIL_DIR, LINKS_DIR,
                     DELEGATIONS_DIR, AUDIT_DIR, RATE_DIR):
            _ensure_dir(base / name, create=True)
    _scope_guard(base / MESH_FILE, repo, mesh, create=create)
    return base


def mesh_dir(root: str | Path, mid: str | None = None,
             *, create: bool = False) -> Path:
    base = _open_base(root, create=create, mid=mid)
    if base is None:
        return _layout(root, mid)[0]
    return base


# --------------------------------------------------------------------------- #
# safe JSON and locking
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise MeshIOError(f"cannot read {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MeshSchemaError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MeshSchemaError(f"{path}: record must be a JSON object")
    return data


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_dir(path.parent)
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
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class _Lock:
    def __init__(self, path: Path, timeout: float | None = None) -> None:
        self.path = path
        self.timeout = max(0.0, float(LOCK_TIMEOUT_S if timeout is None
                                      else timeout))
        self.descriptor: int | None = None

    def __enter__(self) -> "_Lock":
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - non-POSIX hosts
            raise MeshIOError(
                "mesh leases need a POSIX flock implementation") from exc
        _ensure_dir(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise MeshIOError(f"cannot open lock {self.path}: {exc}") from exc
        try:
            os.fchmod(self.descriptor, 0o600)
        except OSError:
            pass
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN,
                                     errno.EWOULDBLOCK):
                    os.close(self.descriptor)
                    self.descriptor = None
                    raise MeshIOError(
                        f"cannot lock {self.path}: {exc}") from exc
                if time.monotonic() >= deadline:
                    os.close(self.descriptor)
                    self.descriptor = None
                    raise MeshLockError(
                        f"timed out waiting for mesh lock {self.path}")
                time.sleep(0.01)

    def __exit__(self, *exc: object) -> None:
        if self.descriptor is not None:
            try:
                import fcntl
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - descriptor cannot exist
                pass
            finally:
                os.close(self.descriptor)
                self.descriptor = None


def _lease_lock(base: Path) -> _Lock:
    return _Lock(base / LEASES_DIR / LEASE_LOCK, timeout=LOCK_TIMEOUT_S)


def _mesh_lock(base: Path) -> _Lock:
    """Serialize link, delegation and lease mutations under one short flock.

    Links, delegations and leases share invariants (a delegation is only valid
    while a link is active, and releasing a lease consumes a delegation), so a
    single lock keeps them consistent without lock-ordering games.
    """
    return _lease_lock(base)


def _iter_records(directory: Path, repo: str, mesh: str
                  ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda item: item.name)
    except FileNotFoundError:
        return [], errors
    except OSError as exc:
        raise MeshIOError(f"cannot list {directory}: {exc}") from exc
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        if not entry.is_file(follow_symlinks=False):
            errors.append({"name": entry.name, "error": "not a regular file"})
            continue
        try:
            record = _read_json(Path(entry.path))
        except MeshError as exc:
            errors.append({"name": entry.name, "error": exc.code})
            continue
        if (record.get("repo_id"), record.get("mesh_id")) != (repo, mesh):
            errors.append({"name": entry.name, "error": MeshScopeError.code})
            continue
        records.append(record)
    return records, errors


def _check_scope(record: Mapping[str, Any], repo: str, mesh: str,
                 what: str) -> None:
    if (record.get("repo_id"), record.get("mesh_id")) != (repo, mesh):
        raise MeshScopeError(
            f"{what} belongs to another repository or mesh id; "
            f"this scope is {repo}/{mesh}")


# --------------------------------------------------------------------------- #
# process and liveness evidence
# --------------------------------------------------------------------------- #
def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8").strip() or None
    except OSError:
        return None


def _proc_start_evidence(pid: int) -> tuple[str | None, str]:
    """``(starttime, evidence)`` where evidence is found/missing/unknown."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "unknown"
    except OSError:
        return None, "unknown"
    close = raw.rfind(")")
    if close < 0:
        return None, "unknown"
    fields = raw[close + 1:].split()
    # field 22 overall; after the comm field, starttime is index 19.
    if len(fields) <= 19:
        return None, "unknown"
    return fields[19], "found"


def _proc_start(pid: int) -> str | None:
    start, evidence = _proc_start_evidence(pid)
    return start if evidence == "found" else None


def _pid_state(pid: Any, expected: Any) -> str:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if number <= 0:
        return "unknown"
    expected_text = str(expected) if expected not in (None, "") else ""
    if Path("/proc").is_dir():
        start, evidence = _proc_start_evidence(number)
        if evidence == "missing":
            return "dead"
        if evidence != "found":
            return "unknown"
        if expected_text and start == expected_text:
            return "match"
        if expected_text:
            return "reused"
        return "unknown"
    # Non-Linux fallback: existence only, never a claim of liveness.
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    return "unknown"


def session_liveness(record: Mapping[str, Any], *,
                     now: float | None = None) -> str:
    """``live|stale|dead|ended|foreign|unknown`` for one registration."""
    if record.get("state") == "ended":
        return "ended"
    current_boot = _boot_id()
    recorded_boot = record.get("boot_id")
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return "foreign"
    moment = _epoch() if now is None else float(now)
    heartbeat = _parse_iso(record.get("heartbeat_at"))
    if heartbeat is None:
        return "unknown"
    age = max(0.0, moment - heartbeat)
    pid_state = _pid_state(record.get("pid"), record.get("pid_start"))
    if pid_state == "match":
        return "live" if age <= STALE_S else "stale"
    if pid_state in {"dead", "reused"}:
        return "dead" if age > DEAD_S else "stale"
    return "unknown" if age <= DEAD_S else "stale"


_LIVENESS_RANK = {"live": 0, "stale": 1, "unknown": 2, "dead": 3,
                  "foreign": 4, "ended": 5}


# --------------------------------------------------------------------------- #
# links and delegations
# --------------------------------------------------------------------------- #
_RECIPROCAL = {"peer": "peer", "sub": "super", "super": "sub"}


def _endpoint_key(endpoint: Any) -> str | None:
    if not isinstance(endpoint, Mapping):
        return None
    value = endpoint.get("key")
    return str(value) if value else None


def _session_map(base: Path, repo: str, mesh: str) -> dict[str, dict[str, Any]]:
    records, _ = _sessions(base, repo, mesh)
    return {str(item.get("key")): item for item in records if item.get("key")}


def _link_records(base: Path, repo: str, mesh: str
                  ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _iter_records(base / LINKS_DIR, repo, mesh)


def _delegation_records(base: Path, repo: str, mesh: str
                        ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _iter_records(base / DELEGATIONS_DIR, repo, mesh)


def _relation_from(record: Mapping[str, Any], key: str) -> str:
    """The relation as seen from ``key`` (the other end sees the reciprocal)."""
    relation = str(record.get("relation") or "peer")
    if _endpoint_key(record.get("from")) == key:
        return relation
    if _endpoint_key(record.get("to")) == key:
        return _RECIPROCAL.get(relation, "peer")
    return "peer"


def _peer_endpoint(record: Mapping[str, Any], key: str) -> dict[str, Any]:
    if _endpoint_key(record.get("from")) == key:
        return dict(record.get("to") or {})
    if _endpoint_key(record.get("to")) == key:
        return dict(record.get("from") or {})
    return {}


def _link_expiry(record: Mapping[str, Any]) -> float | None:
    """A pending link expires ``ttl_s`` after creation; an accepted one at
    ``expires_at``.  Nothing is written by this computation."""
    due = _parse_iso(record.get("expires_at"))
    if due is not None:
        return due
    created = _parse_iso(record.get("created_at"))
    ttl = record.get("ttl_s")
    if created is not None and isinstance(ttl, int) and ttl > 0:
        return created + ttl
    return None


def _endpoint_liveness(endpoint: Any, sessions: Mapping[str, Mapping[str, Any]],
                       now: float) -> str:
    key = _endpoint_key(endpoint)
    match = sessions.get(key or "")
    return "missing" if match is None else session_liveness(match, now=now)


def _link_effective(record: Mapping[str, Any],
                    sessions: Mapping[str, Mapping[str, Any]], *,
                    now: float) -> tuple[str, str, str]:
    """Return ``(effective_state, from_liveness, to_liveness)``.

    `declined` and `revoked` are terminal.  Expiry is computed from the record
    and never written by a read.  A link whose either endpoint is stale, dead,
    ended, foreign or missing is reported `stale`, not `dead`.
    """
    from_live = _endpoint_liveness(record.get("from"), sessions, now)
    to_live = _endpoint_liveness(record.get("to"), sessions, now)
    stored = str(record.get("state") or "pending")
    if stored in {"declined", "revoked"}:
        return stored, from_live, to_live
    due = _link_expiry(record)
    if stored == "expired" or (due is not None and now >= due):
        return "expired", from_live, to_live
    if from_live != "live" or to_live != "live":
        return "stale", from_live, to_live
    return ("active" if stored == "active" else "pending"), from_live, to_live


def _link_view(record: Mapping[str, Any],
               sessions: Mapping[str, Mapping[str, Any]], *,
               now: float, viewer: str | None = None) -> dict[str, Any]:
    state, from_live, to_live = _link_effective(record, sessions, now=now)
    due = _link_expiry(record)
    view = dict(record)
    view.pop("idempotency_key", None)
    view["effective_state"] = state
    view["from_liveness"] = from_live
    view["to_liveness"] = to_live
    view["reciprocal_relation"] = _RECIPROCAL.get(
        str(record.get("relation") or "peer"), "peer")
    view["expires_at"] = record.get("expires_at") or (
        _iso(due) if due is not None else None)
    view["expires_in_s"] = (None if due is None
                            else max(0, int(due - now)))
    view["expired"] = state == "expired"
    view["history"] = list(record.get("history") or ())[-MAX_HISTORY:]
    if viewer is not None:
        peer = _peer_endpoint(record, viewer)
        viewer_relation = _relation_from(record, viewer)
        view["viewer_relation"] = viewer_relation
        view["reciprocal_relation"] = _RECIPROCAL.get(viewer_relation, "peer")
        view["peer"] = peer
        view["peer_liveness"] = _endpoint_liveness(
            peer, sessions, now)
        view["direction"] = ("outgoing"
                             if _endpoint_key(record.get("from")) == viewer
                             else "incoming")
    return view


def _link_compact(record: Mapping[str, Any],
                  sessions: Mapping[str, Mapping[str, Any]], *,
                  now: float, viewer: str) -> dict[str, Any]:
    state, _, _ = _link_effective(record, sessions, now=now)
    peer = _peer_endpoint(record, viewer)
    due = _link_expiry(record)
    relation = _relation_from(record, viewer)
    return {
        "link_id": record.get("link_id"),
        "peer": peer,
        "relation": relation,
        "reciprocal_relation": _RECIPROCAL.get(relation, "peer"),
        "state": state,
        "created_at": record.get("created_at"),
        "accepted_at": record.get("accepted_at"),
        "expires_at": record.get("expires_at") or (
            _iso(due) if due is not None else None),
        "expires_in_s": (None if due is None else max(0, int(due - now))),
    }


def _role_from_links(links: Iterable[Mapping[str, Any]]) -> str:
    relations = {str(item.get("relation")) for item in links
                 if item.get("state") == "active"}
    if {"sub", "super"} <= relations:
        return "mixed"
    if "sub" in relations:
        return "sub"
    if "super" in relations:
        return "super"
    return "peer"


def _link_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """A compact status entry: no history, reason or capability payload."""
    relation = item.get("viewer_relation", item.get("relation"))
    return {
        "link_id": item.get("link_id"),
        "peer": item.get("peer") or item.get("to"),
        "from": item.get("from"),
        "to": item.get("to"),
        "relation": relation,
        "reciprocal_relation": _RECIPROCAL.get(str(relation), "peer"),
        "state": item.get("effective_state", item.get("state")),
        "accepted_at": item.get("accepted_at"),
        "expires_at": item.get("expires_at"),
        "expires_in_s": item.get("expires_in_s"),
    }


def _delegation_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """A compact status entry: no history, reason or mailbox contents."""
    return {
        "delegation_id": item.get("delegation_id"),
        "grantor": item.get("grantor"),
        "grantee": item.get("grantee"),
        "link_id": item.get("link_id"),
        "link_state": item.get("link_state"),
        "actions": item.get("actions"),
        "lease_id": item.get("lease_id"),
        "resource": item.get("resource"),
        "mailbox": item.get("mailbox"),
        "scope": item.get("scope"),
        "state": item.get("effective_state", item.get("state")),
        "expires_at": item.get("expires_at"),
        "expires_in_s": item.get("expires_in_s"),
        "max_uses": item.get("max_uses"),
        "used": item.get("used"),
        "remaining_uses": item.get("remaining_uses"),
    }


def _session_link_summaries(base: Path, repo: str, mesh: str, key: str,
                            now: float) -> list[dict[str, Any]]:
    records, _ = _link_records(base, repo, mesh)
    sessions = _session_map(base, repo, mesh)
    summaries = [
        _link_compact(item, sessions, now=now, viewer=key)
        for item in records
        if key in {_endpoint_key(item.get("from")),
                   _endpoint_key(item.get("to"))}
    ]
    summaries.sort(key=lambda item: (str(item.get("created_at")),
                                     str(item.get("link_id"))))
    return summaries


def _delegation_effective(record: Mapping[str, Any],
                          sessions: Mapping[str, Mapping[str, Any]],
                          links: Mapping[str, Mapping[str, Any]], *,
                          now: float) -> tuple[str, str | None]:
    """``(effective_state, reason)``; never writes."""
    if str(record.get("state") or "active") == "revoked":
        return "revoked", None
    due = _parse_iso(record.get("expires_at"))
    if due is not None and now >= due:
        return "expired", "time"
    used = int(record.get("used") or 0)
    maximum = int(record.get("max_uses") or 1)
    if used >= maximum:
        return "expired", "uses"
    link = links.get(str(record.get("link_id") or ""))
    if link is None:
        return "expired", "link_missing"
    link_state, _, _ = _link_effective(link, sessions, now=now)
    if link_state != "active":
        return "expired", f"link_{link_state}"
    return "active", None


def _delegation_scope(record: Mapping[str, Any]) -> str:
    actions = sorted(str(item) for item in record.get("actions") or ())
    parts: list[str] = []
    if "lease_release" in actions:
        parts.append(f"lease_release:{record.get('lease_id') or '-'}")
    if "mailbox_read" in actions:
        parts.append("mailbox_read")
    if "merge_on_behalf" in actions:
        parts.append("merge_on_behalf:reference")
    return ",".join(parts)


def _delegation_view(record: Mapping[str, Any],
                     sessions: Mapping[str, Mapping[str, Any]],
                     links: Mapping[str, Mapping[str, Any]], *,
                     now: float, viewer: str | None = None
                     ) -> dict[str, Any]:
    state, reason = _delegation_effective(record, sessions, links, now=now)
    due = _parse_iso(record.get("expires_at"))
    view = dict(record)
    view["effective_state"] = state
    view["effective_reason"] = reason
    view["expires_in_s"] = (None if due is None else max(0, int(due - now)))
    view["remaining_uses"] = max(
        0, int(record.get("max_uses") or 1) - int(record.get("used") or 0))
    view["link_state"] = (
        _link_effective(links[str(record.get("link_id"))], sessions,
                        now=now)[0]
        if str(record.get("link_id")) in links else None)
    view["scope"] = _delegation_scope(record)
    view["expired"] = state == "expired"
    view["history"] = list(record.get("history") or ())[-MAX_HISTORY:]
    if viewer is not None:
        if _endpoint_key(record.get("grantor")) == viewer:
            view["direction"] = "outgoing"
        elif _endpoint_key(record.get("grantee")) == viewer:
            view["direction"] = "incoming"
        else:
            view["direction"] = None
    return view


def _active_link_between(records: Sequence[Mapping[str, Any]],
                         sessions: Mapping[str, Mapping[str, Any]],
                         first: str, second: str, *, now: float
                         ) -> tuple[Mapping[str, Any] | None, str | None]:
    candidates = [
        item for item in records
        if {_endpoint_key(item.get("from")),
            _endpoint_key(item.get("to"))} == {first, second}
    ]
    candidates.sort(key=lambda item: str(item.get("created_at")),
                    reverse=True)
    latest: tuple[Mapping[str, Any] | None, str | None] = (None, None)
    for item in candidates:
        state, _, _ = _link_effective(item, sessions, now=now)
        if state == "active":
            return item, state
        if latest[0] is None:
            latest = (item, state)
    return latest


def _link_refusal_for(state: str | None) -> tuple[str, str]:
    if state == "pending":
        return "link_pending", "link request is still pending"
    if state == "expired":
        return "link_expired", "link has expired"
    if state == "stale":
        return "link_stale", "link peer is not live"
    return "not_linked", "no active link with that session"


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def _session_path(base: Path, key: str) -> Path:
    return base / SESSIONS_DIR / f"{key}.json"


def _session_view(record: Mapping[str, Any], base: Path, *,
                  now: float | None = None) -> dict[str, Any]:
    view = dict(record)
    effect = session_liveness(record, now=now)
    view["effective_state"] = effect
    heartbeat = _parse_iso(record.get("heartbeat_at"))
    view["heartbeat_age_s"] = (None if heartbeat is None
                               else max(0, int((now or _epoch()) - heartbeat)))
    effect_rank = _LIVENESS_RANK.get(effect, 9)
    unread = 0
    try:
        messages, _ = _iter_records(base / MAIL_DIR / str(record.get("key")),
                                    str(record["repo_id"]),
                                    str(record["mesh_id"]))
        unread = sum(1 for item in messages if not item.get("read_at"))
    except MeshError:
        pass
    leases, _ = _iter_records(base / LEASES_DIR, str(record["repo_id"]),
                              str(record["mesh_id"]))
    active: list[str] = []
    for lease in leases:
        state, _ = _lease_effective(lease, base, now=now)
        if (state in {"active", "stale"} and _lease_holder_key(lease)
                == record.get("key")):
            active.append(str(lease.get("lease_id")))
    view["unread"] = unread
    view["active_leases"] = sorted(active)
    key = str(record.get("key"))
    links = _session_link_summaries(base, str(record["repo_id"]),
                                    str(record["mesh_id"]), key,
                                    now or _epoch())
    view["links"] = links
    view["role"] = _role_from_links(links)
    view["role_source"] = "derived"
    view["is_self"] = False
    view["liveness_rank"] = effect_rank
    return view


def _find_session(base: Path, repo: str, mesh: str, harness: str,
                  session: str) -> dict[str, Any] | None:
    key = session_key(repo, mesh, harness, session)
    path = _session_path(base, key)
    try:
        record = _read_json(path)
    except FileNotFoundError:
        return None
    _check_scope(record, repo, mesh, "session registration")
    if (record.get("harness"), record.get("session")) != (harness, session):
        raise MeshSchemaError(f"{path}: session key does not match identity")
    return record


def _sessions(base: Path, repo: str, mesh: str) -> tuple[list[dict[str, Any]],
                                                        list[dict[str, str]]]:
    return _iter_records(base / SESSIONS_DIR, repo, mesh)


def _require_session(base: Path, repo: str, mesh: str, harness: str,
                     session: str, *, live: bool = False) -> dict[str, Any]:
    record = _find_session(base, repo, mesh, harness, session)
    if record is None:
        raise MeshRefusal("session is not registered", code="unknown_session")
    effect = session_liveness(record)
    if live and effect != "live":
        raise MeshRefusal(
            f"session is {effect}, not live", code="session_not_live")
    return record


def register(root: str | Path, harness: str, session: str, *, pid: int | None = None,
             pid_start: str | None = None, boot_id: str | None = None,
             worktree: str | None = None, branch: str | None = None,
             exposure: str = "none", source: str = "cli",
             capabilities: Iterable[str] | None = None) -> dict[str, Any]:
    """Register a session, idempotently for the same live identity."""
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    if exposure not in EXPOSURES:
        raise MeshRefusal("unknown exposure", code="invalid_exposure")
    if source not in SOURCES:
        raise MeshRefusal("unknown registration source", code="invalid_source")
    number = int(pid if pid is not None else os.getpid())
    if number <= 0:
        raise MeshRefusal("pid must be positive", code="invalid_value")
    start = str(pid_start) if pid_start is not None else _proc_start(number)
    boot = boot_id if boot_id is not None else _boot_id()
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    key = session_key(repo, mesh, harness, session)
    path = _session_path(base, key)
    now = _epoch()
    stamp = _iso(now)
    caps = [str(item)[:120] for item in (capabilities or ())][:32]
    same = False
    try:
        existing = _read_json(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        _check_scope(existing, repo, mesh, "session registration")
        same = (str(existing.get("pid")) == str(number)
                and str(existing.get("pid_start") or "") == str(start or "")
                and (existing.get("boot_id") in (None, "", boot)
                     or boot in (None, "")))
        effect = session_liveness(existing, now=now)
        if not same and effect in {"live", "unknown"}:
            raise MeshRefusal(
                "a different live process already registered this session",
                code="session_already_registered")
    record = {
        "schema": SCHEMA,
        "repo_id": repo,
        "mesh_id": mesh,
        "key": key,
        "harness": harness,
        "session": session,
        "pid": number,
        "pid_start": start,
        "boot_id": boot,
        "started_at": ((existing or {}).get("started_at", stamp)
                       if existing is not None and same else stamp),
        "heartbeat_at": stamp,
        "state": "active",
        "exposure": exposure,
        "exposure_source": source,
        "capabilities": caps,
        "worktree": str(worktree) if worktree else None,
        "branch": str(branch) if branch else None,
        "role": "peer",
        "links": [],
        "unread": 0,
        "active_leases": [],
        "source": source,
    }
    # A re-registration after stale/dead keeps the original start time only
    # when the same process returns; a new process starts a new record.
    if existing is not None and same:
        record["started_at"] = existing.get("started_at", stamp)
    if worktree is None and existing is not None and same:
        record["worktree"] = existing.get("worktree")
    if branch is None and existing is not None and same:
        record["branch"] = existing.get("branch")
    _write_json(path, record)
    _audit(base, "register", key=key, harness=harness, session=session,
           exposure=exposure)
    return _session_view(record, base, now=now)


def heartbeat(root: str | Path, harness: str, session: str, *,
              pid: int | None = None, pid_start: str | None = None,
              boot_id: str | None = None) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    record = _require_session(base, repo, mesh, harness, session)
    if record.get("state") == "ended":
        raise MeshRefusal("session is ended", code="session_ended")
    number = int(pid if pid is not None else record.get("pid") or os.getpid())
    start = (str(pid_start) if pid_start is not None
             else _proc_start(number) if number else None)
    boot = boot_id if boot_id is not None else _boot_id()
    now = _epoch()
    effect = session_liveness(record, now=now)
    if effect != "live":
        record.update(pid=number, pid_start=start, boot_id=boot,
                      state="active")
    record["heartbeat_at"] = _iso(now)
    _write_json(_session_path(base, str(record["key"])), record)
    return _session_view(record, base, now=now)


def unregister(root: str | Path, harness: str, session: str, *,
               release_leases: bool = False) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    record = _require_session(base, repo, mesh, harness, session)
    released: list[str] = []
    if release_leases:
        for lease in lease_list(root, include_all=True)["leases"]:
            holder = lease.get("holder") or {}
            if (lease.get("effective_state") not in {"active", "stale"}):
                continue
            if (holder.get("harness"), holder.get("session")) == (harness,
                                                                  session):
                try:
                    result = lease_release(root, harness, session,
                                           str(lease["lease_id"]))
                    released.extend(
                        item["lease_id"] for item in (result.get("lease"),
                                                      result.get("promoted"))
                        if item)
                except MeshRefusal:
                    continue
        # A queued request is not a held lease: remove this session's own
        # entries so it is not promoted after it has left the mesh.
        key = str(record.get("key"))
        with _lease_lock(base):
            records, _ = _lease_records(base, repo, mesh)
            for item in records:
                queue = list(item.get("queue") or ())
                kept = [entry for entry in queue if entry.get("key") != key]
                if len(kept) == len(queue):
                    continue
                item["queue"] = kept
                item.setdefault("history", []).append({
                    "event": "queue_cancelled", "at": _iso(),
                    "holder": key})
                item["history"] = item["history"][-MAX_HISTORY:]
                _write_json(base / LEASES_DIR
                            / f"{item['lease_id']}.json", item)
    now = _epoch()
    record["state"] = "ended"
    record["ended_at"] = _iso(now)
    record["heartbeat_at"] = record["ended_at"]
    _write_json(_session_path(base, str(record["key"])), record)
    _audit(base, "unregister", key=record.get("key"), released=released)
    view = _session_view(record, base, now=now)
    view["released_leases"] = sorted(set(released))
    return view


def peers(root: str | Path, *, include_all: bool = False) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "peers": [], "errors": [], "counts": {}}
    records, errors = _sessions(base, repo, mesh)
    now = _epoch()
    views = [_session_view(item, base, now=now) for item in records]
    if not include_all:
        views = [item for item in views
                 if item["effective_state"] in {"live", "stale"}]
    views.sort(key=lambda item: (item["harness"], item["session"]))
    counts: dict[str, int] = {}
    for item in views:
        state = str(item["effective_state"])
        counts[state] = counts.get(state, 0) + 1
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "peers": views, "errors": errors, "counts": counts}


def status(root: str | Path, *, harness: str | None = None,
           session: str | None = None) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "sessions": [], "session_counts": {}, "leases": [],
                "lease_counts": {}, "links": [], "link_counts": {},
                "delegations": [], "delegation_counts": {},
                "counts": {}, "errors": []}
    records, errors = _sessions(base, repo, mesh)
    now = _epoch()
    views = [_session_view(item, base, now=now) for item in records]
    me = None
    if harness and session:
        for item in views:
            if (item.get("harness"), item.get("session")) == (harness, session):
                item["is_self"] = True
                me = item
                break
    views.sort(key=lambda item: (item["harness"], item["session"]))
    session_counts: dict[str, int] = {}
    for item in views:
        state = str(item["effective_state"])
        session_counts[state] = session_counts.get(state, 0) + 1
    leases = lease_list(root)
    links = link_list(root, harness=harness, session=session)
    delegations = delegation_list(root, harness=harness, session=session)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "self": me, "sessions": views, "session_counts": session_counts,
            "leases": leases["leases"], "lease_counts": leases["counts"],
            "links": [_link_summary(item) for item in links["links"]],
            "link_counts": links["counts"],
            "delegations": [_delegation_summary(item)
                            for item in delegations["delegations"]],
            "delegation_counts": delegations["counts"],
            "counts": leases["counts"],
            "errors": (errors + leases["errors"] + links["errors"]
                       + delegations["errors"])}


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #
def _clean_reason(reason: str) -> str:
    why, _ = _redact(_bounded_text(reason, "reason", MAX_REASON,
                                   allow_empty=True, controls=True))
    return why


def _coerce_ttl(value: Any, *, default: int, maximum: int,
                name: str = "ttl") -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise MeshRefusal(f"{name} must be an integer", code="invalid_ttl")
    if not 1 <= value <= maximum:
        raise MeshRefusal(f"{name} must be 1..{maximum} seconds",
                          code="invalid_ttl")
    return value


def _split_identity(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or ":" not in value:
        raise MeshRefusal("--to must be HARNESS:SESSION",
                          code="invalid_recipient")
    harness, session = value.split(":", 1)
    return _safe_name(harness, "harness"), _safe_name(session, "session")


def _find_link(base: Path, repo: str, mesh: str, link_id: str
               ) -> tuple[Path, dict[str, Any]]:
    if not LINK_RE.fullmatch(str(link_id)):
        raise MeshRefusal("invalid link id", code="invalid_link_id")
    path = base / LINKS_DIR / f"{link_id}.json"
    try:
        record = _read_json(path)
    except FileNotFoundError:
        raise MeshRefusal("unknown link", code="unknown_link") from None
    _check_scope(record, repo, mesh, "link")
    return path, record


def _find_delegation(base: Path, repo: str, mesh: str, delegation_id: str
                     ) -> tuple[Path, dict[str, Any]]:
    if not DELEGATION_RE.fullmatch(str(delegation_id)):
        raise MeshRefusal("invalid delegation id", code="invalid_delegation_id")
    path = base / DELEGATIONS_DIR / f"{delegation_id}.json"
    try:
        record = _read_json(path)
    except FileNotFoundError:
        raise MeshRefusal("unknown delegation",
                          code="unknown_delegation") from None
    _check_scope(record, repo, mesh, "delegation")
    return path, record


def _link_result(record: Mapping[str, Any], base: Path, *,
                 created: bool, actor_key: str | None) -> dict[str, Any]:
    sessions = _session_map(base, str(record["repo_id"]),
                            str(record["mesh_id"]))
    view = _link_view(record, sessions, now=_epoch(), viewer=actor_key)
    return {"schema": SCHEMA, "repo_id": record["repo_id"],
            "mesh_id": record["mesh_id"], "created": created, "link": view}


def link_request(root: str | Path, harness: str, session: str, *, to: str,
                 relation: str, ttl_s: int = LINK_TTL_S, reason: str = "",
                 idempotency_key: str | None = None) -> dict[str, Any]:
    """Request a 1:1 link.  ``relation`` is from the requester's perspective."""
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    if relation not in RELATIONS:
        raise MeshRefusal("unknown relation", code="invalid_relation")
    ttl = _coerce_ttl(ttl_s, default=LINK_TTL_S, maximum=LINK_TTL_MAX_S)
    target_h, target_s = _split_identity(to)
    key_text = None
    if idempotency_key is not None:
        key_text = str(_bounded_text(idempotency_key, "idempotency key",
                                     IDEMPOTENCY_MAX, controls=True)).strip()
        if not key_text:
            raise MeshRefusal("idempotency key must not be empty",
                              code="invalid_value")
    why = _clean_reason(reason)
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    if (target_h, target_s) == (harness, session):
        raise MeshRefusal("a session cannot link to itself", code="self_link")
    target = _find_session(base, repo, mesh, target_h, target_s)
    if target is None:
        raise MeshRefusal("unknown peer", code="unknown_peer")
    target_live = session_liveness(target)
    if target_live != "live":
        raise MeshRefusal(f"peer is {target_live}, not live",
                          code="peer_not_live")
    actor_key = str(actor["key"])
    target_key = str(target["key"])
    with _mesh_lock(base):
        records, _ = _link_records(base, repo, mesh)
        sessions = _session_map(base, repo, mesh)
        now = _epoch()
        if key_text is not None:
            for item in records:
                if item.get("idempotency_key") != key_text:
                    continue
                if _endpoint_key(item.get("from")) != actor_key:
                    raise MeshRefusal(
                        "idempotency key belongs to another session",
                        code="id_conflict")
                if (_endpoint_key(item.get("to")) != target_key
                        or item.get("relation") != relation):
                    raise MeshRefusal(
                        "idempotency key was used for a different request",
                        code="id_conflict")
                return _link_result(item, base, created=False,
                                    actor_key=actor_key)
        open_links = [
            item for item in records
            if {_endpoint_key(item.get("from")),
                _endpoint_key(item.get("to"))} == {actor_key, target_key}
            and _link_effective(item, sessions, now=now)[0]
            in {"pending", "active"}
        ]
        for item in open_links:
            if _relation_from(item, actor_key) != relation:
                raise MeshRefusal(
                    "a different relation is already pending or active",
                    code="link_conflict")
        if open_links:
            latest = max(open_links, key=lambda item: str(
                item.get("created_at")))
            return _link_result(latest, base, created=False,
                                actor_key=actor_key)
        link_id = _hash_id("lnk", secrets.token_hex(16), 16)
        record = {
            "schema": SCHEMA,
            "repo_id": repo,
            "mesh_id": mesh,
            "link_id": link_id,
            "from": {"harness": harness, "session": session,
                     "key": actor_key},
            "to": {"harness": target_h, "session": target_s,
                   "key": target_key},
            "relation": relation,
            "state": "pending",
            "created_at": _iso(now),
            "accepted_at": None,
            "expires_at": None,
            "revoked_at": None,
            "ttl_s": ttl,
            "capabilities": [],
            "reason": why,
            "idempotency_key": key_text,
            "history": [{"event": "requested", "at": _iso(now),
                         "by": actor_key, "relation": relation}],
        }
        _write_json(base / LINKS_DIR / f"{link_id}.json", record)
    _audit(base, "link_request", link_id=link_id, relation=relation,
           from_key=actor_key, to_key=target_key)
    return _link_result(record, base, created=True, actor_key=actor_key)


def link_accept(root: str | Path, harness: str, session: str, link_id: str
                ) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    actor_key = str(actor["key"])
    with _mesh_lock(base):
        path, record = _find_link(base, repo, mesh, link_id)
        if _endpoint_key(record.get("to")) != actor_key:
            raise MeshRefusal("only the target may accept a link",
                              code="not_target")
        stored = str(record.get("state") or "pending")
        if stored == "active":
            return _link_result(record, base, created=False,
                                actor_key=actor_key)
        if stored in {"declined", "revoked"}:
            raise MeshRefusal("link is no longer pending",
                              code="link_closed")
        sessions = _session_map(base, repo, mesh)
        state, _, _ = _link_effective(record, sessions, now=_epoch())
        if state == "expired":
            raise MeshRefusal("link has expired", code="link_expired")
        if state == "stale":
            raise MeshRefusal("link peer is not live", code="link_stale")
        if state != "pending":
            raise MeshRefusal("link is no longer pending",
                              code="link_closed")
        now = _epoch()
        ttl = _coerce_ttl(record.get("ttl_s"), default=LINK_TTL_S,
                          maximum=LINK_TTL_MAX_S)
        record["state"] = "active"
        record["accepted_at"] = _iso(now)
        record["expires_at"] = _iso(now + ttl)
        record.setdefault("history", []).append({
            "event": "accepted", "at": _iso(now), "by": actor_key})
        record["history"] = record["history"][-MAX_HISTORY:]
        _write_json(path, record)
    _audit(base, "link_accept", link_id=link_id, by=actor_key)
    return _link_result(record, base, created=True, actor_key=actor_key)


def link_decline(root: str | Path, harness: str, session: str, link_id: str,
                 *, reason: str = "") -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    why = _clean_reason(reason)
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    actor_key = str(actor["key"])
    with _mesh_lock(base):
        path, record = _find_link(base, repo, mesh, link_id)
        if _endpoint_key(record.get("to")) != actor_key:
            raise MeshRefusal("only the target may decline a link",
                              code="not_target")
        stored = str(record.get("state") or "pending")
        if stored == "declined":
            return _link_result(record, base, created=False,
                                actor_key=actor_key)
        if stored == "active":
            raise MeshRefusal("an active link must be revoked, not declined",
                              code="link_active")
        if stored == "revoked":
            raise MeshRefusal("link is no longer pending",
                              code="link_closed")
        sessions = _session_map(base, repo, mesh)
        if _link_effective(record, sessions, now=_epoch())[0] == "expired":
            raise MeshRefusal("link has expired", code="link_expired")
        if stored != "pending":
            raise MeshRefusal("link is no longer pending",
                              code="link_closed")
        now = _epoch()
        record["state"] = "declined"
        record["declined_at"] = _iso(now)
        record.setdefault("history", []).append({
            "event": "declined", "at": _iso(now), "by": actor_key,
            "reason": why})
        record["history"] = record["history"][-MAX_HISTORY:]
        _write_json(path, record)
    _audit(base, "link_decline", link_id=link_id, by=actor_key)
    return _link_result(record, base, created=True, actor_key=actor_key)


def link_revoke(root: str | Path, harness: str, session: str, link_id: str,
                *, reason: str = "") -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    why = _clean_reason(reason)
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    actor_key = str(actor["key"])
    with _mesh_lock(base):
        path, record = _find_link(base, repo, mesh, link_id)
        if actor_key not in {_endpoint_key(record.get("from")),
                             _endpoint_key(record.get("to"))}:
            raise MeshRefusal("only a party may revoke a link",
                              code="not_party")
        stored = str(record.get("state") or "pending")
        if stored == "revoked":
            return _link_result(record, base, created=False,
                                actor_key=actor_key)
        if stored == "declined":
            raise MeshRefusal("link is already closed", code="link_closed")
        now = _epoch()
        record["state"] = "revoked"
        record["revoked_at"] = _iso(now)
        record.setdefault("history", []).append({
            "event": "revoked", "at": _iso(now), "by": actor_key,
            "reason": why})
        record["history"] = record["history"][-MAX_HISTORY:]
        _write_json(path, record)
    _audit(base, "link_revoke", link_id=link_id, by=actor_key)
    return _link_result(record, base, created=True, actor_key=actor_key)


def _viewer_key(repo: str, mesh: str, harness: str | None,
                session: str | None) -> str | None:
    if not harness or not session:
        return None
    return session_key(repo, mesh, harness, session)


def link_list(root: str | Path, *, harness: str | None = None,
              session: str | None = None, include_all: bool = True
              ) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    viewer = _viewer_key(repo, mesh, harness, session)
    if base is None:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "links": [], "errors": [], "counts": {}}
    records, errors = _link_records(base, repo, mesh)
    sessions = _session_map(base, repo, mesh)
    now = _epoch()
    views = [_link_view(item, sessions, now=now, viewer=viewer)
             for item in records]
    if not include_all:
        views = [item for item in views
                 if item["effective_state"] in {"pending", "active", "stale"}]
    views.sort(key=lambda item: (str(item.get("created_at")),
                                 str(item.get("link_id"))))
    counts: dict[str, int] = {}
    for item in views:
        state = str(item["effective_state"])
        counts[state] = counts.get(state, 0) + 1
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "links": views, "errors": errors, "counts": counts}


def link_show(root: str | Path, link_id: str, *, harness: str | None = None,
              session: str | None = None) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        raise MeshRefusal("unknown link", code="unknown_link")
    _, record = _find_link(base, repo, mesh, link_id)
    viewer = _viewer_key(repo, mesh, harness, session)
    sessions = _session_map(base, repo, mesh)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "link": _link_view(record, sessions, now=_epoch(),
                               viewer=viewer)}


# --------------------------------------------------------------------------- #
# delegations
# --------------------------------------------------------------------------- #
def _delegation_problem(record: Mapping[str, Any], actor_key: str, *,
                        action: str, owner_key: str | None,
                        lease_id: str | None, mailbox: bool,
                        sessions: Mapping[str, Mapping[str, Any]],
                        links: Mapping[str, Mapping[str, Any]],
                        now: float) -> tuple[str, str] | None:
    if _endpoint_key(record.get("grantee")) != actor_key:
        return "delegation_denied", "delegation is for another grantee"
    actions = {str(item) for item in record.get("actions") or ()}
    if action not in actions:
        return ("delegation_denied",
                f"delegation does not include {action}")
    if owner_key is not None and _endpoint_key(
            record.get("grantor")) != owner_key:
        return "delegation_denied", "delegation was granted by another session"
    if action == "lease_release":
        if str(record.get("lease_id") or "") != str(lease_id or ""):
            return "delegation_denied", "delegation is scoped to another lease"
    elif action == "mailbox_read":
        if not record.get("mailbox") or not mailbox:
            return "delegation_denied", "delegation is not for a mailbox"
    elif action == "merge_on_behalf":
        return ("delegation_denied",
                "merge_on_behalf is a handoff reference and never authorizes "
                "an action")
    state, reason = _delegation_effective(record, sessions, links, now=now)
    if state == "revoked":
        return "delegation_revoked", "delegation has been revoked"
    if state == "expired":
        if reason == "uses":
            return "delegation_used", "delegation has no uses left"
        if reason == "time":
            return "delegation_expired", "delegation has expired"
        return ("delegation_denied",
                f"delegation is not usable ({reason})")
    return None


def _consume_delegation(base: Path, repo: str, mesh: str,
                        delegation_id: str, actor: Mapping[str, Any], *,
                        action: str, owner_key: str | None,
                        lease_id: str | None = None,
                        mailbox: bool = False) -> dict[str, Any]:
    """Verify and consume one use.  The caller holds ``_mesh_lock``."""
    path, record = _find_delegation(base, repo, mesh, delegation_id)
    sessions = _session_map(base, repo, mesh)
    link_records, _ = _link_records(base, repo, mesh)
    links = {str(item.get("link_id")): item for item in link_records}
    problem = _delegation_problem(
        record, str(actor.get("key")), action=action, owner_key=owner_key,
        lease_id=lease_id, mailbox=mailbox, sessions=sessions, links=links,
        now=_epoch())
    if problem is not None:
        raise MeshRefusal(problem[1], code=problem[0])
    now = _epoch()
    record["used"] = int(record.get("used") or 0) + 1
    record.setdefault("history", []).append({
        "event": "used", "at": _iso(now), "action": action,
        "by": actor.get("key"), "lease_id": lease_id,
        "mailbox": bool(mailbox)})
    record["history"] = record["history"][-MAX_HISTORY:]
    _write_json(path, record)
    _audit(base, "delegation_use", delegation_id=delegation_id,
           action=action, by=actor.get("key"), lease_id=lease_id,
           mailbox=bool(mailbox))
    return record


def _authorize_mailbox_read(base: Path, repo: str, mesh: str,
                            delegation_id: str, actor: Mapping[str, Any]
                            ) -> dict[str, Any]:
    _, record = _find_delegation(base, repo, mesh, delegation_id)
    sessions = _session_map(base, repo, mesh)
    link_records, _ = _link_records(base, repo, mesh)
    links = {str(item.get("link_id")): item for item in link_records}
    problem = _delegation_problem(
        record, str(actor.get("key")), action="mailbox_read",
        owner_key=_endpoint_key(record.get("grantor")), lease_id=None,
        mailbox=True, sessions=sessions, links=links, now=_epoch())
    if problem is not None:
        raise MeshRefusal(problem[1], code=problem[0])
    return record


def delegation_grant(root: str | Path, harness: str, session: str, *, to: str,
                     actions: Sequence[str], lease_id: str | None = None,
                     mailbox: bool = False, ttl_s: int = DELEGATION_TTL_S,
                     max_uses: int = 1, reason: str = "") -> dict[str, Any]:
    """Grant a bounded, scoped delegation to an actively linked session."""
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    action_list = [str(item) for item in (actions or ())]
    if not action_list:
        raise MeshRefusal("at least one action is required",
                          code="invalid_action")
    for item in action_list:
        if item not in DELEGATION_ACTIONS:
            raise MeshRefusal("unknown delegation action",
                              code="invalid_action")
    action_list = list(dict.fromkeys(action_list))
    ttl = _coerce_ttl(ttl_s, default=DELEGATION_TTL_S,
                      maximum=DELEGATION_TTL_MAX_S)
    if isinstance(max_uses, bool) or not isinstance(max_uses, int) or not (
            1 <= max_uses <= DELEGATION_MAX_USES):
        raise MeshRefusal(f"max-uses must be 1..{DELEGATION_MAX_USES}",
                          code="invalid_value")
    if lease_id is not None and not LEASE_RE.fullmatch(str(lease_id)):
        raise MeshRefusal("lease must be a lease id", code="invalid_lease_id")
    needs_lease = "lease_release" in action_list or (
        "merge_on_behalf" in action_list)
    if needs_lease and lease_id is None:
        raise MeshRefusal("--lease is required for this action",
                          code="invalid_scope")
    if "mailbox_read" in action_list and not mailbox:
        raise MeshRefusal("--mailbox is required for mailbox_read",
                          code="invalid_scope")
    if not needs_lease and lease_id is not None:
        raise MeshRefusal("--lease only applies to lease actions",
                          code="invalid_scope")
    if "mailbox_read" not in action_list and mailbox:
        raise MeshRefusal("--mailbox requires the mailbox_read action",
                          code="invalid_scope")
    target_h, target_s = _split_identity(to)
    why = _clean_reason(reason)
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    if (target_h, target_s) == (harness, session):
        raise MeshRefusal("a session cannot delegate to itself",
                          code="self_delegation")
    target = _find_session(base, repo, mesh, target_h, target_s)
    if target is None:
        raise MeshRefusal("unknown peer", code="unknown_peer")
    target_live = session_liveness(target)
    if target_live != "live":
        raise MeshRefusal(f"peer is {target_live}, not live",
                          code="peer_not_live")
    actor_key = str(actor["key"])
    with _mesh_lock(base):
        link_records, _ = _link_records(base, repo, mesh)
        sessions = _session_map(base, repo, mesh)
        now = _epoch()
        link, link_state = _active_link_between(
            link_records, sessions, actor_key, str(target["key"]), now=now)
        if link is None or link_state != "active":
            code, message = _link_refusal_for(link_state)
            raise MeshRefusal(message, code=code)
        resource = None
        lease_kind = None
        if lease_id is not None:
            _, lease = _find_lease(base, repo, mesh, lease_id)
            if _lease_holder_key(lease) != actor_key:
                raise MeshRefusal(
                    "only the lease holder may delegate its release",
                    code="not_holder")
            lease_state, _ = _lease_effective(lease, base, now=now)
            if lease_state not in {"active", "stale"}:
                raise MeshRefusal("lease is no longer held",
                                  code="lease_closed")
            resource_obj = lease.get("resource") or {}
            lease_kind = str(resource_obj.get("kind") or "")
            resource = str(resource_obj.get("id") or "")
            if "merge_on_behalf" in action_list and lease_kind != "merge":
                raise MeshRefusal(
                    "merge_on_behalf requires a merge lease reference",
                    code="invalid_scope")
        delegation_id = _hash_id("dlg", secrets.token_hex(16), 16)
        record = {
            "schema": SCHEMA,
            "repo_id": repo,
            "mesh_id": mesh,
            "delegation_id": delegation_id,
            "grantor": {"harness": harness, "session": session,
                        "key": actor_key},
            "grantee": {"harness": target_h, "session": target_s,
                        "key": target["key"]},
            "link_id": link.get("link_id"),
            "actions": action_list,
            "lease_id": lease_id,
            "resource": resource,
            "mailbox": bool(mailbox),
            "state": "active",
            "created_at": _iso(now),
            "expires_at": _iso(now + ttl),
            "ttl_s": ttl,
            "max_uses": max_uses,
            "used": 0,
            "reason": why,
            "history": [{"event": "granted", "at": _iso(now),
                         "by": actor_key, "actions": action_list}],
        }
        _write_json(base / DELEGATIONS_DIR / f"{delegation_id}.json", record)
    _audit(base, "delegation_grant", delegation_id=delegation_id,
           grantor=actor_key, grantee=target["key"],
           actions=action_list, lease_id=lease_id)
    return _delegation_result(record, base, created=True,
                              actor_key=actor_key)


def _delegation_result(record: Mapping[str, Any], base: Path, *,
                       created: bool, actor_key: str | None) -> dict[str, Any]:
    sessions = _session_map(base, str(record["repo_id"]),
                            str(record["mesh_id"]))
    link_records, _ = _link_records(base, str(record["repo_id"]),
                                    str(record["mesh_id"]))
    links = {str(item.get("link_id")): item for item in link_records}
    view = _delegation_view(record, sessions, links, now=_epoch(),
                            viewer=actor_key)
    return {"schema": SCHEMA, "repo_id": record["repo_id"],
            "mesh_id": record["mesh_id"], "created": created,
            "delegation": view}


def delegation_revoke(root: str | Path, harness: str, session: str,
                      delegation_id: str, *, reason: str = ""
                      ) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    why = _clean_reason(reason)
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    actor_key = str(actor["key"])
    with _mesh_lock(base):
        path, record = _find_delegation(base, repo, mesh, delegation_id)
        if _endpoint_key(record.get("grantor")) != actor_key:
            raise MeshRefusal("only the grantor may revoke a delegation",
                              code="not_grantor")
        if str(record.get("state") or "active") == "revoked":
            return _delegation_result(record, base, created=False,
                                      actor_key=actor_key)
        now = _epoch()
        record["state"] = "revoked"
        record["revoked_at"] = _iso(now)
        record.setdefault("history", []).append({
            "event": "revoked", "at": _iso(now), "by": actor_key,
            "reason": why})
        record["history"] = record["history"][-MAX_HISTORY:]
        _write_json(path, record)
    _audit(base, "delegation_revoke", delegation_id=delegation_id,
           by=actor_key)
    return _delegation_result(record, base, created=True, actor_key=actor_key)


def delegation_list(root: str | Path, *, harness: str | None = None,
                    session: str | None = None, include_all: bool = True
                    ) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    viewer = _viewer_key(repo, mesh, harness, session)
    if base is None:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "delegations": [], "errors": [], "counts": {}}
    records, errors = _delegation_records(base, repo, mesh)
    sessions = _session_map(base, repo, mesh)
    link_records, _ = _link_records(base, repo, mesh)
    links = {str(item.get("link_id")): item for item in link_records}
    now = _epoch()
    views = [_delegation_view(item, sessions, links, now=now, viewer=viewer)
             for item in records]
    if not include_all:
        views = [item for item in views
                 if item["effective_state"] == "active"]
    views.sort(key=lambda item: (str(item.get("created_at")),
                                 str(item.get("delegation_id"))))
    counts: dict[str, int] = {}
    for item in views:
        state = str(item["effective_state"])
        counts[state] = counts.get(state, 0) + 1
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "delegations": views, "errors": errors, "counts": counts}


def delegation_show(root: str | Path, delegation_id: str, *,
                    harness: str | None = None, session: str | None = None
                    ) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        raise MeshRefusal("unknown delegation", code="unknown_delegation")
    _, record = _find_delegation(base, repo, mesh, delegation_id)
    sessions = _session_map(base, repo, mesh)
    link_records, _ = _link_records(base, repo, mesh)
    links = {str(item.get("link_id")): item for item in link_records}
    viewer = _viewer_key(repo, mesh, harness, session)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "delegation": _delegation_view(record, sessions, links,
                                           now=_epoch(), viewer=viewer)}


# --------------------------------------------------------------------------- #
# mailbox
# --------------------------------------------------------------------------- #
def _mailbox(base: Path, key: str) -> Path:
    return base / MAIL_DIR / key


def _mailbox_stats(directory: Path, repo: str, mesh: str
                   ) -> tuple[int, int, int, list[dict[str, str]]]:
    messages, errors = _iter_records(directory, repo, mesh)
    total = 0
    for path in (directory.glob("*.json") if directory.is_dir() else ()):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            pass
    unread = sum(1 for item in messages if not item.get("read_at"))
    return len(messages), total, unread, errors


def _validate_message_text(subject: str, body: str) -> None:
    _bounded_text(subject, "subject", SUBJECT_MAX, controls=True)
    if any(char in subject for char in ("\n", "\r", "\t")):
        raise MeshRefusal("subject must be one line",
                          code="control_character")
    _bounded_text(body, "body", BODY_MAX, allow_empty=True, controls=True)
    for text in (subject, body):
        if _SECRET_REF.search(text):
            raise MeshRefusal(
                "inline secret= references are not accepted",
                code="inline_credential")


def _envelope(record: Mapping[str, Any], *, from_rec: Mapping[str, Any],
              to_rec: Mapping[str, Any], kind: str, subject: str, body: str,
              in_reply_to: str | None, lease_id: str | None,
              evidence: Sequence[str], ttl_s: int, broadcast: bool,
              msg_id: str) -> dict[str, Any]:
    created = _epoch()
    redacted_subject, subject_redactions = _redact(subject)
    redacted_body, body_redactions = _redact(body)
    return {
        "schema": SCHEMA,
        "repo_id": record["repo_id"],
        "mesh_id": record["mesh_id"],
        "msg_id": msg_id,
        "kind": kind,
        "from": {
            "harness": from_rec.get("harness"),
            "session": from_rec.get("session"),
            "key": from_rec.get("key"),
        },
        "to": {
            "harness": to_rec.get("harness"),
            "session": to_rec.get("session"),
            "key": to_rec.get("key"),
        },
        "broadcast": broadcast,
        "broadcast_id": msg_id if broadcast else None,
        "subject": redacted_subject,
        "body": redacted_body,
        "untrusted": True,
        "body_untrusted": True,
        "in_reply_to": in_reply_to,
        "lease_id": lease_id,
        "evidence": list(evidence),
        "created_at": _iso(created),
        "expires_at": _iso(created + ttl_s),
        "delivered_at": _iso(created),
        "read_at": None,
        "redactions": sorted(set(subject_redactions + body_redactions)),
    }


def _prepare_recipients(base: Path, repo: str, mesh: str,
                        sender: Mapping[str, Any], to: str | None,
                        broadcast: bool) -> list[dict[str, Any]]:
    records, _ = _sessions(base, repo, mesh)
    if broadcast:
        return [item for item in records
                if item.get("key") != sender.get("key")
                and session_liveness(item) == "live"]
    if not to or ":" not in to:
        raise MeshRefusal("--to must be HARNESS:SESSION", code="invalid_recipient")
    harness, session = to.split(":", 1)
    target = _find_session(base, repo, mesh, harness, session)
    if target is None:
        raise MeshRefusal("unknown peer", code="unknown_peer")
    effect = session_liveness(target)
    if effect != "live":
        raise MeshRefusal(f"peer is {effect}, not live", code="unknown_peer")
    return [target]


def send(root: str | Path, harness: str, session: str, *, kind: str,
         subject: str, body: str = "", to: str | None = None,
         broadcast: bool = False, in_reply_to: str | None = None,
         lease_id: str | None = None, evidence: Sequence[str] | None = None,
         ttl_s: int = MESSAGE_TTL_S) -> dict[str, Any]:
    """Deliver one bounded, redacted message to live same-scope peers."""
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    if kind not in KINDS:
        raise MeshRefusal("unknown message kind", code="invalid_kind")
    if broadcast == bool(to):
        raise MeshRefusal("choose exactly one of --to or --broadcast",
                          code="invalid_recipient")
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int):
        raise MeshRefusal("ttl must be an integer", code="invalid_ttl")
    if not 1 <= ttl_s <= MESSAGE_TTL_MAX_S:
        raise MeshRefusal(f"ttl must be 1..{MESSAGE_TTL_MAX_S} seconds",
                          code="invalid_ttl")
    _validate_message_text(subject, body)
    if in_reply_to is not None and not MSG_RE.fullmatch(str(in_reply_to)):
        raise MeshRefusal("in_reply_to must be a message id",
                          code="invalid_message_id")
    if lease_id is not None and not LEASE_RE.fullmatch(str(lease_id)):
        raise MeshRefusal("lease must be a lease id", code="invalid_lease_id")
    refs = [str(item)[:200] for item in (evidence or ())][:16]
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    sender = _require_session(base, repo, mesh, harness, session, live=True)
    recipients = _prepare_recipients(base, repo, mesh, sender, to, broadcast)
    if not recipients:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "delivered": 0, "recipients": [], "message": None}
    msg_id = _hash_id("msg", f"{secrets.token_hex(16)}", 16)
    envelopes = {
        str(target["key"]): _envelope(
            sender, from_rec=sender, to_rec=target, kind=kind, subject=subject,
            body=body, in_reply_to=in_reply_to, lease_id=lease_id,
            evidence=refs, ttl_s=ttl_s, broadcast=broadcast, msg_id=msg_id)
        for target in recipients
    }
    for key, envelope in envelopes.items():
        directory = _mailbox(base, key)
        count, size, unread, _ = _mailbox_stats(directory, repo, mesh)
        encoded = len((json.dumps(envelope, indent=2, sort_keys=True,
                                  allow_nan=False) + "\n").encode("utf-8"))
        if (count >= MAILBOX_MAX_MESSAGES
                or size + encoded > MAILBOX_MAX_BYTES
                or unread >= MAILBOX_MAX_UNREAD):
            raise MeshRefusal(
                f"mailbox {key} is full ({count} messages, {unread} unread); "
                "read or prune before sending", code="mailbox_full")
    for key, envelope in envelopes.items():
        _write_json(_mailbox(base, key) / f"{msg_id}.json", envelope)
    _audit(base, "send", msg_id=msg_id, kind=kind,
           recipients=sorted(envelopes), broadcast=broadcast)
    metadata = None
    if envelopes:
        first = envelopes[next(iter(envelopes))]
        metadata = {name: value for name, value in first.items()
                    if name != "body"}
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "delivered": len(envelopes), "recipients": sorted(envelopes),
            "message": metadata}


def inbox(root: str | Path, harness: str, session: str, *,
          unread: bool = False, metadata_only: bool = True,
          delegation_id: str | None = None) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "messages": [], "unread": 0, "count": 0, "errors": []}
    record = _require_session(base, repo, mesh, harness, session)
    mailbox_key = str(record["key"])
    delegated_from = None
    if delegation_id is not None:
        delegation = _authorize_mailbox_read(
            base, repo, mesh, delegation_id, record)
        delegated_from = _endpoint_key(delegation.get("grantor"))
        mailbox_key = delegated_from or mailbox_key
    messages, errors = _iter_records(_mailbox(base, mailbox_key),
                                     repo, mesh)
    now = _epoch()
    selected = []
    for item in messages:
        if unread and item.get("read_at"):
            continue
        view = (dict(item) if not metadata_only
                else {key: value for key, value in item.items()
                      if key != "body"})
        expires = _parse_iso(item.get("expires_at"))
        view["expired"] = bool(expires is not None and expires <= now)
        selected.append(view)
    selected.sort(key=lambda item: (str(item.get("created_at")),
                                    str(item.get("msg_id"))), reverse=True)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "messages": selected,
            "unread": sum(1 for item in selected if not item.get("read_at")),
            "count": len(selected), "errors": errors,
            "delegation_id": delegation_id,
            "mailbox_owner": delegated_from}


def _message_path(base: Path, key: str, msg_id: str) -> Path:
    if not MSG_RE.fullmatch(str(msg_id)):
        raise MeshRefusal("invalid message id", code="invalid_message_id")
    return _mailbox(base, key) / f"{msg_id}.json"


def _read_message(base: Path, repo: str, mesh: str, key: str, msg_id: str
                  ) -> tuple[Path, dict[str, Any]]:
    path = _message_path(base, key, msg_id)
    try:
        record = _read_json(path)
    except FileNotFoundError:
        raise MeshRefusal("unknown message", code="unknown_message") from None
    _check_scope(record, repo, mesh, "message")
    return path, record


def read(root: str | Path, harness: str, session: str, msg_id: str, *,
         peek: bool = False, delegation_id: str | None = None
         ) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=not peek)
    if base is None:
        raise MeshRefusal("session is not registered",
                          code="unknown_session")
    repo = repo_id(root)
    mesh = mesh_id(root)
    record = _require_session(base, repo, mesh, harness, session)
    mailbox_key = str(record["key"])
    delegated_from = None
    if delegation_id is not None:
        with _mesh_lock(base):
            delegation = _consume_delegation(
                base, repo, mesh, delegation_id, record,
                action="mailbox_read", owner_key=None, mailbox=True)
        delegated_from = _endpoint_key(delegation.get("grantor"))
        mailbox_key = delegated_from or mailbox_key
    path, message = _read_message(base, repo, mesh, mailbox_key, msg_id)
    if not peek and not message.get("read_at"):
        message["read_at"] = _iso()
        _write_json(path, message)
    view = dict(message)
    view["peeked"] = peek
    view["unread"] = _unread_count(base, repo, mesh, mailbox_key)
    view["delegation_id"] = delegation_id
    view["mailbox_owner"] = delegated_from
    return view


def ack(root: str | Path, harness: str, session: str, msg_id: str
        ) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    record = _require_session(base, repo, mesh, harness, session)
    path, message = _read_message(base, repo, mesh, str(record["key"]),
                                  msg_id)
    if not message.get("read_at"):
        message["read_at"] = _iso()
        _write_json(path, message)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "msg_id": msg_id, "cursor": msg_id,
            "read_at": message.get("read_at"),
            "unread": _unread_count(base, repo, mesh, str(record["key"]))}


def _unread_count(base: Path, repo: str, mesh: str, key: str) -> int:
    messages, _ = _iter_records(_mailbox(base, key), repo, mesh)
    return sum(1 for item in messages if not item.get("read_at"))


def prune(root: str | Path, *, expired: bool = False,
          delivered_before: float | None = None, apply: bool = False) -> dict[str, Any]:
    """Remove only read messages; unread mail is never deleted automatically."""
    if bool(expired) == (delivered_before is not None):
        raise MeshRefusal(
            "choose exactly one of --expired or --delivered-before",
            code="invalid_prune")
    if delivered_before is not None and delivered_before < 0:
        raise MeshRefusal("delivered-before must be non-negative",
                          code="invalid_prune")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    now = _epoch()
    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    try:
        mailboxes = sorted((base / MAIL_DIR).iterdir())
    except FileNotFoundError:
        mailboxes = []
    for directory in mailboxes:
        if not directory.is_dir() or directory.is_symlink():
            continue
        messages, _ = _iter_records(directory, repo, mesh)
        for item in messages:
            reason = None
            if not item.get("read_at"):
                reason = "unread"
            elif expired:
                due = _parse_iso(item.get("expires_at"))
                if due is not None and due <= now:
                    reason = "expired"
            else:
                read_at = _parse_iso(item.get("read_at"))
                if read_at is not None and now - read_at >= float(
                        delivered_before or 0):
                    reason = "delivered-before"
            entry = {"msg_id": item.get("msg_id"),
                     "mailbox": directory.name,
                     "reason": reason,
                     "expires_at": item.get("expires_at"),
                     "read_at": item.get("read_at")}
            if reason in {"expired", "delivered-before"}:
                candidates.append(entry)
            else:
                kept.append(entry)
    if apply:
        for entry in candidates:
            path = _mailbox(base, entry["mailbox"]) / f"{entry['msg_id']}.json"
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise MeshIOError(f"cannot remove {path}: {exc}") from exc
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "dry_run": not apply, "removed": candidates if apply else [],
            "candidates": candidates, "kept": kept}


# --------------------------------------------------------------------------- #
# leases
# --------------------------------------------------------------------------- #
def _normalize_branch(value: str) -> str:
    branch = str(value).strip()
    branch = branch.removeprefix("refs/heads/")
    if not BRANCH_RE.fullmatch(branch) or ".." in branch or branch.startswith(
            ".") or branch.endswith("/"):
        raise MeshRefusal("invalid branch name", code="invalid_resource")
    return branch


def normalize_resource(root: str | Path, kind: str, value: str) -> str:
    if kind not in RESOURCE_KINDS:
        raise MeshRefusal("unknown resource kind", code="invalid_resource")
    text = str(value).strip()
    if not text or len(text) > 512 or _CONTROL.search(text):
        raise MeshRefusal("invalid resource id", code="invalid_resource")
    if kind == "worktree":
        path = Path(text).expanduser().resolve()
        return f"worktree:{path}"
    if kind in {"branch", "merge"}:
        return f"{kind}:{_normalize_branch(text)}"
    if kind == "path":
        root_p = Path(root).resolve()
        path = Path(text)
        if not path.is_absolute():
            path = root_p / path
        try:
            rel = path.resolve().relative_to(root_p).as_posix()
        except ValueError:
            raise MeshRefusal("path resource must be inside the repository",
                              code="invalid_resource") from None
        return f"path:{rel}"
    if kind == "grant":
        if not RE_GRANT.fullmatch(text):
            raise MeshRefusal("invalid grant label", code="invalid_resource")
        return f"grant:{text}"
    if not RE_OTHER.fullmatch(text):
        raise MeshRefusal("other resources must be namespace:label",
                          code="invalid_resource")
    return f"other:{text}"


RE_GRANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RE_OTHER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _lease_holder_key(record: Mapping[str, Any]) -> str | None:
    holder = record.get("holder")
    if not isinstance(holder, Mapping):
        return None
    value = holder.get("key")
    return str(value) if value else None


def _lease_effective(record: Mapping[str, Any], base: Path, *,
                     now: float | None = None) -> tuple[str, str]:
    """Return ``(effective_state, holder_liveness)`` without writing."""
    stored = str(record.get("state") or "active")
    if stored in {"released", "revoked"}:
        return stored, "ended"
    moment = _epoch() if now is None else float(now)
    due = _parse_iso(record.get("expires_at"))
    if due is not None and moment >= due:
        return "expired", "expired"
    holder = record.get("holder") if isinstance(record.get("holder"),
                                                Mapping) else {}
    key = str(holder.get("key") or "")
    sessions, _ = _iter_records(base / SESSIONS_DIR, str(record.get("repo_id")),
                                str(record.get("mesh_id")))
    match = next((item for item in sessions if item.get("key") == key), None)
    if match is None:
        return "expired", "missing"
    effect = session_liveness(match, now=moment)
    if effect in {"ended", "dead", "foreign"}:
        return "expired", effect
    if effect in {"stale", "unknown"}:
        return "stale", effect
    return "active", effect


def _queue_entry(record: Mapping[str, Any], actor: Mapping[str, Any],
                 now: float) -> dict[str, Any]:
    return {
        "harness": actor.get("harness"),
        "session": actor.get("session"),
        "key": actor.get("key"),
        "pid": actor.get("pid"),
        "pid_start": actor.get("pid_start"),
        "requested_at": _iso(now),
    }


def _lease_view(record: Mapping[str, Any], base: Path, *,
                now: float | None = None) -> dict[str, Any]:
    state, holder_state = _lease_effective(record, base, now=now)
    view = dict(record)
    view["effective_state"] = state
    view["holder_liveness"] = holder_state
    due = _parse_iso(record.get("expires_at"))
    view["expires_in_s"] = (None if due is None
                            else max(0, int(due - (now or _epoch()))))
    view["expired"] = state == "expired"
    view["orphaned"] = holder_state in {"missing", "dead", "ended", "foreign"}
    view["queue_positions"] = [
        {**dict(entry), "position": index + 1}
        for index, entry in enumerate(record.get("queue") or ())
    ]
    return view


def _lease_records(base: Path, repo: str, mesh: str
                   ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _iter_records(base / LEASES_DIR, repo, mesh)


def _find_lease(base: Path, repo: str, mesh: str, lease_id: str
                ) -> tuple[Path, dict[str, Any]]:
    if not LEASE_RE.fullmatch(str(lease_id)):
        raise MeshRefusal("invalid lease id", code="invalid_lease_id")
    path = base / LEASES_DIR / f"{lease_id}.json"
    try:
        record = _read_json(path)
    except FileNotFoundError:
        raise MeshRefusal("unknown lease", code="unknown_lease") from None
    _check_scope(record, repo, mesh, "lease")
    return path, record


def _conflicts(existing: Mapping[str, Any], mode: str) -> bool:
    state, _ = existing.get("_effective", ("active", "live"))
    if state not in {"active", "stale"}:
        return False
    existing_mode = str((existing.get("resource") or {}).get("mode") or "")
    if mode in {"write", "merge"}:
        return True
    return existing_mode in {"write", "merge"}


def _queue_position(records: Iterable[Mapping[str, Any]], key: str
                    ) -> tuple[str, int] | None:
    for record in records:
        for index, item in enumerate(record.get("queue") or ()):
            if item.get("key") == key:
                return str(record.get("lease_id")), index + 1
    return None


def lease_acquire(root: str | Path, harness: str, session: str, *,
                  kind: str, resource_id: str | None = None, mode: str,
                  ttl_s: int = LEASE_TTL_S, reason: str = "",
                  resource: str | None = None) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    if resource_id is None:
        resource_id = resource
    if resource_id is None:
        raise MeshRefusal("a resource id is required", code="invalid_resource")
    mode = str(mode)
    if mode not in MODES:
        raise MeshRefusal("unknown lease mode", code="invalid_mode")
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int):
        raise MeshRefusal("ttl must be an integer", code="invalid_ttl")
    if not 1 <= ttl_s <= LEASE_TTL_MAX_S:
        raise MeshRefusal(f"ttl must be 1..{LEASE_TTL_MAX_S} seconds",
                          code="invalid_ttl")
    why, _ = _redact(_bounded_text(reason, "reason", MAX_REASON,
                                  allow_empty=True, controls=True))
    normalized = normalize_resource(root, kind, resource_id)
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    with _lease_lock(base):
        records, _ = _lease_records(base, repo, mesh)
        for item in records:
            item["_effective"] = _lease_effective(item, base)
        blocking = [
            item for item in records
            if (item.get("resource") or {}).get("kind") == kind
            and (item.get("resource") or {}).get("id") == normalized
            and _conflicts(item, mode)
        ]
        # The same identity asking again gets its existing lease or queue slot.
        for item in blocking:
            resource = item.get("resource") or {}
            if (resource.get("mode") == mode
                    and _lease_holder_key(item) == actor.get("key")
                    and item["_effective"] in {"active", "stale"}):
                return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                        "acquired": True, "queued": False,
                        "position": 0, "lease": _lease_view(item, base)}
        existing = _queue_position(records, str(actor.get("key")))
        if existing is not None and existing[0] in {
                str(item.get("lease_id")) for item in blocking}:
            _, position = existing
            lease_id = existing[0]
            holder_record = next(item for item in records
                                 if item.get("lease_id") == lease_id)
            return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                    "acquired": False, "queued": True,
                    "position": position,
                    "lease": _lease_view(holder_record, base),
                    "holder": holder_record.get("holder")}
        now = _epoch()
        if blocking:
            holder_record = min(blocking,
                                key=lambda item: str(item.get("created_at")))
            queue = holder_record.setdefault("queue", [])
            if len(queue) >= LEASE_QUEUE_MAX:
                raise MeshRefusal("lease queue is full", code="queue_full")
            queue.append(_queue_entry(holder_record, actor, now))
            holder_record.setdefault("history", []).append({
                "event": "queued", "at": _iso(now),
                "holder": actor.get("key"),
            })
            holder_record["history"] = holder_record["history"][-MAX_HISTORY:]
            _write_json(base / LEASES_DIR / f"{holder_record['lease_id']}.json",
                        _public_record(holder_record))
            _audit(base, "lease_queue", lease_id=holder_record["lease_id"],
                   resource=normalized, mode=mode)
            return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                    "acquired": False, "queued": True,
                    "position": len(queue), "lease": _lease_view(
                        holder_record, base),
                    "holder": holder_record.get("holder")}
        lease_id = _hash_id("lease", secrets.token_hex(16), 16)
        record = {
            "schema": SCHEMA,
            "repo_id": repo,
            "mesh_id": mesh,
            "lease_id": lease_id,
            "resource": {"kind": kind, "id": normalized, "mode": mode},
            "holder": {
                "harness": harness, "session": session,
                "key": actor.get("key"), "registration": actor.get("key"),
                "pid": actor.get("pid"), "pid_start": actor.get("pid_start"),
            },
            "state": "active",
            "created_at": _iso(now),
            "expires_at": _iso(now + ttl_s),
            "renewed_at": None,
            "ttl_s": ttl_s,
            "renew_count": 0,
            "queue": [],
            "took_over": None,
            "reason": why,
            "history": [{"event": "acquired", "at": _iso(now),
                         "holder": actor.get("key")}],
        }
        _write_json(base / LEASES_DIR / f"{lease_id}.json", record)
    _audit(base, "lease_acquire", lease_id=lease_id, resource=normalized,
           mode=mode)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "acquired": True, "queued": False, "position": 0,
            "lease": _lease_view(record, base)}


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items()
            if not key.startswith("_")}


def lease_renew(root: str | Path, harness: str, session: str, lease_id: str, *,
                ttl_s: int | None = None) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    with _lease_lock(base):
        path, record = _find_lease(base, repo, mesh, lease_id)
        if _lease_holder_key(record) != actor.get("key"):
            raise MeshRefusal("only the holder may renew this lease",
                              code="not_holder")
        state, _ = _lease_effective(record, base)
        if state == "expired":
            raise MeshRefusal("lease is expired", code="lease_expired")
        if state in {"released", "revoked"}:
            raise MeshRefusal("lease is no longer active", code="lease_closed")
        now = _epoch()
        ttl = record.get("ttl_s", LEASE_TTL_S) if ttl_s is None else ttl_s
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not (
                1 <= ttl <= LEASE_TTL_MAX_S):
            raise MeshRefusal("invalid lease ttl", code="invalid_ttl")
        renew_count = int(record.get("renew_count") or 0)
        created = _parse_iso(record.get("created_at")) or now
        if renew_count >= LEASE_MAX_RENEWALS:
            raise MeshRefusal("lease renewal limit reached",
                              code="lease_limit")
        new_expiry = min(now + ttl, created + LEASE_MAX_LIFETIME_S)
        if new_expiry <= now:
            raise MeshRefusal("lease lifetime limit reached",
                              code="lease_limit")
        record["expires_at"] = _iso(new_expiry)
        record["renewed_at"] = _iso(now)
        record["renew_count"] = renew_count + 1
        record.setdefault("history", []).append({
            "event": "renewed", "at": _iso(now), "holder": actor.get("key")})
        record["history"] = record["history"][-MAX_HISTORY:]
        _write_json(path, record)
    _audit(base, "lease_renew", lease_id=lease_id)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "lease": _lease_view(record, base)}


def _promote(base: Path, record: dict[str, Any], *, now: float
             ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Build a new lease for the first live queued request.

    The released record is left in place as history.  Stale or dead queued
    entries are skipped and recorded; entries after the promoted request move
    to the new lease's queue.
    """
    queue = list(record.get("queue") or [])
    skipped: list[dict[str, Any]] = []
    for index, entry in enumerate(queue):
        sessions, _ = _iter_records(
            base / SESSIONS_DIR, str(record["repo_id"]),
            str(record["mesh_id"]))
        candidate = next(
            (item for item in sessions if item.get("key") == entry.get("key")),
            None)
        effect = session_liveness(candidate) if candidate is not None else "missing"
        if effect != "live" or candidate is None:
            skipped.append({"entry": entry, "liveness": effect})
            record.setdefault("history", []).append({
                "event": "skipped", "at": _iso(now),
                "holder": entry.get("key"), "liveness": effect})
            continue
        previous = dict(record.get("holder") or {})
        promoted = {
            "schema": SCHEMA,
            "repo_id": record["repo_id"],
            "mesh_id": record["mesh_id"],
            "lease_id": _hash_id("lease", secrets.token_hex(16), 16),
            "resource": dict(record.get("resource") or {}),
            "holder": {
                "harness": candidate.get("harness"),
                "session": candidate.get("session"),
                "key": candidate.get("key"),
                "registration": candidate.get("key"),
                "pid": candidate.get("pid"),
                "pid_start": candidate.get("pid_start"),
            },
            "state": "active",
            "created_at": _iso(now),
            "expires_at": _iso(
                now + int(record.get("ttl_s") or LEASE_TTL_S)),
            "renewed_at": None,
            "ttl_s": int(record.get("ttl_s") or LEASE_TTL_S),
            "renew_count": 0,
            "queue": queue[index + 1:],
            "took_over": None,
            "reason": record.get("reason") or "",
            "history": [{
                "event": "promoted", "at": _iso(now),
                "from": previous.get("key"), "holder": candidate.get("key")}],
        }
        return promoted, skipped
    return None, skipped


def lease_release(root: str | Path, harness: str, session: str, lease_id: str,
                  *, delegation_id: str | None = None) -> dict[str, Any]:
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session)
    with _lease_lock(base):
        path, record = _find_lease(base, repo, mesh, lease_id)
        holder_key = _lease_holder_key(record)
        delegated_from = None
        if holder_key != actor.get("key"):
            if delegation_id is None:
                raise MeshRefusal("only the holder may release this lease",
                                  code="not_holder")
            delegation = _consume_delegation(
                base, repo, mesh, delegation_id, actor,
                action="lease_release", owner_key=holder_key,
                lease_id=lease_id)
            delegated_from = _endpoint_key(delegation.get("grantor"))
        now = _epoch()
        promoted = None
        skipped: list[dict[str, Any]] = []
        if record.get("state") not in {"released", "revoked"}:
            record["state"] = "released"
            record["released_at"] = _iso(now)
            event = {"event": "released", "at": _iso(now),
                     "holder": actor.get("key")}
            if delegated_from is not None:
                event["delegated_from"] = delegated_from
                event["delegation_id"] = delegation_id
            record.setdefault("history", []).append(event)
            promoted, skipped = _promote(base, record, now=now)
            record["queue"] = []
            record["history"] = record.get("history", [])[-MAX_HISTORY:]
            _write_json(path, record)
            if promoted is not None:
                _write_json(base / LEASES_DIR
                            / f"{promoted['lease_id']}.json", promoted)
    _audit(base, "lease_release", lease_id=lease_id,
           promoted=promoted is not None, delegation_id=delegation_id,
           delegated_from=delegated_from)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "lease": _lease_view(record, base),
            "promoted": _lease_view(promoted, base) if promoted else None,
            "skipped": skipped, "delegation_id": delegation_id}


def lease_takeover(root: str | Path, harness: str, session: str, lease_id: str,
                   *, confirm: bool = False, reason: str,
                   ttl_s: int = LEASE_TTL_S) -> dict[str, Any]:
    if not confirm:
        raise MeshRefusal("takeover requires --confirm", code="confirm_required")
    why, _ = _redact(_bounded_text(reason, "reason", MAX_REASON,
                                  controls=True))
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not (
            1 <= ttl_s <= LEASE_TTL_MAX_S):
        raise MeshRefusal("invalid lease ttl", code="invalid_ttl")
    harness = _safe_name(harness, "harness")
    session = _safe_name(session, "session")
    base = _open_base(root, create=True)
    assert base is not None
    repo = repo_id(root)
    mesh = mesh_id(root)
    actor = _require_session(base, repo, mesh, harness, session, live=True)
    with _lease_lock(base):
        path, record = _find_lease(base, repo, mesh, lease_id)
        state, holder_state = _lease_effective(record, base)
        if state == "active":
            raise MeshRefusal("lease holder is live; takeover refused",
                              code="live_holder")
        if state != "expired":
            raise MeshRefusal("only an expired lease can be taken over",
                              code="takeover_refused")
        now = _epoch()
        previous = dict(record.get("holder") or {})
        record["took_over"] = {
            "at": _iso(now), "holder": previous, "holder_state": holder_state,
            "reason": why,
        }
        record["holder"] = {
            "harness": harness, "session": session, "key": actor.get("key"),
            "registration": actor.get("key"), "pid": actor.get("pid"),
            "pid_start": actor.get("pid_start"),
        }
        record["state"] = "active"
        record["created_at"] = _iso(now)
        record["expires_at"] = _iso(now + ttl_s)
        record["ttl_s"] = ttl_s
        record["renewed_at"] = None
        record["renew_count"] = 0
        record["queue"] = [item for item in record.get("queue") or ()
                           if item.get("key") != actor.get("key")]
        record.setdefault("history", []).append({
            "event": "took_over", "at": _iso(now),
            "from": previous.get("key"), "holder": actor.get("key"),
            "holder_state": holder_state, "reason": why})
        record["history"] = record["history"][-MAX_HISTORY:]
        _write_json(path, record)
    _audit(base, "lease_takeover", lease_id=lease_id,
           from_holder=previous.get("key"), holder_state=holder_state)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "lease": _lease_view(record, base)}


def lease_list(root: str | Path, *, include_all: bool = True
               ) -> dict[str, Any]:
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
                "leases": [], "errors": [], "counts": {}}
    records, errors = _lease_records(base, repo, mesh)
    now = _epoch()
    views = [_lease_view(item, base, now=now) for item in records]
    if not include_all:
        views = [item for item in views
                 if item["effective_state"] in {"active", "stale"}]
    views.sort(key=lambda item: (str(item.get("resource", {}).get("kind")),
                                 str(item.get("resource", {}).get("id")),
                                 str(item.get("created_at"))))
    counts = {
        "active": sum(1 for item in views
                      if item["effective_state"] == "active"),
        "queued": sum(len(item.get("queue") or ()) for item in views),
        "expired": sum(1 for item in views
                       if item["effective_state"] == "expired"),
        "orphaned": sum(1 for item in views if item["orphaned"]),
        "stale": sum(1 for item in views
                     if item["effective_state"] == "stale"),
        "released": sum(1 for item in views
                        if item["effective_state"] == "released"),
    }
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "leases": views, "errors": errors, "counts": counts}


def lease_status(root: str | Path, lease_id: str | None = None
                 ) -> dict[str, Any]:
    if lease_id is None:
        return lease_list(root)
    base = _open_base(root, create=False)
    repo = repo_id(root)
    mesh = mesh_id(root)
    if base is None:
        raise MeshRefusal("unknown lease", code="unknown_lease")
    path, record = _find_lease(base, repo, mesh, lease_id)
    return {"schema": SCHEMA, "repo_id": repo, "mesh_id": mesh,
            "lease": _lease_view(record, base)}


# --------------------------------------------------------------------------- #
# worktree and merge negotiation
# --------------------------------------------------------------------------- #
def _discover_worktree(root: str | Path, *, path: str | None,
                       branch: str | None) -> dict[str, Any]:
    records = W.discover(root)
    wanted_branch = _normalize_branch(branch) if branch else None
    wanted_path = Path(path).expanduser().resolve() if path else None
    match = None
    for item in records:
        if wanted_path is not None and Path(item["path"]).resolve() != wanted_path:
            continue
        if wanted_branch is not None and item.get("branch") != wanted_branch:
            continue
        if not item.get("exists") or item.get("bare"):
            continue
        match = item
        break
    if match is None:
        raise MeshRefusal("no existing matching worktree", code="worktree_missing")
    return match


def worktree_request(root: str | Path, harness: str, session: str, *,
                     branch: str | None = None, path: str | None = None,
                     reason: str = "") -> dict[str, Any]:
    if not branch and not path:
        raise MeshRefusal("worktree request needs --branch or --path",
                          code="invalid_resource")
    match = _discover_worktree(root, path=path, branch=branch)
    branch_name = match.get("branch")
    if branch and branch_name != _normalize_branch(branch):
        raise MeshRefusal("worktree branch does not match", code="worktree_missing")
    worktree_lease = lease_acquire(
        root, harness, session, kind="worktree",
        resource_id=str(match["path"]), mode="write", reason=reason)
    branch_lease = None
    if worktree_lease.get("acquired") and branch_name:
        try:
            branch_lease = lease_acquire(
                root, harness, session, kind="branch",
                resource_id=str(branch_name), mode="write", reason=reason)
        except MeshRefusal as exc:
            lease_release(root, harness, session,
                          str(worktree_lease["lease"]["lease_id"]))
            raise exc
    if (branch_lease and branch_lease.get("queued")
            and worktree_lease.get("acquired")):
        released = lease_release(root, harness, session,
                                 str(worktree_lease["lease"]["lease_id"]))
        worktree_lease = {**released, "acquired": False}
    result = {
        "schema": SCHEMA, "repo_id": repo_id(root), "mesh_id": mesh_id(root),
        "worktree": match, "advisory": True,
        "lease": worktree_lease.get("lease"),
        "branch_lease": branch_lease.get("lease") if branch_lease else None,
        "leases": [item for item in (worktree_lease.get("lease"),
                                     branch_lease.get("lease")
                                     if branch_lease else None) if item],
        "acquired": bool(worktree_lease.get("acquired")
                         and (branch_lease is None
                              or branch_lease.get("acquired"))),
        "queued": bool(worktree_lease.get("queued")
                       or (branch_lease and branch_lease.get("queued"))),
        "position": worktree_lease.get("position", 0),
    }
    return result


def worktree_offer(root: str | Path, harness: str, session: str, *,
                   branch: str | None = None, path: str | None = None,
                   lease: bool = False, to: str | None = None,
                   broadcast: bool = False) -> dict[str, Any]:
    match = _discover_worktree(root, path=path, branch=branch)
    branch_name = match.get("branch")
    status = W.status(match["path"])
    if status.get("error"):
        raise MeshRefusal(
            f"worktree is not readable: {status['error']}",
            code="worktree_unreadable")
    leases = []
    if lease:
        leases.append(lease_acquire(
            root, harness, session, kind="worktree",
            resource_id=str(match["path"]), mode="read",
            reason="worktree offer"))
        if branch_name:
            leases.append(lease_acquire(
                root, harness, session, kind="branch",
                resource_id=str(branch_name), mode="read",
                reason="worktree offer"))
    message = None
    if to or broadcast:
        message = send(
            root, harness, session, kind="offer",
            subject=f"worktree offer: {branch_name or match['path']}",
            body="", to=to, broadcast=broadcast)
    return {"schema": SCHEMA, "repo_id": repo_id(root), "mesh_id": mesh_id(root),
            "worktree": match, "status": {
                "branch": status.get("branch"), "dirty": status.get("dirty")},
            "leases": [item.get("lease") for item in leases],
            "message": message}


def merge_request(root: str | Path, harness: str, session: str, *,
                  target: str, ttl_s: int = LEASE_TTL_S,
                  reason: str = "") -> dict[str, Any]:
    """Queue for the advisory merge lease.

    ``merge_on_behalf`` delegations are handoff references only and are
    deliberately never consulted here.  Holding or being granted a merge lease
    never authorizes a git merge; the repository's normal merge policy and
    approvals remain authoritative.
    """
    branch: str | None = None
    candidate = Path(target).expanduser()
    if candidate.exists() and candidate.is_dir():
        match = _discover_worktree(root, path=str(candidate), branch=None)
        branch = match.get("branch")
        if not branch:
            raise MeshRefusal("target worktree has no branch",
                              code="invalid_resource")
    else:
        branch = _normalize_branch(target)
    result = lease_acquire(
        root, harness, session, kind="merge", resource_id=branch,
        mode="merge", ttl_s=ttl_s, reason=reason)
    result["target"] = branch
    result["advisory"] = True
    return result


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def _audit(base: Path, event: str, **fields: Any) -> None:
    directory = base / AUDIT_DIR
    _ensure_dir(directory)
    path = directory / "events.jsonl"
    line = json.dumps({"schema": SCHEMA, "at": _iso(), "event": event,
                       **{key: value for key, value in fields.items()
                          if key not in {"body", "subject", "text"}}},
                      sort_keys=True, allow_nan=False)
    line = SEC.redact_text(line) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(
        os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MeshIOError(f"cannot append audit record {path}: {exc}") from exc
