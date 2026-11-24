"""A compact, stateful, read-only graph engine for LLM sessions.

One MCP tool (``graph``) and one versioned wire protocol replace a family of
separate ``dev_*`` calls.  The engine is a *view* over the existing services:

* ``ozagia.integrations.mcp.graph_surface.GraphSurface`` for ``find``,
  ``outline``, ``expand`` and ``impact``;
* ``ozagia.core.memory.DevMemory`` for context and test discovery;
* ``ozagia.core.repo_shape`` for the bounded repository overview.

It deliberately has no edit, create, check, refresh or shell operation.  A
result is withheld whenever the snapshot is stale or its freshness cannot be
verified.

Wire version: ``ozagia.graph_engine/2``.  See
``docs/plans/2026-11-25_graph_engine_protocol.md`` and
``docs/graph_engine.md``.

Speculation is asynchronous in this slice: after a response with ``spec=true``
the engine queues at most six suggestions, a single in-process daemon worker
prepares at most two bounded payloads, and the handler returns without waiting
for it.  The worker is generation/snapshot/scope checked, deadline-bounded and
cancellable.  It still shares the interpreter's GIL and is not a daemon process.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import multiprocessing
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

PROTOCOL = "ozagia.graph_engine/2"
PROTOCOL_VERSION = "2.0"
DEFAULT_BUDGET = 1200
MAX_BUDGET = 8000
MIN_BUDGET = 64
MAX_SESSIONS = 16
MAX_SPEC = 8
MAX_SPEC_PREPARE = 2
MAX_SPEC_SUGGEST = 6
SPEC_BUDGET = 400
SPEC_TTL_S = 30.0
SPEC_WORKER_BUDGET_MS = 300
MAX_FETCH_WAIT_MS = 250
SPEC_QUEUE_SIZE = 8
MAX_ROWS = 200

OPS = ("hello", "find", "outline", "expand", "impact", "tests", "context",
       "shape", "fetch", "bye")
FEATURES = ("compact", "known", "spec", "fetch", "async-spec")
CAP_BITS = {"find": 1, "outline": 2, "expand": 4, "impact": 8, "tests": 16,
            "context": 32, "shape": 64, "fetch": 128, "bye": 256}
CAP_MASK = sum(CAP_BITS.values())
_SHORT_KEYS = {
    "rows": "r", "cols": "c", "unchanged": "u", "truncated": "x",
    "omitted": "o", "next": "n", "spec": "q", "edges": "ed",
    "counts": "ct", "items": "it", "qop": "qo", "fetch_state": "fs",
}

# Short column names keep the tabular payload small.  ``_COL_NAMES`` is only
# used by the verbose debugging view.
FIND_COLS = ["k", "n", "f", "sp", "sig", "sum", "sc"]
OUTLINE_COLS = ["r", "k", "h", "n", "f", "sp", "sig", "sum", "e"]
EXPAND_COLS = ["k", "n", "f", "sp"]
IMPACT_COLS = ["g", "k", "d", "why"]
TESTS_COLS = ["k", "n", "f", "sp", "why"]
CONTEXT_COLS = ["k", "h", "n", "f", "sp", "why", "txt"]
SHAPE_COLS = ["sec", "k", "v"]
_COL_NAMES = {
    "k": "key", "h": "hash", "n": "name", "f": "file", "sp": "span",
    "sig": "signature", "sum": "summary", "sc": "score", "e": "edges",
    "r": "role", "g": "group", "d": "depth", "why": "why", "txt": "text",
    "sec": "section", "v": "value",
}
_TEXT_COLS = frozenset({"sig", "sum", "why", "txt", "v", "e"})
_TEXT_KEYS = frozenset({"signature", "summary", "why", "text", "value",
                        "edges", "description"})
_TRIM_LISTS = frozenset({"rows", "unchanged", "spec", "items", "edges"})


# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #
def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


ENGINE_TOOL_NAME = "graph"
ENGINE_TOOLS: list[dict[str, Any]] = [{
    "name": ENGINE_TOOL_NAME,
    "description": (
        "Stateful read-only graph session: hello, find, outline, expand, "
        "impact, tests, context, shape, fetch. Compact rows, token budget, "
        "known deltas, speculative next handles."),
    "inputSchema": _schema({
        "op": {"type": "string", "enum": list(OPS)},
        "session": {"type": "string"},
        "q": {"type": "string"},
        "kinds": {"type": "array", "items": {"type": "string"}},
        "key": {"type": "string"},
        "keys": {"type": "array", "items": {"type": "string"}},
        "edges": {"type": "array", "items": {"type": "string"}},
        "depth": {"type": "integer"},
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
        "wait_ms": {"type": "integer"},
        "task": {"type": "string"},
        "mode": {"type": "string", "enum": ["auto", "outline", "full"]},
        "budget": {"type": "integer"},
        "compact": {"type": "boolean"},
        "verbose": {"type": "boolean"},
        "caps": {"type": "boolean"},
        "detail": {"type": "boolean"},
        "stats": {"type": "boolean"},
        "cols": {"type": "boolean"},
        "known": {"type": "array", "items": {
            "type": "object",
            "properties": {"key": {"type": "string"},
                           "hash": {"type": "string"}},
            "required": ["key", "hash"],
            "additionalProperties": False}},
        "spec": {"type": "boolean"},
        "fetch": {"type": "array", "items": {"type": "string"}},
        "h": {"type": "string"},
    }, ["op"]),
}]


def description_tokens(tools: Iterable[dict[str, Any]] = ENGINE_TOOLS) -> int:
    """Estimated tokens (characters / 4) of the tool and parameter text."""
    chars = 0
    for tool in tools:
        chars += len(tool["description"])
        for spec in tool["inputSchema"]["properties"].values():
            chars += len(spec.get("description", ""))
    return chars // 4


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
class EngineError(ValueError):
    """A request cannot be answered as asked."""


def estimate_tokens(value: Any) -> int:
    """The same rough estimate the rest of ozagia reports (~4 chars/token)."""
    text = value if isinstance(value, str) else json.dumps(value)
    return max(1, (len(text) + 3) // 4)


def _json_size(value: Any) -> int:
    return len(json.dumps(value))


def _clean(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items()
            if v is not None and v is not False
            and v != "" and v != [] and v != {}}


def _kind(key: str) -> str:
    return str(key).partition(":")[0]


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _worker_budget_s() -> float:
    raw = os.environ.get("OZAGIA_ENGINE_SPEC_MS")
    try:
        milliseconds = int(raw) if raw is not None else SPEC_WORKER_BUDGET_MS
    except ValueError:
        milliseconds = SPEC_WORKER_BUDGET_MS
    return max(0.05, min(2.0, milliseconds / 1000.0))


def _span(start: Any, end: Any = None) -> list[int] | None:
    if start in (None, ""):
        return None
    try:
        first = int(start)
    except (TypeError, ValueError):
        return None
    try:
        last = int(end) if end not in (None, "") else first
    except (TypeError, ValueError):
        last = first
    return [first, last]


def _hash_item(item: dict[str, Any]) -> str:
    raw = json.dumps(item, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _target_for_key(key: str) -> str:
    """A key as the target string ``DevMemory.tests_for`` expects."""
    kind, _, body = str(key).partition(":")
    if kind in ("sym", "test") and "::" in body:
        return body.split("::", 1)[1]
    if kind in ("file", "doc"):
        return body
    return str(key)


def _row(cols: list[str], item: dict[str, Any]) -> list[Any]:
    return [item.get(col) for col in cols]


def _compact(items: list[dict[str, Any]], cols: list[str]) -> list[list[Any]]:
    return [_row(cols, item) for item in items]


def _short_keys(value: Any) -> Any:
    """Rename response metadata to its short wire spelling, recursively."""
    if isinstance(value, dict):
        return {_SHORT_KEYS.get(str(key), str(key)): _short_keys(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_short_keys(item) for item in value]
    return value


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
@dataclass
class EngineSession:
    id: str
    snap: str
    fresh: str = "unknown"
    budget: int = DEFAULT_BUDGET
    compact: bool = True
    cursor: dict[str, Any] = field(default_factory=dict)
    cols: dict[str, list[str]] = field(default_factory=dict)
    known: OrderedDict[str, str] = field(default_factory=OrderedDict)
    tokens: int = 0
    spec: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    spec_seq: int = 0
    spec_generation: int = 0
    scope: str = ""
    closed: bool = False
    ledger: dict[str, Any] = field(default_factory=lambda: {
        "prepared": 0, "fetched": 0, "wasted": 0, "evicted": 0,
        "cancelled": 0, "worker_ms": 0.0, "hits": 0, "misses": 0})
    touched: float = field(default_factory=time.monotonic)


@dataclass
class SpecJob:
    """One background speculation job, bound to a session generation."""
    job_id: str
    session_id: str
    generation: int
    scope: str
    snap: str
    fresh: str
    budget: int
    deadline: float
    requests: list[tuple[str, dict[str, Any]]]
    suggestions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PreparedSpec:
    """Worker output; the parent applies it only after revalidation."""
    results: list[tuple[str, dict[str, Any]]]
    reason: str | None = None
    worker_ms: float = 0.0


class BackgroundSpecWorker:
    """One daemon thread that prepares at most one job at a time.

    It blocks on a bounded queue (no busy spin), checks a deadline and a
    stop event between candidates, and can be closed.  The GIL still limits
    true parallelism with the handler; this overlaps I/O/CPU work only at the
    interpreter's scheduling granularity.
    """

    def __init__(self, engine: "GraphEngine", *,
                 queue_size: int = SPEC_QUEUE_SIZE) -> None:
        self.engine = engine
        self._queue: queue.Queue[SpecJob | None] = queue.Queue(
            maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="ozagia-engine-spec", daemon=True)
        self._thread.start()

    def submit(self, job: SpecJob) -> bool:
        if self._stop.is_set():
            return False
        try:
            self._ensure_thread()
            self._queue.put_nowait(job)
            return True
        except (queue.Full, RuntimeError):
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                self.engine._run_spec_job(job)
            except Exception:  # noqa: BLE001 - a worker failure cancels its job
                self.engine._cancel_job(job, "worker_error")
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        thread.join(timeout)
        if not thread.is_alive():
            self._thread = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def mode(self) -> str:
        return "closed" if self._stop.is_set() else "thread"


class InlineSpecWorker:
    """Synchronous worker for measurements and deterministic tests."""

    def __init__(self) -> None:
        self.engine: GraphEngine | None = None
        self.closed = False

    @property
    def mode(self) -> str:
        return "closed" if self.closed else "inline"

    def bind(self, engine: "GraphEngine") -> None:
        self.engine = engine

    def submit(self, job: SpecJob) -> bool:
        if self.closed or self.engine is None:
            return False
        self.engine._run_spec_job(job)
        return True

    def close(self, timeout: float = 1.0) -> None:
        self.closed = True


def _fork_available() -> bool:
    try:
        return ("fork" in multiprocessing.get_all_start_methods()
                and hasattr(os, "fork"))
    except Exception:  # noqa: BLE001 - no fork is an honest fallback
        return False


def _process_spec_main(conn: Any, memory: Any, root: str,
                       worker_budget_s: float) -> None:
    """Child loop: compute payloads only; never apply parent session state."""
    try:
        engine = GraphEngine(memory, workspace=root, spec_enabled=False)
        engine.worker_budget_s = worker_budget_s
    except Exception as exc:  # noqa: BLE001 - report a failed child honestly
        try:
            conn.send({"fatal": f"{type(exc).__name__}: {exc}"})
        except (OSError, EOFError):
            pass
        conn.close()
        return
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            break
        if job is None:
            break
        try:
            prepared = engine._prepare_spec_requests(job)
            conn.send({"job_id": job.job_id, "prepared": prepared})
        except Exception as exc:  # noqa: BLE001 - parent cancels the job
            try:
                conn.send({
                    "job_id": job.job_id,
                    "prepared": PreparedSpec([], "worker_error", 0.0),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            except (OSError, EOFError):
                break
    try:
        conn.close()
    except OSError:
        pass


class ProcessSpecWorker:
    """One forked child computes payloads; a parent listener applies them.

    The child inherits the graph snapshot through ``fork`` and builds its own
    ``GraphEngine`` with speculation disabled.  It sends ``PreparedSpec``
    messages; the parent validates the session generation/snapshot/scope/expiry
    before applying anything.  If the child dies or cannot be started, new
    jobs go to the thread fallback and outstanding jobs are cancelled.
    """

    def __init__(self, engine: "GraphEngine", *,
                 queue_size: int = SPEC_QUEUE_SIZE) -> None:
        self.engine = engine
        self.queue_size = max(1, int(queue_size))
        self._ctx = multiprocessing.get_context("fork")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._parent_conn: Any = None
        self._process: Any = None
        self._listener: threading.Thread | None = None
        self._jobs: dict[str, SpecJob] = {}
        self._fallback: BackgroundSpecWorker | None = None
        self._dead = False

    @property
    def mode(self) -> str:
        with self._lock:
            if self._stop.is_set():
                return "closed"
            if self._process is not None and self._process.is_alive() and not self._dead:
                return "process"
            if self._fallback is not None:
                return "thread"
            if self._process is None and not self._dead:
                return "process"
            return "dead"

    @property
    def alive(self) -> bool:
        with self._lock:
            process = self._process
            listener = self._listener
            fallback = self._fallback
        return bool((process is not None and process.is_alive())
                    or (listener is not None and listener.is_alive())
                    or (fallback is not None and fallback.alive))

    def _ensure_process_locked(self) -> None:
        if self._process is not None:
            return
        parent, child = self._ctx.Pipe(duplex=True)
        process = self._ctx.Process(
            target=_process_spec_main,
            args=(child, self.engine.mem, str(self.engine.root),
                  self.engine.worker_budget_s),
            name="ozagia-engine-spec-proc", daemon=True)
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._parent_conn = parent
        self._process = process
        self._listener = threading.Thread(
            target=self._listen, name="ozagia-engine-spec-ipc", daemon=True)
        self._listener.start()

    def _ensure_fallback_locked(self) -> None:
        if self._fallback is None:
            self._fallback = BackgroundSpecWorker(self.engine)

    def submit(self, job: SpecJob) -> bool:
        if self._stop.is_set():
            return False
        with self._lock:
            if self._process is not None and not self._process.is_alive():
                self._dead = True
            if not self._dead:
                try:
                    self._ensure_process_locked()
                except Exception:  # noqa: BLE001 - fork is not usable here
                    self._dead = True
            if not self._dead and self._process is not None:
                if self._process.is_alive():
                    self._jobs[job.job_id] = job
                    try:
                        self._parent_conn.send(job)
                        return True
                    except (OSError, EOFError, BrokenPipeError):
                        self._jobs.pop(job.job_id, None)
                        self._dead = True
            self._ensure_fallback_locked()
            fallback = self._fallback
        return fallback.submit(job) if fallback is not None else False

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._parent_conn.recv()
            except (EOFError, OSError):
                break
            if message is None:
                break
            if "fatal" in message:
                break
            job_id = str(message.get("job_id") or "")
            with self._lock:
                job = self._jobs.pop(job_id, None)
            if job is None:
                continue
            prepared = message.get("prepared")
            if message.get("error") or prepared is None:
                self.engine._cancel_job(job, "worker_error")
            else:
                self.engine._apply_spec_results(job, prepared)
        with self._lock:
            self._dead = True
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            self.engine._cancel_job(job, "worker_died")

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            conn = self._parent_conn
            process = self._process
            listener = self._listener
            fallback = self._fallback
        if conn is not None:
            try:
                conn.send(None)
            except (OSError, EOFError, BrokenPipeError):
                pass
        if listener is not None:
            listener.join(timeout)
        if process is not None:
            process.join(0.5)
            if process.is_alive():
                process.terminate()
                process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(0.5)
        if listener is not None and listener.is_alive():
            listener.join(1.0)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if fallback is not None:
            fallback.close()
        with self._lock:
            self._dead = True
            self._parent_conn = None
            self._process = None
            self._listener = None


class GraphEngine:
    """One bounded set of sessions over one ``DevMemory`` snapshot."""

    def __init__(self, memory: Any, *, workspace: str | Path | None = None,
                 max_sessions: int = MAX_SESSIONS,
                 spec_worker: Any | None = None,
                 spec_enabled: bool | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        if memory is None:
            raise ValueError("the graph engine needs a dev-memory snapshot")
        self.mem = memory
        self.root = Path(workspace or memory.raw_repo_root).resolve()
        self.max_sessions = max(1, int(max_sessions))
        self.sessions: OrderedDict[str, EngineSession] = OrderedDict()
        self._seq = 0
        self._surface_obj: Any = None
        self._shape_cache: tuple[str, dict[str, Any]] | None = None
        self._snap_cache: tuple[tuple[Any, ...], str] | None = None
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.spec_enabled = (_env_flag("OZAGIA_ENGINE_SPEC", True)
                             if spec_enabled is None else bool(spec_enabled))
        self.worker_budget_s = _worker_budget_s()
        if not self.spec_enabled:
            self.spec_worker: Any | None = None
        elif spec_worker is not None:
            self.spec_worker = spec_worker
        else:
            preference = os.environ.get("OZAGIA_ENGINE_SPEC_PROCESS",
                                        "auto").strip().lower()
            use_process = (preference not in {"0", "thread", "false", "off",
                                               "no"}
                           and _fork_available())
            if use_process:
                try:
                    self.spec_worker = ProcessSpecWorker(self)
                except Exception:  # noqa: BLE001 - unsafe fork falls back
                    self.spec_worker = BackgroundSpecWorker(self)
            else:
                self.spec_worker = BackgroundSpecWorker(self)
        if self.spec_worker is not None and hasattr(self.spec_worker, "bind"):
            self.spec_worker.bind(self)
        self.totals = {"sessions": 0, "evicted_sessions": 0,
                       "prepared": 0, "fetched": 0, "wasted": 0,
                       "evicted": 0, "cancelled": 0, "worker_ms": 0.0,
                       "hits": 0, "misses": 0}

    # -- public ------------------------------------------------------------- #
    def handle(self, args: dict[str, Any]) -> dict[str, Any]:
        """Answer one engine request. Never raises for a bad request."""
        if not isinstance(args, dict):
            return self._error("arguments must be an object")
        op = str(args.get("op") or "").strip()
        if op not in OPS:
            return self._error(f"unknown op {op!r}; known: {list(OPS)}")
        if op == "hello":
            return self._hello(args)
        session = self._session(args.get("session"))
        if session is None:
            return self._error("unknown or expired session; call hello")
        if op == "bye":
            self._close_session(session)
            result = {"bye": 1, "t": 0}
            result["t"] = estimate_tokens(result)
            return result
        fresh, snap = self._guard(session)
        if fresh != "fresh":
            return self._withheld(snap, fresh)
        budget = _clamp(args.get("budget", session.budget), MIN_BUDGET,
                        MAX_BUDGET, session.budget)
        session.budget = budget
        session.compact = bool(args.get("compact", session.compact))
        if op == "fetch":
            result = self._fetch(args, session, snap, budget)
        else:
            result = self._execute(op, args, session, snap, budget,
                                   allow_spec=True)
        result["t"] = estimate_tokens(result)
        session.tokens += int(result["t"])
        return result

    def stats(self) -> dict[str, Any]:
        """Bounded operational counters; no graph content."""
        worker = self.spec_worker
        return {
            "sessions": len(self.sessions),
            "max_sessions": self.max_sessions,
            "spec_enabled": bool(self.spec_enabled),
            "spec_per_session": MAX_SPEC,
            "prepared_per_response": MAX_SPEC_PREPARE,
            "worker_alive": bool(getattr(worker, "alive", False)),
            "worker_mode": getattr(worker, "mode", "none") if worker else "none",
            "totals": dict(self.totals),
        }

    def close(self) -> None:
        """Close every session and stop the background worker."""
        with self._lock:
            for session in list(self.sessions.values()):
                self._close_session_locked(session)
            self.sessions.clear()
        worker = self.spec_worker
        if worker is not None:
            worker.close()
        self.spec_worker = None

    # -- sessions ----------------------------------------------------------- #
    def _hello(self, args: dict[str, Any]) -> dict[str, Any]:
        existing = self._session(args.get("session"))
        if existing is not None:
            session = existing
            session.touched = time.monotonic()
        else:
            session = self._new_session()
        fresh, snap = self._freshness()
        with self._lock:
            session.snap = snap
            session.fresh = fresh
            session.scope = self._scope_key(snap)
            session.closed = False
        session.budget = _clamp(args.get("budget", session.budget),
                                MIN_BUDGET, MAX_BUDGET, DEFAULT_BUDGET)
        session.compact = bool(args.get("compact", True))
        result: dict[str, Any] = {
            "p": PROTOCOL,
            "v": PROTOCOL_VERSION,
            "sid": session.id,
            "s": snap,
            "f": fresh,
            "b": [DEFAULT_BUDGET, MAX_BUDGET],
            "c": CAP_MASK,
        }
        if args.get("caps") is True or args.get("detail") is True:
            state = self._last_freshness or {}
            result["ops"] = list(OPS)
            result["ft"] = list(FEATURES)
            result["bd"] = {"d": DEFAULT_BUDGET, "m": MAX_BUDGET,
                            "c": session.budget}
            result["sn"] = {
                "h": state.get("snapshot_head"),
                "g": state.get("generated_at"),
                "n": len(self.mem.nodes),
                "e": len(self.mem.edges),
            }
            result["compact"] = session.compact
            result["st"] = self._ledger(session)
        if args.get("stats") is True:
            result.setdefault("st", self._ledger(session))
        result["t"] = estimate_tokens(result)
        return result

    def _scope_key(self, snap: str) -> str:
        return f"{self.root}:{snap}"

    def _new_session(self) -> EngineSession:
        with self._lock:
            if len(self.sessions) >= self.max_sessions:
                _, old = self.sessions.popitem(last=False)
                self._expire_locked(old)
                self.totals["evicted_sessions"] += 1
            self._seq += 1
            session = EngineSession(id=f"s{self._seq}", snap="")
            self.sessions[session.id] = session
            self.totals["sessions"] += 1
            return session

    def _session(self, handle: Any) -> EngineSession | None:
        if not isinstance(handle, str):
            return None
        with self._lock:
            session = self.sessions.get(handle)
            if session is not None:
                session.touched = time.monotonic()
                self.sessions.move_to_end(handle)
            return session

    def _expire(self, session: EngineSession) -> None:
        with self._lock:
            self._expire_locked(session)

    def _expire_locked(self, session: EngineSession) -> None:
        self._cancel_entries_locked(session, reason="evicted")
        session.closed = True
        session.spec_generation += 1
        session.spec.clear()

    # -- freshness and snapshot identity ------------------------------------ #
    def _freshness(self) -> tuple[str, str]:
        """(freshness, snapshot token). Never trusts a stale or unknown tree."""
        try:
            state = self.mem.staleness(force=True, allow_project_adapters=False)
        except Exception as exc:  # noqa: BLE001 - unknown is an honest result
            self._last_freshness = {"freshness": "unknown",
                                    "verification_errors": [str(exc)]}
            return "unknown", self._snapshot_token()
        self._last_freshness = state
        freshness = str(state.get("freshness") or "unknown")
        return freshness, self._snapshot_token(state)

    @property
    def _last_freshness(self) -> dict[str, Any]:
        return getattr(self, "_freshness_state", {})

    @_last_freshness.setter
    def _last_freshness(self, value: dict[str, Any]) -> None:
        self._freshness_state = value

    def _snapshot_token(self, state: dict[str, Any] | None = None) -> str:
        state = state or self._last_freshness
        stamp = (id(self.mem), getattr(self.mem, "_index_version", 0),
                 len(self.mem.nodes), len(self.mem.edges),
                 getattr(self.mem, "_next_id", 0),
                 state.get("snapshot_head"), state.get("generated_at"),
                 state.get("current_tree_digest"))
        if self._snap_cache is not None and self._snap_cache[0] == stamp:
            return self._snap_cache[1]
        digest = hashlib.sha256()
        digest.update(str(self.root).encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(str(stamp[5]).encode("utf-8", "replace"))
        digest.update(b"\0")
        for key in sorted(getattr(self.mem, "_key_to_id", {})):
            digest.update(key.encode("utf-8", "replace"))
            digest.update(b"\0")
        token = digest.hexdigest()[:16]
        self._snap_cache = (stamp, token)
        return token

    def _guard(self, session: EngineSession) -> tuple[str, str]:
        fresh, snap = self._freshness()
        with self._lock:
            if fresh != "fresh":
                self._drop_specs_locked(session, reason="stale")
                session.fresh = fresh
                session.snap = snap
                session.scope = self._scope_key(snap)
                return fresh, snap
            if session.snap and session.snap != snap:
                self._drop_specs_locked(session, reason="snapshot_changed")
                session.scope = self._scope_key(snap)
            session.snap = snap
            session.fresh = fresh
        return fresh, snap

    def _drop_specs(self, session: EngineSession, *, wasted: bool = True) -> None:
        with self._lock:
            self._drop_specs_locked(session,
                                    reason="wasted" if wasted else "evicted")

    def _drop_specs_locked(self, session: EngineSession,
                           *, reason: str) -> None:
        self._cancel_entries_locked(session, reason=reason)
        session.spec_generation += 1
        session.spec.clear()

    def _cancel_entries_locked(self, session: EngineSession,
                               *, reason: str) -> None:
        for handle, entry in list(session.spec.items()):
            status = entry.get("status", "pending")
            if status in ("pending", "ready"):
                self._cancel_entry_locked(session, handle, reason=reason)
                session.ledger["evicted"] += 1
                self.totals["evicted"] += 1

    def _close_session(self, session: EngineSession) -> None:
        with self._lock:
            self._close_session_locked(session)
            self.sessions.pop(session.id, None)

    def _close_session_locked(self, session: EngineSession) -> None:
        self._cancel_entries_locked(session, reason="closed")
        session.closed = True
        session.spec_generation += 1
        session.spec.clear()
        session.known.clear()
        session.cols.clear()

    # -- dispatch ----------------------------------------------------------- #
    def _execute(self, op: str, args: dict[str, Any], session: EngineSession,
                 snap: str, budget: int, *, allow_spec: bool,
                 compact: bool = True,
                 cache_cols: bool = True) -> dict[str, Any]:
        try:
            payload = self._op(op, args, session)
        except (EngineError, ValueError) as exc:
            return self._error(str(exc), snap=snap)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return self._error(f"{type(exc).__name__}: {exc}", snap=snap)
        keys = list(payload.pop("__keys", []) or [])
        next_args = payload.pop("__next", None)
        offset = max(0, int(args.get("offset", 0) or 0))
        total_rows = len(payload.get("rows") or [])
        result: dict[str, Any] = dict(payload)
        if allow_spec and args.get("spec") is True:
            result.update(self._speculate(session, op, args, keys, snap,
                                          budget))
        if args.get("stats") is True:
            result["st"] = self._ledger(session)
        verbose = bool(args.get("verbose")) or not session.compact
        if verbose:
            result = self._verbose(result)
        result = self._fit(result, budget)
        rows = result.get("rows")
        session.cursor = {"op": op, "offset": offset + len(rows or [])}
        if (next_args is not None and isinstance(rows, list)
                and len(rows) < total_rows):
            next_args = dict(next_args)
            next_args["offset"] = offset + len(rows)
            result["next"] = next_args
            session.cursor = {"op": op, "offset": next_args["offset"]}
        result = self._enforce_budget(result, budget)
        if cache_cols and not verbose:
            result = self._maybe_omit_cols(result, op, session,
                                           force=args.get("cols") is True)
        return _short_keys(result) if compact else result

    def _enforce_budget(self, result: dict[str, Any],
                        budget: int) -> dict[str, Any]:
        """A final hard check after optional metadata such as ``next``.

        Optional metadata (``total``, ``budget``, ``next``) is dropped before
        rows, and the freshness label is never dropped.  Rows are always
        accompanied by ``truncated``/``omitted`` when the budget cuts them.
        """
        limit = max(1, budget - 4)
        while estimate_tokens(result) > limit:
            removed = False
            for key in ("total", "budget", "next"):
                if key in result:
                    result.pop(key)
                    removed = True
                    break
            if removed:
                continue
            rows = result.get("rows")
            if isinstance(rows, list) and rows:
                rows.pop()
                result["truncated"] = True
                result["omitted"] = int(result.get("omitted") or 0) + 1
                continue
            if "cols" in result:
                result.pop("cols")
                continue
            result = {"truncated": True}
            break
        return result

    def _op(self, op: str, args: dict[str, Any],
            session: EngineSession) -> dict[str, Any]:
        handler: Callable[[dict[str, Any], EngineSession], dict[str, Any]] = {
            "find": self._find,
            "outline": self._outline,
            "expand": self._expand,
            "impact": self._impact,
            "tests": self._tests,
            "context": self._context,
            "shape": self._shape,
        }[op]
        return handler(args, session)

    # -- rows, known deltas, verbose ---------------------------------------- #
    @staticmethod
    def _known(args: dict[str, Any], session: EngineSession) -> dict[str, str]:
        known: dict[str, str] = {}
        for entry in args.get("known") or []:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            digest = entry.get("hash")
            if isinstance(key, str) and key and isinstance(digest, str):
                known[key] = digest
        return known

    @staticmethod
    def _remember(session: EngineSession, key: str, digest: str) -> None:
        session.known[key] = digest
        session.known.move_to_end(key)
        while len(session.known) > 1000:
            session.known.popitem(last=False)

    def _known_rows(
            self, session: EngineSession, args: dict[str, Any],
            items: list[dict[str, Any]], cols: list[str]
    ) -> tuple[list[list[Any]], list[str]]:
        """Split items into changed rows and unchanged key handles."""
        known = self._known(args, session)
        rows: list[dict[str, Any]] = []
        unchanged: list[str] = []
        for item in items:
            key = str(item.get("k") or "")
            digest = _hash_item(item)
            item = {**item, "h": digest}
            if key:
                self._remember(session, key, digest)
                if known.get(key) == digest:
                    unchanged.append(key)
                    continue
            rows.append(item)
        return _compact(rows, cols), unchanged

    def _verbose(self, result: dict[str, Any]) -> dict[str, Any]:
        """Expand short tabular rows into named dicts for debugging."""
        result = dict(result)
        if result.get("cols") and isinstance(result.get("rows"), list):
            cols = list(result.pop("cols"))
            result["rows"] = [
                {_COL_NAMES.get(col, col): value
                 for col, value in zip(cols, row)}
                for row in result["rows"]
            ]
        items = result.get("items")
        if isinstance(items, list):
            result["items"] = [
                self._verbose(item) if isinstance(item, dict) else item
                for item in items
            ]
        return result

    def _maybe_omit_cols(self, result: dict[str, Any], op: str,
                         session: EngineSession, *, force: bool) -> dict[str, Any]:
        """Send fixed per-op columns once per session unless asked again."""
        cols = result.get("cols")
        if not isinstance(cols, list) or not cols:
            return result
        current = [str(col) for col in cols]
        if force:
            session.cols[op] = current
            return result
        if session.cols.get(op) == current:
            result.pop("cols")
        else:
            session.cols[op] = current
        return result

    # -- fitting ------------------------------------------------------------ #
    def _fit(self, result: dict[str, Any], budget: int) -> dict[str, Any]:
        """Trim the payload until the rendered JSON fits the estimated budget.

        The final ``tok`` field is added by the caller.  Eight tokens are kept
        for it.  Lists are trimmed from the end, long text cells first, so a
        caller always gets a usable prefix rather than a silent overflow.
        """
        target = max(120, (budget - 8) * 4)
        omitted = int(result.get("omitted") or 0)
        for _ in range(1000):
            if _json_size(result) <= target:
                break
            best = self._longest_text_cell(result)
            if best is not None:
                container, key, value, column = best
                keep = max(48, len(value) // 2)
                container[key] = value[:keep].rstrip() + "..."
                result["truncated"] = True
                continue
            lists = self._trimmable_lists(result)
            if not lists:
                break
            container, key, values = max(
                lists, key=lambda item: _json_size(item[2]))
            values.pop()
            omitted += 1
            result["truncated"] = True
            result["omitted"] = omitted
        if omitted:
            result["omitted"] = omitted
            result["truncated"] = True
        # A budget this small should still produce an honest, tiny answer.
        if _json_size(result) > target:
            result = {"truncated": True}
        return result

    def _longest_text_cell(
            self, result: dict[str, Any]
    ) -> tuple[Any, Any, str, str] | None:
        best: tuple[Any, Any, str, str] | None = None
        for container, key, rows, cols in self._tables(result):
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, list):
                    for index, value in enumerate(row):
                        column = cols[index] if index < len(cols) else ""
                        if (column in _TEXT_COLS and isinstance(value, str)
                                and len(value) > 48):
                            if best is None or len(value) > len(best[2]):
                                best = (row, index, value, column)
                elif isinstance(row, dict):
                    for column, value in row.items():
                        if (column in _TEXT_KEYS
                                and isinstance(value, str) and len(value) > 48):
                            if best is None or len(value) > len(best[2]):
                                best = (row, column, value, column)
        return best

    def _tables(self, result: dict[str, Any]) -> Iterable[
            tuple[Any, Any, Any, list[str]]]:
        if isinstance(result.get("rows"), list):
            yield (result, "rows", result["rows"],
                   list(result.get("cols") or []))
        for item in result.get("items") or []:
            if isinstance(item, dict):
                yield from self._tables(item)

    def _trimmable_lists(
            self, result: dict[str, Any]
    ) -> list[tuple[Any, Any, list[Any]]]:
        found: list[tuple[Any, Any, list[Any]]] = []

        def walk(obj: Any) -> None:
            if not isinstance(obj, dict):
                return
            for key, value in obj.items():
                if key in _TRIM_LISTS and isinstance(value, list) and value:
                    found.append((obj, key, value))
                elif key == "items" and isinstance(value, list):
                    for item in value:
                        walk(item)

        walk(result)
        return found

    # -- speculation -------------------------------------------------------- #
    def _speculate(self, session: EngineSession, op: str,
                   args: dict[str, Any], keys: list[str], snap: str,
                   budget: int) -> dict[str, Any]:
        if not self.spec_enabled or self.spec_worker is None:
            return {"spec": {"h": [], "r": 0, "p": 0}}
        eligible = [request for request in self._candidates(op, args, keys)
                    if not self._same_request(op, args, request)]
        planned = eligible[:MAX_SPEC_PREPARE]
        remaining = max(0, len(eligible) - len(planned))
        if not planned:
            return {"spec": {"h": [], "r": 0, "p": remaining}}
        now = self._clock()
        handles: list[str] = []
        requests: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            self._cancel_pending_locked(session, reason="superseded")
            session.spec_generation += 1
            generation = session.spec_generation
            while len(session.spec) + len(planned) > MAX_SPEC:
                self._evict_one_locked(session)
            for request in planned:
                handle = f"q{session.spec_seq + 1}"
                session.spec_seq += 1
                session.spec[handle] = {
                    "request": request,
                    "result": None,
                    "snap": snap,
                    "generation": generation,
                    "scope": session.scope,
                    "status": "pending",
                    "created": now,
                    "expires": now + SPEC_TTL_S,
                    "fetched": False,
                    "event": threading.Event(),
                }
                handles.append(handle)
                requests.append((handle, request))
        job = SpecJob(job_id=f"{session.id}:{generation}",
                      session_id=session.id, generation=generation,
                      scope=session.scope, snap=snap, fresh=session.fresh,
                      budget=min(SPEC_BUDGET, budget),
                      deadline=now + self.worker_budget_s,
                      requests=requests,
                      suggestions=eligible[:MAX_SPEC_SUGGEST])
        if not self.spec_worker.submit(job):
            with self._lock:
                self._cancel_handles_locked(session, handles,
                                            reason="queue_full")
            return {"spec": {"h": [], "r": 0, "p": remaining}}
        with self._lock:
            ready = sum(1 for handle in handles
                        if (session.spec.get(handle) or {}).get("status")
                        == "ready")
        return {"spec": {"h": handles, "r": ready, "p": remaining}}

    def _candidates(self, op: str, args: dict[str, Any],
                    keys: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in keys[:3]:
            kind = _kind(key)
            requests: list[dict[str, Any]] = [
                {"op": "outline", "key": key},
            ]
            if kind in ("sym", "test"):
                requests.append({"op": "expand", "keys": [key],
                                 "edges": ["callers"], "depth": 1,
                                 "limit": 8})
                requests.append({"op": "expand", "keys": [key],
                                 "edges": ["callees"], "depth": 1,
                                 "limit": 8})
            requests.append({"op": "tests", "key": key})
            for request in requests:
                signature = json.dumps(request, sort_keys=True)
                if signature in seen:
                    continue
                seen.add(signature)
                out.append(request)
                if len(out) >= MAX_SPEC_SUGGEST:
                    return out
        return out

    @staticmethod
    def _same_request(op: str, args: dict[str, Any],
                      request: dict[str, Any]) -> bool:
        if request.get("op") != op:
            return False
        if op == "outline":
            return request.get("key") == args.get("key")
        if op == "tests":
            return request.get("key") == args.get("key")
        if op == "expand":
            return (request.get("keys") == args.get("keys")
                    and request.get("edges") == args.get("edges"))
        return False

    def _job_valid_locked(self, session: EngineSession | None,
                          job: SpecJob) -> bool:
        return bool(session is not None and not session.closed
                    and session.spec_generation == job.generation
                    and session.snap == job.snap
                    and session.fresh == job.fresh
                    and session.scope == job.scope)

    def _job_valid(self, job: SpecJob) -> bool:
        with self._lock:
            return self._job_valid_locked(
                self.sessions.get(job.session_id), job)

    def _cancel_pending_locked(self, session: EngineSession,
                               *, reason: str) -> None:
        for handle, entry in list(session.spec.items()):
            if entry.get("status") != "pending":
                continue
            self._cancel_entry_locked(session, handle, reason=reason)
            session.spec.pop(handle, None)
            session.ledger["evicted"] += 1
            self.totals["evicted"] += 1

    def _cancel_entry_locked(self, session: EngineSession, handle: str,
                             *, reason: str) -> None:
        entry = session.spec.get(handle)
        if entry is None:
            return
        status = entry.get("status")
        if status not in ("pending", "ready"):
            return
        if status == "ready" and not entry.get("fetched"):
            session.ledger["wasted"] += 1
            self.totals["wasted"] += 1
        elif status == "pending":
            session.ledger["cancelled"] += 1
            self.totals["cancelled"] += 1
        entry["status"] = "cancelled"
        entry["reason"] = reason
        event = entry.get("event")
        if event is not None:
            event.set()

    def _cancel_handles_locked(self, session: EngineSession,
                               handles: Iterable[str], *, reason: str) -> None:
        for handle in handles:
            self._cancel_entry_locked(session, handle, reason=reason)

    def _cancel_job(self, job: SpecJob, reason: str,
                    handles: Iterable[str] | None = None) -> None:
        with self._lock:
            session = self.sessions.get(job.session_id)
            if session is None:
                return
            targets = list(handles) if handles is not None else [
                handle for handle, _request in job.requests]
            self._cancel_handles_locked(session, targets, reason=reason)

    def _evict_one_locked(self, session: EngineSession) -> None:
        if not session.spec:
            return
        _, entry = session.spec.popitem(last=False)
        status = entry.get("status")
        if status == "ready" and not entry.get("fetched"):
            session.ledger["wasted"] += 1
            self.totals["wasted"] += 1
        elif status == "pending":
            session.ledger["cancelled"] += 1
            self.totals["cancelled"] += 1
        event = entry.get("event")
        if event is not None:
            event.set()
        session.ledger["evicted"] += 1
        self.totals["evicted"] += 1

    def _prepare_spec_requests(self, job: SpecJob) -> PreparedSpec:
        """Compute payloads in a detached session; never apply parent state."""
        detached = EngineSession(id=job.session_id, snap=job.snap,
                                 fresh=job.fresh, scope=job.scope,
                                 budget=job.budget)
        results: list[tuple[str, dict[str, Any]]] = []
        started = self._clock()
        reason: str | None = None
        for handle, request in job.requests:
            if self._clock() >= job.deadline:
                reason = "budget"
                break
            try:
                payload = self._execute(
                    request["op"], dict(request), detached, job.snap,
                    job.budget, allow_spec=False, compact=False,
                    cache_cols=False)
            except Exception as exc:  # noqa: BLE001 - parent cancels it
                payload = {"e": f"{type(exc).__name__}: {exc}"}
            results.append((handle, payload))
            time.sleep(0)  # yield between candidates; do not spin
        if len(results) < len(job.requests) and reason is None:
            reason = "budget"
        worker_ms = max(0.0, (self._clock() - started) * 1000.0)
        return PreparedSpec(results, reason, worker_ms)

    def _run_spec_job(self, job: SpecJob) -> None:
        """Thread/inline path: prepare, then apply in the parent."""
        if not self._job_valid(job):
            self._cancel_job(job, "stale")
            return
        prepared = self._prepare_spec_requests(job)
        self._apply_spec_results(job, prepared)

    def _apply_spec_results(self, job: SpecJob,
                            prepared: PreparedSpec) -> None:
        """Validate the job again before mutating the parent session."""
        self.totals["worker_ms"] += prepared.worker_ms
        with self._lock:
            session = self.sessions.get(job.session_id)
            if not self._job_valid_locked(session, job):
                if session is not None:
                    self._cancel_handles_locked(
                        session, [item[0] for item in job.requests],
                        reason="stale")
                return
            session.ledger["worker_ms"] += prepared.worker_ms
            applied: set[str] = set()
            for handle, payload in prepared.results:
                entry = session.spec.get(handle)
                if entry is None or entry.get("status") != "pending":
                    continue
                applied.add(handle)
                if self._clock() > entry.get("expires", self._clock()):
                    self._cancel_entry_locked(session, handle,
                                              reason="expired")
                    continue
                if "e" in payload:
                    entry["status"] = "cancelled"
                    entry["reason"] = "error"
                    entry["error"] = payload["e"]
                    session.ledger["cancelled"] += 1
                    self.totals["cancelled"] += 1
                else:
                    entry["result"] = payload
                    entry["status"] = "ready"
                    session.ledger["prepared"] += 1
                    self.totals["prepared"] += 1
                event = entry.get("event")
                if event is not None:
                    event.set()
            reason = prepared.reason or "budget"
            for handle, _request in job.requests:
                if handle not in applied:
                    self._cancel_entry_locked(session, handle, reason=reason)

    @staticmethod
    def _ledger(session: EngineSession) -> dict[str, Any]:
        hits = int(session.ledger.get("hits", 0))
        misses = int(session.ledger.get("misses", 0))
        attempts = hits + misses
        return {
            "prepared": int(session.ledger.get("prepared", 0)),
            "fetched": int(session.ledger.get("fetched", 0)),
            "wasted": int(session.ledger.get("wasted", 0)),
            "evicted": int(session.ledger.get("evicted", 0)),
            "cancelled": int(session.ledger.get("cancelled", 0)),
            "worker_ms": round(float(session.ledger.get("worker_ms", 0.0)), 1),
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / attempts, 3) if attempts else 0.0,
        }

    # -- fetch -------------------------------------------------------------- #
    def _fetch(self, args: dict[str, Any], session: EngineSession,
               snap: str, budget: int) -> dict[str, Any]:
        raw = args.get("fetch")
        handles = [str(x) for x in raw] if isinstance(raw, list) else []
        if not handles and args.get("h"):
            handles = [str(args["h"])]
        if not handles:
            return self._error("fetch needs one or more handles", snap=snap)
        if len(handles) > 4:
            return self._error("at most four handles per fetch", snap=snap)
        wait_ms = _clamp(args.get("wait_ms", 0), 0, MAX_FETCH_WAIT_MS, 0)
        wait_deadline = time.monotonic() + wait_ms / 1000.0
        items: list[dict[str, Any]] = []
        for handle in handles:
            while True:
                event = None
                timeout = 0.0
                with self._lock:
                    entry = session.spec.get(handle)
                    if entry is None:
                        return self._error(
                            f"unknown or expired spec handle {handle!r}",
                            snap=snap)
                    if entry.get("snap") != snap:
                        self._drop_specs_locked(session,
                                                reason="snapshot_changed")
                        return self._withheld(snap, "stale")
                    prepared_op = str(
                        (entry.get("request") or {}).get("op") or "")
                    now = self._clock()
                    if now > entry.get("expires", now):
                        self._cancel_entry_locked(session, handle,
                                                  reason="expired")
                    status = entry.get("status")
                    if status == "ready":
                        payload = dict(entry["result"])
                        for key in ("next", "spec", "st"):
                            payload.pop(key, None)
                        payload["h"] = handle
                        payload["qop"] = prepared_op
                        del session.spec[handle]
                        entry["fetched"] = True
                        session.ledger["fetched"] += 1
                        self.totals["fetched"] += 1
                        session.ledger["hits"] += 1
                        self.totals["hits"] += 1
                        items.append(payload)
                        break
                    if status in ("cancelled", "expired"):
                        item: dict[str, Any] = {
                            "h": handle, "qop": prepared_op,
                            "fetch_state": "cancelled",
                        }
                        reason = entry.get("reason")
                        if reason:
                            item["reason"] = reason
                        session.spec.pop(handle, None)
                        session.ledger["evicted"] += 1
                        self.totals["evicted"] += 1
                        session.ledger["misses"] += 1
                        self.totals["misses"] += 1
                        items.append(item)
                        break
                    event = entry.get("event")
                    timeout = wait_deadline - time.monotonic()
                    if wait_ms <= 0 or timeout <= 0:
                        items.append({"h": handle, "qop": prepared_op,
                                      "fetch_state": "pending"})
                        session.ledger["misses"] += 1
                        self.totals["misses"] += 1
                        break
                if event is not None:
                    event.wait(timeout)
        if len(items) == 1:
            result = dict(items[0])
        else:
            result = {"items": items}
        if args.get("stats") is True:
            result["st"] = self._ledger(session)
        verbose = bool(args.get("verbose")) or not session.compact
        if verbose:
            result = self._verbose(result)
        result = self._fit(result, budget)
        result = self._enforce_budget(result, budget)
        if not verbose:
            if len(items) == 1:
                result = self._maybe_omit_cols(
                    result, str(items[0].get("qop") or ""), session,
                    force=args.get("cols") is True)
            else:
                for item in result.get("items") or []:
                    if isinstance(item, dict):
                        self._maybe_omit_cols(
                            item, str(item.get("qop") or ""), session,
                            force=args.get("cols") is True)
        return _short_keys(result)

    # -- services ----------------------------------------------------------- #
    def _surface(self) -> Any:
        if self._surface_obj is None:
            from ozagia.integrations.mcp.graph_surface import GraphSurface
            self._surface_obj = GraphSurface(self.mem)
        return self._surface_obj

    # -- operations --------------------------------------------------------- #
    def _find(self, args: dict[str, Any],
              session: EngineSession) -> dict[str, Any]:
        query = str(args.get("q") or "").strip()
        if not query:
            raise EngineError("find needs q")
        limit = _clamp(args.get("limit", 20), 1, MAX_ROWS, 20)
        offset = max(0, int(args.get("offset", 0) or 0))
        result = self._surface().find(
            query, kinds=args.get("kinds") or None,
            limit=min(MAX_ROWS, offset + limit))
        hits = result.get("hits") or []
        page = hits[offset:offset + limit]
        items = [{
            "k": hit.get("key"), "n": hit.get("name"),
            "f": hit.get("file"),
            "sp": _span(hit.get("line"), hit.get("line")),
            "sig": hit.get("signature"), "sum": hit.get("summary"),
            "sc": hit.get("score"),
        } for hit in page]
        rows = _compact(items, FIND_COLS)
        return _clean({"cols": FIND_COLS, "rows": rows,
                       "__keys": [i["k"] for i in items if i.get("k")],
                       "__next": self._next_args(args, session)})

    def _outline(self, args: dict[str, Any],
                 session: EngineSession) -> dict[str, Any]:
        key = str(args.get("key") or "").strip()
        if not key:
            raise EngineError("outline needs key")
        limit = _clamp(args.get("limit", 40), 1, MAX_ROWS, 40)
        node = self._surface().node(key, view="outline")
        items = [{
            "r": "root", "k": node.get("key"), "n": node.get("name"),
            "f": node.get("file"),
            "sp": _span((node.get("lines") or [None, None])[0],
                        (node.get("lines") or [None, None])[1]),
            "sig": node.get("signature"), "sum": node.get("summary"),
            "e": node.get("edges"),
        }]
        if node.get("kind") == "file":
            try:
                expanded = self._surface().expand(
                    keys=[key], edges=["defines"], depth=1, limit=limit + 1)
            except ValueError:
                expanded = {"nodes": []}
            for child in expanded.get("nodes") or []:
                if child.get("key") == key:
                    continue
                items.append({
                    "r": "child", "k": child.get("key"), "n": child.get("name"),
                    "f": child.get("file"),
                    "sp": _span(child.get("line"), child.get("line")),
                })
                if len(items) > limit:
                    break
        rows, unchanged = self._known_rows(session, args, items, OUTLINE_COLS)
        return _clean({"cols": OUTLINE_COLS, "rows": rows,
                       "unchanged": unchanged,
                       "__keys": [i["k"] for i in items if i.get("k")],
                       "__next": self._next_args(args, session)})

    def _expand(self, args: dict[str, Any],
                session: EngineSession) -> dict[str, Any]:
        keys = [str(k) for k in (args.get("keys") or []) if str(k).strip()]
        if not keys:
            raise EngineError("expand needs keys")
        limit = _clamp(args.get("limit", 40), 1, MAX_ROWS, 40)
        offset = max(0, int(args.get("offset", 0) or 0))
        depth = _clamp(args.get("depth", 1), 0, 3, 1)
        result = self._surface().expand(
            keys=keys, edges=args.get("edges") or None, depth=depth,
            limit=min(MAX_ROWS, offset + limit + len(keys)))
        nodes = (result.get("nodes") or [])[offset:offset + limit]
        items = [{
            "k": node.get("key"), "n": node.get("name"),
            "f": node.get("file"),
            "sp": _span(node.get("line"), node.get("line")),
        } for node in nodes]
        edges = (result.get("edges") or [])[:MAX_ROWS]
        return _clean({"cols": EXPAND_COLS, "rows": _compact(items, EXPAND_COLS),
                       "edges": edges,
                       "truncated": bool(result.get("truncated")),
                       "__keys": [i["k"] for i in items if i.get("k")],
                       "__next": self._next_args(args, session)})

    def _impact(self, args: dict[str, Any],
                session: EngineSession) -> dict[str, Any]:
        keys = [str(k) for k in (args.get("keys") or []) if str(k).strip()]
        if not keys:
            raise EngineError("impact needs keys")
        limit = _clamp(args.get("limit", 40), 1, MAX_ROWS, 40)
        depth = _clamp(args.get("depth", 2), 1, 3, 2)
        result = self._surface().impact(keys=keys, depth=depth, limit=limit)
        items: list[dict[str, Any]] = []
        for group in ("symbols", "files", "tests"):
            for row in result.get(group) or []:
                items.append({"g": group[0], "k": row.get("key"),
                              "d": row.get("depth"),
                              "why": row.get("why")})
        return _clean({"cols": IMPACT_COLS,
                       "rows": _compact(items, IMPACT_COLS),
                       "counts": result.get("counts"),
                       "truncated": bool(result.get("truncated")),
                       "__keys": [i["k"] for i in items if i.get("k")]})

    def _tests(self, args: dict[str, Any],
               session: EngineSession) -> dict[str, Any]:
        keys = [str(k) for k in (args.get("keys") or []) if str(k).strip()]
        if args.get("key"):
            keys.insert(0, str(args["key"]))
        if not keys:
            raise EngineError("tests needs key or keys")
        limit = _clamp(args.get("limit", 40), 1, MAX_ROWS, 40)
        items: list[dict[str, Any]] = []
        found: set[str] = set()
        for key in keys[:8]:
            result = self.mem.tests_for(_target_for_key(key))
            if not result.get("ok"):
                continue
            for row in result.get("tests") or []:
                view = row.get("test") or {}
                test_key = view.get("key")
                if not test_key or test_key in found:
                    continue
                found.add(test_key)
                items.append({
                    "k": test_key, "n": view.get("label"),
                    "f": row.get("file"),
                    "sp": _span(row.get("line"), row.get("line")),
                    "why": row.get("why"),
                })
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        return _clean({"cols": TESTS_COLS,
                       "rows": _compact(items, TESTS_COLS),
                       "__keys": [i["k"] for i in items if i.get("k")]})

    def _context(self, args: dict[str, Any],
                 session: EngineSession) -> dict[str, Any]:
        task = str(args.get("task") or "").strip()
        if not task:
            raise EngineError("context needs task")
        limit = _clamp(args.get("limit", 8), 1, 50, 8)
        offset = max(0, int(args.get("offset", 0) or 0))
        budget = _clamp(args.get("budget", session.budget), MIN_BUDGET,
                        MAX_BUDGET, session.budget)
        pack = self.mem.context(
            task, max_items=min(50, offset + limit),
            mode=str(args.get("mode") or "auto"),
            budget_tokens=max(MIN_BUDGET, budget))
        raw = pack.get("items") or []
        items: list[dict[str, Any]] = []
        for entry in raw[offset:offset + limit]:
            view = entry.get("node") or {}
            attrs = entry.get("attrs") or {}
            snippet = entry.get("snippet") or {}
            key = view.get("key")
            if not key:
                continue
            items.append({
                "k": key, "n": view.get("label"),
                "f": snippet.get("path") or attrs.get("file")
                     or attrs.get("path"),
                "sp": _span(attrs.get("start_line"), attrs.get("end_line")),
                "why": entry.get("why"),
                "txt": snippet.get("text"),
            })
        rows, unchanged = self._known_rows(session, args, items, CONTEXT_COLS)
        return _clean({"cols": CONTEXT_COLS, "rows": rows,
                       "unchanged": unchanged,
                       "__keys": [i["k"] for i in items if i.get("k")],
                       "__next": self._next_args(args, session)})

    def _shape(self, args: dict[str, Any],
               session: EngineSession) -> dict[str, Any]:
        from ozagia.core import repo_shape as RS
        snap = session.snap
        if self._shape_cache is None or self._shape_cache[0] != snap:
            shape = RS.build_shape(self.root, max_modules=20)
            self._shape_cache = (snap, shape)
        shape = self._shape_cache[1]
        identity = shape.get("identity") or {}
        counts = identity.get("counts") or {}
        items: list[dict[str, Any]] = []
        for name in ("name", "package", "version", "description", "license"):
            value = identity.get(name)
            if value not in (None, ""):
                items.append({"sec": "repo", "k": name, "v": value})
        for name in ("files", "source_files", "test_files", "doc_files",
                     "symbols", "tests", "modules"):
            value = counts.get(name)
            if value is not None:
                items.append({"sec": "count", "k": name, "v": value})
        for row in identity.get("languages") or []:
            items.append({"sec": "lang", "k": row.get("language"),
                          "v": row.get("files")})
        limit = _clamp(args.get("limit", 20), 1, 100, 20)
        groups = (shape.get("modules") or {}).get("groups") or []
        modules = [row for group in groups
                   for row in (group.get("modules") or [])]
        for row in modules[:limit]:
            items.append({"sec": "module", "k": row.get("path"),
                          "v": row.get("responsibility")})
        commands = shape.get("commands") or {}
        for kind in ("build", "test", "lint"):
            for command in (commands.get(kind) or [])[:2]:
                items.append({"sec": "cmd", "k": kind, "v": command})
        for row in shape.get("conventions") or []:
            items.append({"sec": "conv", "k": row.get("source"),
                          "v": row.get("text")})
        return _clean({"cols": SHAPE_COLS,
                       "rows": _compact(items, SHAPE_COLS),
                       "__keys": []})

    # -- small response helpers --------------------------------------------- #
    @staticmethod
    def _next_args(args: dict[str, Any], session: EngineSession
                   ) -> dict[str, Any] | None:
        """A self-contained continuation request for a trimmed page."""
        base = {key: value for key, value in args.items()
                if key not in {"known", "spec", "fetch", "h", "budget",
                               "verbose", "session", "offset"}}
        if not base:
            return None
        base["op"] = str(args.get("op"))
        base["session"] = session.id
        return base

    @staticmethod
    def _error(message: str, *, snap: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"e": message}
        if snap:
            result["s"] = snap
        result["t"] = estimate_tokens(result)
        return result

    @staticmethod
    def _withheld(snap: str, fresh: str) -> dict[str, Any]:
        result = {
            "e": "snapshot is not verifiably fresh; structural data is withheld",
            "f": fresh,
            "s": snap,
        }
        result["t"] = estimate_tokens(result)
        return result


__all__ = [
    "BackgroundSpecWorker", "CAP_BITS", "CAP_MASK", "DEFAULT_BUDGET",
    "ENGINE_TOOL_NAME", "ENGINE_TOOLS", "FEATURES", "GraphEngine",
    "InlineSpecWorker", "MAX_BUDGET", "MAX_FETCH_WAIT_MS", "MAX_SESSIONS",
    "MAX_SPEC", "MIN_BUDGET", "OPS", "PROTOCOL", "PROTOCOL_VERSION",
    "PreparedSpec", "ProcessSpecWorker", "SPEC_BUDGET", "SPEC_TTL_S",
    "SPEC_WORKER_BUDGET_MS", "SpecJob", "description_tokens",
    "estimate_tokens",
]
