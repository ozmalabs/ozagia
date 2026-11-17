"""
task.py — Long-running task visibility (v5.15 TASK).

Reference implementation of the TASK capability: elastic-await handles,
declarative wake predicates, ambient overrun surfacing, first-class
budget / on_overrun, opt-in JSONL events, and scope-aware cross-session
publication.

Design principle: one primitive, not a tool grab-bag. Synchronous
Bash(cmd) is a degenerate Task (budget=0, auto-awaited). Over-
threshold spawns return live handles; the turn continues. The model
never writes `sleep N; check` again.

Key surface:
    TaskRuntime          — owns the store and in-flight handles
    Task                 — live handle (user-facing)
    TaskResult           — terminal outcome
    TaskEvent            — structured event from JSONL sentinel stream
    spawn()              — canonical entry point
    await_any / await_all combinators

Scope and cross-session:
    scope="session" — in-memory; visible to declaring session only
    scope="workspace" (default) — persisted; visible to every session
                                  in the workspace
    scope="user" — user-level; cross-workspace
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import signal as _signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Pattern, Protocol


# ---------------------------------------------------------------------------
# Exceptions — map 1:1 onto the task_* Error Type registry kinds (§TASK)
# ---------------------------------------------------------------------------

class TaskError(Exception):
    """Base for all TASK-capability errors."""


class TaskBudgetTooSmall(TaskError):
    """`task_budget_too_small` — budget_ms <= elastic_threshold_ms."""


class TaskStateError(TaskError):
    """`task_state_error` — control op issued in an illegal state."""


class TaskComplianceEscalation(TaskError):
    """`task_compliance_escalation_forbidden` — child declared broader tags."""


class TaskAuthDenied(TaskError):
    """`task_auth_denied` — cross-session control op rejected by ACL."""


class TaskAuthMalformed(TaskError):
    """`task_auth_malformed` — auth= value did not parse."""


class TaskAuthUnknownPrincipal(TaskError):
    """`task_auth_unknown_principal` — ACL principal kind outside the grammar."""


class TaskScopeUnknown(TaskError):
    """`task_scope_unknown` — scope= not in {session, workspace, user}."""


class TaskBackendUnavailable(TaskError):
    """`task_backend_unavailable` — configured task-store adapter failed."""


class TaskSubprocessRestartFailed(TaskError):
    """`task_subprocess_restart_failed` — shim-restart re-attach could not find PID."""


class TaskHandleEvicted(TaskError):
    """`task_handle_evicted` — terminal task requested after retention eviction."""


class TaskBufferOverflow(TaskError):
    """`task_buffer_overflow` — ring buffer byte cap exceeded before paging."""


class TaskWakeOnMalformed(TaskError):
    """`task_wake_on_malformed` — wake_on entry did not match any predicate form."""


# ---------------------------------------------------------------------------
# Duration parsing — shared grammar with SCHEDULE
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"(\d+)\s*(ms|s|m|h|d)", re.IGNORECASE)
_DURATION_UNITS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def parse_duration_ms(expr: str | int | float | None) -> int | None:
    """`"5m"` → 300000 ms. Bare int/float → ms. `None` → None. `0`/`"0"` → 0."""
    if expr is None:
        return None
    if isinstance(expr, (int, float)):
        return int(expr)
    expr = expr.strip()
    if expr == "0":
        return 0
    total = 0
    matched_any = False
    for m in _DURATION_RE.finditer(expr):
        matched_any = True
        n, unit = int(m.group(1)), m.group(2).lower()
        total += n * _DURATION_UNITS_MS[unit]
    if not matched_any:
        raise ValueError(f"invalid duration: {expr!r}")
    return total


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TaskStatus = Literal[
    "running", "idle", "done", "failed",
    "cancelled", "killed", "suspended",
]
TaskKind = Literal["subprocess", "tool-call", "dispatch", "schedule-fire"]
OnOverrun = Literal["suspend_and_ask", "kill", "warn", "extend"]
Scope = Literal["session", "workspace", "user"]

# Auth is either a named policy ("owner-only" | "user" | "workspace")
# or an EXPLICIT_ACL string ("acl:..."). Validated by _parse_auth().
TaskAuth = str

VALID_SCOPES: frozenset[str] = frozenset({"session", "workspace", "user"})
VALID_AUTH_NAMES: frozenset[str] = frozenset({"owner-only", "user", "workspace"})
VALID_ACL_PRINCIPAL_KINDS: frozenset[str] = frozenset(
    {"agent", "session", "role", "workspace", "*"}
)
VALID_ACL_OPS: frozenset[str] = frozenset({"observe", "control", "destruct", "all"})
_ALL_OPS: frozenset[str] = frozenset({"observe", "control", "destruct"})


@dataclass
class TaskEvent:
    """Structured event from the JSONL sentinel stream."""
    t: str                                              # "progress" | "warn" | "error" | "result" | "info" | "<vendor>/<name>"
    ts: datetime
    data: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    """Terminal outcome snapshot (statuses mirror spec §TASK — TaskResult)."""
    status: Literal["done", "failed", "cancelled", "killed"]
    exit_code: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = 0
    events: list[TaskEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# EXPLICIT_ACL parsing — `auth=` grammar from spec §TASK
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ACLEntry:
    """Parsed ACL entry: a principal binding with a set of ops."""
    principal_kind: str                  # "agent" | "session" | "role" | "workspace" | "*"
    principal_id: str                    # identifier body; "" for "*"
    ops: frozenset[str]                  # subset of {observe, control, destruct}


def _looks_like_op_set(s: str) -> bool:
    """True when s unambiguously parses as OP_SET.

    Grammar ambiguity: `session:s:48f9a1` has an IDENT that looks a bit like
    a token list, and bare IDENTs can legitimately appear as principal ids.
    Rule: an OP_SET is identified by EITHER containing `|` (multiple ops)
    OR being an exact match for one of the reserved OP names. Anything else
    falls through to the principal id — so `agent:matt:some-id` resolves to
    principal_id='matt:some-id', and `agent:matt:observe|telepath` is an
    OP_SET that fails to parse (raising TaskAuthMalformed on the telepath
    token).
    """
    if not s:
        return False
    if "|" in s:
        return True
    return s.strip() in VALID_ACL_OPS


def _parse_op_set(spec: str) -> frozenset[str]:
    """Expand OP_SET ('observe|control' or 'all') into the atomic op set."""
    out: set[str] = set()
    for tok in spec.split("|"):
        tok = tok.strip()
        if tok not in VALID_ACL_OPS:
            raise TaskAuthMalformed(f"unknown op {tok!r} in OP_SET")
        if tok == "all":
            out.update(_ALL_OPS)
        else:
            out.add(tok)
    return frozenset(out)


def _parse_acl_entry(raw: str) -> ACLEntry:
    """Parse a single ACL_ENTRY per the §TASK grammar."""
    raw = raw.strip()
    if not raw:
        raise TaskAuthMalformed("empty ACL entry")
    # Wildcard principal
    if raw == "*":
        return ACLEntry("*", "", _ALL_OPS)
    if raw.startswith("*:"):
        return ACLEntry("*", "", _parse_op_set(raw[2:]))

    # KIND:... — KIND is the first colon-separated token
    kind, sep, rest = raw.partition(":")
    if not sep:
        raise TaskAuthMalformed(f"ACL entry missing principal id: {raw!r}")
    if kind not in VALID_ACL_PRINCIPAL_KINDS or kind == "*":
        # "*" already handled; anything else outside the set is a registry error.
        raise TaskAuthUnknownPrincipal(
            f"unknown principal kind {kind!r} in ACL entry {raw!r}")
    # Greedy-from-end split: the principal_id may contain colons
    # (e.g. `session:s:48f9a1`). Find the longest OP_SET suffix.
    parts = rest.split(":")
    for i in range(len(parts) - 1, 0, -1):
        tail = ":".join(parts[i:])
        if _looks_like_op_set(tail):
            principal_id = ":".join(parts[:i])
            if not principal_id:
                raise TaskAuthMalformed(
                    f"ACL entry has empty principal id: {raw!r}")
            return ACLEntry(kind, principal_id, _parse_op_set(tail))
    # No OP_SET suffix — grant full ops.
    if not rest:
        raise TaskAuthMalformed(f"ACL entry has empty principal id: {raw!r}")
    return ACLEntry(kind, rest, _ALL_OPS)


def _parse_acl(spec: str) -> list[ACLEntry]:
    """Parse `acl:ENTRY(,ENTRY)*`."""
    if not spec.startswith("acl:"):
        raise TaskAuthMalformed(f"EXPLICIT_ACL must start with 'acl:' — got {spec!r}")
    body = spec[4:].strip()
    if not body:
        raise TaskAuthMalformed("empty EXPLICIT_ACL")
    entries = [_parse_acl_entry(e) for e in body.split(",")]
    if not entries:
        raise TaskAuthMalformed(f"no valid ACL entries in {spec!r}")
    return entries


def _parse_auth(spec: str) -> list[ACLEntry] | str:
    """Return parsed ACL entries OR the named-policy string unchanged.

    Raises TaskAuthMalformed / TaskAuthUnknownPrincipal on invalid input."""
    if spec in VALID_AUTH_NAMES:
        return spec
    if spec.startswith("acl:"):
        return _parse_acl(spec)
    raise TaskAuthMalformed(
        f"auth={spec!r} is not a named policy or EXPLICIT_ACL")


def _caller_matches(entry: ACLEntry,
                    caller_agent: str,
                    caller_session: str,
                    caller_roles: frozenset[str],
                    caller_workspace: str) -> bool:
    """True if (caller_agent, caller_session, caller_roles) matches entry."""
    if entry.principal_kind == "*":
        return True
    if entry.principal_kind == "agent":
        return entry.principal_id == caller_agent
    if entry.principal_kind == "session":
        return entry.principal_id == caller_session
    if entry.principal_kind == "role":
        return entry.principal_id in caller_roles
    if entry.principal_kind == "workspace":
        return entry.principal_id == caller_workspace
    return False


# ---------------------------------------------------------------------------
# Registry defaults (matching docs/protocol.md §TASK registry)
# ---------------------------------------------------------------------------

DEFAULT_ELASTIC_THRESHOLD_MS = 5_000
DEFAULT_BUDGET_MS = 300_000

# Sentinel distinguishing "caller omitted budget=" from "caller passed budget=None".
# Spec §TASK: explicit None is legal and means unbounded (emits task_budget_unbounded).
_BUDGET_UNSET: Any = object()
DEFAULT_SURFACE_IDLE_MS = 60_000
DEFAULT_SURFACE_OVERRUN_MS = 30_000
DEFAULT_RETENTION_PER_SESSION = 50
DEFAULT_RETENTION_PER_WORKSPACE = 200
DEFAULT_RETENTION_PER_USER = 500
DEFAULT_BUFFER_STDOUT_BYTES = 65_536
DEFAULT_BUFFER_STDERR_BYTES = 16_384
DEFAULT_BUFFER_EVENTS_BYTES = 4_096
DEFAULT_EVENT_SENTINEL = "@@ozma-event@@"
DEFAULT_SCOPE: Scope = "workspace"
DEFAULT_AUTH: TaskAuth = "user"
DEFAULT_OVERRUN_POLICY: OnOverrun = "suspend_and_ask"


# ---------------------------------------------------------------------------
# Ring buffer — bounded append-only string buffer with byte cap
# ---------------------------------------------------------------------------

class _RingBuffer:
    """Byte-capped buffer with byte-offset cursor semantics."""

    __slots__ = ("_cap", "_buf", "_lock", "_total")

    def __init__(self, cap_bytes: int) -> None:
        self._cap = cap_bytes
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._total = 0            # total bytes ever written (monotonic)

    def append(self, data: bytes) -> None:
        with self._lock:
            self._total += len(data)
            self._buf.extend(data)
            if len(self._buf) > self._cap:
                # Drop from the front; keep the most recent cap bytes.
                drop = len(self._buf) - self._cap
                del self._buf[:drop]

    def read_since(self, cursor: int | None, max_bytes: int | None = None) -> tuple[str, int]:
        """Return (text, new_cursor). cursor=None → from start of buffer."""
        with self._lock:
            lo = self._total - len(self._buf)      # cursor value for first buffered byte
            if cursor is None:
                start_abs = lo
            else:
                start_abs = max(cursor, lo)
            start_rel = start_abs - lo
            slice_ = bytes(self._buf[start_rel:])
            if max_bytes is not None and len(slice_) > max_bytes:
                slice_ = slice_[-max_bytes:]
                start_abs = self._total - len(slice_)
            return slice_.decode("utf-8", errors="replace"), self._total

    def tail_bytes(self, n_bytes: int) -> str:
        with self._lock:
            return bytes(self._buf[-n_bytes:]).decode("utf-8", errors="replace")

    @property
    def total(self) -> int:
        with self._lock:
            return self._total


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

WakePredicate = (
    str
    | Pattern[str]
    | dict                                           # {"stream":..., "re":...} or {"event":..., "where":...}
    | Callable[["TaskEvent | str"], bool]
)


def _compile_predicate(pred: WakePredicate) -> Callable[[str, list[TaskEvent]], Any | None]:
    """Return a function that, given (newline text delta, new events), returns
    a match payload if the predicate fires (truthy), else None."""

    if isinstance(pred, re.Pattern):
        rx = pred
        def match(text: str, events: list[TaskEvent]) -> Any | None:
            for line in text.splitlines():
                m = rx.search(line)
                if m:
                    return {"line": line, "match": m.group(0)}
            return None
        return match

    if isinstance(pred, str):
        rx = re.compile(pred)
        def match(text: str, events: list[TaskEvent]) -> Any | None:
            for line in text.splitlines():
                m = rx.search(line)
                if m:
                    return {"line": line, "match": m.group(0)}
            return None
        return match

    if isinstance(pred, dict):
        # Stream-filtered regex: {"stream": "stderr", "re": "..."}
        if "re" in pred:
            rx = re.compile(pred["re"])
            # stream filter is handled at the shim level (we only get the
            # merged text here); kept in sig for completeness.
            def match(text: str, events: list[TaskEvent]) -> Any | None:
                for line in text.splitlines():
                    m = rx.search(line)
                    if m:
                        return {"line": line, "match": m.group(0)}
                return None
            return match
        # Event filter: {"event": "progress", "where": callable}
        if "event" in pred:
            target = pred["event"]
            where = pred.get("where")
            def match(text: str, events: list[TaskEvent]) -> Any | None:
                for ev in events:
                    if ev.t == target and (where is None or where(ev)):
                        return {"event": ev}
                return None
            return match
        raise ValueError(f"unknown predicate dict: {pred!r}")

    if callable(pred):
        def match(text: str, events: list[TaskEvent]) -> Any | None:
            for ev in events:
                try:
                    if pred(ev):
                        return {"event": ev}
                except Exception:
                    continue
            return None
        return match

    raise TypeError(f"unsupported predicate type: {type(pred)!r}")


# ---------------------------------------------------------------------------
# Store Protocol — persistence layer
# ---------------------------------------------------------------------------

class TaskStore(Protocol):
    """Persistence + cross-session publication layer.

    Deployments implement for in-memory (tests), filesystem (default),
    SQLite, or external backends.
    """
    def put_metadata(self, task_snapshot: dict) -> None: ...
    def append_stdout(self, task_id: str, data: bytes) -> None: ...
    def append_stderr(self, task_id: str, data: bytes) -> None: ...
    def append_events(self, task_id: str, lines: list[str]) -> None: ...
    def get_metadata(self, task_id: str) -> dict | None: ...
    def list_visible(self, scope: Scope, workspace_id: str | None,
                     user_id: str | None, session_id: str | None) -> list[dict]: ...
    def delete(self, task_id: str) -> None: ...


class MemoryTaskStore:
    """In-memory TaskStore — for tests and session-scope tasks."""

    def __init__(self) -> None:
        self._meta: dict[str, dict] = {}
        self._stdout: dict[str, bytearray] = {}
        self._stderr: dict[str, bytearray] = {}
        self._events: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def put_metadata(self, snap: dict) -> None:
        with self._lock:
            self._meta[snap["id"]] = dict(snap)

    def append_stdout(self, task_id: str, data: bytes) -> None:
        with self._lock:
            self._stdout.setdefault(task_id, bytearray()).extend(data)

    def append_stderr(self, task_id: str, data: bytes) -> None:
        with self._lock:
            self._stderr.setdefault(task_id, bytearray()).extend(data)

    def append_events(self, task_id: str, lines: list[str]) -> None:
        with self._lock:
            self._events.setdefault(task_id, []).extend(lines)

    def get_metadata(self, task_id: str) -> dict | None:
        with self._lock:
            snap = self._meta.get(task_id)
            return dict(snap) if snap else None

    def list_visible(self, scope: Scope, workspace_id: str | None,
                     user_id: str | None, session_id: str | None) -> list[dict]:
        with self._lock:
            out = []
            for snap in self._meta.values():
                if scope == "session" and snap.get("owner_session") == session_id:
                    out.append(dict(snap))
                elif scope == "workspace" and snap.get("workspace_id") == workspace_id:
                    out.append(dict(snap))
                elif scope == "user" and snap.get("user_id") == user_id:
                    out.append(dict(snap))
            return out

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._meta.pop(task_id, None)
            self._stdout.pop(task_id, None)
            self._stderr.pop(task_id, None)
            self._events.pop(task_id, None)


class FileTaskStore:
    """Filesystem-backed TaskStore.

    Layout (matches docs/protocol.md §TASK — Scope and persistence):

        <root>/session/<session_id>/<task_id>.{json,stdout,stderr,events}
        <root>/workspace/<workspace_id>/<task_id>.{...}
        <root>/user/<user_id>/<task_id>.{...}
    """

    def __init__(self, root: Path | str = "~/.ozma/tasks") -> None:
        self.root = Path(root).expanduser()
        self._lock = threading.Lock()

    def _dir_for(self, snap: dict) -> Path:
        scope = snap.get("scope", DEFAULT_SCOPE)
        if scope == "session":
            owner = snap.get("owner_session") or "default"
            return self.root / "session" / owner
        if scope == "workspace":
            owner = snap.get("workspace_id") or "default"
            return self.root / "workspace" / owner
        if scope == "user":
            owner = snap.get("user_id") or "default"
            return self.root / "user" / owner
        raise ValueError(f"unknown scope: {scope!r}")

    def _path(self, snap: dict, ext: str) -> Path:
        d = self._dir_for(snap)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{snap['id']}.{ext}"

    def put_metadata(self, snap: dict) -> None:
        path = self._path(snap, "json")
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            tmp.write_text(json.dumps(snap, default=str))
            tmp.replace(path)

    def append_stdout(self, task_id: str, data: bytes) -> None:
        self._append_stream(task_id, "stdout", data)

    def append_stderr(self, task_id: str, data: bytes) -> None:
        self._append_stream(task_id, "stderr", data)

    def append_events(self, task_id: str, lines: list[str]) -> None:
        snap = self._lookup(task_id)
        if snap is None:
            return
        path = self._path(snap, "events")
        with self._lock, path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line if line.endswith("\n") else line + "\n")

    def _append_stream(self, task_id: str, ext: str, data: bytes) -> None:
        snap = self._lookup(task_id)
        if snap is None:
            return
        path = self._path(snap, ext)
        with self._lock, path.open("ab") as f:
            f.write(data)

    def _lookup(self, task_id: str) -> dict | None:
        for scope_dir in ("session", "workspace", "user"):
            base = self.root / scope_dir
            if not base.exists():
                continue
            for owner_dir in base.iterdir():
                cand = owner_dir / f"{task_id}.json"
                if cand.exists():
                    return json.loads(cand.read_text())
        return None

    def get_metadata(self, task_id: str) -> dict | None:
        return self._lookup(task_id)

    def list_visible(self, scope: Scope, workspace_id: str | None,
                     user_id: str | None, session_id: str | None) -> list[dict]:
        if scope == "session":
            if session_id is None:
                return []
            base = self.root / "session" / session_id
        elif scope == "workspace":
            if workspace_id is None:
                return []
            base = self.root / "workspace" / workspace_id
        elif scope == "user":
            if user_id is None:
                return []
            base = self.root / "user" / user_id
        else:
            return []
        if not base.exists():
            return []
        out = []
        for p in base.glob("*.json"):
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                continue
        return out

    def delete(self, task_id: str) -> None:
        snap = self._lookup(task_id)
        if snap is None:
            return
        d = self._dir_for(snap)
        for ext in ("json", "stdout", "stderr", "events"):
            p = d / f"{task_id}.{ext}"
            if p.exists():
                p.unlink()


# ---------------------------------------------------------------------------
# Task — live handle
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_task_id() -> str:
    return "t:" + secrets.token_hex(4)


# Single-process cross-runtime handle registry. Peer-control ops use this to
# route into the owning runtime when multiple runtimes share a task-store
# adapter. A production deployment would route via the workspace coordinator;
# this module-level table is the reference implementation of the same idea.
_HANDLE_REGISTRY: dict[str, tuple["TaskRuntime", "Task"]] = {}
_HANDLE_REGISTRY_LOCK = threading.Lock()


def _register_handle(runtime: "TaskRuntime", task: "Task") -> None:
    with _HANDLE_REGISTRY_LOCK:
        _HANDLE_REGISTRY[task.id] = (runtime, task)


def _unregister_handle(task_id: str) -> None:
    with _HANDLE_REGISTRY_LOCK:
        _HANDLE_REGISTRY.pop(task_id, None)


def _lookup_handle(task_id: str) -> tuple["TaskRuntime", "Task"] | None:
    with _HANDLE_REGISTRY_LOCK:
        return _HANDLE_REGISTRY.get(task_id)


@dataclass
class Task:
    """Live task handle. Lifecycle owned by TaskRuntime.

    Users get handles back from `runtime.spawn(...)`. Observation and
    control methods dispatch through the runtime's lock.
    """
    id: str
    cmd: str
    kind: TaskKind
    started_at: datetime
    status: TaskStatus = "running"
    exit_code: int | None = None
    finished_at: datetime | None = None
    scope: Scope = DEFAULT_SCOPE
    owner_session: str = ""
    owner_agent: str = ""
    workspace_id: str = ""
    user_id: str = ""
    budget_ms: int | None = None
    on_overrun: OnOverrun = DEFAULT_OVERRUN_POLICY
    extend_cap_ms: int = 0
    compliance: list[str] = field(default_factory=list)
    auth: TaskAuth = DEFAULT_AUTH
    parent_id: str | None = None

    # Non-snapshot state — populated by runtime
    _runtime: "TaskRuntime | None" = field(default=None, repr=False, compare=False)
    _stdout: _RingBuffer | None = field(default=None, repr=False, compare=False)
    _stderr: _RingBuffer | None = field(default=None, repr=False, compare=False)
    _events: list[TaskEvent] = field(default_factory=list, repr=False, compare=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    _pid: int | None = field(default=None, repr=False, compare=False)
    _reader_threads: list[threading.Thread] = field(default_factory=list, repr=False, compare=False)
    _wake_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _done_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _wake_predicates: list[dict] = field(default_factory=list, repr=False, compare=False)
    _wake_fires: list[dict] = field(default_factory=list, repr=False, compare=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    _last_output_at: datetime | None = field(default=None, repr=False, compare=False)
    _last_surface_at: datetime | None = field(default=None, repr=False, compare=False)
    _surface_policy: str = field(default="ambient", repr=False, compare=False)
    _events_enabled: bool = field(default=False, repr=False, compare=False)

    # -- Observation ---------------------------------------------------------

    def stdout_since(self, cursor: int | None = None, max_bytes: int = 4096) -> str:
        assert self._stdout is not None
        text, _ = self._stdout.read_since(cursor, max_bytes)
        return text

    def stderr_since(self, cursor: int | None = None, max_bytes: int = 4096) -> str:
        assert self._stderr is not None
        text, _ = self._stderr.read_since(cursor, max_bytes)
        return text

    def last_lines(self, n: int = 10) -> list[str]:
        assert self._stdout is not None
        tail = self._stdout.tail_bytes(self._stdout._cap)
        lines = tail.splitlines()
        return lines[-n:]

    def events_since(self, cursor: int | None = None) -> list[TaskEvent]:
        with self._lock:
            if cursor is None:
                return list(self._events)
            return list(self._events[cursor:])

    # -- Wakes ---------------------------------------------------------------

    def await_done(self, timeout_ms: int | None = None) -> TaskResult:
        """Block until terminal; raise TimeoutError if timeout elapses.

        Consistent with await_line / await_stdout_match / await_event — a
        non-completing task is a TimeoutError, not a synthetic TaskResult.
        Matches spec §TASK which removed the `"timeout"` status.
        """
        to = None if timeout_ms is None else timeout_ms / 1000.0
        self._done_event.wait(timeout=to)
        if not self._done_event.is_set():
            raise TimeoutError(
                f"task {self.id} did not complete within {timeout_ms}ms "
                f"(status={self.status!r})"
            )
        assert self._runtime is not None
        return self._runtime._result_for(self)

    def await_line(self, pattern: str | Pattern, timeout_ms: int | None = None) -> str:
        m = self._await_predicate(pattern, timeout_ms)
        if m is None:
            raise TimeoutError(f"timed out awaiting line matching {pattern!r}")
        return m.get("line", "")

    def await_stdout_match(self, pattern: str | Pattern, timeout_ms: int | None = None) -> re.Match:
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        result = self._await_predicate(rx, timeout_ms)
        if result is None:
            raise TimeoutError(f"timed out awaiting match for {rx.pattern!r}")
        m = rx.search(result["line"])
        assert m is not None                                   # predicate matched; regex must rematch
        return m

    def await_event(self, predicate: Callable[[TaskEvent], bool],
                    timeout_ms: int | None = None) -> TaskEvent:
        result = self._await_predicate(predicate, timeout_ms)
        if result is None:
            raise TimeoutError("timed out awaiting event")
        return result["event"]

    def await_idle_for(self, ms: int, timeout_ms: int | None = None) -> None:
        deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000.0
        target = ms / 1000.0
        while True:
            with self._lock:
                last = self._last_output_at
            if last is None:
                elapsed = 0.0
            else:
                elapsed = (_now() - last).total_seconds()
            if elapsed >= target:
                return
            if self._done_event.is_set():
                return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"timed out awaiting {ms}ms idle")
            time.sleep(min(0.1, max(target - elapsed, 0.01)))

    def _await_predicate(self, pred: WakePredicate, timeout_ms: int | None) -> dict | None:
        compiled = _compile_predicate(pred)
        deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000.0
        last_stdout_cursor = 0
        last_event_cursor = 0
        while True:
            # Pull new output + events, test predicate
            text = ""
            if self._stdout is not None:
                text, last_stdout_cursor = self._stdout.read_since(last_stdout_cursor)
            with self._lock:
                new_events = list(self._events[last_event_cursor:])
                last_event_cursor = len(self._events)
            result = compiled(text, new_events)
            if result is not None:
                return result
            if self._done_event.is_set():
                # One final pass after termination
                final_text = ""
                if self._stdout is not None:
                    final_text, _ = self._stdout.read_since(last_stdout_cursor)
                with self._lock:
                    tail_events = list(self._events[last_event_cursor:])
                result = compiled(final_text, tail_events)
                return result
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            self._wake_event.wait(timeout=min(0.25, remaining) if remaining else 0.25)
            self._wake_event.clear()

    # -- Control -------------------------------------------------------------

    def cancel(self, grace_ms: int = 2000, signal: int = _signal.SIGTERM) -> None:
        assert self._runtime is not None
        self._runtime._cancel(self, grace_ms=grace_ms, signal=signal, terminal="cancelled")

    def kill(self) -> None:
        assert self._runtime is not None
        self._runtime._cancel(self, grace_ms=0, signal=_signal.SIGKILL, terminal="killed")

    def send_input(self, text: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise TaskStateError("task has no open stdin")
        if self.status in {"done", "failed", "cancelled", "killed"}:
            raise TaskStateError(f"cannot send_input to {self.status} task")
        try:
            self._proc.stdin.write(text.encode() if isinstance(text, str) else text)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TaskStateError(f"stdin closed: {exc}") from exc

    def extend_budget(self, ms: int) -> None:
        with self._lock:
            self.budget_ms = (self.budget_ms or 0) + ms
        assert self._runtime is not None
        self._runtime._notify_budget_change(self)

    def resume(self) -> None:
        if self.status != "suspended":
            raise TaskStateError(f"resume only legal from suspended, not {self.status}")
        assert self._runtime is not None
        self._runtime._resume(self)

    # -- Snapshots -----------------------------------------------------------

    def snapshot(self) -> dict:
        """Metadata-only snapshot suitable for persistence + surfacing."""
        with self._lock:
            return {
                "id": self.id,
                "cmd": self.cmd,
                "kind": self.kind,
                "started_at": self.started_at.isoformat(),
                "status": self.status,
                "exit_code": self.exit_code,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "scope": self.scope,
                "owner_session": self.owner_session,
                "owner_agent": self.owner_agent,
                "workspace_id": self.workspace_id,
                "user_id": self.user_id,
                "budget_ms": self.budget_ms,
                "on_overrun": self.on_overrun,
                "compliance": list(self.compliance),
                "auth": self.auth,
                "parent_id": self.parent_id,
                "pid": self._pid,
                "last_line": self.last_lines(1)[0] if self.last_lines(1) else "",
                "elapsed_ms": int((_now() - self.started_at).total_seconds() * 1000),
                "wake_fires": list(self._wake_fires),
            }


# ---------------------------------------------------------------------------
# Event sentinel parsing
# ---------------------------------------------------------------------------

def _parse_event_line(raw_line: str, sentinel: str) -> TaskEvent | None:
    """Parse a sentinel-prefixed line. Returns None if not an event line."""
    if not raw_line.startswith(sentinel):
        return None
    payload = raw_line[len(sentinel):].lstrip()
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return TaskEvent(t="warn", ts=_now(),
                         data={"code": "malformed_event", "raw": raw_line})
    if not isinstance(obj, dict) or "t" not in obj:
        return TaskEvent(t="warn", ts=_now(),
                         data={"code": "malformed_event", "raw": raw_line})
    kind = str(obj.pop("t"))
    return TaskEvent(t=kind, ts=_now(), data=obj)


# ---------------------------------------------------------------------------
# TaskRuntime — the orchestrator
# ---------------------------------------------------------------------------

@dataclass
class TaskRuntimeConfig:
    elastic_threshold_ms: int = DEFAULT_ELASTIC_THRESHOLD_MS
    budget_default_ms: int = DEFAULT_BUDGET_MS
    surface_cadence_idle_ms: int = DEFAULT_SURFACE_IDLE_MS
    surface_cadence_overrun_ms: int = DEFAULT_SURFACE_OVERRUN_MS
    retention_per_session: int = DEFAULT_RETENTION_PER_SESSION
    retention_per_workspace: int = DEFAULT_RETENTION_PER_WORKSPACE
    retention_per_user: int = DEFAULT_RETENTION_PER_USER
    buffer_stdout_bytes: int = DEFAULT_BUFFER_STDOUT_BYTES
    buffer_stderr_bytes: int = DEFAULT_BUFFER_STDERR_BYTES
    buffer_events_bytes: int = DEFAULT_BUFFER_EVENTS_BYTES
    event_sentinel: str = DEFAULT_EVENT_SENTINEL
    scope_default: Scope = DEFAULT_SCOPE
    auth_default: TaskAuth = DEFAULT_AUTH
    overrun_default_policy: OnOverrun = DEFAULT_OVERRUN_POLICY


class TaskRuntime:
    """Owns task lifecycle, stores, and surfacing.

    Typical setup:
        runtime = TaskRuntime(
            session_id="s:48f9a1",
            workspace_id="/home/matt/ozma",
            user_id="matt",
            store=FileTaskStore(),
        )
        t = runtime.spawn("npm install", budget="3m")
    """

    def __init__(self,
                 session_id: str = "",
                 agent_id: str = "",
                 workspace_id: str = "",
                 user_id: str = "",
                 store: TaskStore | None = None,
                 config: TaskRuntimeConfig | None = None,
                 telemetry: Callable[[str, dict], None] | None = None) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.store: TaskStore = store or MemoryTaskStore()
        self.config = config or TaskRuntimeConfig()
        self._telemetry = telemetry or (lambda name, data: None)
        self._tasks: dict[str, Task] = {}
        # Per-scope retention rings — spec §TASK Cleanup — "Retain last N
        # terminal handles per scope; suspended handles persist until resolved."
        self._terminal_rings: dict[str, list[str]] = {
            "session": [], "workspace": [], "user": [],
        }
        # Ids evicted from retention rings (for task_handle_evicted lookup)
        self._evicted: set[str] = set()
        self._lock = threading.RLock()

    # -- Spawn ---------------------------------------------------------------

    def spawn(self,
              cmd: str | list[str],
              *,
              budget: Any = _BUDGET_UNSET,
              on_overrun: OnOverrun | None = None,
              extend_cap_ms: int = 0,
              wake_on: list[WakePredicate] | None = None,
              events: bool = False,
              surface: Literal["ambient", "silent", "verbose"] = "ambient",
              scope: Scope | None = None,
              auth: TaskAuth | None = None,
              compliance: list[str] | None = None,
              parent_id: str | None = None,
              cwd: str | Path | None = None,
              env: dict | None = None,
              shell: bool | None = None,
              kind: TaskKind = "subprocess",
              stdin_pipe: bool = False) -> Task:
        """Canonical TASK spawn. Elastic-await semantics — returns terminal
        Task synchronously if the work completes under the threshold, live
        Task handle otherwise."""

        # Explicit budget=None ≡ unbounded (spec §TASK). Omitting the arg
        # falls back to the configured default.
        if budget is _BUDGET_UNSET:
            budget_ms = self.config.budget_default_ms
        else:
            budget_ms = parse_duration_ms(budget)

        # Invariant: elastic threshold < budget (or budget == 0 for sync-only)
        if budget_ms is not None and budget_ms != 0 and budget_ms <= self.config.elastic_threshold_ms:
            raise TaskBudgetTooSmall(
                f"budget_ms={budget_ms} must exceed elastic threshold "
                f"{self.config.elastic_threshold_ms} (or be 0 for synchronous)"
            )

        scope = scope or self.config.scope_default
        if scope not in VALID_SCOPES:
            raise TaskScopeUnknown(f"scope={scope!r} not in {sorted(VALID_SCOPES)}")
        auth = auth or self.config.auth_default
        _parse_auth(auth)                    # raises TaskAuthMalformed / TaskAuthUnknownPrincipal
        on_overrun = on_overrun or self.config.overrun_default_policy
        compliance = list(compliance or [])

        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        if shell is None:
            shell = isinstance(cmd, str)

        task = Task(
            id=_new_task_id(),
            cmd=cmd_str,
            kind=kind,
            started_at=_now(),
            status="running",
            scope=scope,
            owner_session=self.session_id,
            owner_agent=self.agent_id,
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            budget_ms=budget_ms,
            on_overrun=on_overrun,
            extend_cap_ms=extend_cap_ms,
            compliance=compliance,
            auth=auth,
            parent_id=parent_id,
        )
        task._runtime = self
        task._stdout = _RingBuffer(self.config.buffer_stdout_bytes)
        task._stderr = _RingBuffer(self.config.buffer_stderr_bytes)
        task._surface_policy = surface
        task._events_enabled = events
        for p in wake_on or []:
            try:
                compiled = _compile_predicate(p)
            except (ValueError, TypeError) as exc:
                # Malformed predicates are dropped with telemetry; other
                # predicates still fire (spec §TASK Error Type registry —
                # `task_wake_on_malformed` is Log-only).
                self._telemetry("task.wake_on_malformed", {
                    "task_id": task.id, "pred": repr(p)[:128],
                    "error": str(exc),
                })
                continue
            task._wake_predicates.append({"pred": compiled, "raw": p})

        # Start subprocess (kind="tool-call" spawns are handled by spawn_tool)
        try:
            proc = subprocess.Popen(
                cmd,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                bufsize=0,                               # unbuffered — we want streaming
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            task.status = "failed"
            task.exit_code = -1
            task.finished_at = _now()
            task._done_event.set()
            self._persist_snapshot(task)
            self._telemetry("task.spawn_failed",
                            {"task_id": task.id, "error": str(exc)})
            return task

        task._proc = proc
        task._pid = proc.pid

        with self._lock:
            self._tasks[task.id] = task
        _register_handle(self, task)
        self._telemetry("task.spawn", {
            "task_id": task.id,
            "cmd": cmd_str,
            "kind": kind,
            "budget_ms": budget_ms,
            "scope": scope,
            "parent_id": parent_id,
            "pid": proc.pid,
        })
        if budget_ms is None:
            self._telemetry("task.budget_unbounded", {"task_id": task.id})
        self._persist_snapshot(task)

        # Start reader threads
        self._start_readers(task)

        # Elastic-await: block up to threshold
        if budget_ms == 0:
            # Synchronous degenerate case — wait indefinitely for completion
            self._wait_for(task, timeout_ms=None)
        else:
            self._wait_for(task, timeout_ms=self.config.elastic_threshold_ms)
            if task.status == "running" or task.status == "idle":
                # Promoted to live handle
                self._telemetry("task.promoted_to_handle", {"task_id": task.id})

        return task

    def spawn_tool(self, tool_name: str, args: dict | None = None,
                   *, runner: Callable[..., Any] | None = None, **spawn_kwargs) -> Task:
        """Long-running model-tool TASK (kind='tool-call').

        `runner(args) -> Any` executes the tool in a background thread;
        its return value becomes a single `result` TaskEvent. Observation
        shape is identical to subprocess tasks from the caller's view.
        """
        if runner is None:
            raise TaskError("spawn_tool requires runner=")

        budget_raw = spawn_kwargs.pop("budget", _BUDGET_UNSET)
        if budget_raw is _BUDGET_UNSET:
            budget_ms = self.config.budget_default_ms
        else:
            budget_ms = parse_duration_ms(budget_raw)
        scope = spawn_kwargs.pop("scope", None) or self.config.scope_default
        if scope not in VALID_SCOPES:
            raise TaskScopeUnknown(f"scope={scope!r} not in {sorted(VALID_SCOPES)}")
        auth = spawn_kwargs.pop("auth", None) or self.config.auth_default
        _parse_auth(auth)
        on_overrun = spawn_kwargs.pop("on_overrun", self.config.overrun_default_policy)
        compliance = list(spawn_kwargs.pop("compliance", []) or [])

        task = Task(
            id=_new_task_id(),
            cmd=f"tool:{tool_name}",
            kind="tool-call",
            started_at=_now(),
            status="running",
            scope=scope,
            owner_session=self.session_id,
            owner_agent=self.agent_id,
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            budget_ms=budget_ms,
            on_overrun=on_overrun,
            compliance=compliance,
            auth=auth,
        )
        task._runtime = self
        task._stdout = _RingBuffer(self.config.buffer_stdout_bytes)
        task._stderr = _RingBuffer(self.config.buffer_stderr_bytes)
        task._events_enabled = True

        def _run():
            try:
                result = runner(args or {})
                task._events.append(TaskEvent(t="result", ts=_now(),
                                              data={"return": result}))
                task.status = "done"
                task.exit_code = 0
            except Exception as exc:
                task._events.append(TaskEvent(t="error", ts=_now(),
                                              data={"code": type(exc).__name__, "msg": str(exc)}))
                task.status = "failed"
                task.exit_code = -1
            finally:
                task.finished_at = _now()
                task._done_event.set()
                task._wake_event.set()
                self._persist_snapshot(task)
                self._telemetry("task.completed",
                                {"task_id": task.id, "kind": "tool-call",
                                 "exit_code": task.exit_code})

        with self._lock:
            self._tasks[task.id] = task
        _register_handle(self, task)
        self._persist_snapshot(task)
        th = threading.Thread(target=_run, name=f"tool-{task.id}", daemon=True)
        th.start()
        task._reader_threads.append(th)
        self._telemetry("task.spawn", {"task_id": task.id, "kind": "tool-call",
                                       "budget_ms": budget_ms, "scope": scope})
        if budget_ms is None:
            self._telemetry("task.budget_unbounded", {"task_id": task.id})

        self._wait_for(task, timeout_ms=self.config.elastic_threshold_ms if budget_ms != 0 else None)
        if task.status == "running":
            self._telemetry("task.promoted_to_handle", {"task_id": task.id})
        return task

    # -- Reader / budget threads --------------------------------------------

    def _start_readers(self, task: Task) -> None:
        proc = task._proc
        assert proc is not None

        def _read_stream(stream, ring: _RingBuffer, is_stdout: bool) -> None:
            try:
                for raw in iter(stream.readline, b""):
                    if not raw:
                        break
                    ring.append(raw)
                    with task._lock:
                        task._last_output_at = _now()
                    if is_stdout:
                        line = raw.decode("utf-8", errors="replace").rstrip("\n")
                        # Event sentinel parsing
                        if task._events_enabled:
                            ev = _parse_event_line(line, self.config.event_sentinel)
                            if ev is not None:
                                with task._lock:
                                    task._events.append(ev)
                                self.store.append_events(task.id, [line])
                                if ev.t == "warn" and ev.data.get("code") == "malformed_event":
                                    self._telemetry("task.malformed_event",
                                                    {"task_id": task.id,
                                                     "raw": ev.data.get("raw", "")[:256]})
                                task._wake_event.set()
                                continue
                        # Persist line to stdout stream
                        self.store.append_stdout(task.id, raw)
                    else:
                        self.store.append_stderr(task.id, raw)
                    # Predicate fan-out on NEW data only
                    self._eval_wake_on(task, raw, is_stdout)
                    task._wake_event.set()
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_read_stream,
                                 args=(proc.stdout, task._stdout, True),
                                 name=f"stdout-{task.id}", daemon=True)
        t_err = threading.Thread(target=_read_stream,
                                 args=(proc.stderr, task._stderr, False),
                                 name=f"stderr-{task.id}", daemon=True)
        t_out.start()
        t_err.start()
        task._reader_threads.extend([t_out, t_err])

        # Waiter thread — marks terminal status when proc exits
        def _waiter() -> None:
            rc = proc.wait()
            # Join readers so buffers catch up before marking terminal
            for r in (t_out, t_err):
                r.join(timeout=1.0)
            with task._lock:
                if task.status in {"cancelled", "killed", "suspended"}:
                    pass                                 # keep control-origin status
                else:
                    task.status = "done" if rc == 0 else "failed"
                task.exit_code = rc
                task.finished_at = _now()
            self._persist_snapshot(task)
            task._done_event.set()
            task._wake_event.set()
            self._telemetry("task.completed", {
                "task_id": task.id,
                "exit_code": rc,
                "duration_ms": int((_now() - task.started_at).total_seconds() * 1000),
                "bytes_stdout": task._stdout.total if task._stdout else 0,
                "event_count": len(task._events),
            })
            self._retain_terminal(task)

        threading.Thread(target=_waiter, name=f"waiter-{task.id}", daemon=True).start()

        # Budget enforcement thread
        if task.budget_ms is not None and task.budget_ms > 0:
            threading.Thread(target=self._budget_watchdog, args=(task,),
                             name=f"budget-{task.id}", daemon=True).start()

    def _eval_wake_on(self, task: Task, raw: bytes, is_stdout: bool) -> None:
        if not task._wake_predicates:
            return
        text = raw.decode("utf-8", errors="replace")
        with task._lock:
            new_events = list(task._events[-10:])          # small tail
            fires = task._wake_fires
        for entry in task._wake_predicates:
            try:
                result = entry["pred"](text, new_events)
            except Exception:
                continue
            if result is not None:
                with task._lock:
                    fires.append({"at": _now().isoformat(), **result,
                                  "raw_pred": repr(entry["raw"])[:128]})
                self._telemetry("task.wake_fired", {"task_id": task.id,
                                                    "pred": repr(entry["raw"])[:128]})

    def _wait_for(self, task: Task, timeout_ms: int | None) -> None:
        """Block for up to timeout_ms (None = indefinite) or completion."""
        to = None if timeout_ms is None else timeout_ms / 1000.0
        task._done_event.wait(timeout=to)

    def _budget_watchdog(self, task: Task) -> None:
        budget_ms = task.budget_ms
        while True:
            if task._done_event.is_set():
                return
            elapsed_ms = int((_now() - task.started_at).total_seconds() * 1000)
            if budget_ms is None:
                return
            if elapsed_ms >= budget_ms:
                self._fire_overrun(task)
                return
            # Re-read current budget in case extend_budget() ran
            with task._lock:
                budget_ms = task.budget_ms
            time.sleep(min(0.25, max(0.01, (budget_ms - elapsed_ms) / 1000.0)))

    def _fire_overrun(self, task: Task) -> None:
        self._telemetry("task.overrun", {
            "task_id": task.id,
            "budget_ms": task.budget_ms,
            "actual_ms": int((_now() - task.started_at).total_seconds() * 1000),
            "policy": task.on_overrun,
        })
        policy = task.on_overrun
        if policy == "kill":
            self._cancel(task, grace_ms=2000, signal=_signal.SIGTERM, terminal="killed")
        elif policy == "suspend_and_ask":
            self._suspend(task)
        elif policy == "warn":
            # Keep running; ambient cadence switches to overrun cadence
            pass
        elif policy == "extend":
            if task.extend_cap_ms > 0:
                new_budget = (task.budget_ms or 0) * 2
                if new_budget - (task.budget_ms or 0) <= task.extend_cap_ms:
                    with task._lock:
                        task.budget_ms = new_budget
                    self._telemetry("task.budget_extended",
                                    {"task_id": task.id, "new_budget_ms": new_budget})
                    threading.Thread(target=self._budget_watchdog, args=(task,),
                                     daemon=True).start()
                    return
            # Cap exhausted — escalate to suspend
            self._suspend(task)
        self._persist_snapshot(task)

    def _suspend(self, task: Task) -> None:
        if task._proc is not None and task._proc.poll() is None:
            try:
                task._proc.send_signal(_signal.SIGSTOP)
            except (ProcessLookupError, OSError):
                pass
        with task._lock:
            task.status = "suspended"
        self._persist_snapshot(task)
        self._telemetry("task.suspended", {"task_id": task.id})
        task._wake_event.set()

    def _resume(self, task: Task) -> None:
        if task._proc is not None:
            try:
                task._proc.send_signal(_signal.SIGCONT)
            except (ProcessLookupError, OSError):
                pass
        with task._lock:
            task.status = "running"
        self._persist_snapshot(task)
        self._telemetry("task.resumed", {"task_id": task.id})
        task._wake_event.set()

    def _cancel(self, task: Task, grace_ms: int, signal: int, terminal: TaskStatus) -> None:
        if task._done_event.is_set():
            return
        # Claim terminal status FIRST so the waiter preserves it regardless
        # of exit-ordering race (the waiter only overwrites "running"/"idle").
        with task._lock:
            if task.status in {"done", "failed", "cancelled", "killed"}:
                return
            task.status = terminal
        proc = task._proc
        if proc is None:
            with task._lock:
                task.finished_at = _now()
            task._done_event.set()
            task._wake_event.set()
            return
        try:
            proc.send_signal(signal)
        except (ProcessLookupError, OSError):
            pass
        if grace_ms > 0:
            try:
                proc.wait(timeout=grace_ms / 1000.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        self._telemetry(f"task.{terminal}", {"task_id": task.id})
        # Cascade to children
        with self._lock:
            for child in list(self._tasks.values()):
                if child.parent_id == task.id and not child._done_event.is_set():
                    self._cancel(child, grace_ms=grace_ms, signal=signal, terminal=terminal)
        task._wake_event.set()

    def _notify_budget_change(self, task: Task) -> None:
        # Budget watchdog reads task.budget_ms on each loop; nothing more needed.
        pass

    # -- Results -------------------------------------------------------------

    def _result_for(self, task: Task) -> TaskResult:
        """Build a TaskResult for a terminal task. Caller MUST ensure
        task._done_event is set — spec §TASK removed the `timeout` status."""
        if task.status not in {"done", "failed", "cancelled", "killed"}:
            raise TaskStateError(
                f"_result_for called on non-terminal task {task.id} "
                f"(status={task.status!r})"
            )
        return TaskResult(
            status=task.status,                                              # type: ignore[arg-type]
            exit_code=task.exit_code,
            stdout_tail=task._stdout.tail_bytes(4096) if task._stdout else "",
            stderr_tail=task._stderr.tail_bytes(4096) if task._stderr else "",
            duration_ms=int(((task.finished_at or _now()) - task.started_at).total_seconds() * 1000),
            events=list(task._events),
        )

    # -- Persistence helpers ------------------------------------------------

    def _persist_snapshot(self, task: Task) -> None:
        try:
            self.store.put_metadata(task.snapshot())
        except Exception as exc:
            # Spec: task_backend_unavailable — fall back to in-memory,
            # cross-session publication suppressed, log+warn.
            self._telemetry("task.backend_unavailable",
                            {"task_id": task.id, "error": str(exc)})

    def _retain_terminal(self, task: Task) -> None:
        with self._lock:
            ring = self._terminal_rings.setdefault(task.scope, [])
            ring.append(task.id)
            if task.scope == "session":
                cap = self.config.retention_per_session
            elif task.scope == "workspace":
                cap = self.config.retention_per_workspace
            else:
                cap = self.config.retention_per_user
            while len(ring) > cap:
                old_id = ring.pop(0)
                self._evicted.add(old_id)
                old = self._tasks.pop(old_id, None)
                _unregister_handle(old_id)
                if old is not None:
                    try:
                        self.store.delete(old_id)
                    except Exception as exc:
                        self._telemetry("task.backend_unavailable",
                                        {"task_id": old_id, "error": str(exc)})

    # -- Cross-session surface ----------------------------------------------

    def list_visible_tasks(self) -> list[dict]:
        """Enumerate tasks visible to this session: own session-scope +
        all workspace-scope + all user-scope."""
        out: list[dict] = []
        out.extend(self.store.list_visible("session", self.workspace_id,
                                           self.user_id, self.session_id))
        out.extend(self.store.list_visible("workspace", self.workspace_id,
                                           self.user_id, self.session_id))
        out.extend(self.store.list_visible("user", self.workspace_id,
                                           self.user_id, self.session_id))
        # Dedupe by id (session-scope and workspace-scope don't overlap but
        # defensive against misconfig)
        seen = set()
        unique = []
        for snap in out:
            if snap["id"] in seen:
                continue
            seen.add(snap["id"])
            unique.append(snap)
        return unique

    # -- Handle lookup with eviction semantics ------------------------------

    def get_handle(self, task_id: str) -> Task:
        """Return the live handle; raise TaskHandleEvicted if retention has
        aged it out (spec §TASK Error Type — `task_handle_evicted`)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                return task
            if task_id in self._evicted:
                raise TaskHandleEvicted(
                    f"task {task_id} evicted from retention ring")
        # Not local — see if the store has a snapshot so callers can still
        # inspect terminal state. We don't rehydrate a live handle.
        snap = self.store.get_metadata(task_id)
        if snap is not None:
            raise TaskHandleEvicted(
                f"task {task_id} is terminal; snapshot-only — "
                f"status={snap.get('status')!r}")
        raise TaskStateError(f"unknown task {task_id}")

    # -- Peer control (cross-session) ---------------------------------------

    def _check_peer_acl(self, task_snap: dict, op: str, *,
                       caller_agent: str | None,
                       caller_roles: frozenset[str] | None) -> None:
        """Enforce the task's auth policy for the current session acting as
        a peer (spec §TASK — Auth and peer control)."""
        # Owner always has implicit "all".
        if task_snap.get("owner_session") == self.session_id:
            return
        auth = task_snap.get("auth") or "user"
        caller_agent = caller_agent or self.agent_id
        caller_roles = caller_roles or frozenset()
        # Named policies
        if auth == "owner-only":
            raise TaskAuthDenied(
                f"task {task_snap.get('id')} auth='owner-only' — peers denied")
        if auth == "user":
            if task_snap.get("user_id") != self.user_id:
                raise TaskAuthDenied(
                    f"task {task_snap.get('id')} auth='user' — peers "
                    f"outside user scope denied")
            return
        if auth == "workspace":
            if task_snap.get("workspace_id") != self.workspace_id:
                raise TaskAuthDenied(
                    f"task {task_snap.get('id')} auth='workspace' — peers "
                    f"outside workspace denied")
            return
        # EXPLICIT_ACL
        parsed = _parse_auth(auth)
        if isinstance(parsed, str):                      # shouldn't happen — named handled above
            return
        for entry in parsed:
            if op not in entry.ops:
                continue
            if _caller_matches(entry,
                               caller_agent=caller_agent,
                               caller_session=self.session_id,
                               caller_roles=caller_roles,
                               caller_workspace=self.workspace_id):
                return
        raise TaskAuthDenied(
            f"task {task_snap.get('id')} ACL denies op={op!r} to this caller")

    def peer_cancel(self, task_id: str, *, confirm: bool = False,
                    grace_ms: int = 2000,
                    caller_roles: frozenset[str] | None = None) -> None:
        """Peer-session destructive op: cancel a task owned by another session.
        Requires `confirm=True` (spec §TASK — Auth and peer control)."""
        self._peer_destructive(task_id, "cancel", confirm=confirm,
                               grace_ms=grace_ms, signal=_signal.SIGTERM,
                               terminal="cancelled", caller_roles=caller_roles)

    def peer_kill(self, task_id: str, *, confirm: bool = False,
                  caller_roles: frozenset[str] | None = None) -> None:
        """Peer-session destructive op: hard kill a task owned by another session."""
        self._peer_destructive(task_id, "kill", confirm=confirm,
                               grace_ms=0, signal=_signal.SIGKILL,
                               terminal="killed", caller_roles=caller_roles)

    def _peer_destructive(self, task_id: str, op_name: str, *,
                          confirm: bool, grace_ms: int, signal: int,
                          terminal: str,
                          caller_roles: frozenset[str] | None) -> None:
        snap = self.store.get_metadata(task_id)
        if snap is None:
            raise TaskStateError(f"unknown task {task_id}")
        if snap.get("owner_session") != self.session_id and not confirm:
            raise TaskAuthDenied(
                f"peer {op_name}() on task {task_id} requires confirm=true")
        self._check_peer_acl(snap, "destruct", caller_agent=None,
                             caller_roles=caller_roles)
        # Audit log against the owning session's trail.
        self._telemetry("task.peer_control", {
            "task_id": task_id, "op": op_name,
            "owner_session": snap.get("owner_session"),
            "by_session": self.session_id,
            "by_agent": self.agent_id,
        })
        owner_runtime: TaskRuntime
        task: Task | None
        with self._lock:
            task = self._tasks.get(task_id)
        if task is not None:
            owner_runtime = self
        else:
            # Route to the owning runtime via the process-level handle registry.
            hit = _lookup_handle(task_id)
            if hit is not None:
                owner_runtime, task = hit
            else:
                # No live handle anywhere in this process — mark snapshot as
                # terminal so cross-process observers see the state change.
                snap["status"] = terminal
                snap["finished_at"] = _now().isoformat()
                try:
                    self.store.put_metadata(snap)
                except Exception as exc:
                    raise TaskBackendUnavailable(
                        f"store refused peer {op_name} snapshot: {exc}") from exc
                return
        owner_runtime._cancel(task, grace_ms=grace_ms, signal=signal, terminal=terminal)  # type: ignore[arg-type]

    def peer_extend_budget(self, task_id: str, ms: int, *,
                           caller_roles: frozenset[str] | None = None) -> None:
        """Peer-session control op — extend budget on a task owned by another
        session. `control` ACL op required."""
        snap = self.store.get_metadata(task_id)
        if snap is None:
            raise TaskStateError(f"unknown task {task_id}")
        self._check_peer_acl(snap, "control", caller_agent=None,
                             caller_roles=caller_roles)
        self._telemetry("task.peer_control",
                        {"task_id": task_id, "op": "extend_budget",
                         "by_session": self.session_id, "ms": ms})
        task = self._tasks.get(task_id)
        if task is None:
            hit = _lookup_handle(task_id)
            if hit is not None:
                _, task = hit
        if task is not None:
            task.extend_budget(ms)

    def peer_resume(self, task_id: str, *,
                    caller_roles: frozenset[str] | None = None) -> None:
        """Peer-session control op — resume a suspended task."""
        snap = self.store.get_metadata(task_id)
        if snap is None:
            raise TaskStateError(f"unknown task {task_id}")
        self._check_peer_acl(snap, "control", caller_agent=None,
                             caller_roles=caller_roles)
        self._telemetry("task.peer_control",
                        {"task_id": task_id, "op": "resume",
                         "by_session": self.session_id})
        task = self._tasks.get(task_id)
        if task is None:
            hit = _lookup_handle(task_id)
            if hit is not None:
                _, task = hit
        if task is not None:
            task.resume()

    def render_task_status_block(self, snap: dict, *,
                                 initial: bool = False,
                                 tail_lines: int = 3) -> str:
        """Render a [task-status] block suitable for turn-boundary injection."""
        visible = ("initial" if initial else "this-session")
        lines = [
            f"[task-status id={snap['id']} owner={snap.get('owner_session','?')} "
            f"scope={snap.get('scope','?')} visible_to={visible} "
            f"elapsed={_format_elapsed(snap.get('elapsed_ms', 0))} "
            f"budget={_format_budget(snap.get('budget_ms'))} "
            f"status={snap.get('status','?')} "
            f"last_update={_now().isoformat()}]",
            f"  cmd: {snap.get('cmd','')}",
        ]
        if initial:
            lines.append(f"  spawned_by: session {snap.get('owner_session','?')}")
        last = snap.get("last_line", "")
        if last:
            lines.append(f"  last: {last!r}")
        fires = snap.get("wake_fires", [])
        if fires:
            lines.append(f"  wake_fires: {len(fires)} (latest: {fires[-1].get('match', fires[-1].get('line',''))!r})")
        status = snap.get("status", "")
        if status == "suspended":
            lines.append("  options: [resume, extend_budget(ms=...), cancel, kill]")
        else:
            lines.append("  options: [observe, cancel(confirm=true), extend_budget(ms=...)]")
        lines.append("[/task-status]")
        return "\n".join(lines)

    def ambient_surface_blocks(self, *, initial: bool = False) -> list[str]:
        """Compute the set of [task-status] blocks to inject at the next
        turn boundary. Cadence semantics per docs/protocol.md §TASK."""
        out: list[str] = []
        for snap in self.list_visible_tasks():
            status = snap.get("status")
            if status in {"done", "failed", "cancelled", "killed"}:
                if initial:
                    continue                              # don't replay history on resume
                # Completed tasks surface once at next turn boundary; cadence
                # mgmt requires persisting "last_surface_at" which we do via
                # _terminal_rings. For a first-pass implementation we let
                # callers filter by status.
                out.append(self.render_task_status_block(snap))
                continue
            if status == "suspended":
                out.append(self.render_task_status_block(snap))
                continue
            # Running / idle / overrun — surface based on cadence
            elapsed_ms = snap.get("elapsed_ms", 0)
            budget_ms = snap.get("budget_ms")
            if budget_ms is not None and elapsed_ms > budget_ms:
                out.append(self.render_task_status_block(snap))
            elif budget_ms is not None and elapsed_ms >= int(budget_ms * 0.8):
                out.append(self.render_task_status_block(snap))
            elif initial:
                out.append(self.render_task_status_block(snap, initial=True))
        if out:
            self._telemetry("task.surfaced_ambient",
                            {"count": len(out), "initial": initial,
                             "session": self.session_id})
        return out


