"""The working set of an agent session, and the text that gives it back.

After a compaction the model keeps a summary and loses the files, keys and
test results it was working with; it then reads them again. The daemon
keeps, per session (`ozagia.intent.ledger.SessionState.work`, keyed like the
read ledger by (harness, session, sub)), what the session was working on,
and the compaction hooks hand it back as one bounded block of text:

- files and node keys it edited: handled `edit`/`write`/key edits, and
  `after` refreshes of edits, writes, patches and shell commands that
  changed files (`meta.changed_paths`, the files the refresh took in);
- the tests that failed in its last pytest runs (captured runs and the
  `[ozagia] failed test:...` lines of `after`), with the command that
  reruns them; a later run that selects a test and does not fail it
  clears it (`apply_test_run`);
- the files and line ranges it read most recently (from the ledger's
  records, which a compaction clears; this copy is not cleared);
- running background jobs (the daemon's job table, when it builds the
  text) and the current step of the newest open `DevSession`
  (`devsession_line`, best effort: no link between a harness session and
  a DevSession is recorded, so it is the newest open one in the root).

The working set survives `session_event` `compact` and `start` (that is its
point); it goes with the session state on `end` and on eviction from the
store. Bounds: `MAX_FILES` files, `MAX_KEYS` keys, `MAX_TESTS` failing
tests, `MAX_READ_FILES` read files with `MAX_READ_RANGES` ranges each.

`working_set_text` renders it: an `[ozagia] working set` header, then the
failing tests and their rerun command, running jobs, the plan step, the
edited files with their keys, and the recent reads, each line something to
act on, in that order, deterministic for the same state, at most
`TEXT_MAX_CHARS` (about 2,000 tokens; Codex shows 2,500 tokens of hook
output). Lines that do not fit are dropped from the end of each section
with an `... N more` line.

`session_brief` is the "since the last session" variant for session start:
the branch, dirty files and recent commits from git, the tests failing at
the last pytest run in the root (`load_test_record`, kept in
`<state dir>/working_set/tests.json` so it outlives the daemon), running
jobs, and the MCP tools to prefer, at most `BRIEF_MAX_CHARS`.

Standard library only; callers pass in what needs the graph (`key_exists`)
or the shell parser (`command_heads`).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

MAX_FILES = 20
MAX_KEYS = 30
MAX_TESTS = 10
MAX_READ_FILES = 12
MAX_READ_RANGES = 4
MAX_JOBS = 8
# Keys shown on one edited file's line.
KEYS_PER_FILE = 6
TEXT_MAX_CHARS = 7000
BRIEF_MAX_CHARS = 1500
BRIEF_DIRTY_FILES = 15
BRIEF_COMMITS = 3
BRIEF_TESTS = 5
BRIEF_JOBS = 5
GIT_TIMEOUT_S = 3
HEADER = "[ozagia] working set"
BRIEF_HEADER = "[ozagia] session brief"
TEST_RECORD = ("working_set", "tests.json")

MCP_TOOLS_LINE = (
    "Prefer the ozagia MCP tools to reading whole files: dev_symbol / dev_node "
    "(one symbol by key, sym:path::name), dev_outline, dev_impact before "
    "changing a shared module, dev_context to start a task.")
# Added to the brief only when the project enables jobs (`[jobs] enabled`).
JOBS_LINE = (
    "Long commands: job_start, then job_wait (blocks until exit or a log line, "
    "returns only new output) instead of polling write_stdin, sleep or ps. "
    "Jobs run outside the Codex sandbox and approvals, with the ozagia "
    "daemon's privileges.")

# How each harness reads one node by key.
READ_HINTS = {
    "codex": "read a key with the ozagia MCP tool dev_node (key=...), a range "
             "with sed -n 'a,bp' FILE",
    "claude": "Read takes a key as file_path (sym:..., test:...); or dev_node",
    "kiro": "read_file takes a key as path (sym:..., test:...); or dev_node",
    "opencode": "read takes a key as path (sym:..., test:...)",
}
DEFAULT_READ_HINT = "read a key with the ozagia MCP tool dev_node (key=...)"

KEY_PREFIXES = ("sym:", "test:", "doc:", "text:", "json:", "yaml:", "toml:")
# `[ozagia] failed|error test:<rel>::<name>[param]: message` (captured
# pytest and `after`'s pytest summary lines).
_FAIL_RE = re.compile(
    r"^\[ozagia\] (failed|error) (test:\S+?)(\[[^\]]*\])?(?:: (.*))?$")
# pytest's own short summary: `FAILED tests/x.py::T::test_y[p] - message`.
_RAW_FAIL_RE = re.compile(
    r"^(FAILED|ERROR) (\S+?\.py)::(\S+?)(\[[^\]]*\])?(?: - (.*))?$")
# The captured run's head line: `[ozagia] pytest failed: exit 1, ...`.
_CAPTURED_RE = re.compile(r"^\[ozagia\] pytest (\w+): exit (-?\d+|None)")
# A captured run that affected-test selection cut to a subset.
_AFFECTED_RE = re.compile(r"^\[ozagia\] ran \d+ (?:affected )?tests? of \d+",
                          re.M)
# pytest's final summary: `3 failed, 10 passed in 1.2s` (with or without `=`).
_SUMMARY_RE = re.compile(
    r"^\W*(\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|"
    r"warnings?|rerun)(?:, )?)+.* in [\d.]+s\b")
_COUNT_RE = re.compile(r"(\d+) (failed|errors?)\b")
# pytest options that take a value as the next argument.
_VALUE_OPTS = frozenset({
    "-k", "-m", "-p", "-c", "-o", "-n", "-W", "-r", "--tb", "--maxfail",
    "--deselect", "--ignore", "--ignore-glob", "--rootdir", "--confcutdir",
    "--junitxml", "--durations", "--timeout", "--basetemp", "--cov",
    "--cov-report", "--log-level", "--capture", "--import-mode", "--dist",
    "--numprocesses", "--count", "--reruns", "--color"})
# Options that select tests by something other than a path: a test the run
# did not fail may not have been selected.
_FILTER_OPTS = frozenset({"-k", "-m", "--deselect", "--lf", "--last-failed",
                          "--ff", "--failed-first"})
# Options that stop a run early: after a failure, later tests did not run.
_STOP_OPTS = frozenset({"-x", "--exitfirst", "--maxfail", "--sw", "--stepwise"})


def _now() -> float:
    return time.time()


def key_file(key: str) -> str | None:
    """The file a node key is in (`sym:a/b.py::f` -> `a/b.py`), or None."""
    if not isinstance(key, str) or not key.startswith(KEY_PREFIXES):
        return None
    rest = key.split(":", 1)[1]
    rel = rest.split("::", 1)[0].split("#", 1)[0]
    return rel or None


# --------------------------------------------------------------------------- #
# pytest runs
# --------------------------------------------------------------------------- #
@dataclass
class TestRun:
    """One pytest run: its command, the arguments that rerun it, the
    root-relative selectors it ran (empty: everything), and the tests it
    failed (`{key: {"outcome", "params", "message"}}`). `complete`: the run
    reported a summary, so a selected test it did not fail passed, unless
    `filtered` (`-k`, `-m`, `--deselect`, `--lf`: not every test under the
    selectors ran) or `stops` (`-x`, `--maxfail`: after a failure the rest
    did not run)."""
    __test__ = False
    command: str
    prefix: list[str]
    selectors: list[str]
    failures: dict[str, dict[str, Any]]
    complete: bool
    filtered: bool = False
    stops: bool = False
    ts: float = field(default_factory=_now)


def pytest_head(heads: Iterable[list[str]]) -> tuple[list[str], list[str]] | None:
    """`(prefix, args)` of the first pytest command in `heads` (the commands
    a command line runs): `pytest ...` or `python -m pytest ...`."""
    for head in heads:
        if not head:
            continue
        base = os.path.basename(head[0])
        if base in ("pytest", "py.test"):
            return head[:1], head[1:]
        if (re.match(r"^python(?:3(?:\.\d+)?)?$", base)
                and head[1:3] == ["-m", "pytest"]):
            return head[:3], head[3:]
    return None


def _selectors(args: list[str], wd: Path, root: Path
               ) -> tuple[list[str], bool, bool]:
    """The positional arguments of a pytest command made root-relative, and
    whether options filter the selection or stop the run early."""
    out: list[str] = []
    filtered = stops = False
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            if name in _FILTER_OPTS:
                filtered = True
            if name in _STOP_OPTS or (not name.startswith("--") and "x" in name[1:]
                                      and name not in _VALUE_OPTS):
                stops = True
            if name in _VALUE_OPTS and "=" not in a:
                skip = True
            continue
        path, sep, rest = a.partition("::")
        full = os.path.normpath(os.path.join(str(wd), path))
        rel = os.path.relpath(full, str(root)).replace(os.sep, "/")
        if rel.startswith(".."):
            continue
        out.append(rel + (sep + rest if sep else ""))
    return out, filtered, stops


def _rerun_prefix(prefix: list[str], wd: Path, root: Path) -> list[str]:
    """The pytest prefix, with a relative interpreter path made relative to
    the root (the rerun line runs from the root)."""
    p0 = prefix[0]
    if "/" in p0 and not os.path.isabs(p0):
        p0 = os.path.relpath(os.path.normpath(os.path.join(str(wd), p0)), str(root))
    return [p0, *prefix[1:]]


def parse_failures(text: str, wd: str | Path | None = None,
                   root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """The failed tests in `text`, by key: the `[ozagia] failed|error
    test:...` lines, and with `root` also pytest's own `FAILED path::name`
    summary lines (paths relative to `wd`, default the root)."""
    out: dict[str, dict[str, Any]] = {}

    def add(outcome: str, key: str, param: str | None, message: str | None) -> None:
        rec = out.setdefault(key, {"outcome": outcome, "params": [],
                                   "message": (message or "")[:200]})
        if param and param not in rec["params"]:
            rec["params"].append(param)
    for line in (text or "").splitlines():
        s = line.strip()
        m = _FAIL_RE.match(s)
        if m:
            add(m.group(1), m.group(2), m.group(3), m.group(4))
            continue
        m = _RAW_FAIL_RE.match(s) if root is not None else None
        if m:
            base = str(wd or root)
            rel = os.path.relpath(os.path.normpath(os.path.join(base, m.group(2))),
                                  str(root)).replace(os.sep, "/")
            if rel.startswith(".."):
                continue
            add("failed" if m.group(1) == "FAILED" else "error",
                f"test:{rel}::{m.group(3).replace('::', '.')}", m.group(4), m.group(5))
    return out


def _completed(text: str) -> tuple[bool, int]:
    """Whether `text` shows a finished pytest run, and its failed + error
    count (-1 when unknown)."""
    for line in (text or "").splitlines():
        m = _CAPTURED_RE.match(line.strip())
        if m:
            nums = re.findall(r"(\d+) (failed|error)\b", line)
            return True, sum(int(n) for n, _ in nums)
    for line in reversed((text or "").splitlines()[-40:]):
        s = line.strip()
        if _SUMMARY_RE.match(s) or (s.startswith("=") and " in " in s
                                    and _COUNT_RE.search(s)):
            return True, sum(int(n) for n, _ in _COUNT_RE.findall(s))
    return False, -1


def pytest_run(command: str, heads: Iterable[list[str]], text: str, *,
             workdir: str | Path | None, root: str | Path) -> TestRun | None:
    """The pytest run a shell command made, from its command line (`heads`:
    `ozagia.intent.shell.command_heads(command)`) and its text (the
    captured output, or the built-in's output plus the daemon's `append`),
    or None when it ran no pytest."""
    got = pytest_head(heads)
    if got is None:
        return None
    prefix, args = got
    root_p = Path(root)
    wd = Path(workdir) if workdir else root_p
    if not wd.is_absolute():
        wd = root_p / wd
    m = re.match(r"^\s*cd\s+(\S+)\s*&&", command or "")
    if m and "$" not in m.group(1) and "~" not in m.group(1):
        wd = wd / m.group(1).strip("'\"")
    selectors, filtered, stops = _selectors(args, wd, root_p)
    failures = parse_failures(text, wd, root_p)
    complete, count = _completed(text)
    if complete and count > 0 and not failures:
        complete = False        # failures the graph does not know: unknown
    # Affected-test selection (`ozagia.intent.affected`) ran a subset.
    if _AFFECTED_RE.search(text or ""):
        filtered = True
    return TestRun(command=command, prefix=_rerun_prefix(prefix, wd, root_p),
                   selectors=selectors, failures=failures,
                   complete=complete, filtered=filtered, stops=stops)


def _nodeid(key: str) -> tuple[str, str]:
    rest = key[len("test:"):]
    rel, _, name = rest.partition("::")
    return rel, name.replace(".", "::")


def _selected(key: str, selectors: list[str]) -> bool:
    if not selectors:
        return True
    rel, name = _nodeid(key)
    for sel in selectors:
        path, _, rest = sel.partition("::")
        path = path.rstrip("/")
        if path in ("", "."):
            return True
        if rel == path and (not rest or name == rest or name.startswith(rest + "::")):
            return True
        if rel.startswith(path + "/"):
            return True
    return False


def apply_test_run(tests: "OrderedDict[str, dict[str, Any]]", run: TestRun,
                   limit: int = MAX_TESTS) -> bool:
    """Update `tests` (failing tests by key, oldest first) with `run`: its
    failures are added (or refreshed); a test it selected and did not fail
    is removed when the run completed, was not `filtered`, and did not
    stop early (`stops` with failures). Returns whether `tests` changed."""
    before = {k: dict(v) for k, v in tests.items()}
    if run.complete and not run.filtered and not (run.stops and run.failures):
        for key in list(tests):
            if key not in run.failures and _selected(key, run.selectors):
                del tests[key]
    for key, rec in run.failures.items():
        tests.pop(key, None)
        tests[key] = {**rec, "command": run.command, "prefix": list(run.prefix),
                      "ts": run.ts}
    while len(tests) > limit:
        tests.popitem(last=False)
    return before != {k: dict(v) for k, v in tests.items()}


def rerun_command(tests: list[tuple[str, dict[str, Any]]]) -> str | None:
    """One pytest command line that reruns `tests` (from the root), with the
    prefix of the newest one's run."""
    if not tests:
        return None
    prefix = list(tests[-1][1].get("prefix") or ["python", "-m", "pytest"])
    ids: list[str] = []
    for key, rec in tests:
        rel, name = _nodeid(key)
        params = rec.get("params") or []
        nid = f"{rel}::{name}" + (params[0] if len(params) == 1 else "")
        if nid not in ids:
            ids.append(nid)
    return shlex.join([*prefix, "-q", *ids])


