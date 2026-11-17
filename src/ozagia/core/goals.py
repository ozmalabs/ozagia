"""ozagia.core.goals — repo-level goals addressed by graph keys.

A goal names the graph nodes the work is about (`targets`), what the work
may change (`scope`) and machine-checkable `criteria`. Completion is decided
by evaluating the criteria against the graph, the working tree and the
test record, never from an agent's own claim. Goals are repo-level, kept in
`<state dir>/goals/goals.json`; sessions claim them.

Store (schema version 1; atomic write, `fcntl` file lock on
`goals/.lock`, required: a writer that cannot take it gets a GoalError;
at most `MAX_GOALS` goals not dropped, `MAX_DROPPED` dropped ones):

    {"version": 1, "next_id": <n>, "goals": [goal, ...]}

    goal: {id ("g<n>"), title, why, status, targets, scope, criteria,
           claimed_by, history, source, created_at, updated_at, checked_at}
    status:     open | met | regressed | blocked | dropped
    targets:    graph keys the work is about (each resolved at creation)
    scope:      graph keys or root-relative path globs the work may change
    criteria:   [{id ("c<n>"), kind, args, last}]; `last` is the latest
                result {status, evidence, reason, at}, or absent
    claimed_by: [{harness, session, at}]
    history:    [{at, event, by, detail, repeat?}], every event
                (`MAX_HISTORY` kept; the `created` entry always; a
                `checked` like the one before it bumps that one's `repeat`)
    source:     {review_packet, finding_id} or null

Criteria kinds and their args (`KINDS`). Each evaluates to
`{status: pass|fail|stale|unknown, evidence, reason}`:

    test_passes / test_fails  {key: test:...}  from the root's test record
        (`ozagia.core.working_set.recorded_outcome(..., evidence=True)`):
        only runs the daemon captured itself count, and the run must still
        hold: `stale` when a file among the goal's targets, its scope (keys,
        and files matching its globs) and the test's file has other content
        now than when the run started (`ozagia.core.fingerprint`), `unknown`
        without a fingerprint, when the test key no longer resolves, or (for
        test_fails) when the test errored rather than failed.
    symbol_exists / symbol_absent  {key: sym:...}  graph node present/absent.
    calls / not_calls  {caller: sym:..., callee: sym:...}  an edge of the
        syntactic symbol graph (`ozagia.core.symbol_graph`) present/absent;
        `unknown` when a symbol is missing from the symbol graph but still
        in the graph (a file the tag parser skipped).
    no_callers  {key: sym:...}  no symbol-graph edge into the symbol.
    file_exists / file_absent  {key: file:...}  the file in the working tree.
    unchanged  {key, hash}  the key's content hash equals the hash taken at
        creation (file bytes; a symbol's or test's lines; an ops or other
        node's attrs).
    lint_clean  {key}  ozagia keeps no lint results (`ozagia.intent.lint`
        reports findings in the edit's reply and stores nothing), so this is
        always `unknown`, with that reason; `add_goal` warns.
    review_resolved  {packet, finding_id}  given; stored with the finding's
        `type` and `keys` and an `anchor` (the packet's `generated_at`,
        `head_commit`, sha256), all taken from `packet` at creation: a
        regular file under `<state dir>/reviews/` of at most
        `MAX_PACKET_BYTES`, written by `review_repo` (`generator`
        `ozagia.repo_review`) for this root, that contains the finding.
        Checked against the newest packet in the reviews dir
        (`latest.json`, else the newest `repo_review-*.json`), which must
        also be ozagia's and for this root, by
        `ozagia.core.repo_review.finding_state`: present -> fail,
        not_checked -> unknown, absent -> pass only when that packet is a
        later review than the anchor (later `generated_at`, other sha256),
        else unknown. `stale` when a file of the goal's targets, scope or
        the finding's keys has other content now than when that review
        started (the packet's `tree` fingerprint, as for test runs; a
        packet without one falls back to mtimes against `generated_at`,
        which a backdated mtime defeats).
    ops_has  {key: ops:..., attr?, value?, pinned?}  an ops node, optionally
        with a non-empty attribute, optionally equal to (or, for a list,
        containing) `value` (a value needs an attr), or with `pinned: true`
        an image (`attr`, default `image`, else `base_image`) with a digest
        or a tag other than latest (`ozagia.core.ops_graph.image_pinned`,
        the repo review's predicate). The node comes from the graph, or
        when the graph has none (the default index has no ops nodes), from
        the tracked operations sources parsed as the repo review's ops
        section parses them (`repo_review.declared_ops_nodes`); `ops:` keys
        resolve the same way at creation, and `file:` keys also resolve to
        a regular file in the working tree (a committed `.env`).

Kinds that read the graph are `unknown` when there is no graph or the
check's refresh failed; `--no-refresh` results say so in the evidence.

There is no shell-command criterion.

Creation resolves every key (targets, key entries of scope, criterion
subjects) against the graph and refuses unknown ones, except the subject of
`symbol_exists`/`file_exists`; it compiles scope globs and refuses invalid
ones, and bounds input lengths (`MAX_KEY_CHARS`, `MAX_ARG_CHARS`,
`MAX_FINDING_ID`). Status changes only by evaluation (`met` when
every criterion passes, from open or regressed; `regressed` when a met goal
has a failing criterion) or by explicit commands: `drop`, `block` (with a
reason) and `block --clear` (back to open). A blocked or dropped goal is
still evaluated when checked; its status does not change.

Standard library only. Callers pass the `DevMemory` where the graph is
needed; reading goals (hints, the session brief) needs no graph.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import shlex
import stat
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
# Input bounds: a key or scope entry, a criterion argument, a finding id.
MAX_KEY_CHARS = 1000
MAX_ARG_CHARS = 1000
MAX_FINDING_ID = 200
# Store bounds: goals not dropped (adding more is refused), and dropped
# goals kept (the oldest are removed when a write goes over).
MAX_GOALS = 500
MAX_DROPPED = 200
# Review packets: under `<state dir>/reviews/`, a regular file of at most
# this many bytes.
REVIEWS_DIR = "reviews"
MAX_PACKET_BYTES = 16 * 1024 * 1024
# How long a writer waits for the store's file lock.
LOCK_TIMEOUT_S = 30.0
# Files compared against a test run's fingerprint for one criterion.
MAX_FRESH_FILES = 5000
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
    "ops_has": (("key", "attr", "value", "pinned"), 1, {"key": "ops:"}, False),
}
# `ops:` key kinds whose second segment is a source file
# (`ops:compose/<rel>#service/<name>`, `core.ops_graph`).
OPS_FILE_KINDS = ("compose", "containerfile", "quadlet", "unit")
# Who writes repo-review packets (`core.repo_review.GENERATOR`).
REVIEW_GENERATOR = "ozagia.repo_review"
# Kinds whose evidence predates a change to the goal's targets.
TIME_KINDS = frozenset({"test_passes", "test_fails", "review_resolved"})
# Kinds that read the graph (`DevMemory`): `unknown` without one, or when
# the check's refresh failed. (`ops_has` falls back to the tracked
# operations sources, which it reads itself.)
GRAPH_KINDS = frozenset({"test_passes", "test_fails", "symbol_exists",
                         "symbol_absent", "calls", "not_calls", "no_callers"})
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*:")
_GLOB_CHARS = re.compile(r"[*?\[]")
_THREAD_LOCK = threading.Lock()
# `read_goals`'s cache: {store path: (stat signature, goals)}.
_STORE_CACHE: dict[str, tuple[tuple[int, int, int, int], list[dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()


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


def _key_path(key: str) -> str | None:
    """The root-relative file a key's content is in, for freshness: the
    key's file (an anchor after `#` dropped), or for an `ops:` key its
    source file after the kind segment (`ops:compose/deploy/compose.yaml#
    service/web` -> `deploy/compose.yaml`); None when the key names no
    file (`ops:image/nginx`)."""
    if not isinstance(key, str):
        return None
    if key.startswith("ops:"):
        kind, _, rest = key[len("ops:"):].partition("/")
        rel = rest.split("#", 1)[0]
        return rel if kind in OPS_FILE_KINDS and rel else None
    rel = key_file(key)
    return rel.split("#", 1)[0] if rel else None


# --------------------------------------------------------------------------- #
# path globs (scope)
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=512)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """A regex for root-relative POSIX paths: `**` any number of
    directories (a trailing `**` everything below), `*`/`?` within one
    component, `[...]` a class; a pattern ending in `/` matches everything
    below that directory. Raises `re.error` for an invalid class
    (`[z-a]`); `check_glob` turns that into a GoalError."""
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


def check_glob(pattern: str) -> None:
    """Refuse a scope glob that does not compile."""
    try:
        glob_regex(pattern)
    except re.error as e:
        raise GoalError(f"scope: invalid glob {pattern!r}: {e}") from None


def _glob_match(pattern: str, rel: str) -> bool:
    """Whether `rel` matches `pattern`; an invalid glob (a store written
    before globs were checked) matches nothing."""
    try:
        return bool(glob_regex(pattern).match(rel))
    except re.error:
        return False


def scope_glob(entry: str) -> str | None:
    """The path glob of a scope entry (a plain glob, or a `file:`/`doc:`
    key with glob characters), or None for a key."""
    if not is_key(entry):
        return entry
    prefix, _, rest = entry.partition(":")
    if prefix in ("file", "doc") and _GLOB_CHARS.search(entry):
        return rest
    return None


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
        glob = scope_glob(entry)
        if glob is not None:
            if _glob_match(glob, rel):
                return True
            continue
        prefix = entry.split(":", 1)[0]
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
    and briefs, which never fail). The parsed store is cached per path and
    reused while the file's (mtime_ns, ctime_ns, size, inode) stay the
    same, so the daemon's per-edit read is a `stat` in the common case.
    The list and its goals are shared: callers must not modify them."""
    path = goals_path(root)
    try:
        st = path.stat()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    sig = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    with _CACHE_LOCK:
        hit = _STORE_CACHE.get(str(path))
        if hit is not None and hit[0] == sig:
            return hit[1]
    try:
        goals = list(load_store(root)["goals"])
    except GoalError:
        return []
    with _CACHE_LOCK:
        _STORE_CACHE[str(path)] = (sig, goals)
        while len(_STORE_CACHE) > 16:
            _STORE_CACHE.pop(next(iter(_STORE_CACHE)))
    return goals


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
    exception nothing is written. Raises GoalError when the file lock
    cannot be taken (no `fcntl`, a filesystem without locks, another
    writer holding it for `LOCK_TIMEOUT_S`): the store is never written
    unlocked."""
    path = goals_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        raise GoalError("the goal store needs a file lock (fcntl), which this "
                        "platform does not have") from None
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    with _THREAD_LOCK, open(path.parent / LOCK_FILE, "a+") as lk:
        while True:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GoalError(f"the goal store {path} is locked by another "
                                    f"writer (waited {LOCK_TIMEOUT_S:.0f} s)") from None
                time.sleep(0.05)
            except OSError as e:
                raise GoalError(f"cannot lock the goal store {path}: {e}") from None
        try:
            data = load_store(root)
            yield data
            _compact(data)
            _write_store(path, data)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lk, fcntl.LOCK_UN)


def _compact(data: dict[str, Any]) -> None:
    """Keep at most `MAX_DROPPED` dropped goals (the most recently
    updated)."""
    dropped = [g for g in data["goals"] if g.get("status") == "dropped"]
    if len(dropped) <= MAX_DROPPED:
        return
    dropped.sort(key=lambda g: g.get("updated_at") or 0)
    gone = {id(g) for g in dropped[:len(dropped) - MAX_DROPPED]}
    data["goals"] = [g for g in data["goals"] if id(g) not in gone]


def _event(goal: dict[str, Any], event: str, by: str,
           detail: Any = None, at: float | None = None) -> None:
    """Add an event to the goal's history. A `checked` event the same as
    the last one (same `by` and counts) updates that one's time and its
    `repeat` count instead of adding an entry."""
    at = _now() if at is None else at
    hist = goal.setdefault("history", [])
    last = hist[-1] if hist else None
    if (event == "checked" and isinstance(last, dict) and last.get("event") == "checked"
            and last.get("by") == by and last.get("detail") == detail):
        last["at"] = at
        last["repeat"] = int(last.get("repeat") or 1) + 1
        goal["updated_at"] = at
        return
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
        if len(raw) > 3 * MAX_ARG_CHARS:
            raise GoalError(f"criterion longer than {3 * MAX_ARG_CHARS} characters")
        return parse_criterion(raw)
    if not isinstance(raw, dict):
        raise GoalError(f"criterion must be a string or a table, got {raw!r}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise GoalError(f"criterion kind {kind!r}: expected one of {', '.join(KINDS)}")
    args = raw.get("args")
    if isinstance(args, str):
        if len(args) > 3 * MAX_ARG_CHARS:
            raise GoalError(f"criterion {kind}: args longer than "
                            f"{3 * MAX_ARG_CHARS} characters")
        return parse_criterion(f"{kind}:{args}")
    if args is None:
        args = {k: v for k, v in raw.items() if k not in ("kind", "id", "last")}
    if isinstance(args, list):
        args = dict(zip(KINDS[kind][0], args))
    if not isinstance(args, dict):
        raise GoalError(f"criterion {kind}: args must be a table")
    return {"kind": kind, "args": dict(args)}


def _resolves(memory: Any, key: str, root: Path | None = None) -> bool:
    """Whether `key` names something: a graph node, or (with `root`) an
    `ops:` node declared in a tracked operations source
    (`repo_review.declared_ops_nodes`; the default index has no ops nodes)
    or, for a `file:` key, a regular file in the working tree (a committed
    `.env` or key file the indexer leaves out)."""
    if memory is not None:
        try:
            if memory.node_by_key(key) is not None:
                return True
        except Exception:  # noqa: BLE001 - treat as unknown
            pass
    if root is None or not isinstance(key, str):
        return False
    if key.startswith("ops:"):
        return declared_ops_node(root, key) is not None
    if key.startswith("file:"):
        return _working_file(root, key) is not None
    return False


def _working_file(root: Path, key: str) -> str | None:
    """The root-relative path of a `file:` key when it is a regular file
    (not a symlink) inside the root and outside the state dir."""
    from ozagia.config import state_rel
    rel = key_file(key)
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    state = state_rel(root)
    if state and (rel == state or rel.startswith(state.rstrip("/") + "/")):
        return None
    if rel not in _tracked_paths(root):
        # Only exact paths git tracks: not ignored files, `.git/` internals
        # or aliases (`./a.py`, `a//b`) that would never match a
        # fingerprint's paths.
        return None
    p = root / rel
    try:
        if p.is_symlink() or not p.is_file():
            return None
        if not p.resolve().is_relative_to(root.resolve()):
            return None
    except OSError:
        return None
    return rel


def _tracked_paths(root: Path) -> frozenset[str]:
    """Root-relative paths `git ls-files` lists (empty outside git)."""
    import subprocess
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                              capture_output=True, timeout=30, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if proc.returncode != 0:
        return frozenset()
    return frozenset(p.decode("utf-8", "surrogateescape")
                     for p in proc.stdout.split(b"\0") if p)


def declared_ops_node(root: Path, key: str,
                      nodes: dict[str, dict[str, Any]] | None = None
                      ) -> dict[str, Any] | None:
    """The attrs of an `ops:` node declared in the tracked operations
    sources (parsed like the repo review's ops section), or None. A key
    that names a file (`ops:compose/<rel>#service/web`) reads only that
    file; others (`ops:image/nginx:1.25`) read every source, unless
    `nodes` (all declared nodes, cached by the caller) is given."""
    if nodes is not None:
        return nodes.get(key)
    from ozagia.core.repo_review import declared_ops_nodes
    rel = _key_path(key) if key.startswith("ops:") else None
    found, _errors = declared_ops_nodes(root, [rel] if rel else None)
    return found.get(key)


def _root_of(memory: Any, root: str | Path | None) -> Path:
    if root is not None:
        return Path(root).resolve()
    return Path(memory.raw_repo_root).resolve()


def reviews_dir(root: str | Path) -> Path:
    """Where review packets must be: `<state dir>/reviews/`."""
    from ozagia.config import state_path
    return state_path(root, REVIEWS_DIR)


def _packet_arg(root: Path, value: str) -> str:
    """A packet path as stored: root-relative when inside the root. A
    relative `value` is relative to the root (the CLI passes absolute
    paths). The path must be a regular file under the reviews dir
    (`reviews_dir`, symlinks resolved) of at most `MAX_PACKET_BYTES`;
    anything else is refused."""
    if len(value) > MAX_ARG_CHARS:
        raise GoalError(f"review packet path longer than {MAX_ARG_CHARS} characters")
    p = Path(os.path.expanduser(value))
    if not p.is_absolute():
        p = root / p
    real = Path(os.path.realpath(p))
    _read_packet_bytes(root, real)          # raises GoalError
    try:
        return real.relative_to(root).as_posix()
    except ValueError:
        return str(real)


def _packet_file(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def _rel_or_abs(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _read_packet_bytes(root: Path, path: Path) -> bytes:
    """The bytes of a review packet, read safely: the path (symlinks
    resolved) must be under the reviews dir, the file is opened without
    following a final symlink and without blocking (a FIFO), must be a
    regular file and at most `MAX_PACKET_BYTES`. Raises GoalError."""
    base = os.path.realpath(reviews_dir(root))
    real = os.path.realpath(path)
    if os.path.commonpath([base, real]) != base or real == base:
        raise GoalError(f"review packet {path} is not under the reviews "
                        f"directory {base}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(
        os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as e:
        raise GoalError(f"cannot open review packet {path}: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise GoalError(f"review packet {path} is not a regular file")
        if st.st_size > MAX_PACKET_BYTES:
            raise GoalError(f"review packet {path} is larger than "
                            f"{MAX_PACKET_BYTES} bytes")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PACKET_BYTES:
                raise GoalError(f"review packet {path} is larger than "
                                f"{MAX_PACKET_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as e:
        raise GoalError(f"cannot read review packet {path}: {e.strerror or e}") from None
    finally:
        os.close(fd)


def _pinned_arg(value: Any) -> bool:
    """`ops_has`'s `pinned`: only true is meaningful (the image has a
    digest or a tag other than latest)."""
    if value is True or (isinstance(value, str)
                         and value.strip().lower() in ("true", "yes", "1")):
        return True
    raise GoalError(f"criterion ops_has: pinned must be true, got {value!r} (to "
                    "require a particular tag, use ops_has:KEY image VALUE)")


def _finding_info(packet: dict[str, Any], finding_id: str
                  ) -> tuple[str, list[str]] | None:
    """The type and keys of a finding in a packet: from its entry in
    `findings`, or for a finding the section cap left out of that list,
    from `sections[*].ids_by_type` with the keys rebuilt from `id_files`
    (`file:` keys, which is what `finding_state` needs). None when the id
    is not in the packet."""
    f = _finding(packet, finding_id)
    if f is not None and isinstance(f.get("type"), str):
        return f["type"], [k for k in f.get("keys") or [] if isinstance(k, str)]
    for section in (packet.get("sections") or {}).values():
        if not isinstance(section, dict):
            continue
        for ftype, ids in (section.get("ids_by_type") or {}).items():
            if isinstance(ids, list) and finding_id in ids:
                files = (section.get("id_files") or {}).get(finding_id) or []
                return str(ftype), [f"file:{p}" for p in files if isinstance(p, str)]
    return None


def _review_anchor(root: Path, packet_arg: str, finding_id: str) -> dict[str, Any]:
    """What `review_resolved` stores at creation besides the packet path and
    the finding id: the finding's `type` and `keys` (for
    `repo_review.finding_state`), and the `anchor`, the packet's
    `generated_at`, `head_commit` and sha256: the criterion passes only on
    a later, different packet. The packet must be under the reviews dir,
    written by `review_repo` (`generator`), for this root, and contain the
    finding. Raises GoalError."""
    stored = _packet_arg(root, packet_arg)
    path = _packet_file(root, stored)
    raw = _read_packet_bytes(root, path)
    packet = _parse_packet(raw, path)
    problem = _not_ours(packet, root)
    if problem:
        raise GoalError(f"review packet {path}: {problem}")
    info = _finding_info(packet, finding_id)
    if info is None:
        raise GoalError(f"finding {finding_id} is not in {path}")
    ftype, keys = info
    keys = [k for k in keys if isinstance(k, str) and len(k) <= MAX_KEY_CHARS
            and _safe_key_path(k)][:MAX_KEYS]
    from ozagia.core.repo_review import check_population
    anchor = {"generated_at": packet.get("generated_at"),
              "head_commit": packet.get("head_commit"),
              "sha256": _sha(raw)}
    population = check_population(packet, ftype)
    if population is not None:
        # The probed interpreters of an import check: a later review over
        # another set cannot decide the criterion.
        anchor["population"] = population
    return {"packet": stored, "type": _clip(ftype, 100), "keys": keys,
            "anchor": anchor}


def _safe_key_path(key: str) -> bool:
    """A packet key whose file (if any) is a canonical root-relative path:
    not absolute, no `.`/`..`/empty parts. Packet keys feed the files a
    check hashes, so they get the path checks targets get."""
    rel = _key_path(key) if key.startswith("ops:") else key_file(key)
    if not rel:
        return True
    if rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def _not_ours(packet: dict[str, Any], root: Path) -> str | None:
    """Why a packet cannot decide `review_resolved` for this root (not
    written by ozagia's review, another root, no `generated_at`), or None."""
    from ozagia.core.repo_review import parse_generated_at
    if packet.get("generator") != REVIEW_GENERATOR:
        return (f"not written by ozagia's review (generator "
                f"{_clip(packet.get('generator'), 60)!r}, expected "
                f"{REVIEW_GENERATOR!r}); run ozagia review-repo --save")
    if str(packet.get("root") or "") != str(root):
        return f"it reviews {_clip(packet.get('root'), 200)}, not {root}"
    if parse_generated_at(packet.get("generated_at")) is None:
        return "it has no readable generated_at"
    return None


def newest_packet(root: Path) -> tuple[Path | None, bytes | None, str | None]:
    """The newest review packet in the reviews dir: `latest.json`, or when
    that is missing or unreadable the newest timestamped
    `repo_review-*.json` (by name, which starts with the UTC time). Returns
    (path, bytes, None), or (path or None, None, why not)."""
    from ozagia.core.repo_review import LATEST_NAME, PACKET_PREFIX
    base = reviews_dir(root)
    candidates = [base / LATEST_NAME]
    try:
        stamped = sorted((p for p in base.glob(f"{PACKET_PREFIX}*.json")),
                         key=lambda p: p.name, reverse=True)
    except OSError:
        stamped = []
    candidates += stamped[:1]
    errors: list[str] = []
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            return path, _read_packet_bytes(root, path), None
        except GoalError as e:
            errors.append(str(e))
    return None, None, ("; ".join(errors)
                        or f"no review packet in {base}; run ozagia review-repo --save")


def tracking_goals(root: str | Path) -> dict[str, str]:
    """{finding id: goal id} for goals that are not dropped and came from a
    review finding (`source.finding_id`)."""
    out: dict[str, str] = {}
    for g in read_goals(root):
        src = g.get("source") or {}
        fid = src.get("finding_id") if isinstance(src, dict) else None
        if isinstance(fid, str) and g.get("status") != "dropped":
            out.setdefault(fid, str(g.get("id")))
    return out


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
    pinned = None
    if kind == "ops_has" and "pinned" in args:
        pinned = _pinned_arg(args.pop("pinned"))
    out: dict[str, Any] = {}
    for name in names:
        if args.get(name) is None:
            continue
        if not isinstance(args[name], (str, int, float)) or isinstance(args[name], bool):
            raise GoalError(f"criterion {kind}: {name} must be a string")
        value = str(args[name]).strip()
        limit = MAX_FINDING_ID if name == "finding_id" else MAX_ARG_CHARS
        if len(value) > limit:
            raise GoalError(f"criterion {kind}: {name} is longer than {limit} "
                            "characters")
        if name in key_params:
            prefix = key_params[name]
            value = _default_prefix(value, prefix)
            if prefix and not value.startswith(prefix):
                raise GoalError(f"criterion {kind}: {name} must be a {prefix} key, "
                                f"got {value!r}")
            if not is_key(value):
                raise GoalError(f"criterion {kind}: {name} {value!r} is not a key")
            if not (may_be_absent and name == "key") and not _resolves(memory, value, root):
                raise GoalError(f"criterion {kind}: unknown key {value}")
        out[name] = value
    if kind == "ops_has" and out.get("value") is not None and not out.get("attr"):
        raise GoalError("criterion ops_has: a value needs an attr (ops_has:KEY "
                        "ATTR VALUE); without one only existence is checked")
    if pinned:
        if out.get("value") is not None:
            raise GoalError("criterion ops_has: pinned and value exclude each other")
        out["pinned"] = True
    if kind == "review_resolved":
        out.update(_review_anchor(root, out["packet"], out["finding_id"]))
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


MAX_HASH_BYTES = 256 * 1024 * 1024


def _sha_file(path: Path) -> str:
    """sha256 of a regular file, read in chunks, refusing symlinks, files
    that are not regular and files over `MAX_HASH_BYTES` (OSError)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if st.st_size > MAX_HASH_BYTES:
            raise OSError(f"{path} is over {MAX_HASH_BYTES} bytes")
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


def content_hash(root: Path, memory: Any, key: str) -> tuple[str | None, dict[str, Any]]:
    """The content hash of a key and how it was taken: a file's bytes
    (`file:`, `doc:`), the lines of a `text:` range or of a symbol's or
    test's span (from the graph), else the node's attrs. (None, ...) when
    the key does not resolve or the file cannot be read."""
    prefix = key.split(":", 1)[0]
    rel = key_file(key)
    if prefix in ("file", "doc") and rel:
        try:
            return _sha_file(root / rel), {"basis": "file bytes", "file": rel}
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
    file (None when no file exists; `ops:` keys count by their source)."""
    best: tuple[float | None, str | None] = (None, None)
    for key in keys:
        rel = _key_path(key)
        if not rel:
            continue
        t = _mtime(root, rel)
        if t is not None and (best[0] is None or t > best[0]):
            best = (t, rel)
    return best


def read_packet(root: str | Path, path: Path) -> dict[str, Any]:
    """A repo-review packet read safely (`_read_packet_bytes`). Raises
    GoalError when it is not under the reviews dir, not a regular file,
    too large, not JSON or of another schema."""
    return _parse_packet(_read_packet_bytes(Path(root).resolve(), path), path)


def _parse_packet(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GoalError(f"review packet {path} is not JSON") from None
    if not isinstance(data, dict) or data.get("schema") != PACKET_SCHEMA:
        raise GoalError(f"no readable {PACKET_SCHEMA} packet at {path}")
    if not isinstance(data.get("findings"), list):
        raise GoalError(f"no readable {PACKET_SCHEMA} packet at {path}")
    return data


def load_packet(root: str | Path, path: Path) -> dict[str, Any] | None:
    """A repo-review packet (see `read_packet`), or None."""
    try:
        return read_packet(root, path)
    except GoalError:
        return None


def _finding(packet: dict[str, Any], finding_id: str) -> dict[str, Any] | None:
    for f in packet.get("findings") or []:
        if isinstance(f, dict) and f.get("id") == finding_id:
            return f
    return None


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
class Evaluator:
    """Evaluates criteria for one check: the test record, the symbol graph
    and the git view are loaded once, on first use.

    `graph` says what the graph is: `fresh` (refreshed for this check, or
    nothing was pending), `not refreshed` (`--no-refresh`: recorded in the
    evidence), `refresh failed: ...` or `no graph`. In the last two cases
    every kind that reads the graph (`GRAPH_KINDS`, `unchanged` of a
    non-file key) is `unknown`."""

    def __init__(self, root: str | Path, memory: Any, *, graph: str | None = None
                 ) -> None:
        self.root = Path(root).resolve()
        self.memory = memory
        self.graph = graph or ("fresh" if memory is not None else "no graph")
        self._record: dict[str, Any] | None = None
        self._sgraph: Any = None
        self._sgraph_error: str | None = None
        self._view: Any = None
        self._ops: dict[str, dict[str, Any]] | None = None
        self._ops_errors: dict[str, str] = {}

    @property
    def graph_usable(self) -> bool:
        return self.memory is not None and self.graph in ("fresh", "not refreshed")

    # -- shared sources ---------------------------------------------------- #
    def record(self) -> dict[str, Any]:
        if self._record is None:
            from ozagia.core.working_set import load_test_record
            self._record = load_test_record(self.root)
        return self._record

    def view(self) -> Any:
        if self._view is None:
            from ozagia.core.fingerprint import TreeView
            self._view = TreeView(self.root)
        return self._view

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

    def ops_node(self, key: str) -> tuple[dict[str, Any] | None, str]:
        """An `ops:` node's attrs and where they came from: the graph (when
        usable and it has the node), else the tracked operations sources
        (`repo_review.declared_ops_nodes`, parsed once per check)."""
        if self.graph_usable:
            node = self.node(key)
            if node is not None:
                return (node.get("attrs") or {}), "graph"
        if self._ops is None:
            from ozagia.core.repo_review import declared_ops_nodes
            self._ops, self._ops_errors = declared_ops_nodes(self.root)
        return self._ops.get(key), "sources"

    # -- one criterion ----------------------------------------------------- #
    def evaluate(self, goal: dict[str, Any], crit: dict[str, Any]) -> dict[str, Any]:
        kind = crit.get("kind")
        fn = getattr(self, f"_eval_{kind}", None)
        if fn is None:
            return _result("unknown", {}, f"unknown criterion kind {kind!r}")
        args = dict(crit.get("args") or {})
        if kind in GRAPH_KINDS and not self.graph_usable:
            return _result("unknown", {"graph": self.graph},
                           f"the graph is unusable for this check ({self.graph})")
        try:
            res = fn(goal, args)
        except Exception as e:  # noqa: BLE001 - a criterion never fails a check
            return _result("unknown", {}, f"evaluation failed: {type(e).__name__}: {e}")
        if self.graph != "fresh" and kind in GRAPH_KINDS:
            res["evidence"] = {**res.get("evidence", {}), "graph": self.graph}
        return res

    def relevant_files(self, goal: dict[str, Any], run_tree: dict[str, Any],
                       extra: Iterable[str] = ()) -> tuple[list[str], str | None]:
        """The files whose content a test result depends on, as far as the
        goal says: the files of its targets, of its scope keys, the files
        matching its scope globs (among the paths tracked or untracked now,
        in the run's commit, or dirty at the run), and of the `extra` keys
        (the test). A reason instead when there are too many."""
        files: list[str] = []
        for key in [*(goal.get("targets") or []), *extra]:
            rel = _key_path(key)
            if rel:
                files.append(rel)
        globs = []
        for entry in goal.get("scope") or []:
            glob = scope_glob(entry)
            if glob is None:
                rel = _key_path(entry)
                if rel:
                    files.append(rel)
            else:
                globs.append(glob)
        if globs:
            view = self.view()
            candidates = set(view.paths())
            candidates.update(view.tree(run_tree.get("head")) or {})
            candidates.update(run_tree.get("dirty") or {})
            candidates.update(run_tree.get("unhashed") or [])
            for rel in sorted(candidates):
                if any(_glob_match(g, rel) for g in globs):
                    files.append(rel)
                    if len(files) > MAX_FRESH_FILES:
                        break
        files = list(dict.fromkeys(files))
        if len(files) > MAX_FRESH_FILES:
            return files[:MAX_FRESH_FILES], (
                f"the goal's targets and scope cover more than {MAX_FRESH_FILES} "
                "files")
        return files, None

    def _fresh(self, goal: dict[str, Any], run: dict[str, Any], test_key: str
               ) -> tuple[str | None, dict[str, Any]]:
        """None when the run's evidence still holds for this goal, else the
        status (`stale` or `unknown`) and the evidence: the content of
        every relevant file (`relevant_files`) now must equal its content
        when the run started (`ozagia.core.fingerprint`)."""
        from ozagia.core.fingerprint import changed_since
        tree = run.get("tree")
        if not isinstance(tree, dict):
            return "unknown", {"reason": "the run has no working-tree fingerprint "
                               "(not a git repository, or git failed)"}
        files, err = self.relevant_files(goal, tree, [test_key])
        ev: dict[str, Any] = {"run_head": tree.get("head"), "files_compared": len(files)}
        if err:
            return "unknown", {**ev, "reason": err}
        changed, why = changed_since(self.view(), tree, files)
        if why:
            return "stale", {**ev, "changed_files": changed[:5], "reason": why}
        if changed:
            return "stale", {**ev, "changed_files": changed[:5],
                             "changed_count": len(changed),
                             "reason": f"{changed[0]} changed since the run started"
                             + (f" (+{len(changed) - 1} more)" if len(changed) > 1 else "")}
        return None, ev

    def _test(self, goal: dict[str, Any], args: dict[str, Any], want: str
              ) -> dict[str, Any]:
        from ozagia.core.working_set import recorded_outcome
        key = args["key"]
        ev: dict[str, Any] = {"key": key}
        if self.node(key) is None:
            return _result("unknown", ev, f"{key} is no longer in the graph "
                           "(deleted or renamed?)")
        got = recorded_outcome(self.record(), key, evidence=True)
        run = got.get("run") or {}
        ev.update({"outcome": got["outcome"], "run_at": got.get("ts"),
                   "run_source": run.get("source")})
        if got["outcome"] == "unknown":
            return _result("unknown", ev, got["reason"])
        status, fev = self._fresh(goal, run, key)
        ev.update({k: v for k, v in fev.items() if k != "reason"})
        if status is not None:
            return _result(status, ev, f"the last run ({got['outcome']}): "
                           f"{fev.get('reason')}")
        if got["outcome"] == "error":
            if want == "failed":
                return _result("unknown", ev, f"test errored, which is not a "
                               f"failure: {got['reason']}")
            return _result("fail", ev, f"test errored: {got['reason']}")
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
            ev["missing"] = missing
            # A symbol the tag parser skipped (a syntax error, an unsupported
            # language) is still in the graph: the edge cannot be told.
            known = [sid for sid in missing if self.node("sym:" + sid) is not None]
            if known:
                ev["in_graph_only"] = known
                return _result("unknown", ev, f"{', '.join(known)} is in the graph "
                               "but not in the symbol graph (tag parser skipped it?)")
            return _result("fail" if want else "pass", ev,
                           f"{', '.join(missing)} not in the graph or the symbol "
                           "graph, so no edge")
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
        if not key.startswith(("file:", "doc:")) and not self.graph_usable:
            # A symbol's span or a node's attrs come from the graph.
            return _result("unknown", {"key": key, "graph": self.graph},
                           f"the graph is unusable for this check ({self.graph})")
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
        """From the newest packet in the reviews dir (`newest_packet`), by
        `repo_review.finding_state`: present -> fail, not_checked ->
        unknown, absent -> pass only when that packet is a later review
        than the one the criterion was created from (`anchor`: later
        `generated_at`, other sha256). The packet must be written by
        `review_repo` for this root, and `stale` when a file of the goal's
        targets, scope or the finding's keys has other content now than when
        the review started (the packet's `tree` fingerprint; a packet
        without one falls back to file mtimes against `generated_at`)."""
        from ozagia.core.repo_review import finding_state, parse_generated_at
        fid = args["finding_id"]
        anchor = args.get("anchor") if isinstance(args.get("anchor"), dict) else None
        ev: dict[str, Any] = {"finding_id": fid, "created_from": args.get("packet")}
        if anchor is None or not args.get("type"):
            return _result("unknown", ev, "the criterion has no review anchor (made "
                           "before anchoring); recreate the goal from a saved review")
        path, raw, why = newest_packet(self.root)
        if raw is None or path is None:
            return _result("unknown", ev, why or "no review packet")
        ev["packet"] = _rel_or_abs(self.root, path)
        ev["packet_sha256"] = _sha(raw)
        try:
            packet = _parse_packet(raw, path)
        except GoalError as e:
            return _result("unknown", ev, str(e))
        problem = _not_ours(packet, self.root)
        if problem:
            return _result("unknown", ev, f"{ev['packet']}: {problem}")
        ev.update({"generated_at": packet.get("generated_at"),
                   "head_commit": packet.get("head_commit")})
        keys = [k for k in args.get("keys") or [] if isinstance(k, str)]
        status, sev = self._review_fresh(goal, packet, keys)
        ev.update({k: v for k, v in sev.items() if k != "reason"})
        if status is not None:
            return _result(status, ev, f"{sev.get('reason')}; run ozagia review-repo "
                           "--save again")
        state = finding_state(packet, fid, type=args["type"], keys=keys,
                              population=anchor.get("population"))
        ev["state"] = state
        if state == "present":
            return _result("fail", ev, f"finding {fid} is still in the review of "
                           f"{packet.get('generated_at')}")
        if state != "absent":
            return _result("unknown", ev, f"the review of {packet.get('generated_at')} "
                           f"did not check {args['type']} over its full population")
        then = parse_generated_at(anchor.get("generated_at"))
        now = parse_generated_at(packet.get("generated_at"))
        if then is None or now is None or now <= then or ev["packet_sha256"] == \
                anchor.get("sha256"):
            return _result("unknown", ev, f"finding {fid} is absent, but the newest "
                           "packet is not a later review than the one the goal was "
                           "created from; run ozagia review-repo --save")
        return _result("pass", ev, f"finding {fid} is not in the review of "
                       f"{packet.get('generated_at')} (a later review than the "
                       f"goal's, {anchor.get('generated_at')})")

    def _review_fresh(self, goal: dict[str, Any], packet: dict[str, Any],
                      keys: list[str]) -> tuple[str | None, dict[str, Any]]:
        """None when the packet still describes the goal's files, else
        (`stale` or `unknown`, evidence with a reason)."""
        from ozagia.core.fingerprint import changed_since, clean_fingerprint
        from ozagia.core.repo_review import parse_generated_at
        tree = clean_fingerprint(packet.get("tree"))
        if tree is not None:
            files, err = self.relevant_files(goal, tree, keys)
            ev: dict[str, Any] = {"basis": "content", "files_compared": len(files)}
            if err:
                return "unknown", {**ev, "reason": err}
            changed, why = changed_since(self.view(), tree, files,
                                         skip_ignored=True)
            if why:
                return "stale", {**ev, "changed_files": changed[:5], "reason": why}
            if changed:
                return "stale", {**ev, "changed_files": changed[:5],
                                 "reason": f"{changed[0]} changed since the review "
                                 "started" + (f" (+{len(changed) - 1} more)"
                                              if len(changed) > 1 else "")}
            return None, ev
        # Weaker: a packet without a fingerprint (an older ozagia, or not a
        # git repository). File mtimes against generated_at; a backdated
        # mtime hides an edit, and scope globs are not expanded.
        at = parse_generated_at(packet.get("generated_at"))
        scope_keys = [e for e in goal.get("scope") or [] if scope_glob(e) is None]
        changed_at, rel = last_change(self.root, [*(goal.get("targets") or []),
                                                  *scope_keys, *keys])
        ev = {"basis": "mtime", "targets_changed_at": changed_at, "changed_file": rel}
        if at is not None and changed_at is not None and changed_at > at:
            return "stale", {**ev, "reason": f"{rel} was modified after the review "
                             "(by mtime: the packet has no tree fingerprint)"}
        return None, ev

    def _eval_ops_has(self, goal: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.ops_graph import image_pinned
        key = args["key"]
        attrs, origin = self.ops_node(key)
        ev: dict[str, Any] = {"key": key, "found": attrs is not None, "origin": origin}
        if attrs is None:
            where = "the graph or the tracked operations sources"
            if self._ops_errors.get(_key_path(key) or ""):
                where += f" ({_key_path(key)}: {self._ops_errors[_key_path(key) or '']})"
            return _result("fail", ev, f"{key} is not in {where}")
        attr = args.get("attr")
        if not attr and args.get("value") is not None:
            return _result("unknown", ev, "ops_has has a value but no attr "
                           "(refused at creation since; recreate the goal)")
        if args.get("pinned") and not attr:
            attr = "image" if attrs.get("image") else "base_image"
        if not attr:
            return _result("pass", ev, f"{key} is declared ({origin})")
        value = attrs.get(attr)
        ev["attr"] = attr
        ev["actual"] = value if isinstance(value, (str, int, float, bool)) or value is None \
            else (value[:10] if isinstance(value, list) else str(type(value).__name__))
        if value in (None, "", [], {}):
            return _result("fail", ev, f"{key} has no {attr}")
        if args.get("pinned"):
            pinned = image_pinned(value) if isinstance(value, str) else None
            if pinned is None:
                return _result("unknown", ev, f"{key} {attr} {_clip(value, 80)!r} is "
                               "not an image reference ozagia can read")
            return _result("pass" if pinned else "fail", ev,
                           f"{key} {attr} {value} is "
                           + ("pinned" if pinned else "not pinned (no digest, tag latest)"))
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
    if len(value) > MAX_KEYS:
        raise GoalError(f"{name}: at most {MAX_KEYS} entries")
    out = list(dict.fromkeys(v.strip() for v in value if v.strip()))
    long = [v for v in out if len(v) > MAX_KEY_CHARS]
    if long:
        raise GoalError(f"{name}: an entry is longer than {MAX_KEY_CHARS} characters")
    return out


def _check_scope_entry(entry: str, memory: Any, root: Path | None = None) -> None:
    glob = scope_glob(entry)
    if glob is not None:
        check_glob(glob)
        return
    if not _resolves(memory, entry, root):
        raise GoalError(f"scope: unknown key {entry}")


def _warnings(criteria: list[dict[str, Any]]) -> list[str]:
    """What the caller should know about criteria that were accepted."""
    out = []
    for c in criteria:
        if c["kind"] == "lint_clean":
            out.append(f"{c['id']} lint_clean cannot currently pass (ozagia keeps "
                       "no lint results), so this goal cannot be met until it does")
    return out


def add_goal(root: str | Path | None, memory: Any, spec: dict[str, Any], *,
             by: str = "cli", source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a goal from `spec` {title, why?, targets, scope?, criteria}
    and return it, with `warnings` (not stored) when a criterion was
    accepted that cannot currently pass (`lint_clean`). Every key must
    resolve in `memory` (see the module docstring); nothing is written
    when any does not. At most `MAX_GOALS` goals that are not dropped."""
    root_p = _root_of(memory, root)
    if not isinstance(spec, dict):
        raise GoalError("a goal spec must be a table")
    allowed = {"title", "why", "targets", "scope", "criteria"}
    extra = sorted(set(spec) - allowed)
    if extra:
        raise GoalError(f"unknown goal field(s): {', '.join(extra)}")
    title = _clip(str(spec.get("title") or "")[:4 * MAX_TITLE], MAX_TITLE)
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
        if not _resolves(memory, t, root_p):
            raise GoalError(f"targets: unknown key {t}")
    for entry in scope:
        _check_scope_entry(entry, memory, root_p)
    criteria = []
    for i, raw in enumerate(raw_criteria, start=1):
        c = validate_criterion(raw, memory, root_p)
        criteria.append({"id": f"c{i}", **c})
    now = _now()
    with locked_store(root_p) as data:
        active = sum(1 for g in data["goals"] if g.get("status") != "dropped")
        if active >= MAX_GOALS:
            raise GoalError(f"the store has {active} goals that are not dropped "
                            f"(at most {MAX_GOALS}); drop some first")
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
    warnings = _warnings(criteria)
    return {**goal, "warnings": warnings} if warnings else goal


def _refresh_state(memory: Any, refresh: bool) -> str:
    """Take pending changes into the in-memory graph (the snapshot on disk
    is not rewritten) and say how that went (see `Evaluator.graph`)."""
    if memory is None:
        return "no graph"
    if not refresh:
        return "not refreshed"
    if not hasattr(memory, "refresh"):
        return "fresh"
    try:
        if memory.pending_changes():
            memory.refresh(write=False)
    except Exception as e:  # noqa: BLE001 - reported as unknown results
        return f"refresh failed: {type(e).__name__}: {_clip(e, 120)}"
    return "fresh"


def _same_criterion(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a.get("id") == b.get("id") and a.get("kind") == b.get("kind")
            and (a.get("args") or {}) == (b.get("args") or {}))


def check_goals(root: str | Path | None, memory: Any, ids: Iterable[str] | None = None,
                *, by: str = "cli", refresh: bool = True) -> list[dict[str, Any]]:
    """Evaluate the criteria of the named goals (default: every goal not
    dropped), record each result on its criterion, apply the status
    transitions, and return one report per goal: {id, status, previous,
    results: [{id, kind, status, evidence, reason}]}. With `refresh`, the
    in-memory graph takes in pending changes first; a refresh that fails
    makes the graph-backed kinds `unknown`.

    The criteria are evaluated from a snapshot of the store without its
    lock (tag-cache and symbol-graph builds can take seconds); the results
    are then merged under the lock, by goal id and criterion id (a
    criterion whose kind or args changed meanwhile keeps its old result),
    and the transitions use the goal's status at that point."""
    root_p = _root_of(memory, root)
    graph = _refresh_state(memory, refresh)
    ev = Evaluator(root_p, memory, graph=graph)
    wanted = list(dict.fromkeys(ids)) if ids else None
    snapshot = load_store(root_p)
    if wanted:
        goals = [_find(snapshot, gid) for gid in wanted]
    else:
        goals = [g for g in snapshot["goals"] if g.get("status") != "dropped"]
    evaluated: list[tuple[str, float, list[tuple[dict[str, Any], dict[str, Any]]]]] = []
    for goal in goals:
        at = _now()
        evaluated.append((goal["id"], at, [(crit, ev.evaluate(goal, crit))
                                           for crit in goal.get("criteria") or []]))
    reports = []
    with locked_store(root_p) as data:
        by_id = {g.get("id"): g for g in data["goals"]}
        for gid, now, pairs in evaluated:
            goal = by_id.get(gid)
            if goal is None:
                continue                      # removed meanwhile
            results = []
            for crit in goal.get("criteria") or []:
                res = next((r for c, r in pairs if _same_criterion(c, crit)), None)
                if res is None:
                    res = _result("unknown", {}, "the criterion changed during the "
                                  "check; check again")
                else:
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
                            "status": new, "previous": prev, "results": results,
                            "graph": graph})
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


def from_review(root: str | Path | None, memory: Any, packet: str | None,
                finding_id: str, *, by: str = "cli") -> dict[str, Any]:
    """A goal from a repo-review finding's `suggested_goal` {title, targets,
    criteria}, with `review_resolved` added as a criterion and the finding
    as its source. `packet` defaults to the reviews dir's `latest.json`.
    Targets default to the finding's keys. A finding the section cap left
    out of the packet's `findings` list (only its id is kept) gets a goal
    with only `review_resolved`, its files as targets."""
    root_p = _root_of(memory, root)
    if not isinstance(finding_id, str) or not finding_id.strip() or \
            len(finding_id) > MAX_FINDING_ID:
        raise GoalError(f"a finding id is a string of at most {MAX_FINDING_ID} "
                        "characters")
    if packet is None:
        from ozagia.core.repo_review import LATEST_NAME
        packet = str(reviews_dir(root_p) / LATEST_NAME)
    stored = _packet_arg(root_p, packet)
    data = read_packet(root_p, _packet_file(root_p, stored))
    finding = _finding(data, finding_id)
    info = _finding_info(data, finding_id)
    if info is None:
        raise GoalError(f"finding {finding_id} is not in {packet}")
    finding = finding or {"type": info[0], "keys": info[1]}
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
        if kind == "ops_has" and args.get("pinned"):
            subject += " pinned"
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
    """Whether (harness, session) claimed the goal; False when either is
    missing (a claim is always by both)."""
    if not session or not harness:
        return False
    return any(c.get("session") == session and c.get("harness") == harness
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
               limit: int = BRIEF_CRITERIA, *, titles: bool = True) -> str:
    claims = goal.get("claimed_by") or []
    who = ("claimed by this session" if claimed_by(goal, harness, session)
           else f"claimed by {len(claims)} session(s)" if claims else "unclaimed")
    pending = unmet(goal)
    parts = [f"{criterion_label(c)} ({s})" for c, s in pending[:limit]]
    if len(pending) > limit:
        parts.append(f"+{len(pending) - limit} more")
    title = f" {_clip(goal.get('title'), 80)}" if titles else ""
    line = (f"{goal.get('id')} [{goal.get('status')}, {who}]{title}: unmet "
            + "; ".join(parts))
    return "  " + _clip(line, LINE_CHARS - 2)


def brief_lines(root: str | Path, *, harness: str | None = None,
                session: str | None = None, max_chars: int = BRIEF_CHARS,
                goals: list[dict[str, Any]] | None = None,
                titles: bool = True) -> list[str]:
    """Lines for the session brief and the compaction context: goals that
    are open or regressed, or claimed and not dropped or met, with unmet
    criteria (from their last check; `unchecked` before any), the session's
    claims first, at most `BRIEF_GOALS` and `max_chars` in all. [] when
    there are none. `titles=False` leaves out the goals' titles (free text
    whoever made the goal wrote): only ids, statuses, keys and criterion
    labels remain (the working set's `free_text: false`)."""
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
        line = _goal_line(g, harness, session, titles=titles)
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
      files), for the first `HINT_GOALS` claimed goals; a line names the
      claimed goals beyond those when the edit touched their targets;
    - one advisory scope note naming the edited files outside the union
      of the scopes of every goal the session claimed (none when one of
      them has no scope: its work is not bounded).

    Never blocks anything; [] when the session claimed no goal. The store
    is read through `read_goals`'s cache."""
    if not session or not harness:
        return []
    files = [f for f in dict.fromkeys(files) if isinstance(f, str) and f]
    keys = [k for k in dict.fromkeys(keys) if isinstance(k, str)]
    if not files:
        return []
    goals = read_goals(root) if goals is None else goals
    mine = [g for g in goals if claimed_by(g, harness, session)
            and g.get("status") != "dropped"]
    if not mine:
        return []
    lines: list[str] = []
    skipped: list[str] = []
    for n, goal in enumerate(mine):
        touched = []
        for t in goal.get("targets") or []:
            rel = key_file(t)
            file_keys = [k for k in keys if key_file(k) == rel]
            if any(_related(t, k) for k in keys):
                touched.append(t)
            elif rel in files and (t.startswith(("file:", "doc:")) or not file_keys):
                touched.append(t)
        if not touched:
            continue
        if n >= HINT_GOALS:
            skipped.append(str(goal.get("id")))
            continue
        affected = []
        for c in goal.get("criteria") or []:
            subj = [v for n_, v in (c.get("args") or {}).items()
                    if n_ in KINDS.get(c.get("kind"), ((), 0, {}, False))[2]]
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
    if skipped:
        names = ", ".join(skipped[:5]) + (f" (+{len(skipped) - 5})" if len(skipped) > 5 else "")
        lines.append(_clip(f"[ozagia] goals: this edit also touched targets of "
                           f"claimed goal(s) {names}, not detailed (more than "
                           f"{HINT_GOALS} claimed); ozagia goal list", 300))
    if all(g.get("scope") for g in mine):
        union = [entry for g in mine for entry in g.get("scope") or []]
        outside = [f for f in files if not in_scope(union, f, keys)]
        if outside:
            ids = ", ".join(str(g.get("id")) for g in mine[:5]) + (
                f" (+{len(mine) - 5})" if len(mine) > 5 else "")
            names = ", ".join(outside[:HINT_FILES]) + (
                f" (+{len(outside) - HINT_FILES})" if len(outside) > HINT_FILES else "")
            lines.append(_clip(f"[ozagia] goal scope note: {names} is outside the "
                               f"scope of the goals this session claimed ({ids}) "
                               "(advisory)", 300))
    return lines


__all__ = ["BRIEF_CHARS", "Evaluator", "GoalError", "KINDS", "PACKET_SCHEMA",
           "RESULT_STATUSES", "SCHEMA_VERSION", "STATUSES", "add_goal",
           "block_goal", "brief_lines", "check_glob", "check_goals", "claim_goal",
           "claimed_by", "content_hash", "criterion_label", "drop_goal",
           "edit_hints", "from_review", "get_goal", "glob_regex", "goals_path",
           "in_scope", "last_change", "list_goals", "load_goal_file",
           "load_packet", "load_store", "locked_store", "parse_criterion",
           "newest_packet", "read_goals", "read_packet", "reviews_dir",
           "scope_glob", "summary", "tracking_goals", "unmet", "validate_criterion"]
