"""Per-session read ledger: what each agent session has already read.

The daemon keeps one `SessionState` per `(harness, session, sub)` in a
`SessionStore` and passes it to `ozagia.intent.handle`/`after`. `sub`
separates agents that share a session id: Claude Code subagents (the hook
payload's `agent_id`), Codex threads (`transcript_path`, and `agent_id` when
a payload has one). The native loop and calls without a session id get no
state, and nothing here runs for them.

The ledger records only text the model received:

- A read the daemon serves (the read tool, a shell read captured by
  `ozagia.intent.run`) is *staged* with a token, returned as `meta.ack`.
  It counts once the plugin acknowledges delivery: OpenCode's plugin sends
  the token in `acks` on its next request for the session, after the
  permission check passed and the output went to the model. A refused or
  failed permission, a late answer or a dropped ack leaves it unrecorded.
- A read the harness ran itself (a built-in Read seen through `after`, a
  shell read in `after`) is checked against the `output` the harness
  passed: the recorded range is the contiguous run of the command's lines
  that the output shows, from the first (`delivered_spec`; line-number
  prefixes such as `<n>: `, `<n>\\t` or `<n>→` are accepted when the number
  matches). Truncated output records only the matched prefix; output that
  does not match the file as it is now (it was edited between the read
  and the hook) records nothing from the first differing line on. Claude
  Code's Read reports `file.startLine`/`numLines`, sent as `delivered`,
  which bounds the range first.

Shell reads are `cat`, `head`, `tail`, `sed -n`, `nl`, also
`nl -ba F | sed -n 'a,bp'` and a leading `cd`. Under a hook harness
(`HOOK_HARNESSES`) a command with a `cd` is not tracked: Claude Code's
shell keeps its directory between calls and the hook's `cwd` follows it,
so PreToolUse and PostToolUse would resolve the `cd` differently. A record
is (file, sha256 of the file at read time, line range, read number, call
index). A file's records all have one sha: reading it with another sha (it
was edited) drops the older ones.

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

Every harness defaults to `note` (`HARNESS_REPEAT`). `stub` is opt-in and
experimental until the live eval shows no drop in success rate: a stub is
only correct while the model still holds the earlier text, and harnesses
drop context without a compaction event this module hears about
(OpenCode's tool-output pruning and `/undo` revert, Claude Code's rewind).

Even when configured, `stub` applies to a session only once its harness
has confirmed it reports compaction (`effective_policy`); until then the
session gets `note`:

    opencode  the plugin's `compaction` session hook registered: the
              plugin sends `compaction_hook: true` in `hello` and on its
              requests.
    claude    a `session_event` carrying `compaction_hook: true` came from
              the hook's SessionStart (installed with the compact matcher),
              and an `agent_id` was seen in the session: a Claude Code
              version that does not send `agent_id` would merge a
              subagent's ledger into its parent's.
    codex     a `session_event` with `compaction_hook: true` (SessionStart;
              PreCompact is installed with it). Codex's docs say subagent
              hooks carry the parent's session id; `transcript_path` and an
              `agent_id`, when present, key its sessions apart.
    other     never (no compaction reports).

A `session_event` that failed (daemon down or slow) is not lost: the hook
(a marker file in the state dir) or the plugin (memory) sends
`reset_pending: true` on its next request for the session, and the daemon
applies it as a `compact` first.

Bounds: `MAX_SESSIONS` sessions (least recently used dropped first),
`MAX_FILES` files per session (least recently read dropped), `MAX_RANGES`
ranges per file (oldest dropped).
"""
from __future__ import annotations

import hashlib
import re
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
# Served reads staged and not yet acknowledged, per session.
MAX_STAGED = 32
# Leading output lines (a harness's header) skipped when matching output.
MAX_HEADER_LINES = 10
POLICIES = ("stub", "note", "off")
HARNESS_REPEAT = {"opencode": "note", "claude": "note", "codex": "note"}
DEFAULT_REPEAT = "note"
SESSION_EVENTS = ("compact", "start", "end")
# Harnesses whose reads reach the ledger through PreToolUse/PostToolUse hooks.
HOOK_HARNESSES = frozenset({"claude", "codex"})
# Session capabilities (`SessionStore.confirm`).
CAP_COMPACTION = "compaction"
CAP_AGENT_ID = "agent_id"
STUB_HARNESSES = frozenset({"opencode", "claude", "codex"})