# ---------------------------------------------------------------------------
# Combinators — await_any / await_all
# ---------------------------------------------------------------------------

def await_any(tasks: list[Task], timeout_ms: int | None = None) -> Task:
    """Return the first task that becomes terminal (or raises TimeoutError)."""
    deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000.0
    while True:
        for t in tasks:
            if t._done_event.is_set():
                return t
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("await_any timeout")
        # Wait on any wake event
        for t in tasks:
            t._wake_event.wait(timeout=0.05)
            t._wake_event.clear()


def await_all(tasks: list[Task], timeout_ms: int | None = None) -> list[Task]:
    """Block until every task is terminal."""
    deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000.0
    for t in tasks:
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        to_ms = None if remaining is None else int(remaining * 1000)
        t.await_done(timeout_ms=to_ms)
    return tasks


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_elapsed(ms: int) -> str:
    if ms < 1000:
        return f"{ms}ms"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _format_budget(ms: int | None) -> str:
    if ms is None:
        return "unbounded"
    if ms == 0:
        return "0"
    return _format_elapsed(ms)


# ---------------------------------------------------------------------------
# Module-level convenience — bind a default runtime for single-process use
# ---------------------------------------------------------------------------

_default_runtime: TaskRuntime | None = None
_default_lock = threading.Lock()


def default_runtime() -> TaskRuntime:
    """Lazy module-level runtime — MemoryTaskStore, default config."""
    global _default_runtime
    with _default_lock:
        if _default_runtime is None:
            _default_runtime = TaskRuntime()
    return _default_runtime


def set_default_runtime(runtime: TaskRuntime) -> None:
    """Install a configured runtime as the module default."""
    global _default_runtime
    with _default_lock:
        _default_runtime = runtime


def spawn(cmd: str | list[str], **kwargs) -> Task:
    """Module-level shortcut — delegates to the default runtime."""
    return default_runtime().spawn(cmd, **kwargs)


def spawn_tool(tool_name: str, args: dict | None = None, **kwargs) -> Task:
    """Module-level shortcut for tool-call tasks."""
    return default_runtime().spawn_tool(tool_name, args, **kwargs)
