"""Per-session read ledger: what each agent session has already read.

The daemon keeps one `SessionState` per `(harness, session, sub)` in a
`SessionStore` and passes it to `ozagia.intent.handle`/`after`. `sub`
separates agents that share a session id: Claude Code subagents (the hook
payload's `agent_id`), Codex threads (`transcript_path`, and `agent_id` when
a payload has one). The native loop and calls without a session id get no
state, and nothing here runs for them.

The ledger records every read the daemon serves or observes: the read tool
(served, or a built-in Read seen through `after`), and shell reads (`cat`,
`head`, `tail`, `sed -n`, `nl`, also `nl -ba F | sed -n 'a,bp'` and a
leading `cd`) served by the capture or seen through `after`. A record is
(file, sha256 of the file at read time, line range, read number, call
index). A file's records all have one sha: reading it with another sha
(it was edited) drops the older ones.

A read *repeats* when the same session reads a range of the same file that
lies inside one recorded range (full containment) and the file's sha256 is
unchanged since that read, with no compaction since (a `session_event`
clears the session's ledger). Partial overlap is not a repeat: the text is
returned with a note of the lines already read.

The repeat policy (`[reads] repeat`, `[reads] repeat_by_harness`, else
`HARNESS_REPEAT`):

    stub   the repeat gets one line instead of the text:
           [ozagia] <rel> unchanged since read N (lines a-b, sha <12 hex>);
           keys: <up to 5>; read with a different range or after an edit
           to get text
    note   the text, then `[ozagia] unchanged since read N (lines a-b, sha ...)`
    off    no ledger.

`stub` is the default only for harnesses that report compaction and whose
subagents are told apart: OpenCode (its plugin's `compaction` hook; a
subagent is its own session) and Claude Code (`SessionStart` compact/clear;
subagent calls carry `agent_id`). Codex reports compaction too, but its
docs say subagent hooks carry the parent's session id and document no
subagent field for PreToolUse/PostToolUse; a subagent without its parent's
context would get stubs for text it never saw. Codex therefore defaults to
`note`; `transcript_path` and an `agent_id`, when present, still key its
sessions apart. Every other harness gets `note`.

Bounds: `MAX_SESSIONS` sessions (least recently used dropped first),
`MAX_FILES` files per session (least recently read dropped), `MAX_RANGES`
ranges per file (oldest dropped).
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_SESSIONS = 64
MAX_FILES = 256
MAX_RANGES = 32
# Keys listed in a stub.
STUB_KEYS = 5
# Earlier overlapping reads named in a partial-overlap note.
OVERLAP_NOTES = 3
POLICIES = ("stub", "note", "off")
HARNESS_REPEAT = {"opencode": "stub", "claude": "stub", "codex": "note"}
DEFAULT_REPEAT = "note"
SESSION_EVENTS = ("compact", "start", "end")


def policy_for(root: Any, harness: str) -> str:
    """The repeat policy for `harness` in the project at `root`."""
    from ozagia.config import ConfigError, load_project_config
    try:
        cfg = load_project_config(root)
    except ConfigError:
        return HARNESS_REPEAT.get(harness, DEFAULT_REPEAT)
    by = dict(cfg.repeat_by_harness)
    if harness in by:
        return by[harness]
    if cfg.repeat is not None:
        return cfg.repeat
    return HARNESS_REPEAT.get(harness, DEFAULT_REPEAT)


@dataclass(frozen=True)
class Read:
    n: int          # read number in the session, from 1
    call: int       # call index in the session when it was read
    a: int
    b: int
    sha: str


@dataclass
class ReadSpec:
    """One read: the file, its sha256 now, and the 1-based line range."""
    rel: str
    path: Path
    a: int
    b: int
    sha: str
    lines: list[str]

    def text_bytes(self) -> int:
        """The size of the range in read format (`<n>: <line>`)."""
        return sum(len(f"{i}: {self.lines[i - 1]}") + 1
                   for i in range(self.a, self.b + 1))


class SessionState:
    """The ledger of one session. Methods take the store's lock."""

    def __init__(self, store: "SessionStore", harness: str, session: str,
                 sub: str, policy: str) -> None:
        self._store = store
        self.harness, self.session, self.sub = harness, session, sub
        self.policy = policy
        self.reads = 0
        self.calls = 0
        self.compactions = 0
        self._pending = False       # a tool/check whose `after` comes next
        self.files: OrderedDict[str, list[Read]] = OrderedDict()

    # -- call index ---------------------------------------------------------- #
    def tick(self, *, then_after: bool = False) -> int:
        """Count one tool call. `then_after`: an `after` for the same call
        follows (passthrough, a hook's PreToolUse check) and does not count
        again."""
        with self._store.lock:
            self.calls += 1
            self._pending = then_after
            return self.calls

    def tick_after(self) -> int:
        with self._store.lock:
            if self._pending:
                self._pending = False
            else:
                self.calls += 1
            return self.calls

    # -- ledger -------------------------------------------------------------- #
    def find(self, spec: ReadSpec) -> Read | None:
        """The latest recorded read of the same unchanged file whose range
        contains `spec`'s."""
        with self._store.lock:
            for r in reversed(self.files.get(spec.rel, ())):
                if r.sha == spec.sha and r.a <= spec.a and spec.b <= r.b:
                    return r
            return None

    def overlaps(self, spec: ReadSpec) -> list[Read]:
        """Recorded reads of the unchanged file that overlap `spec`'s range
        without containing it."""
        with self._store.lock:
            return [r for r in self.files.get(spec.rel, ())
                    if r.sha == spec.sha and r.a <= spec.b and spec.a <= r.b
                    and not (r.a <= spec.a and spec.b <= r.b)]

    def record(self, spec: ReadSpec) -> Read:
        with self._store.lock:
            self.reads += 1
            rec = Read(self.reads, self.calls, spec.a, spec.b, spec.sha)
            old = self.files.pop(spec.rel, [])
            kept = [r for r in old if r.sha == spec.sha][-(MAX_RANGES - 1):]
            self.files[spec.rel] = [*kept, rec]
            while len(self.files) > MAX_FILES:
                self.files.popitem(last=False)
            return rec

    def clear(self) -> None:
        with self._store.lock:
            self.files.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._store.lock:
            return {"harness": self.harness, "session": self.session,
                    "sub": self.sub, "policy": self.policy,
                    "reads": self.reads, "calls": self.calls,
                    "compactions": self.compactions,
                    "files": {rel: [(r.n, r.a, r.b, r.sha[:12]) for r in rs]
                              for rel, rs in self.files.items()}}


