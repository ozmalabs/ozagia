"""ozagia.core.goals — repo-level goals addressed by graph keys.

A goal names the graph nodes the work is about (`targets`), what the work
may change (`scope`) and machine-checkable `criteria`. Completion is decided
by evaluating the criteria against the graph, the working tree and the
test record, never from an agent's own claim. Goals are repo-level, kept in
`<state dir>/goals/goals.json`; sessions claim them.

Store (schema version 1; atomic write, `fcntl` file lock on
`goals/.lock`):

    {"version": 1, "next_id": <n>, "goals": [goal, ...]}

    goal: {id ("g<n>"), title, why, status, targets, scope, criteria,
           claimed_by, history, source, created_at, updated_at, checked_at}
    status:     open | met | regressed | blocked | dropped
    targets:    graph keys the work is about (each resolved at creation)
    scope:      graph keys or root-relative path globs the work may change
    criteria:   [{id ("c<n>"), kind, args, last}]; `last` is the latest
                result {status, evidence, reason, at}, or absent
    claimed_by: [{harness, session, at}]
    history:    [{at, event, by, detail}], every event (`MAX_HISTORY`
                kept; the `created` entry always)
    source:     {review_packet, finding_id} or null

Criteria kinds and their args (`KINDS`). Each evaluates to
`{status: pass|fail|stale|unknown, evidence, reason}`:

    test_passes / test_fails  {key: test:...}  from the root's test record
        (`ozagia.core.working_set.recorded_outcome`) with the run time; `stale`
        when the run is older than the last change to the goal's targets or
        the test's file (file mtimes).
    symbol_exists / symbol_absent  {key: sym:...}  graph node present/absent.
    calls / not_calls  {caller: sym:..., callee: sym:...}  an edge of the
        syntactic symbol graph (`ozagia.core.symbol_graph`) present/absent.
    no_callers  {key: sym:...}  no symbol-graph edge into the symbol.
    file_exists / file_absent  {key: file:...}  the file in the working tree.
    unchanged  {key, hash}  the key's content hash equals the hash taken at
        creation (file bytes; a symbol's or test's lines; an ops or other
        node's attrs).
    lint_clean  {key}  ozagia keeps no lint results (`ozagia.intent.lint`
        reports findings in the edit's reply and stores nothing), so this is
        always `unknown`, with that reason.
    review_resolved  {packet, finding_id}  the finding id is absent from the
        repo-review packet (`ozagia.repo_review.packet.v1`) at `packet`;
        `stale` when the packet is older than the last change to the targets.
    ops_has  {key: ops:..., attr?, value?}  an ops-graph node, optionally with
        a non-empty attribute, optionally equal to (or, for a list,
        containing) `value`.

There is no shell-command criterion.

Creation resolves every key (targets, key entries of scope, criterion
subjects) against the graph and refuses unknown ones, except the subject of
`symbol_exists`/`file_exists`. Status changes only by evaluation (`met` when
every criterion passes, from open or regressed; `regressed` when a met goal
has a failing criterion) or by explicit commands: `drop`, `block` (with a
reason) and `block --clear` (back to open). A blocked or dropped goal is
still evaluated when checked; its status does not change.

Standard library only. Callers pass the `DevMemory` where the graph is
needed; reading goals (hints, the session brief) needs no graph.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1
GOALS_DIR = "goals"
GOALS_FILE = "goals.json"
LOCK_FILE = ".lock"
PACKET_SCHEMA = "ozagia.repo_review.packet.v1"
STATUSES = ("open", "met", "regressed", "blocked", "dropped")
RESULT_STATUSES = ("pass", "fail", "stale", "unknown")
MAX_HISTORY = 200
MAX_TITLE = 200
MAX_WHY = 2000
MAX_REASON = 300
MAX_KEYS = 100
MAX_CRITERIA = 50
# Hint and brief bounds.
HINT_GOALS = 3
HINT_CRITERIA = 4
HINT_FILES = 3
BRIEF_GOALS = 4
BRIEF_CRITERIA = 3
BRIEF_CHARS = 450
LINE_CHARS = 240

# kind -> (parameter names in positional order, required count,
#          key parameters with their required prefix,
#          whether the subject may be absent from the graph at creation)
KINDS: dict[str, tuple[tuple[str, ...], int, dict[str, str], bool]] = {
    "test_passes": (("key",), 1, {"key": "test:"}, False),
    "test_fails": (("key",), 1, {"key": "test:"}, False),
    "symbol_exists": (("key",), 1, {"key": "sym:"}, True),
    "symbol_absent": (("key",), 1, {"key": "sym:"}, False),
    "calls": (("caller", "callee"), 2, {"caller": "sym:", "callee": "sym:"}, False),
    "not_calls": (("caller", "callee"), 2, {"caller": "sym:", "callee": "sym:"}, False),
    "no_callers": (("key",), 1, {"key": "sym:"}, False),
    "file_exists": (("key",), 1, {"key": "file:"}, True),
    "file_absent": (("key",), 1, {"key": "file:"}, False),
    "unchanged": (("key",), 1, {"key": ""}, False),
    "lint_clean": (("key",), 1, {"key": ""}, False),
    "review_resolved": (("packet", "finding_id"), 2, {}, False),
    "ops_has": (("key", "attr", "value"), 1, {"key": "ops:"}, False),
}
# Kinds whose evidence predates a change to the goal's targets.
TIME_KINDS = frozenset({"test_passes", "test_fails", "review_resolved"})
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*:")
_GLOB_CHARS = re.compile(r"[*?\[]")
_THREAD_LOCK = threading.Lock()


class GoalError(ValueError):
    """A goal request that cannot be carried out (bad input, unknown key,
    unknown goal, unreadable store)."""


def _now() -> float:
    return time.time()


def _clip(text: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[:limit - 3] + "..."


def key_file(key: str) -> str | None:
    """The repository file of a key (`ozagia.core.keys.key_file`)."""
    from ozagia.core.keys import key_file as kf
    try:
        return kf(key)
    except Exception:  # noqa: BLE001 - not a key
        return None


def is_key(entry: str) -> bool:
    return bool(_KEY_RE.match(entry or ""))


# --------------------------------------------------------------------------- #
# path globs (scope)
# --------------------------------------------------------------------------- #
def glob_regex(pattern: str) -> re.Pattern[str]:
    """A regex for root-relative POSIX paths: `**` any number of
    directories (a trailing `**` everything below), `*`/`?` within one
    component, `[...]` a class; a pattern ending in `/` matches everything
    below that directory."""
    pat = pattern.strip()
    while pat.startswith("./"):
        pat = pat[2:]
    if pat.endswith("/"):
        pat += "**"
    parts = [p for p in pat.split("/") if p not in ("", ".")]
    out: list[str] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(r".*" if last else r"(?:[^/]+/)*")
            continue
        seg, j = [], 0
        while j < len(part):
            c = part[j]
            if c == "*":
                seg.append(r"[^/]*")
            elif c == "?":
                seg.append(r"[^/]")
            elif c == "[":
                k = part.find("]", j + 1)
                if k < 0:
                    seg.append(re.escape(c))
                else:
                    body = part[j + 1:k]
                    if body.startswith("!"):
                        body = "^" + body[1:]
                    seg.append("[" + body.replace("\\", "\\\\") + "]")
                    j = k
            else:
                seg.append(re.escape(c))
            j += 1
        out.append("".join(seg) + ("" if last else "/"))
    return re.compile("".join(out) + r"\Z")


def _related(a: str, b: str) -> bool:
    """Same key, or one names a member of the other (`sym:f.py::C` and
    `sym:f.py::C.m`)."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def in_scope(scope: list[str], rel: str, keys: Iterable[str] = ()) -> bool:
    """Whether an edit of file `rel` (whose changed or enclosing keys are
    `keys`, if known) stays inside `scope`. A glob or a `file:`/`doc:` key
    admits the whole file; `sym:`/`test:` keys admit the edit when every
    known key of it in that file is one of them, a member or an enclosing
    symbol (with no known keys, the file is admitted)."""
    in_file: list[str] = []
    for entry in scope:
        if not is_key(entry):
            if glob_regex(entry).match(rel):
                return True
            continue
        prefix = entry.split(":", 1)[0]
        if prefix in ("file", "doc") and _GLOB_CHARS.search(entry):
            if glob_regex(entry.split(":", 1)[1]).match(rel):
                return True
            continue
        if key_file(entry) != rel:
            continue
        if prefix in ("file", "doc"):
            return True
        in_file.append(entry)
    if not in_file:
        return False
    mine = [k for k in keys if isinstance(k, str) and key_file(k) == rel
            and k.startswith(("sym:", "test:"))]
    if not mine:
        return True
    return all(any(_related(k, s) for s in in_file) for k in mine)


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
def goals_path(root: str | Path) -> Path:
    from ozagia.config import state_path
    return state_path(root, GOALS_DIR, GOALS_FILE)