# --------------------------------------------------------------------------- #
# the per-session working set
# --------------------------------------------------------------------------- #
@dataclass
class View:
    """A copy of a working set (or a merge of several), newest first."""
    files: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    tests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reads: list[tuple[str, list[tuple[int, int]]]] = field(default_factory=list)
    last_test: dict[str, Any] | None = None
    last_active: float = 0.0

    def empty(self) -> bool:
        return not (self.files or self.keys or self.tests or self.reads)


class WorkingSet:
    """What one session edited, failed and read. Thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.files: OrderedDict[str, float] = OrderedDict()
        self.keys: OrderedDict[str, float] = OrderedDict()
        self.tests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.reads: OrderedDict[str, list[tuple[int, int]]] = OrderedDict()
        self.last_test: dict[str, Any] | None = None
        self.last_active = _now()

    def note_edit(self, files: Iterable[str], keys: Iterable[str]) -> None:
        now = _now()
        with self.lock:
            for rel in files:
                if isinstance(rel, str) and rel:
                    self.files.pop(rel, None)
                    self.files[rel] = now
            for key in keys:
                if isinstance(key, str) and key.startswith(KEY_PREFIXES):
                    self.keys.pop(key, None)
                    self.keys[key] = now
                    rel = key_file(key)
                    if rel and rel not in self.files:
                        self.files[rel] = now
            while len(self.files) > MAX_FILES:
                self.files.popitem(last=False)
            while len(self.keys) > MAX_KEYS:
                self.keys.popitem(last=False)
            self.last_active = now

    def note_read(self, rel: str, a: int, b: int) -> None:
        with self.lock:
            spans = self.reads.pop(rel, [])
            spans.append((a, b))
            spans.sort()
            merged: list[tuple[int, int]] = []
            for x, y in spans:
                if merged and x <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], y))
                else:
                    merged.append((x, y))
            if len(merged) > MAX_READ_RANGES:
                # Keep the ranges around the newest read.
                merged.sort(key=lambda s: (not (s[0] <= a and b <= s[1]),
                                           abs(s[0] - a)))
                merged = sorted(merged[:MAX_READ_RANGES])
            self.reads[rel] = merged
            while len(self.reads) > MAX_READ_FILES:
                self.reads.popitem(last=False)
            self.last_active = _now()

    def note_tests(self, run: TestRun) -> None:
        with self.lock:
            apply_test_run(self.tests, run)
            self.last_test = {"command": run.command, "ts": run.ts,
                              "failed": len(run.failures),
                              "complete": run.complete}
            self.last_active = _now()

    def view(self) -> View:
        with self.lock:
            return View(files=list(reversed(self.files)),
                        keys=list(reversed(self.keys)),
                        tests=[(k, dict(v)) for k, v in self.tests.items()],
                        reads=[(r, list(s)) for r, s in reversed(self.reads.items())],
                        last_test=dict(self.last_test) if self.last_test else None,
                        last_active=self.last_active)

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {"files": len(self.files), "keys": len(self.keys),
                    "tests": len(self.tests), "reads": len(self.reads)}


def merge_views(views: list[View]) -> View:
    """One view of several (a session's subagents), the most recently
    active first; duplicates keep their first place."""
    views = sorted(views, key=lambda v: -v.last_active)
    out = View()
    for v in views:
        out.files.extend(f for f in v.files if f not in out.files)
        out.keys.extend(k for k in v.keys if k not in out.keys)
        have = {k for k, _ in out.tests}
        out.tests.extend((k, r) for k, r in v.tests if k not in have)
        seen = {r for r, _ in out.reads}
        out.reads.extend((r, s) for r, s in v.reads if r not in seen)
        if out.last_test is None and v.last_test:
            out.last_test = v.last_test
        out.last_active = max(out.last_active, v.last_active)
    out.files, out.keys = out.files[:MAX_FILES], out.keys[:MAX_KEYS]
    out.tests = sorted(out.tests, key=lambda t: t[1].get("ts", 0))[-MAX_TESTS:]
    out.reads = out.reads[:MAX_READ_FILES]
    return out


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _clock(ts: Any) -> str:
    try:
        return time.strftime("%H:%M:%S UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "?"


def _short(text: Any, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[:limit - 3] + "..."


def failing_lines(tests: list[tuple[str, dict[str, Any]]], limit: int = MAX_TESTS
               ) -> list[str]:
    """`  failed test:...: message` lines, newest last, and the rerun line."""
    shown = tests[-limit:]
    out = []
    for key, rec in shown:
        params = rec.get("params") or []
        p = params[0] if len(params) == 1 else (f"[{len(params)} params]" if params else "")
        msg = f": {_short(rec.get('message'), 140)}" if rec.get("message") else ""
        out.append(f"  {rec.get('outcome', 'failed')} {key}{p}{msg}")
    rerun = rerun_command(shown)
    if rerun:
        out.append(f"  rerun (from the repo root): {rerun}")
    return out


def job_lines(jobs: list[dict[str, Any]], session: str | None = None,
              limit: int = MAX_JOBS) -> list[str]:
    """One line per running job: id, command, age, and the call that
    waits for it. The session's own jobs first."""
    mine = [j for j in jobs if session and j.get("session") == session]
    rest = [j for j in jobs if j not in mine]
    out = []
    for j in (mine + rest)[:limit]:
        el = j.get("elapsed_s")
        age = f", {int(el)} s" if isinstance(el, (int, float)) else ""
        whose = "" if j in mine or not session else " (another session)"
        jid = j.get("id") or j.get("job")
        out.append(f"  {jid} `{_short(j.get('command'), 80)}` {j.get('status')}"
                   f"{age}{whose}: job_wait job={jid} (or job_log)")
    return out


def _fit(sections: list[tuple[str, list[str]]], head: list[str],
         max_chars: int) -> str:
    """`head`, then each section's title and lines while they fit in
    `max_chars`; a section cut short ends with `  ... N more`."""
    lines = list(head)
    size = sum(len(x) + 1 for x in lines)
    for title, body in sections:
        if not body:
            continue
        if size + len(title) + 1 + len(body[0]) + 1 > max_chars:
            continue
        lines.append(title)
        size += len(title) + 1
        for i, line in enumerate(body):
            more = f"  ... {len(body) - i} more"
            # Leave room for the `more` line unless this is the last one.
            room = 0 if i == len(body) - 1 else len(more) + 1
            if size + len(line) + 1 + room > max_chars:
                if size + len(more) + 1 <= max_chars:
                    lines.append(more)
                    size += len(more) + 1
                break
            lines.append(line)
            size += len(line) + 1
    return "\n".join(lines)[:max_chars]


def working_set_text(view: View, *, harness: str | None = None,
                     session: str | None = None, compactions: int = 0,
                     jobs: list[dict[str, Any]] | None = None,
                     plan: str | None = None,
                     key_exists: Callable[[str], bool] | None = None,
                     max_chars: int = TEXT_MAX_CHARS) -> str:
    """The working set as text for the model, or "" when there is nothing
    to give back (no edits, tests, reads, jobs or plan)."""
    keys = [k for k in view.keys if key_exists is None or _exists(key_exists, k)]
    by_file: dict[str, list[str]] = {}
    for k in keys:
        by_file.setdefault(key_file(k) or "", []).append(k)
    files = list(view.files)
    for rel in by_file:
        if rel and rel not in files:
            files.append(rel)
    edited = []
    for rel in files[:MAX_FILES]:
        ks = by_file.get(rel, [])
        shown = ", ".join(ks[:KEYS_PER_FILE]) + (
            f" (+{len(ks) - KEYS_PER_FILE})" if len(ks) > KEYS_PER_FILE else "")
        edited.append(f"  {rel}" + (f": {shown}" if shown else ""))
    reads = [f"  {rel} lines " + ", ".join(f"{a}-{b}" for a, b in spans)
             for rel, spans in view.reads[:MAX_READ_FILES]]
    jl = job_lines(jobs or [], session)
    tl = failing_lines(view.tests)
    if not (edited or reads or jl or tl or plan):
        return ""
    who = " ".join(x for x in (harness, f"session {_short(session, 40)}"
                               if session else None) if x)
    head = [f"{HEADER}" + (f" ({who})" if who else "")
            + ": what this session edited, ran and read before the context was "
            "compacted"
            + (f" ({compactions} compaction(s) so far)" if compactions else "")
            + ". Re-read only what the next step needs.",
            f"How to read: {READ_HINTS.get(harness or '', DEFAULT_READ_HINT)}."]
    lt = view.last_test
    test_title = "failing tests" + (
        f" (last pytest run {_clock(lt.get('ts'))}: `{_short(lt.get('command'), 100)}`)"
        if lt else "") + ":"
    sections = [
        (test_title, tl),
        ("running jobs:", jl),
        ("plan:", [f"  {plan}"] if plan else []),
        ("edited (most recent first; read a key instead of the whole file):", edited),
        ("recently read (re-read a range only if the summary lacks it):", reads),
    ]
    return _fit(sections, head, max_chars)


def _exists(check: Callable[[str], bool], key: str) -> bool:
    try:
        return bool(check(key))
    except Exception:  # noqa: BLE001 - keep the key when unsure
        return True


def text_meta(view: View, text: str) -> dict[str, Any]:
    """Counts for a reply's `meta` (`keys` there is the list of keys)."""
    return {"files": len(view.files), "edited_keys": len(view.keys),
            "tests": len(view.tests), "reads": len(view.reads),
            "chars": len(text)}


# --------------------------------------------------------------------------- #
# the root's last test results (for the next session's brief)
# --------------------------------------------------------------------------- #
def _record_path(root: str | Path) -> Path:
    from ozagia.config import state_path
    return state_path(root, *TEST_RECORD)


def load_test_record(root: str | Path) -> dict[str, Any]:
    """`{"tests": {key: rec}, "last": {...}}` from the state dir; empty
    when missing or unreadable."""
    try:
        data = json.loads(_record_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tests": {}, "last": None}
    if not isinstance(data, dict) or not isinstance(data.get("tests"), dict):
        return {"tests": {}, "last": None}
    return {"tests": data["tests"], "last": data.get("last")}


def save_test_record(root: str | Path, record: dict[str, Any]) -> None:
    path = _record_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="tests.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


class TestRecord:
    """The root's failing tests across sessions, persisted on change."""
    __test__ = False

    def __init__(self, root: str | Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        rec = load_test_record(root)
        self.tests: OrderedDict[str, dict[str, Any]] = OrderedDict(
            sorted(rec["tests"].items(), key=lambda kv: kv[1].get("ts", 0)
                   if isinstance(kv[1], dict) else 0))
        self.last = rec["last"]

    def apply(self, run: TestRun) -> None:
        with self.lock:
            changed = apply_test_run(self.tests, run)
            last = {"command": run.command, "ts": run.ts,
                    "failed": len(run.failures), "complete": run.complete}
            if changed or self.last is None or self.last.get("failed") != last["failed"]:
                self.last = last
                save_test_record(self.root, {"tests": dict(self.tests),
                                             "last": self.last})
            else:
                self.last = last

    def view(self) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any] | None]:
        with self.lock:
            return [(k, dict(v)) for k, v in self.tests.items()], (
                dict(self.last) if self.last else None)


# --------------------------------------------------------------------------- #
# DevSession plan step
# --------------------------------------------------------------------------- #
def devsession_line(root: str | Path, scan: int = 20) -> str | None:
    """The current step of the newest open `DevSession` in the root (best
    effort; None when there is none or it cannot be read)."""
    try:
        from ozagia.core.session import SESSIONS_DIR, load_session
        d = Path(root) / SESSIONS_DIR
        if not d.is_dir():
            return None
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:scan]
        for path in files:
            try:
                s = load_session(path.stem, root=root)
            except Exception:  # noqa: BLE001 - skip unreadable sessions
                continue
            st = s.state()
            if st.get("status") != "open":
                continue
            summ = st.get("summary") or {}
            plan = summ.get("plan") or {}
            steps = plan.get("steps") or []
            done = summ.get("steps") or []
            cur = done[-1] if done else None
            part = f"DevSession {st['session_id']} \"{_short(st.get('task'), 80)}\""
            if cur:
                part += (f": step {cur.get('plan_index') or len(done)}/{len(steps) or '?'}"
                         f" \"{_short(cur.get('title'), 80)}\" ({cur.get('status')})")
            elif steps:
                part += f": plan of {len(steps)} steps, none started"
            nxt = len(done) if cur and cur.get("status") == "passed" else None
            if nxt is not None and nxt < len(steps):
                t = steps[nxt].get("title") if isinstance(steps[nxt], dict) else steps[nxt]
                part += f"; next \"{_short(t, 80)}\""
            return part + "; dev_session_next gives the next action"
    except Exception:  # noqa: BLE001 - best effort
        return None
    return None