def effective_policy(harness: str, configured: str, caps: Any) -> str:
    """The policy a session gets: `configured`, except that `stub` falls
    back to `note` until the harness confirmed what a stub relies on (see
    the module docstring)."""
    if configured != "stub":
        return configured
    if harness not in STUB_HARNESSES or CAP_COMPACTION not in caps:
        return "note"
    if harness == "claude" and CAP_AGENT_ID not in caps:
        return "note"
    return "stub"


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
        self.configured = policy
        self.reads = 0
        self.calls = 0
        self.compactions = 0
        self._pending = False       # a tool/check whose `after` comes next
        self.files: OrderedDict[str, list[Read]] = OrderedDict()
        # token -> (rel, a, b, sha, call) of served reads awaiting an ack.
        self.staged: OrderedDict[str, tuple[str, int, int, str, int]] = OrderedDict()

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
            return self._add(spec.rel, spec.a, spec.b, spec.sha, self.calls)

    def _add(self, rel: str, a: int, b: int, sha: str, call: int) -> Read:
        self.reads += 1
        rec = Read(self.reads, call, a, b, sha)
        old = self.files.pop(rel, [])
        kept = [r for r in old if r.sha == sha][-(MAX_RANGES - 1):]
        self.files[rel] = [*kept, rec]
        while len(self.files) > MAX_FILES:
            self.files.popitem(last=False)
        return rec

    def stage(self, spec: ReadSpec) -> str:
        """Hold a served read until its delivery is acknowledged (`ack`);
        returns the token. The oldest staged reads are dropped beyond
        `MAX_STAGED` (never recorded)."""
        with self._store.lock:
            self._store.tokens += 1
            token = f"r{self._store.tokens}"
            self.staged[token] = (spec.rel, spec.a, spec.b, spec.sha, self.calls)
            while len(self.staged) > MAX_STAGED:
                self.staged.popitem(last=False)
            return token

    def ack(self, tokens: Any) -> int:
        """Record the staged reads named by `tokens` (a list of strings);
        unknown tokens are ignored. Returns how many were recorded."""
        if not isinstance(tokens, list):
            return 0
        n = 0
        with self._store.lock:
            for t in tokens:
                got = self.staged.pop(t, None) if isinstance(t, str) else None
                if got is not None:
                    self._add(*got)
                    n += 1
        return n

    def coverage(self, rel: str, sha: str) -> list[tuple[int, int]]:
        """The merged line ranges of `rel` recorded with `sha`."""
        with self._store.lock:
            spans = sorted((r.a, r.b) for r in self.files.get(rel, ())
                           if r.sha == sha)
        merged: list[tuple[int, int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def clear(self) -> None:
        with self._store.lock:
            self.files.clear()
            self.staged.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._store.lock:
            return {"harness": self.harness, "session": self.session,
                    "sub": self.sub, "policy": self.policy,
                    "configured": self.configured,
                    "reads": self.reads, "calls": self.calls,
                    "compactions": self.compactions,
                    "staged": len(self.staged),
                    "files": {rel: [(r.n, r.a, r.b, r.sha[:12]) for r in rs]
                              for rel, rs in self.files.items()}}


class SessionStore:
    """The daemon's sessions, least recently used dropped beyond
    `max_sessions`. Thread-safe."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self.lock = threading.RLock()
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[tuple[str, str, str], SessionState] = OrderedDict()
        # (harness, session) -> confirmed capabilities (CAP_*); kept apart
        # from the states so a cap seen before the first read still counts.
        self._caps: OrderedDict[tuple[str, str], set[str]] = OrderedDict()
        self.evicted = 0
        self.tokens = 0
        self.events: dict[str, int] = {}

    def confirm(self, harness: str, session: Any, cap: str) -> None:
        """Note that (harness, session) has capability `cap`."""
        if not isinstance(session, str) or not session:
            return
        key = (harness, session)
        with self.lock:
            self._caps.setdefault(key, set()).add(cap)
            self._caps.move_to_end(key)
            while len(self._caps) > 4 * self.max_sessions:
                self._caps.popitem(last=False)
            for k, st in self._sessions.items():
                if k[:2] == key:
                    st.policy = effective_policy(harness, st.configured,
                                                 self._caps[key])

    def caps(self, harness: str, session: str) -> frozenset[str]:
        with self.lock:
            return frozenset(self._caps.get((harness, session), ()))

    def get(self, harness: str, session: Any, sub: Any = None, *,
            policy: str = DEFAULT_REPEAT) -> SessionState | None:
        """The state of a session, or None without a session id. `policy`
        is the configured one; the state's `policy` is what
        `effective_policy` makes of it. A non-empty `sub` confirms that the
        harness sends `agent_id`/sub-keys (`CAP_AGENT_ID`)."""
        if not isinstance(session, str) or not session:
            return None
        sub_s = sub if isinstance(sub, str) else ""
        key = (harness, session, sub_s)
        with self.lock:
            if sub_s:
                self.confirm(harness, session, CAP_AGENT_ID)
            eff = effective_policy(harness, policy,
                                   self._caps.get((harness, session), ()))
            st = self._sessions.get(key)
            if st is None:
                st = SessionState(self, *key, eff)
                self._sessions[key] = st
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
                    self.evicted += 1
            else:
                st.policy = eff
                self._sessions.move_to_end(key)
            st.configured = policy
            return st

    def event(self, harness: str, session: str, event: str) -> int:
        """Apply a session event to every state of (harness, session), all
        sub-keys: `compact` and `start` clear the ledger (and staged reads),
        `end` drops the states and the session's capabilities. Returns how
        many states it touched."""
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
                    st.staged.clear()
                    if event == "compact":
                        st.compactions += 1
            if event == "end":
                self._caps.pop((harness, session), None)
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


def _shell_spec(memory: Any, inp: dict[str, Any], served: bool,
                no_cd: bool = False) -> ReadSpec | None:
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
            if no_cd:
                return None
            wd = wd / cls["cd"]
    elif cls["class"] == "compound":
        steps = compound_steps(cls)
        reads = [s for s in steps if s["kind"] == "read"]
        if len(reads) != 1 or steps[-1] is not reads[0]:
            return None
        for s in steps[:-1]:
            if s["kind"] != "cd" or s.get("tilde") or no_cd:
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


def read_spec(memory: Any, tool: str, inp: Any, *, served: bool,
              no_cd: bool = False) -> ReadSpec | None:
    """What a `read` or `shell` call reads (file, sha256, line range), or
    None when it is not a file read the ledger tracks. `served`: the daemon
    answers it (a plain read may be an outline); else a built-in ran it.
    `no_cd`: a shell read with a `cd` is not tracked (hook harnesses)."""
    if not isinstance(inp, dict):
        return None
    try:
        if tool == "read":
            return _read_tool_spec(memory, inp, served)
        if tool == "shell":
            return _shell_spec(memory, inp, served, no_cd)
    except Exception:  # noqa: BLE001 - the read itself reports any problem
        return None
    return None


# A line-number prefix a harness puts before a file line: `<n>: ` (OpenCode
# read), `<n>\t` (`cat -n`, `nl -ba`), `<n>→` (Claude Code's Read), `<n>| `.
_NUMBERED_RE = re.compile(r"^\s*(\d+)(?:\t|\u2192|: ?|\| ?)(.*)$")


def _same_line(shown: str, line: str, n: int) -> bool:
    if shown == line or shown.rstrip("\r") == line:
        return True
    m = _NUMBERED_RE.match(shown)
    return bool(m) and int(m.group(1)) == n and m.group(2).rstrip("\r") == line


def _run_length(out: list[str], start: int, want: list[str], a: int) -> int:
    k = 0
    for j, line in enumerate(want):
        i = start + j
        if i >= len(out) or not _same_line(out[i], line, a + j):
            break
        k += 1
    return k


def delivered_spec(spec: ReadSpec, output: Any, delivered: Any = None
                   ) -> ReadSpec | None:
    """The part of `spec` the harness's `output` shows: the longest run of
    the range's lines, from its first line, found within the first
    `MAX_HEADER_LINES` output lines (a header such as Codex's exit code may
    come first). `delivered` (`{"start": n, "lines": k}`, e.g. Claude Code
    Read's `file.startLine`/`numLines`) bounds the range first. None when
    nothing matches: then nothing is recorded."""
    a, b = spec.a, spec.b
    if isinstance(delivered, dict):
        try:
            start = _int(delivered.get("start"))
            count = delivered.get("lines")
            if start is not None and isinstance(count, int) and not isinstance(count, bool):
                a, b = max(a, start), min(b, start + count - 1)
        except (TypeError, ValueError):
            return None
    if a > b or not isinstance(output, str) or not output:
        return None
    out = output.splitlines()
    want = spec.lines[a - 1:b]
    best = 0
    for start in range(min(len(out), MAX_HEADER_LINES + 1)):
        best = max(best, _run_length(out, start, want, a))
        if best == len(want):
            break
    if best == 0:
        return None
    return ReadSpec(spec.rel, spec.path, a, a + best - 1, spec.sha, spec.lines)


def partial_view(memory: Any, state: Any, inp: Any) -> str | None:
    """An `[ozagia] warning:` line for a `write` that replaces an existing
    file of which this session's ledger holds only part (an outline's head,
    a range) of the current text; None when it holds all of it or nothing
    (the session may have read it some other way)."""
    from ozagia.intent.common import read_text, resolve, root_of, split_lines, tag
    if state is None or not isinstance(inp, dict):
        return None
    path = inp.get("path", inp.get("filePath"))
    if not isinstance(path, str) or not path:
        return None
    try:
        target = resolve(root_of(memory), path)
        if not target.path.is_file():
            return None
        total = len(split_lines(read_text(target.path)))
        spans = state.coverage(target.rel, _sha(target.path))
    except Exception:  # noqa: BLE001 - no warning rather than a failed write
        return None
    if not spans or total == 0 or (spans[0][0] <= 1 and spans[0][1] >= total):
        return None
    shown = ", ".join(f"{a}-{b}" for a, b in spans[:4]) + (
        ", ..." if len(spans) > 4 else "")
    return tag(f"warning: {target.rel} was replaced as a whole, but this session "
               f"has read only lines {shown} of its {total} lines (an outline "
               f"or a partial read); check that nothing outside them was lost")


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


__all__ = ["CAP_AGENT_ID", "CAP_COMPACTION", "DEFAULT_REPEAT", "HARNESS_REPEAT",
           "HOOK_HARNESSES", "MAX_FILES", "MAX_RANGES", "MAX_SESSIONS",
           "MAX_STAGED", "POLICIES", "Read", "ReadSpec", "SESSION_EVENTS",
           "SessionState", "SessionStore", "delivered_spec", "effective_policy",
           "note_lines", "partial_view", "policy_for", "range_keys", "read_spec",
           "stub_bytes_saved", "stub_text"]