def _empty() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "next_id": 1, "goals": []}


def _parse_store(text: str, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise GoalError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
        raise GoalError(f"{path}: not a goal store")
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise GoalError(f"{path}: schema version {version!r}, this ozagia "
                        f"reads version {SCHEMA_VERSION}")
    if not isinstance(data.get("next_id"), int):
        data["next_id"] = len(data["goals"]) + 1
    return data


def load_store(root: str | Path) -> dict[str, Any]:
    """The store; empty when there is none. Raises GoalError when the file
    is unreadable or of another schema version."""
    path = goals_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as e:
        raise GoalError(f"cannot read {path}: {e}") from e
    return _parse_store(text, path)


def read_goals(root: str | Path) -> list[dict[str, Any]]:
    """The goals, or [] when the store is missing or unreadable (for hints
    and briefs, which never fail)."""
    try:
        return list(load_store(root)["goals"])
    except GoalError:
        return []


def _write_store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix="goals.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked_store(root: str | Path) -> Iterator[dict[str, Any]]:
    """The store under the file lock (and a process lock), written back
    atomically when the block finishes without an exception; on an
    exception nothing is written."""
    path = goals_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, open(path.parent / LOCK_FILE, "a+") as lk:
        try:
            import fcntl
            fcntl.flock(lk, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        data = load_store(root)
        yield data
        _write_store(path, data)


def _event(goal: dict[str, Any], event: str, by: str,
           detail: Any = None, at: float | None = None) -> None:
    at = _now() if at is None else at
    hist = goal.setdefault("history", [])
    hist.append({"at": at, "event": event, "by": by, "detail": detail})
    if len(hist) > MAX_HISTORY:
        head = [h for h in hist[:1] if h.get("event") == "created"]
        goal["history"] = head + hist[len(hist) - (MAX_HISTORY - len(head)):]
    goal["updated_at"] = at


def _find(data: dict[str, Any], goal_id: str) -> dict[str, Any]:
    for g in data["goals"]:
        if g.get("id") == goal_id:
            return g
    raise GoalError(f"no goal {goal_id!r}")


def get_goal(root: str | Path, goal_id: str) -> dict[str, Any]:
    return _find(load_store(root), goal_id)


def list_goals(root: str | Path, *, status: str | None = None,
               include_dropped: bool = False) -> list[dict[str, Any]]:
    goals = load_store(root)["goals"]
    if status:
        return [g for g in goals if g.get("status") == status]
    return [g for g in goals if include_dropped or g.get("status") != "dropped"]


# --------------------------------------------------------------------------- #
# criteria: parsing and validation
# --------------------------------------------------------------------------- #
def _default_prefix(value: str, prefix: str) -> str:
    """`tests/t.py::test_x` -> `test:tests/t.py::test_x` for a test
    parameter, and so on; values that already look like keys are kept."""
    if prefix and not is_key(value):
        return prefix + value
    return value


def parse_criterion(text: str) -> dict[str, Any]:
    """`kind:args` (the CLI form): args are whitespace-separated (shell
    quoting applies), positional in the kind's order or `name=value` for a
    parameter of that kind. `test_passes:tests/t.py::test_x`,
    `calls:sym:a.py::f sym:b.py::g`, `ops_has:ops:image/nginx:1.25`,
    `review_resolved:review.json finding_id=abc`."""
    kind, sep, rest = str(text or "").partition(":")
    kind = kind.strip()
    if not sep or kind not in KINDS:
        raise GoalError(f"criterion {text!r}: expected kind:args with kind one "
                        f"of {', '.join(KINDS)}")
    names = KINDS[kind][0]
    try:
        tokens = shlex.split(rest)
    except ValueError as e:
        raise GoalError(f"criterion {text!r}: {e}") from e
    args: dict[str, Any] = {}
    pos = 0
    for tok in tokens:
        name, eq, value = tok.partition("=")
        if eq and name in names:
            args[name] = value
            continue
        if pos >= len(names):
            raise GoalError(f"criterion {text!r}: too many arguments for {kind}")
        args[names[pos]] = tok
        pos += 1
    return {"kind": kind, "args": args}


def _criterion_spec(raw: Any) -> dict[str, Any]:
    """A criterion from a CLI string, or a mapping `{kind, args}` or
    `{kind, <param>: ...}` (TOML, MCP, review packets)."""
    if isinstance(raw, str):
        return parse_criterion(raw)
    if not isinstance(raw, dict):
        raise GoalError(f"criterion must be a string or a table, got {raw!r}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise GoalError(f"criterion kind {kind!r}: expected one of {', '.join(KINDS)}")
    args = raw.get("args")
    if isinstance(args, str):
        return parse_criterion(f"{kind}:{args}")
    if args is None:
        args = {k: v for k, v in raw.items() if k not in ("kind", "id", "last")}
    if isinstance(args, list):
        args = dict(zip(KINDS[kind][0], args))
    if not isinstance(args, dict):
        raise GoalError(f"criterion {kind}: args must be a table")
    return {"kind": kind, "args": dict(args)}


def _resolves(memory: Any, key: str) -> bool:
    if memory is None:
        return False
    try:
        return memory.node_by_key(key) is not None
    except Exception:  # noqa: BLE001 - treat as unknown
        return False


def _root_of(memory: Any, root: str | Path | None) -> Path:
    if root is not None:
        return Path(root).resolve()
    return Path(memory.raw_repo_root).resolve()


def _packet_arg(root: Path, value: str) -> str:
    """A packet path as stored: root-relative when inside the root. A
    relative `value` is relative to the root (the CLI passes absolute
    paths)."""
    p = Path(os.path.expanduser(value))
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def _packet_file(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def validate_criterion(raw: Any, memory: Any, root: Path) -> dict[str, Any]:
    """The checked `{kind, args}` of a criterion, keys resolved (refused
    when unknown, except where the kind allows an absent subject), the
    content hash of `unchanged` taken now."""
    spec = _criterion_spec(raw)
    kind, args = spec["kind"], spec["args"]
    names, required, key_params, may_be_absent = KINDS[kind]
    unknown_args = sorted(set(args) - set(names) - ({"hash"} if kind == "unchanged" else set()))
    if unknown_args:
        raise GoalError(f"criterion {kind}: unknown argument(s) {', '.join(unknown_args)}")
    for name in names[:required]:
        if not isinstance(args.get(name), str) or not args[name].strip():
            raise GoalError(f"criterion {kind}: missing {name}")
    out: dict[str, Any] = {}
    for name in names:
        if args.get(name) is None:
            continue
        value = str(args[name]).strip()
        if name in key_params:
            prefix = key_params[name]
            value = _default_prefix(value, prefix)
            if prefix and not value.startswith(prefix):
                raise GoalError(f"criterion {kind}: {name} must be a {prefix} key, "
                                f"got {value!r}")
            if not is_key(value):
                raise GoalError(f"criterion {kind}: {name} {value!r} is not a key")
            if not (may_be_absent and name == "key") and not _resolves(memory, value):
                raise GoalError(f"criterion {kind}: unknown key {value}")
        out[name] = value
    if kind == "review_resolved":
        out["packet"] = _packet_arg(root, out["packet"])
        packet = load_packet(_packet_file(root, out["packet"]))
        if packet is None:
            raise GoalError(f"criterion review_resolved: cannot read a "
                            f"{PACKET_SCHEMA} packet at {out['packet']}")
    if kind == "unchanged":
        digest, _ev = content_hash(root, memory, out["key"])
        if digest is None:
            raise GoalError(f"criterion unchanged: cannot hash {out['key']}")
        out["hash"] = digest
    return {"kind": kind, "args": out}


# --------------------------------------------------------------------------- #
# evidence helpers
# --------------------------------------------------------------------------- #
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(root: Path, memory: Any, key: str) -> tuple[str | None, dict[str, Any]]:
    """The content hash of a key and how it was taken: a file's bytes
    (`file:`, `doc:`), the lines of a `text:` range or of a symbol's or
    test's span (from the graph), else the node's attrs. (None, ...) when
    the key does not resolve or the file cannot be read."""
    prefix = key.split(":", 1)[0]
    rel = key_file(key)
    if prefix in ("file", "doc") and rel:
        try:
            return _sha((root / rel).read_bytes()), {"basis": "file bytes", "file": rel}
        except OSError:
            return None, {"basis": "file bytes", "file": rel, "error": "unreadable"}
    if prefix == "text" and rel:
        m = re.search(r"#L(\d+)-L(\d+)$", key)
        span = (int(m.group(1)), int(m.group(2))) if m else None
    else:
        span = None
    nid = None
    if memory is not None:
        try:
            nid = memory.node_by_key(key)
        except Exception:  # noqa: BLE001
            nid = None
    node = memory.node(nid) if nid is not None else None
    attrs = (node or {}).get("attrs") or {}
    if span is None and rel and isinstance(attrs.get("start_line"), int):
        span = (int(attrs["start_line"]), int(attrs.get("end_line") or attrs["start_line"]))
    if span is not None and rel:
        try:
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None, {"basis": "lines", "file": rel, "error": "unreadable"}
        a, b = span
        body = "\n".join(lines[max(a - 1, 0):b])
        return _sha(body.encode("utf-8")), {"basis": "lines", "file": rel,
                                            "lines": [a, b]}
    if node is None:
        return None, {"basis": "node", "error": "not in the graph"}
    blob = json.dumps(attrs, sort_keys=True, default=str).encode("utf-8")
    return _sha(blob), {"basis": "node attrs"}


def _mtime(root: Path, rel: str) -> float | None:
    try:
        return (root / rel).stat().st_mtime
    except OSError:
        return None


def last_change(root: Path, keys: Iterable[str]) -> tuple[float | None, str | None]:
    """The latest modification time of the files the keys are in, and that
    file (None when no file exists)."""
    best: tuple[float | None, str | None] = (None, None)
    for key in keys:
        rel = key_file(key)
        if not rel:
            continue
        t = _mtime(root, rel)
        if t is not None and (best[0] is None or t > best[0]):
            best = (t, rel)
    return best


def load_packet(path: Path) -> dict[str, Any] | None:
    """A repo-review packet, or None when unreadable or of another schema."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != PACKET_SCHEMA:
        return None
    if not isinstance(data.get("findings"), list):
        return None
    return data


def _finding(packet: dict[str, Any], finding_id: str) -> dict[str, Any] | None:
    for f in packet.get("findings") or []:
        if isinstance(f, dict) and f.get("id") == finding_id:
            return f
    return None


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
class Evaluator:
    """Evaluates criteria for one check: the test record and the symbol
    graph are loaded once, on first use."""

    def __init__(self, root: str | Path, memory: Any) -> None:
        self.root = Path(root).resolve()
        self.memory = memory
        self._record: dict[str, Any] | None = None
        self._sgraph: Any = None
        self._sgraph_error: str | None = None

    # -- shared sources ---------------------------------------------------- #
    def record(self) -> dict[str, Any]:
        if self._record is None:
            from ozagia.core.working_set import load_test_record
            self._record = load_test_record(self.root)
        return self._record

    def symbol_graph(self) -> tuple[dict[str, Any] | None, str | None]:
        if self._sgraph is None and self._sgraph_error is None:
            try:
                from ozagia.config import state_path
                from ozagia.core.repo_map import update_tag_cache
                from ozagia.core.symbol_graph import build_symbol_graph
                cache, _stats = update_tag_cache(
                    self.root, cache_path=state_path(self.root, "tags.json"))
                self._sgraph = build_symbol_graph(cache)
            except Exception as e:  # noqa: BLE001 - reported as unknown
                self._sgraph_error = f"symbol graph unavailable: {type(e).__name__}: {e}"
        return self._sgraph, self._sgraph_error

    def node(self, key: str) -> dict[str, Any] | None:
        nid = None
        try:
            nid = self.memory.node_by_key(key) if self.memory is not None else None
        except Exception:  # noqa: BLE001
            nid = None
        return self.memory.node(nid) if nid is not None else None

    # -- one criterion ----------------------------------------------------- #
    def evaluate(self, goal: dict[str, Any], crit: dict[str, Any]) -> dict[str, Any]:
        kind = crit.get("kind")
        fn = getattr(self, f"_eval_{kind}", None)
        if fn is None:
            return _result("unknown", {}, f"unknown criterion kind {kind!r}")
        try:
            return fn(goal, dict(crit.get("args") or {}))
        except Exception as e:  # noqa: BLE001 - a criterion never fails a check
            return _result("unknown", {}, f"evaluation failed: {type(e).__name__}: {e}")

    def _stale_after(self, goal: dict[str, Any], ts: float | None,
                     extra: Iterable[str] = ()) -> tuple[bool, dict[str, Any]]:
        changed, rel = last_change(self.root, [*(goal.get("targets") or []), *extra])
        ev = {"targets_changed_at": changed, "changed_file": rel}
        return (ts is not None and changed is not None and ts < changed), ev

    def _test(self, goal: dict[str, Any], args: dict[str, Any], want: str
              ) -> dict[str, Any]:
        from ozagia.core.working_set import recorded_outcome
        key = args["key"]
        got = recorded_outcome(self.record(), key)
        ev: dict[str, Any] = {"key": key, "outcome": got["outcome"],
                              "run_at": got.get("ts")}
        if got["outcome"] == "unknown":
            return _result("unknown", ev, got["reason"])
        stale, sev = self._stale_after(goal, got.get("ts"), [key])
        ev.update(sev)
        if stale:
            return _result("stale", ev, f"the last run ({got['outcome']}) is older "
                           f"than the last change to {sev['changed_file']}")
        ok = got["outcome"] == want
        return _result("pass" if ok else "fail", ev,
                       f"test {got['outcome']}: {got['reason']}")

    def _eval_test_passes(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._test(goal, args, "passed")

    def _eval_test_fails(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._test(goal, args, "failed")

    def _symbol(self, args: dict[str, Any], want: bool) -> dict[str, Any]:
        key = args["key"]
        node = self.node(key)
        attrs = (node or {}).get("attrs") or {}
        ev = {"key": key, "in_graph": node is not None}
        if node is not None:
            ev["lines"] = [attrs.get("start_line"), attrs.get("end_line")]
        present = node is not None
        return _result("pass" if present == want else "fail", ev,
                       f"{key} is {'in' if present else 'not in'} the graph")

    def _eval_symbol_exists(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._symbol(args, True)

    def _eval_symbol_absent(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._symbol(args, False)

    @staticmethod
    def _sid(key: str) -> str:
        return key[len("sym:"):]

    def _edge(self, args: dict[str, Any], want: bool) -> dict[str, Any]:
        graph, err = self.symbol_graph()
        caller, callee = args["caller"], args["callee"]
        ev: dict[str, Any] = {"caller": caller, "callee": callee}
        if graph is None:
            return _result("unknown", ev, err or "no symbol graph")
        if not graph.get("nodes"):
            return _result("unknown", ev, "the symbol graph is empty (no tag "
                           "parser for these files?)")
        src, tgt = self._sid(caller), self._sid(callee)
        missing = [sid for sid in (src, tgt) if sid not in graph["nodes"]]
        if missing:
            # Both keys resolved at creation: a missing definition was removed,
            # so there is no edge.
            ev["missing"] = missing
            return _result("fail" if want else "pass", ev,
                           f"{', '.join(missing)} not in the symbol graph, so no edge")
        edge = next((e for e in graph["edges"] if e["src"] == src and e["tgt"] == tgt), None)
        if edge is not None:
            ev["edge"] = {k: edge[k] for k in ("resolution", "confidence", "count")}
        present = edge is not None
        return _result("pass" if present == want else "fail", ev,
                       f"{'an' if present else 'no'} edge {src} -> {tgt} in the "
                       "symbol graph (syntactic)")

    def _eval_calls(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._edge(args, True)

    def _eval_not_calls(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._edge(args, False)

    def _eval_no_callers(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        graph, err = self.symbol_graph()
        key = args["key"]
        ev: dict[str, Any] = {"key": key}
        if graph is None:
            return _result("unknown", ev, err or "no symbol graph")
        sid = self._sid(key)
        if sid not in graph["nodes"]:
            return _result("unknown", ev, f"{sid} is not in the symbol graph")
        callers = sorted(e["src"] for e in graph["edges"] if e["tgt"] == sid)
        ev["callers"] = ["sym:" + c for c in callers[:10]]
        ev["caller_count"] = len(callers)
        return _result("fail" if callers else "pass", ev,
                       f"{len(callers)} caller(s) in the symbol graph (syntactic)")

    def _file(self, args: dict[str, Any], want: bool) -> dict[str, Any]:
        key = args["key"]
        rel = key_file(key) or ""
        present = bool(rel) and (self.root / rel).is_file()
        ev = {"key": key, "on_disk": present,
              "in_graph": self.node(key) is not None}
        return _result("pass" if present == want else "fail", ev,
                       f"{rel} {'exists' if present else 'does not exist'} in the "
                       "working tree")

    def _eval_file_exists(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._file(args, True)

    def _eval_file_absent(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self._file(args, False)

    def _eval_unchanged(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        key, want = args["key"], args.get("hash")
        digest, ev = content_hash(self.root, self.memory, key)
        ev = {"key": key, "hash": digest, "created_hash": want, **ev}
        if digest is None:
            return _result("fail", ev, f"{key} cannot be hashed now "
                           f"({ev.get('error', 'missing')})")
        same = digest == want
        return _result("pass" if same else "fail", ev,
                       "content hash equals the hash at creation" if same
                       else "content hash differs from the hash at creation")

    def _eval_lint_clean(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return _result("unknown", {"key": args["key"]},
                       "ozagia keeps no lint results: lint findings are reported "
                       "in an edit's reply (ozagia.intent.lint) and not stored")

    def _eval_review_resolved(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        path = _packet_file(self.root, args["packet"])
        fid = args["finding_id"]
        ev: dict[str, Any] = {"packet": args["packet"], "finding_id": fid}
        packet = load_packet(path)
        if packet is None:
            return _result("unknown", ev, f"no readable {PACKET_SCHEMA} packet at "
                           f"{args['packet']}")
        mtime = _mtime(path.parent, path.name)
        ev["packet_at"] = mtime
        try:
            ev["packet_sha256"] = _sha(path.read_bytes())
        except OSError:
            pass
        stale, sev = self._stale_after(goal, mtime)
        ev.update(sev)
        if stale:
            return _result("stale", ev, "the packet is older than the last change "
                           f"to {sev['changed_file']}; run the repo review again")
        present = _finding(packet, fid) is not None
        return _result("fail" if present else "pass", ev,
                       f"finding {fid} is {'still' if present else 'not'} in the packet")

    def _eval_ops_has(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        key = args["key"]
        node = self.node(key)
        ev: dict[str, Any] = {"key": key, "in_graph": node is not None}
        if node is None:
            return _result("fail", ev, f"{key} is not in the ops graph")
        attrs = node.get("attrs") or {}
        attr = args.get("attr")
        if not attr:
            return _result("pass", ev, f"{key} is in the ops graph")
        value = attrs.get(attr)
        ev["attr"] = attr
        ev["actual"] = value if isinstance(value, (str, int, float, bool)) or value is None \
            else (value[:10] if isinstance(value, list) else str(type(value).__name__))
        if value in (None, "", [], {}):
            return _result("fail", ev, f"{key} has no {attr}")
        want = args.get("value")
        if want is None:
            return _result("pass", ev, f"{key} has {attr}")
        ok = (want in [str(v) for v in value]) if isinstance(value, list) else str(value) == want
        return _result("pass" if ok else "fail", ev,
                       f"{key} {attr} {'matches' if ok else 'does not match'} {want!r}")


def _result(status: str, evidence: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"status": status, "evidence": evidence, "reason": _clip(reason, MAX_REASON)}


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GoalError(f"{name} must be a list of strings")
    out = list(dict.fromkeys(v.strip() for v in value if v.strip()))
    if len(out) > MAX_KEYS:
        raise GoalError(f"{name}: at most {MAX_KEYS} entries")
    return out


def _check_scope_entry(entry: str, memory: Any) -> None:
    if not is_key(entry):
        return
    prefix = entry.split(":", 1)[0]
    if prefix in ("file", "doc") and _GLOB_CHARS.search(entry):
        return
    if not _resolves(memory, entry):
        raise GoalError(f"scope: unknown key {entry}")


def add_goal(root: str | Path | None, memory: Any, spec: dict[str, Any], *,
             by: str = "cli", source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a goal from `spec` {title, why?, targets, scope?, criteria}
    and return it. Every key must resolve in `memory` (see the module
    docstring); nothing is written when any does not."""
    root_p = _root_of(memory, root)
    if not isinstance(spec, dict):
        raise GoalError("a goal spec must be a table")
    allowed = {"title", "why", "targets", "scope", "criteria"}
    extra = sorted(set(spec) - allowed)
    if extra:
        raise GoalError(f"unknown goal field(s): {', '.join(extra)}")
    title = _clip(spec.get("title"), MAX_TITLE)
    if not title:
        raise GoalError("a goal needs a title")
    why = str(spec.get("why") or "").strip()[:MAX_WHY]
    targets = _str_list(spec.get("targets"), "targets")
    scope = _str_list(spec.get("scope"), "scope")
    raw_criteria = spec.get("criteria") or []
    if isinstance(raw_criteria, (str, dict)):
        raw_criteria = [raw_criteria]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise GoalError("a goal needs at least one criterion")
    if len(raw_criteria) > MAX_CRITERIA:
        raise GoalError(f"at most {MAX_CRITERIA} criteria")
    if memory is None:
        raise GoalError("creating a goal needs the graph (.ozagia/graph.json)")
    for t in targets:
        if not is_key(t):
            raise GoalError(f"targets: {t!r} is not a graph key (file:, sym:, test:, doc:, ops:, ...)")
        if not _resolves(memory, t):
            raise GoalError(f"targets: unknown key {t}")
    for entry in scope:
        _check_scope_entry(entry, memory)
    criteria = []
    for i, raw in enumerate(raw_criteria, start=1):
        c = validate_criterion(raw, memory, root_p)
        criteria.append({"id": f"c{i}", **c})
    now = _now()
    with locked_store(root_p) as data:
        if source and source.get("finding_id"):
            for g in data["goals"]:
                src = g.get("source") or {}
                if (src.get("finding_id") == source["finding_id"]
                        and g.get("status") != "dropped"):
                    raise GoalError(f"goal {g['id']} already tracks finding "
                                    f"{source['finding_id']}")
        gid = f"g{data['next_id']}"
        data["next_id"] += 1
        goal = {"id": gid, "title": title, "why": why, "status": "open",
                "targets": targets, "scope": scope, "criteria": criteria,
                "claimed_by": [], "history": [], "source": source,
                "created_at": now, "updated_at": now, "checked_at": None}
        _event(goal, "created", by, {"criteria": len(criteria)}, at=now)
        data["goals"].append(goal)
    return goal


def check_goals(root: str | Path | None, memory: Any, ids: Iterable[str] | None = None,
                *, by: str = "cli", refresh: bool = True) -> list[dict[str, Any]]:
    """Evaluate the criteria of the named goals (default: every goal not
    dropped), record each result on its criterion, apply the status
    transitions, and return one report per goal: {id, status, previous,
    results: [{id, kind, status, evidence, reason}]}. With `refresh`, the
    in-memory graph takes in pending changes first (the snapshot on disk is
    not rewritten)."""
    root_p = _root_of(memory, root)
    if refresh and memory is not None and hasattr(memory, "refresh"):
        with contextlib.suppress(Exception):
            if memory.pending_changes():
                memory.refresh(write=False)
    ev = Evaluator(root_p, memory)
    wanted = list(ids) if ids else None
    reports = []
    with locked_store(root_p) as data:
        if wanted:
            goals = [_find(data, gid) for gid in wanted]
        else:
            goals = [g for g in data["goals"] if g.get("status") != "dropped"]
        for goal in goals:
            now = _now()
            results = []
            for crit in goal.get("criteria") or []:
                res = ev.evaluate(goal, crit)
                crit["last"] = {**res, "at": now}
                results.append({"id": crit["id"], "kind": crit["kind"], **res})
            prev = goal.get("status")
            statuses = [r["status"] for r in results]
            new = prev
            if prev in ("open", "regressed") and statuses and all(
                    s == "pass" for s in statuses):
                new = "met"
            elif prev == "met" and "fail" in statuses:
                new = "regressed"
            counts = {s: statuses.count(s) for s in RESULT_STATUSES if s in statuses}
            goal["checked_at"] = now
            _event(goal, "checked", by, counts, at=now)
            if new != prev:
                goal["status"] = new
                _event(goal, new, by, {"from": prev,
                                       "failing": [r["id"] for r in results
                                                   if r["status"] == "fail"]}, at=now)
            reports.append({"id": goal["id"], "title": goal.get("title"),
                            "status": new, "previous": prev, "results": results})
    return reports


def claim_goal(root: str | Path, goal_id: str, harness: str, session: str, *,
               release: bool = False, by: str | None = None) -> dict[str, Any]:
    """Claim (or with `release`, release) a goal for (harness, session)."""
    if not isinstance(harness, str) or not harness.strip():
        raise GoalError("a claim needs a harness")
    if not isinstance(session, str) or not session.strip():
        raise GoalError("a claim needs a session")
    harness, session = harness.strip(), session.strip()
    who = by or f"{harness}/{session}"
    with locked_store(root) as data:
        goal = _find(data, goal_id)
        claims = goal.setdefault("claimed_by", [])
        mine = [c for c in claims if c.get("harness") == harness
                and c.get("session") == session]
        if release:
            if mine:
                goal["claimed_by"] = [c for c in claims if c not in mine]
                _event(goal, "released", who, {"harness": harness, "session": session})
            return goal
        if goal.get("status") == "dropped":
            raise GoalError(f"goal {goal_id} is dropped")
        if not mine:
            claims.append({"harness": harness, "session": session, "at": _now()})
            _event(goal, "claimed", who, {"harness": harness, "session": session})
        return goal


def drop_goal(root: str | Path, goal_id: str, *, reason: str = "",
              by: str = "cli") -> dict[str, Any]:
    with locked_store(root) as data:
        goal = _find(data, goal_id)
        if goal.get("status") != "dropped":
            prev = goal.get("status")
            goal["status"] = "dropped"
            _event(goal, "dropped", by, {"from": prev, "reason": _clip(reason, MAX_REASON)})
        return goal


def block_goal(root: str | Path, goal_id: str, *, reason: str = "",
               clear: bool = False, by: str = "cli") -> dict[str, Any]:
    """Block a goal (a reason is required), or with `clear` unblock it
    (back to open; the next check decides met)."""
    with locked_store(root) as data:
        goal = _find(data, goal_id)
        prev = goal.get("status")
        if clear:
            if prev != "blocked":
                raise GoalError(f"goal {goal_id} is {prev}, not blocked")
            goal["status"] = "open"
            _event(goal, "unblocked", by, {"reason": _clip(reason, MAX_REASON)})
            return goal
        if not reason.strip():
            raise GoalError("block needs a reason")
        if prev == "dropped":
            raise GoalError(f"goal {goal_id} is dropped")
        goal["status"] = "blocked"
        goal["blocked_reason"] = _clip(reason, MAX_REASON)
        _event(goal, "blocked", by, {"from": prev, "reason": goal["blocked_reason"]})
        return goal


def from_review(root: str | Path | None, memory: Any, packet: str, finding_id: str,
                *, by: str = "cli") -> dict[str, Any]:
    """A goal from a repo-review finding's `suggested_goal` {title, targets,
    criteria}, with `review_resolved` added as a criterion and the finding
    as its source. Targets default to the finding's keys."""
    root_p = _root_of(memory, root)
    stored = _packet_arg(root_p, packet)
    data = load_packet(_packet_file(root_p, stored))
    if data is None:
        raise GoalError(f"no readable {PACKET_SCHEMA} packet at {packet}")
    finding = _finding(data, finding_id)
    if finding is None:
        raise GoalError(f"finding {finding_id} is not in {packet}")
    suggested = finding.get("suggested_goal")
    if not isinstance(suggested, dict):
        suggested = {}
    title = suggested.get("title") or (
        f"Resolve review finding {finding_id}: {finding.get('type', '')}".strip(": "))
    targets = suggested.get("targets")
    if not targets:
        targets = [k for k in finding.get("keys") or [] if isinstance(k, str)]
    criteria = list(suggested.get("criteria") or [])
    criteria.append({"kind": "review_resolved",
                     "args": {"packet": str(_packet_file(root_p, stored)),
                              "finding_id": finding_id}})
    why = suggested.get("why") or (
        f"repo review finding {finding_id} ({finding.get('section', '?')}/"
        f"{finding.get('type', '?')}, {finding.get('severity', '?')})")
    spec = {"title": title, "why": why, "targets": targets,
            "scope": suggested.get("scope") or [], "criteria": criteria}
    return add_goal(root_p, memory, spec, by=by,
                    source={"review_packet": stored, "finding_id": finding_id})


def load_goal_file(path: str | Path) -> dict[str, Any]:
    """A goal spec from a TOML file: title, why, targets, scope and
    criteria (strings `kind:args`, or tables with `kind` and `args` or
    the kind's parameters)."""
    import tomllib
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise GoalError(f"cannot read goal file {path}: {e}") from e
    return data


# --------------------------------------------------------------------------- #
# summaries: unmet criteria, hints, the brief
# --------------------------------------------------------------------------- #
def criterion_label(crit: dict[str, Any]) -> str:
    args = crit.get("args") or {}
    kind = crit.get("kind")
    if kind in ("calls", "not_calls"):
        subject = f"{args.get('caller')} -> {args.get('callee')}"
    elif kind == "review_resolved":
        subject = f"finding {args.get('finding_id')}"
    else:
        subject = str(args.get("key") or "")
        if kind == "ops_has" and args.get("attr"):
            subject += f" {args['attr']}" + (f"={args['value']}" if args.get("value") else "")
    return f"{crit.get('id')} {kind} {subject}".strip()


def unmet(goal: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """(criterion, last status or `unchecked`) for every criterion whose
    last result is not a pass."""
    out = []
    for c in goal.get("criteria") or []:
        last = c.get("last") or {}
        status = last.get("status") or "unchecked"
        if status != "pass":
            out.append((c, status))
    return out


def claimed_by(goal: dict[str, Any], harness: str | None, session: str | None) -> bool:
    if not session:
        return False
    return any(c.get("session") == session
               and (harness is None or c.get("harness") == harness)
               for c in goal.get("claimed_by") or [])


def summary(goal: dict[str, Any]) -> dict[str, Any]:
    """A compact view of a goal (no history)."""
    return {"id": goal.get("id"), "title": goal.get("title"),
            "status": goal.get("status"),
            "claimed_by": [f"{c.get('harness')}/{c.get('session')}"
                           for c in goal.get("claimed_by") or []],
            "targets": list(goal.get("targets") or [])[:10],
            "criteria": len(goal.get("criteria") or []),
            "unmet": [{"id": c.get("id"), "label": criterion_label(c), "status": s}
                      for c, s in unmet(goal)],
            "checked_at": goal.get("checked_at")}


def _goal_line(goal: dict[str, Any], harness: str | None, session: str | None,
               limit: int = BRIEF_CRITERIA) -> str:
    claims = goal.get("claimed_by") or []
    who = ("claimed by this session" if claimed_by(goal, harness, session)
           else f"claimed by {len(claims)} session(s)" if claims else "unclaimed")
    pending = unmet(goal)
    parts = [f"{criterion_label(c)} ({s})" for c, s in pending[:limit]]
    if len(pending) > limit:
        parts.append(f"+{len(pending) - limit} more")
    line = (f"{goal.get('id')} [{goal.get('status')}, {who}] "
            f"{_clip(goal.get('title'), 80)}: unmet " + "; ".join(parts))
    return "  " + _clip(line, LINE_CHARS - 2)


def brief_lines(root: str | Path, *, harness: str | None = None,
                session: str | None = None, max_chars: int = BRIEF_CHARS,
                goals: list[dict[str, Any]] | None = None) -> list[str]:
    """Lines for the session brief and the compaction context: goals that
    are open or regressed, or claimed and not dropped or met, with unmet
    criteria (from their last check; `unchecked` before any), the session's
    claims first, at most `BRIEF_GOALS` and `max_chars` in all. [] when
    there are none."""
    goals = read_goals(root) if goals is None else goals
    shown = [g for g in goals if unmet(g) and (
        g.get("status") in ("open", "regressed")
        or (g.get("claimed_by") and g.get("status") not in ("dropped", "met")))]
    if not shown:
        return []
    shown.sort(key=lambda g: (not claimed_by(g, harness, session),
                              g.get("status") != "regressed",
                              -(g.get("updated_at") or 0)))
    head = "goals (unmet criteria from the last `ozagia goal check`; goal_show for detail):"
    lines = [head]
    size = len(head) + 1
    for i, g in enumerate(shown[:BRIEF_GOALS]):
        line = _goal_line(g, harness, session)
        if size + len(line) + 1 > max_chars:
            rest = len(shown) - i
            more = f"  ... {rest} more goal(s): ozagia goal list"
            if size + len(more) + 1 <= max_chars:
                lines.append(more)
            break
        lines.append(line)
        size += len(line) + 1
    else:
        if len(shown) > BRIEF_GOALS:
            more = f"  ... {len(shown) - BRIEF_GOALS} more goal(s): ozagia goal list"
            if size + len(more) + 1 <= max_chars:
                lines.append(more)
    return lines if len(lines) > 1 else []


def edit_hints(root: str | Path, harness: str | None, session: str | None,
               files: Iterable[str], keys: Iterable[str] = (),
               goals: list[dict[str, Any]] | None = None) -> list[str]:
    """`[ozagia]` lines after an edit by (harness, session) of `files`
    (root-relative) with the changed or enclosing node `keys`:

    - for each goal the session claimed whose targets the edit touched (a
      target key among `keys` or related to one, or the file of a `file:`/
      `doc:` target, or of a `sym:`/`test:` target when the edit names no
      key in that file), the goal and the criteria now affected (test and
      review criteria, which go stale, and criteria about the edited
      files);
    - for each claimed goal with a scope, one advisory note naming the
      edited files outside it.

    Never blocks anything; [] when the session claimed no goal."""
    if not session:
        return []
    files = [f for f in dict.fromkeys(files) if isinstance(f, str) and f]
    keys = [k for k in dict.fromkeys(keys) if isinstance(k, str)]
    if not files:
        return []
    goals = read_goals(root) if goals is None else goals
    mine = [g for g in goals if claimed_by(g, harness, session)
            and g.get("status") != "dropped"]
    lines: list[str] = []
    for goal in mine[:HINT_GOALS]:
        touched = []
        for t in goal.get("targets") or []:
            rel = key_file(t)
            file_keys = [k for k in keys if key_file(k) == rel]
            if any(_related(t, k) for k in keys):
                touched.append(t)
            elif rel in files and (t.startswith(("file:", "doc:")) or not file_keys):
                touched.append(t)
        if touched:
            affected = []
            for c in goal.get("criteria") or []:
                subj = [v for n, v in (c.get("args") or {}).items()
                        if n in KINDS.get(c.get("kind"), ((), 0, {}, False))[2]]
                if c.get("kind") in TIME_KINDS or any(key_file(s) in files for s in subj):
                    affected.append(criterion_label(c))
            shown = ", ".join(touched[:3]) + (f" (+{len(touched) - 3})" if len(touched) > 3 else "")
            crit = "; ".join(affected[:HINT_CRITERIA]) + (
                f"; +{len(affected) - HINT_CRITERIA} more" if len(affected) > HINT_CRITERIA else "")
            line = (f"[ozagia] goal {goal['id']} \"{_clip(goal.get('title'), 60)}\": "
                    f"this edit touched target {shown}"
                    + (f"; criteria affected: {crit}" if crit else "")
                    + f"; re-check with goal_check (ozagia goal check {goal['id']})")
            lines.append(_clip(line, 400))
        scope = goal.get("scope") or []
        if scope:
            outside = [f for f in files if not in_scope(scope, f, keys)]
            if outside:
                names = ", ".join(outside[:HINT_FILES]) + (
                    f" (+{len(outside) - HINT_FILES})" if len(outside) > HINT_FILES else "")
                lines.append(_clip(f"[ozagia] goal {goal['id']} scope note: {names} "
                                   "is outside the claimed goal's scope (advisory)", 300))
    return lines


__all__ = ["BRIEF_CHARS", "Evaluator", "GoalError", "KINDS", "PACKET_SCHEMA",
           "RESULT_STATUSES", "SCHEMA_VERSION", "STATUSES", "add_goal",
           "block_goal", "brief_lines", "check_goals", "claim_goal",
           "claimed_by", "content_hash", "criterion_label", "drop_goal",
           "edit_hints", "from_review", "get_goal", "glob_regex", "goals_path",
           "in_scope", "last_change", "list_goals", "load_goal_file",
           "load_packet", "load_store", "locked_store", "parse_criterion",
           "read_goals", "summary", "unmet", "validate_criterion"]