# --------------------------------------------------------------------------- #
# the session-start brief ("since the last session")
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def session_brief(root: str | Path, max_chars: int = BRIEF_MAX_CHARS, *,
                  tests: list[tuple[str, dict[str, Any]]] | None = None,
                  last_test: dict[str, Any] | None = None,
                  jobs: list[dict[str, Any]] | None = None,
                  jobs_enabled: bool | None = None) -> str:
    """Dirty files, the last commits, the tests failing at the last pytest
    run in the root, running jobs, and the MCP tools to prefer. `tests`
    defaults to the persisted record (`load_test_record`)."""
    root = Path(root)
    lines = [BRIEF_HEADER]
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    if status is not None:
        dirty = [x for x in status.splitlines() if x.strip()]
        head = f"branch {branch.strip()}; " if branch else ""
        if dirty:
            shown = dirty[:BRIEF_DIRTY_FILES]
            more = len(dirty) - len(shown)
            lines.append(f"{head}{len(dirty)} changed file(s):")
            lines.extend(f"  {x}" for x in shown)
            if more:
                lines.append(f"  ... and {more} more")
        else:
            lines.append(f"{head}working tree clean")
    log = _git(root, "log", f"-{BRIEF_COMMITS}", "--format=%h %s")
    if log and log.strip():
        lines.append("recent commits:")
        lines.extend(f"  {x[:120]}" for x in log.splitlines() if x.strip())
    if tests is None:
        rec = load_test_record(root)
        tests = sorted(((k, v) for k, v in rec["tests"].items() if isinstance(v, dict)),
                       key=lambda kv: kv[1].get("ts", 0))
        last_test = last_test or rec["last"]
    if tests:
        when = f" ({_clock(last_test.get('ts'))})" if last_test else ""
        lines.append(f"failing at the last pytest run{when}:")
        lines.extend(failing_lines(tests, BRIEF_TESTS))
    if jobs:
        lines.append("running jobs:")
        lines.extend(job_lines(jobs, None, BRIEF_JOBS))
    if jobs_enabled is None:
        from ozagia.daemon.jobs import jobs_enabled as _enabled
        jobs_enabled = _enabled(root)
    tools = MCP_TOOLS_LINE + (" " + JOBS_LINE if jobs_enabled else "")
    lines.append(tools)
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # Keep the tools line; cut the middle.
    keep = max_chars - len(tools) - len("\n...\n")
    return text[:max(keep, 0)].rstrip() + "\n...\n" + tools


__all__ = ["BRIEF_HEADER", "BRIEF_MAX_CHARS", "HEADER", "JOBS_LINE", "MAX_FILES",
           "MAX_JOBS", "MAX_KEYS", "MAX_READ_FILES", "MAX_READ_RANGES",
           "MAX_TESTS", "MCP_TOOLS_LINE", "TEXT_MAX_CHARS", "TestRecord",
           "TestRun", "View", "WorkingSet", "apply_test_run", "devsession_line",
           "job_lines", "key_file", "load_test_record", "merge_views",
           "failing_lines", "parse_failures", "pytest_head", "rerun_command",
           "session_brief", "pytest_run", "text_meta", "working_set_text"]
