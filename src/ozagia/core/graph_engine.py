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

The speculation in this slice is synchronous: after a response with
``spec=true`` the engine suggests at most six next requests and precomputes at
most two bounded payloads *before* returning.  There is no worker and no
overlap with model work yet.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
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
MAX_ROWS = 200

OPS = ("hello", "find", "outline", "expand", "impact", "tests", "context",
       "shape", "fetch")
FEATURES = ("compact", "known", "spec", "fetch")
CAP_BITS = {"find": 1, "outline": 2, "expand": 4, "impact": 8, "tests": 16,
            "context": 32, "shape": 64, "fetch": 128}
CAP_MASK = sum(CAP_BITS.values())
_SHORT_KEYS = {
    "rows": "r", "cols": "c", "unchanged": "u", "truncated": "x",
    "omitted": "o", "next": "n", "spec": "q", "edges": "ed",
    "counts": "ct", "items": "it", "qop": "qo",
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
    ledger: dict[str, int] = field(default_factory=lambda: {
        "prepared": 0, "fetched": 0, "wasted": 0, "evicted": 0})
    touched: float = field(default_factory=time.monotonic)


class GraphEngine:
    """One bounded set of sessions over one ``DevMemory`` snapshot."""

    def __init__(self, memory: Any, *, workspace: str | Path | None = None,
                 max_sessions: int = MAX_SESSIONS) -> None:
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
        self.totals = {"sessions": 0, "evicted_sessions": 0,
                       "prepared": 0, "fetched": 0, "wasted": 0,
                       "evicted": 0}

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
        return {
            "sessions": len(self.sessions),
            "max_sessions": self.max_sessions,
            "spec_per_session": MAX_SPEC,
            "prepared_per_response": MAX_SPEC_PREPARE,
            "totals": dict(self.totals),
        }

    # -- sessions ----------------------------------------------------------- #
    def _hello(self, args: dict[str, Any]) -> dict[str, Any]:
        existing = self._session(args.get("session"))
        if existing is not None:
            session = existing
            session.touched = time.monotonic()
        else:
            session = self._new_session()
        fresh, snap = self._freshness()
        session.snap = snap
        session.fresh = fresh
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

    def _new_session(self) -> EngineSession:
        if len(self.sessions) >= self.max_sessions:
            _, old = self.sessions.popitem(last=False)
            self._expire(old)
            self.totals["evicted_sessions"] += 1
        self._seq += 1
        session = EngineSession(id=f"s{self._seq}", snap="")
        self.sessions[session.id] = session
        self.totals["sessions"] += 1
        return session

    def _session(self, handle: Any) -> EngineSession | None:
        if not isinstance(handle, str):
            return None
        session = self.sessions.get(handle)
        if session is not None:
            session.touched = time.monotonic()
            self.sessions.move_to_end(handle)
        return session

    def _expire(self, session: EngineSession) -> None:
        for entry in session.spec.values():
            if not entry.get("fetched"):
                session.ledger["wasted"] += 1
                self.totals["wasted"] += 1
            session.ledger["evicted"] += 1
            self.totals["evicted"] += 1
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
        if fresh != "fresh":
            if session.spec:
                self._drop_specs(session, wasted=True)
            session.fresh = fresh
            session.snap = snap
            return fresh, snap
        if session.snap and session.snap != snap:
            self._drop_specs(session, wasted=True)
        session.snap = snap
        session.fresh = fresh
        return fresh, snap

    def _drop_specs(self, session: EngineSession, *, wasted: bool) -> None:
        for entry in session.spec.values():
            if wasted and not entry.get("fetched"):
                session.ledger["wasted"] += 1
                self.totals["wasted"] += 1
            session.ledger["evicted"] += 1
            self.totals["evicted"] += 1
        session.spec.clear()

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
        candidates = self._candidates(op, args, keys)
        ready: list[str] = []
        pending = 0
        for request in candidates[:MAX_SPEC_SUGGEST]:
            if len(ready) >= MAX_SPEC_PREPARE:
                pending += 1
                continue
            if self._same_request(op, args, request):
                continue
            handle = f"q{session.spec_seq + 1}"
            session.spec_seq += 1
            payload = self._execute(request["op"], dict(request), session,
                                    snap, min(SPEC_BUDGET, budget),
                                    allow_spec=False, compact=False,
                                    cache_cols=False)
            if "e" in payload or "withheld" in payload:
                pending += 1
                continue
            self._evict_specs(session)
            session.spec[handle] = {"request": request, "result": payload,
                                    "snap": snap, "fetched": False}
            session.ledger["prepared"] += 1
            self.totals["prepared"] += 1
            ready.append(handle)
        return {"spec": {"h": ready, "r": len(ready), "p": pending}}

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

    def _evict_specs(self, session: EngineSession) -> None:
        while len(session.spec) >= MAX_SPEC:
            _, entry = session.spec.popitem(last=False)
            if not entry.get("fetched"):
                session.ledger["wasted"] += 1
                self.totals["wasted"] += 1
            session.ledger["evicted"] += 1
            self.totals["evicted"] += 1

    @staticmethod
    def _ledger(session: EngineSession) -> dict[str, int]:
        return {key: int(session.ledger.get(key, 0))
                for key in ("prepared", "fetched", "wasted", "evicted")}

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
        items: list[dict[str, Any]] = []
        for handle in handles:
            entry = session.spec.get(handle)
            if entry is None:
                return self._error(f"unknown or expired spec handle {handle!r}",
                                   snap=snap)
            if entry.get("snap") != snap:
                self._drop_specs(session, wasted=True)
                return self._withheld(snap, "stale")
            payload = dict(entry["result"])
            prepared_op = str((entry.get("request") or {}).get("op") or "")
            for key in ("next", "spec", "st"):
                payload.pop(key, None)
            payload["h"] = handle
            payload["qop"] = prepared_op
            items.append(payload)
            del session.spec[handle]
            if not entry.get("fetched"):
                entry["fetched"] = True
                session.ledger["fetched"] += 1
                self.totals["fetched"] += 1
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
                    result, prepared_op, session,
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
    "CAP_BITS", "CAP_MASK", "DEFAULT_BUDGET", "ENGINE_TOOL_NAME",
    "ENGINE_TOOLS", "FEATURES", "GraphEngine", "MAX_BUDGET", "MAX_SESSIONS",
    "MAX_SPEC", "MIN_BUDGET", "OPS", "PROTOCOL", "PROTOCOL_VERSION",
    "SPEC_BUDGET", "description_tokens", "estimate_tokens",
]