class SessionStore:
    """The daemon's sessions, least recently used dropped beyond
    `max_sessions`. Thread-safe."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self.lock = threading.RLock()
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[tuple[str, str, str], SessionState] = OrderedDict()
        self.evicted = 0
        self.events: dict[str, int] = {}

    def get(self, harness: str, session: Any, sub: Any = None, *,
            policy: str = DEFAULT_REPEAT) -> SessionState | None:
        """The state of a session (created with `policy`), or None without
        a session id."""
        if not isinstance(session, str) or not session:
            return None
        key = (harness, session, sub if isinstance(sub, str) else "")
        with self.lock:
            st = self._sessions.get(key)
            if st is None:
                st = SessionState(self, *key, policy)
                self._sessions[key] = st
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
                    self.evicted += 1
            else:
                st.policy = policy
                self._sessions.move_to_end(key)
            return st

    def event(self, harness: str, session: str, event: str) -> int:
        """Apply a session event to every state of (harness, session), all
        sub-keys: `compact` and `start` clear the ledger, `end` drops the
        state. Returns how many states it touched."""
        if event not in SESSION_EVENTS:
            raise ValueError(f"event must be one of {', '.join(SESSION_EVENTS)}")
        with self.lock:
            self.events[event] = self.events.get(event, 0) + 1
            keys = [k for k in self._sessions if k[0] == harness and k[1] == session]
            for k in keys:
                if event == "end":
                    del self._sessions[k]
                else:
                    st = self._sessions[k]
                    st.files.clear()
                    if event == "compact":
                        st.compactions += 1
            return len(keys)

    def __len__(self) -> int:
        with self.lock:
            return len(self._sessions)

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return {"sessions": len(self._sessions), "evicted": self.evicted,
                    "events": dict(self.events)}


# --------------------------------------------------------------------------- #
# what a call reads
# --------------------------------------------------------------------------- #
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    n = int(value)
    if n < 1:
        raise ValueError(n)
    return n


def _spec(target: Any, a: int, b: int, lines: list[str]) -> ReadSpec | None:
    total = len(lines)
    b = min(b, total)
    if total == 0 or a < 1 or a > b:
        return None
    return ReadSpec(target.rel, target.path, a, b, _sha(target.path), lines)


def _structured_outline_applies(memory: Any, target: Any, lines: list[str]) -> bool:
    """Whether a plain read of a .json/.yaml/.toml file may be its outline
    (the thresholds only; such reads are not recorded)."""
    from ozagia.core import structured as S
    from ozagia.intent.read import _outline_limits
    if S.format_of(target.rel) is None:
        return False
    from ozagia.intent.common import root_of
    max_lines, max_bytes = _outline_limits(root_of(memory))
    size = target.path.stat().st_size
    return ((max_bytes > 0 and size > max_bytes)
            or (max_lines > 0 and len(lines) > max_lines))


def _whole_file(memory: Any, target: Any, lines: list[str], *, served: bool,
                cap: int | None) -> ReadSpec | None:
    """A read with no range: the outline's head when the daemon serves it
    as an outline, else lines 1 to `cap` (or the end)."""
    from ozagia.intent.read import outline_head
    if served:
        if _structured_outline_applies(memory, target, lines):
            return None
        got = outline_head(memory, target)
        if got is not None:
            return _spec(target, 1, got[2], lines) if got[2] else None
    return _spec(target, 1, cap or len(lines), lines)


def _read_tool_spec(memory: Any, inp: dict[str, Any], served: bool
                    ) -> ReadSpec | None:
    from ozagia.core.keys import parse_key
    from ozagia.core.structured import parse_key as structured_key
    from ozagia.intent.common import (
        is_node_key,
        read_text,
        resolve,
        root_of,
        split_lines,
    )
    from ozagia.intent.read import DEFAULT_LIMIT, _node_range, section_range
    path = inp.get("path", inp.get("filePath"))
    if not isinstance(path, str) or not path:
        return None
    root = root_of(memory)
    exists = (root / path).exists()
    if structured_key(path) is not None and not exists:
        return None
    offset, limit = _int(inp.get("offset")), _int(inp.get("limit"))
    if is_node_key(path) and not exists:
        kind = path.split(":", 1)[0]
        if kind == "doc" and "#" in path:
            target, a, b = section_range(memory, path)
            return _spec(target, a, b, split_lines(read_text(target.path)))
        rel = parse_key(path)[1]
        if kind in ("file", "doc"):
            path = rel
        else:
            target = resolve(root, rel.split("#", 1)[0] if kind == "text" else rel)
            if not target.path.is_file():
                return None
            text = read_text(target.path)
            lines = split_lines(text)
            a, b, _ = _node_range(memory, path, text, len(lines))
            return _spec(target, a, b, lines)
    target = resolve(root, path)
    if not target.path.is_file():
        return None
    lines = split_lines(read_text(target.path))
    if offset is None and limit is None:
        return _whole_file(memory, target, lines, served=served, cap=DEFAULT_LIMIT)
    a = offset or 1
    return _spec(target, a, a + (limit or DEFAULT_LIMIT) - 1, lines)


def _window(a: int, b: int, f: tuple) -> tuple[int, int] | None:
    """The file lines a line filter (`head`/`tail`/`from`/`lines`) keeps
    of the window a..b."""
    n = b - a + 1
    kind = f[0]
    if kind == "head":
        k = min(n, int(f[1]))
        return (a, a + k - 1) if k > 0 else None
    if kind == "tail":
        k = min(n, int(f[1]))
        return (b - k + 1, b) if k > 0 else None
    if kind == "from":
        return (a + int(f[1]) - 1, b) if 1 <= int(f[1]) <= n else None
    if kind == "lines":
        start = a + int(f[1]) - 1
        end = b if f[2] is None else min(b, a + int(f[2]) - 1)
        return (start, end) if start <= end else None
    return None


def _shell_spec(memory: Any, inp: dict[str, Any], served: bool
                ) -> ReadSpec | None:
    from ozagia.intent.common import read_text, resolve, root_of, split_lines
    from ozagia.intent.compound import _range_bounds
    from ozagia.intent.shell import classify, compound_steps
    command = inp.get("command")
    if not isinstance(command, str):
        return None
    root = root_of(memory)
    wd_in = inp.get("workdir")
    wd = Path(str(wd_in)) if wd_in else root
    if not wd.is_absolute():
        wd = root / wd
    cls = classify(command)
    filters: list[tuple] = []
    simple = cls["class"] == "read"
    if simple:
        step_cls = cls
        if "cd" in cls:
            wd = wd / cls["cd"]
    elif cls["class"] == "compound":
        steps = compound_steps(cls)
        reads = [s for s in steps if s["kind"] == "read"]
        if len(reads) != 1 or steps[-1] is not reads[0]:
            return None
        for s in steps[:-1]:
            if s["kind"] != "cd" or s.get("tilde"):
                return None
            wd = wd / s["dir"]
        step_cls = reads[0]["cls"]
        filters = list(reads[0]["filters"])
        if any(not f or f[0] not in ("head", "tail", "from", "lines")
               for f in filters):
            return None
    else:
        return None
    rng = step_cls["range"]
    if rng[0].startswith("bytes"):
        return None
    p = Path(step_cls["file"])
    target = resolve(root, str(p if p.is_absolute() else wd / p))
    if not target.path.is_file():
        return None
    lines = split_lines(read_text(target.path))
    if (simple and rng == ("all",) and step_cls.get("tool") == "cat"
            and not step_cls.get("number")):
        return _whole_file(memory, target, lines, served=served, cap=None)
    a, b = _range_bounds(rng, len(lines))
    for f in filters:
        got = _window(a, b, f)
        if got is None:
            return None
        a, b = got
    return _spec(target, a, b, lines)


def read_spec(memory: Any, tool: str, inp: Any, *, served: bool
              ) -> ReadSpec | None:
    """What a `read` or `shell` call reads (file, sha256, line range), or
    None when it is not a file read the ledger tracks. `served`: the daemon
    answers it (a plain read may be an outline); else a built-in ran it."""
    if not isinstance(inp, dict):
        return None
    try:
        if tool == "read":
            return _read_tool_spec(memory, inp, served)
        if tool == "shell":
            return _shell_spec(memory, inp, served)
    except Exception:  # noqa: BLE001 - the read itself reports any problem
        return None
    return None


# --------------------------------------------------------------------------- #
# answers
# --------------------------------------------------------------------------- #
def range_keys(memory: Any, spec: ReadSpec, limit: int = STUB_KEYS) -> list[str]:
    """Up to `limit` keys enclosing the lines of `spec`."""
    from ozagia.intent.common import Keyer
    keyer = Keyer(memory, spec.rel, "\n".join(spec.lines) + "\n")
    keys: list[str] = []
    for line in range(spec.a, spec.b + 1):
        k = keyer.key(line)
        if k not in keys:
            keys.append(k)
            if len(keys) > limit:
                break
    return keys


def _since(rec: Read, spec: ReadSpec) -> str:
    return (f"unchanged since read {rec.n} (lines {spec.a}-{spec.b}, "
            f"sha {spec.sha[:12]})")


def stub_text(memory: Any, spec: ReadSpec, rec: Read) -> tuple[str, list[str]]:
    """The one-line answer to a repeat, and its keys."""
    from ozagia.intent.common import tag
    keys = range_keys(memory, spec)
    shown = ", ".join(keys[:STUB_KEYS]) + (", ..." if len(keys) > STUB_KEYS else "")
    return (tag(f"{spec.rel} {_since(rec, spec)}; keys: {shown or '-'}; read "
                f"with a different range or after an edit to get text"),
            keys[:STUB_KEYS])


def note_lines(spec: ReadSpec, rec: Read | None,
               overlaps: list[Read]) -> list[str]:
    """`[ozagia]` lines for a read that repeats `rec` (note policy) or
    overlaps earlier reads."""
    from ozagia.intent.common import tag
    if rec is not None:
        return [tag(_since(rec, spec))]
    out = []
    for r in overlaps[-OVERLAP_NOTES:]:
        a, b = max(r.a, spec.a), min(r.b, spec.b)
        out.append(tag(f"lines {a}-{b} were already read unchanged "
                       f"(read {r.n})"))
    return out


def stub_bytes_saved(spec: ReadSpec, stub: str) -> int:
    return max(0, spec.text_bytes() - len(stub))


__all__ = ["DEFAULT_REPEAT", "HARNESS_REPEAT", "MAX_FILES", "MAX_RANGES",
           "MAX_SESSIONS", "POLICIES", "Read", "ReadSpec", "SESSION_EVENTS",
           "SessionState", "SessionStore", "note_lines", "policy_for",
           "range_keys", "read_spec", "stub_bytes_saved", "stub_text"]
