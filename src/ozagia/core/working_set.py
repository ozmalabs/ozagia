"""The working set of an agent session, and the text that gives it back.

After a compaction the model keeps a summary and loses the files, keys and
test results it was working with; it then reads them again. The daemon
keeps, per session (`ozagia.intent.ledger.SessionState.work`, keyed like the
read ledger by (harness, session, sub)), what the session was working on,
and the compaction hooks hand it back as one bounded block of text:

- files and node keys it edited: handled `edit`/`write`/key edits,
  `after` refreshes of built-in edits, writes and patches (the files the
  call changed, `meta.edited_files`) and of shell commands that changed
  files (`meta.changed_paths`: changed files written in the last minute);
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
the branch, dirty files and recent commits from git, the tests still
failing in the root's record (`load_test_record`, kept in
`<state dir>/working_set/tests.json` so it outlives the daemon; it holds
test ids, outcomes, times and the rerun prefix, never failure messages or
command lines), each with the time of the first run that failed it,
running jobs, linked reference checkout snapshot and pin status, and the MCP
tools to prefer, at most `BRIEF_MAX_CHARS`. When the dirty files name an
external-tool capability (`.pdf`, `.png`, office/media files, ...), a small
`[ozagia] tools:` block from `ozagia.core.toolchain` says what is installed,
what is missing and what installing would involve; it runs no version or sudo
probes and never installs. `[tools] brief = false` turns it off. Reference
status is bounded; it warns when a linked index is missing, stale, unknown, or
awaiting reindex after a sync. Daemon session briefs request a bounded
background update; hook fallbacks only report status.
The brief, and the working set text the daemon renders, also list the
open and claimed goals with unmet criteria (`ozagia.core.goals`).

The test record also keeps the last `MAX_RECORD_RUNS` runs (start time,
the selectors they ran as paths or node ids, the test keys they failed and
those that errored, whether they completed, were filtered or stopped
early, their `source`, for a pytest the daemon ran its failure class
and module and the classes of its tests with an environment cause
(`ozagia.core.failures`; no output text), and for a pytest
the daemon ran itself the working
tree's fingerprint at its start), so `recorded_outcome` can say when a
test last passed or failed (goal criteria `test_passes`/`test_fails`, which
take only `captured` runs as evidence). Per-run lists are capped
(`MAX_RUN_KEYS`); a selector that is not kept makes the run filtered.

A pytest run clears a failing test only when nothing is in doubt: it
finished (a captured run with status `passed`/`failed`, no collection
errors, no `[ozagia] error:`; a raw run with a summary), its summary
counts no more failures and errors than the text names, it selected the
test (no positional argument: the workdir), and no option filtered the
selection (`-k`, `-m`, also with the value attached; `--ignore`,
`--ignore-glob`, `--deselect`, `-o`/`-c` config overrides, and the same in
`PYTEST_ADDOPTS` or the project's `addopts`) or stopped it early, and none
made pytest run no test (`--version`, `--help`, `--co`, `--fixtures`,
`--setup-plan`, ...).

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
# The tools block's lines, including its `[ozagia] tools:` header.
BRIEF_TOOL_LINES = 4
# The goal lines' reserved share of the brief: at most 1/N of its cap.
BRIEF_GOAL_SHARE = 3
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
_CAPTURED_RE = re.compile(r"^\[ozagia\] pytest (\w+)(?: \([^)]*\))?: exit (-?\d+|None)")
# The class line after a captured run's head (`ozagia.intent.run.failure_line`).
_CLASS_RE = re.compile(
    r"^\[ozagia\] failure class (\w+)(?:, module ([A-Za-z_][\w.]{0,99}))?(?::|$)")
# A failing test's own class when the run's differs (`failure_lines`).
_ITEM_RE = re.compile(
    r"^\[ozagia\] failure item (\w+) ((?:test|file):\S{1,300}?)"
    r"(?:, module ([A-Za-z_][\w.]{0,99}))?$")
# A captured run's own error (no report, a crash of the runner).
ERROR_TAG = "[ozagia] error:"
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
    "--numprocesses", "--count", "--reruns", "--color", "--config-file",
    "--override-ini"})
# Options that select tests by something other than a path, or change what
# the paths mean (`--ignore`, a config override, `--pyargs`): a test the run
# did not fail may not have been selected.
_FILTER_OPTS = frozenset({"-k", "-m", "--deselect", "--lf", "--last-failed",
                          "--ff", "--failed-first", "--ignore", "--ignore-glob",
                          "-o", "--override-ini", "-c", "--config-file",
                          "--pyargs", "--rootdir", "--confcutdir",
                          "--sw", "--stepwise", "--sw-skip", "--stepwise-skip"})
# Options that stop a run early: after a failure, later tests did not run.
_STOP_OPTS = frozenset({"-x", "--exitfirst", "--maxfail", "--sw", "--stepwise"})
# Options with which pytest runs no test at all (it reports, lists or plans).
_INFO_OPTS = frozenset({"--version", "-V", "--help", "-h", "--co",
                        "--collect-only", "--collectonly", "--fixtures",
                        "--funcargs", "--fixtures-per-test", "--setup-plan",
                        "--setup-only", "--markers"})
# Commands besides pytest itself that may share a captured pytest's command
# line (`cd DIR && pytest`, `pytest | tail -n 30`).
_HARMLESS_HEADS = frozenset({"cd", "tail", "head"})


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
    did not run). `source`: `captured` when the daemon ran this pytest
    itself (a `shell_pytest` answer: the only runs that count as evidence
    for goal criteria), `compound` (a captured compound command with a
    pytest step), else `reported` (output a harness handed over). `tree`:
    the working tree's fingerprint at the start of a captured run
    (`ozagia.core.fingerprint`), and then `ts` is the start time.
    `failure_class` (captured runs only; never compound ones, whose other
    commands may print a head): the class the daemon's `[ozagia] failure
    class` line names (`ozagia.core.failures`), or `passed` for a head with
    status passed and no such line; None when unknown. `failure_module`:
    the module that line names. `failure_items`: the failing tests whose own
    class differs from the run's (`[ozagia] failure item` lines), as
    `{"key", "class", "module"?}`."""
    __test__ = False
    command: str
    prefix: list[str]
    selectors: list[str]
    failures: dict[str, dict[str, Any]]
    complete: bool
    filtered: bool = False
    stops: bool = False
    ts: float = field(default_factory=_now)
    source: str = "reported"
    tree: dict[str, Any] | None = None
    failure_class: str | None = None
    failure_module: str | None = None
    failure_items: list[dict[str, Any]] | None = None


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


def _short_opts(a: str) -> tuple[set[str], bool]:
    """The short options in a `-abc` cluster, and whether the last one
    takes its value from the next argument. A value option (`-k`, `-m`,
    `-p`, ...) ends the cluster: the rest is its attached value
    (`-ktest_x`, `-mslow`, `-pno:xdist`, `-k=foo`)."""
    names: set[str] = set()
    for i, c in enumerate(a[1:], start=1):
        name = "-" + c
        names.add(name)
        if name in _VALUE_OPTS:
            return names, i == len(a) - 1
    return names, False


def _rel_to_root(path: str | Path, root: Path) -> str | None:
    rel = os.path.relpath(os.path.normpath(str(path)), str(root)).replace(os.sep, "/")
    return None if rel == ".." or rel.startswith("../") else rel


def _scan_opts(args: list[str]) -> tuple[bool, bool, bool, int]:
    """Whether `args` hold options that filter the selection, stop the run
    early, or make pytest run no test (`--version`, `--co`, ...), and the
    number of positional arguments."""
    filtered = stops = info = False
    positional = 0
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            filtered = filtered or name in _FILTER_OPTS
            stops = stops or name in _STOP_OPTS
            info = info or name in _INFO_OPTS
            skip = name in _VALUE_OPTS and "=" not in a
            continue
        if a.startswith("-") and len(a) > 1:
            names, skip = _short_opts(a)
            filtered = filtered or bool(names & _FILTER_OPTS)
            stops = stops or bool(names & _STOP_OPTS)
            info = info or bool(names & _INFO_OPTS)
            continue
        positional += 1
    return filtered, stops, info, positional


def _selectors(args: list[str], wd: Path, root: Path
               ) -> tuple[list[str] | None, bool, bool]:
    """The positional arguments of a pytest command made root-relative
    (with none, the workdir: pytest collects from there), and whether
    options filter the selection or stop the run early. None for the
    selectors when what the run selected is outside the root, or cannot be
    told (then the run clears nothing)."""
    out: list[str] = []
    positional = 0
    filtered = stops = False
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            filtered = filtered or name in _FILTER_OPTS
            stops = stops or name in _STOP_OPTS
            skip = name in _VALUE_OPTS and "=" not in a
            continue
        if a.startswith("-") and len(a) > 1:
            names, skip = _short_opts(a)
            filtered = filtered or bool(names & _FILTER_OPTS)
            stops = stops or bool(names & _STOP_OPTS)
            continue
        positional += 1
        path, sep, rest = a.partition("::")
        rel = _rel_to_root(os.path.join(str(wd), path), root)
        if rel is None:
            continue
        out.append(rel + (sep + rest if sep else ""))
    if positional:
        return (out or None), filtered, stops
    rel = _rel_to_root(wd, root)
    if rel is None:
        return None, filtered, stops
    return ([] if rel == "." else [rel]), filtered, stops


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
    """Whether `text` shows a pytest run that finished (every selected test
    ran and has a result), and its failed + error count (-1 when unknown).
    A captured run (`[ozagia] pytest <status>: exit N, ...`) finished only
    with status `passed` or `failed`, no collection errors and no
    `[ozagia] error:` line; a timeout, an interruption, a usage or internal
    error did not."""
    lines = (text or "").splitlines()
    for line in lines:
        m = _CAPTURED_RE.match(line.strip())
        if m:
            if (m.group(1) not in ("passed", "failed") or "collection error" in line
                    or any(x.strip().startswith(ERROR_TAG) for x in lines)):
                return False, -1
            nums = re.findall(r"(\d+) (failed|errors?)\b", line)
            return True, sum(int(n) for n, _ in nums)
    for line in reversed(lines[-40:]):
        s = line.strip()
        if _SUMMARY_RE.match(s) or (s.startswith("=") and " in " in s
                                    and _COUNT_RE.search(s)):
            return True, sum(int(n) for n, _ in _COUNT_RE.findall(s))
    return False, -1


def _failure_count(failures: dict[str, dict[str, Any]]) -> int:
    """The failed and errored tests in `failures`, each parameter apart."""
    return sum(max(1, len(rec.get("params") or [])) for rec in failures.values())


def _config_addopts(root: Path) -> list[str]:
    """The `addopts` of the project's pytest configuration files at the
    root (`pytest.ini`, `.pytest.ini`, `tox.ini`, `setup.cfg`,
    `pyproject.toml`), as tokens; [] when there are none."""
    import configparser
    out: list[str] = []
    for name, section in (("pytest.ini", "pytest"), (".pytest.ini", "pytest"),
                          ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        p = root / name
        if not p.is_file():
            continue
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(p, encoding="utf-8")
        out.extend(shlex.split(cp.get(section, "addopts", fallback="")))
    p = root / "pyproject.toml"
    if p.is_file():
        import tomllib
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
        pt = tool.get("pytest") if isinstance(tool.get("pytest"), dict) else {}
        for table in (pt.get("ini_options"), pt):
            value = table.get("addopts") if isinstance(table, dict) else None
            if isinstance(value, str):
                out.extend(shlex.split(value))
            elif isinstance(value, list):
                out.extend(str(v) for v in value)
    return out


# A captured run's head line (`ozagia.intent.run.run_pytest`), with or
# without the affected-test scope (`pytest passed (affected ...): exit 0`).
_CAPTURED_HEAD_RE = re.compile(r"^\[ozagia\] pytest \w+(?: \([^)]*\))?: exit ")


def pytest_run(command: str, heads: Iterable[list[str]], text: str, *,
               workdir: str | Path | None, root: str | Path,
               source: str = "reported", env: dict[str, str] | None = None
               ) -> TestRun | None:
    """The pytest run a shell command made, from its command line (`heads`:
    `ozagia.intent.shell.command_heads(command)`) and its text (the
    captured output, or the built-in's output plus the daemon's `append`),
    or None when it ran no pytest. Every doubt makes the run incomplete
    (it then clears no failing test): a run that did not finish, a summary
    with more failures and errors than the text names, a selection outside
    the root, an option with which pytest runs no test (`--version`,
    `--co`, `--fixtures`, ...).

    `source` (see `TestRun`): for `captured`, only the text from the
    daemon's own (last) head line on is read, so what pytest printed
    before it, a test's own output included, cannot forge a result; a
    command line with commands other than pytest, `cd`, `tail` and `head`
    makes it `compound`. Options in `PYTEST_ADDOPTS` (on the command line
    or in `env`) or in the project's `addopts` that filter the selection,
    or positional arguments there, mark the run filtered."""
    heads = [list(h) for h in heads if h]
    got = pytest_head(heads)
    if got is None:
        return None
    prefix, args = got
    pytest_heads = [h for h in heads if pytest_head([h]) is not None]
    others = [h for h in heads if h not in pytest_heads
              and os.path.basename(h[0]) not in _HARMLESS_HEADS]
    if source == "captured" and (others or len(pytest_heads) > 1):
        source = "compound"
    root_p = Path(root)
    wd = Path(workdir) if workdir else root_p
    if not wd.is_absolute():
        wd = root_p / wd
    m = re.match(r"^\s*cd\s+(\S+)\s*&&", command or "")
    if m and "$" not in m.group(1) and "~" not in m.group(1):
        wd = wd / m.group(1).strip("'\"")
    selectors, filtered, stops = _selectors(args, wd, root_p)
    info = _scan_opts(args)[2]
    extra: list[str] = []
    if "PYTEST_ADDOPTS" in (command or ""):
        filtered = True
    try:
        if env and env.get("PYTEST_ADDOPTS"):
            extra.extend(shlex.split(env["PYTEST_ADDOPTS"]))
        extra.extend(_config_addopts(root_p))
    except Exception:  # noqa: BLE001 - options that cannot be read may filter
        filtered = True
    if extra:
        e_filtered, e_stops, e_info, e_positional = _scan_opts(extra)
        filtered = filtered or e_filtered or e_positional > 0
        stops = stops or e_stops
        info = info or e_info
    failure_class = failure_module = failure_items = None
    if source == "captured":
        lines = (text or "").splitlines()
        at = max((i for i, x in enumerate(lines)
                  if _CAPTURED_HEAD_RE.match(x.strip())), default=None)
        text = "\n".join(lines[at:]) if at is not None else ""
        # The text now starts at the daemon's own head, the last one.
        failure_class, failure_module, failure_items = _failure_class(text)
    failures = parse_failures(text, wd, root_p)
    complete, count = _completed(text)
    if complete and count > _failure_count(failures):
        complete = False        # failures the text does not name: unknown
    if info:
        complete = False        # pytest ran no test
    if selectors is None:
        complete, selectors = False, []
    # Affected-test selection (`ozagia.intent.affected`) ran a subset.
    if _AFFECTED_RE.search(text or ""):
        filtered = True
    return TestRun(command=command, prefix=_rerun_prefix(prefix, wd, root_p),
                   selectors=selectors, failures=failures,
                   complete=complete, filtered=filtered, stops=stops,
                   source=source, failure_class=failure_class,
                   failure_module=failure_module, failure_items=failure_items)


def _failure_class(text: str) -> tuple[str | None, str | None,
                                         list[dict[str, Any]] | None]:
    """The failure class, module and items of the daemon's (last) captured
    pytest in `text`: the `[ozagia] failure class` line and the `[ozagia]
    failure item` lines in the run of `[ozagia]` lines right after its head
    line, `passed` for a head with status passed and no class line, else
    (None, None, None). At most `MAX_RUN_KEYS` items."""
    from ozagia.core.failures import CLASSES
    lines = [x.strip() for x in (text or "").splitlines()]
    at = max((i for i, x in enumerate(lines) if _CAPTURED_HEAD_RE.match(x)),
             default=None)
    if at is None:
        return None, None, None
    cls = module = None
    items: list[dict[str, Any]] = []
    for x in lines[at + 1:]:
        if not x.startswith("[ozagia] ") or _CAPTURED_HEAD_RE.match(x):
            break
        m = _CLASS_RE.match(x)
        if m and m.group(1) in CLASSES and cls is None:
            cls, module = m.group(1), m.group(2)
            continue
        m = _ITEM_RE.match(x)
        if m and m.group(1) in CLASSES and len(items) < MAX_RUN_KEYS:
            item = {"key": m.group(2), "class": m.group(1)}
            if m.group(3):
                item["module"] = m.group(3)
            items.append(item)
    if cls is not None:
        return cls, module, items or None
    head = _CAPTURED_RE.match(lines[at])
    if head and head.group(1) == "passed":
        return "passed", None, None
    return None, None, None


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
    failures are added (or refreshed; `since` keeps the time of the first
    run that failed the test, `ts` is the last); a test it selected and did
    not fail is removed when the run completed, was not `filtered`, and did
    not stop early (`stops` with failures). Returns whether `tests`
    changed."""
    before = {k: dict(v) for k, v in tests.items()}
    if run.complete and not run.filtered and not (run.stops and run.failures):
        for key in list(tests):
            if key not in run.failures and _selected(key, run.selectors):
                del tests[key]
    for key, rec in run.failures.items():
        old = tests.pop(key, None) or {}
        since = old.get("since", old.get("ts", run.ts))
        tests[key] = {**rec, "prefix": list(run.prefix), "ts": run.ts,
                      "since": since}
    while len(tests) > limit:
        tests.popitem(last=False)
    return before != {k: dict(v) for k, v in tests.items()}


def safe_prefix(prefix: Any) -> list[str]:
    """A rerun prefix reduced to the interpreter and `-m pytest` (or the
    `pytest` executable): nothing else of a command line is kept."""
    if isinstance(prefix, list) and all(isinstance(x, str) for x in prefix):
        if len(prefix) == 3 and prefix[1:] == ["-m", "pytest"]:
            return list(prefix)
        if len(prefix) == 1 and os.path.basename(prefix[0]) in ("pytest", "py.test"):
            return list(prefix)
    return ["python", "-m", "pytest"]


def rerun_command(tests: list[tuple[str, dict[str, Any]]]) -> str | None:
    """One pytest command line that reruns `tests` (from the root), with the
    prefix of the newest one's run."""
    if not tests:
        return None
    prefix = safe_prefix(tests[-1][1].get("prefix"))
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
    """`ts` as a UTC date and time (`2025-01-02 14:02 UTC`)."""
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "?"


def _short(text: Any, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= limit else s[:limit - 3] + "..."


def failing_lines(tests: list[tuple[str, dict[str, Any]]], limit: int = MAX_TESTS,
                  *, messages: bool = True) -> list[str]:
    """`  failed test:... (failing since <date time>): message` lines,
    newest last, and the rerun line. `since` is the first recorded run
    that failed the test (else its `ts`); `messages=False` leaves out the
    failure messages (free text from the test output)."""
    shown = tests[-limit:]
    out = []
    for key, rec in shown:
        params = rec.get("params") or []
        p = params[0] if len(params) == 1 else (f"[{len(params)} params]" if params else "")
        since = rec.get("since", rec.get("ts"))
        when = f" (failing since {_clock(since)})" if since is not None else ""
        msg = (f": {_short(rec.get('message'), 140)}"
               if messages and rec.get("message") else "")
        out.append(f"  {rec.get('outcome', 'failed')} {key}{p}{when}{msg}")
    rerun = rerun_command(shown)
    if rerun:
        out.append(f"  rerun (from the repo root): {rerun}")
    return out


def job_lines(jobs: list[dict[str, Any]], session: str | None = None,
              limit: int = MAX_JOBS, *, commands: bool = True) -> list[str]:
    """One line per running job: id, command, age, and the call that
    waits for it. The session's own jobs first. `commands=False` leaves
    out the command lines."""
    mine = [j for j in jobs if session and j.get("session") == session]
    rest = [j for j in jobs if j not in mine]
    out = []
    for j in (mine + rest)[:limit]:
        el = j.get("elapsed_s")
        age = f", {int(el)} s" if isinstance(el, (int, float)) else ""
        whose = "" if j in mine or not session else " (another session)"
        jid = j.get("id") or j.get("job")
        cmd = f" `{_short(j.get('command'), 80)}`" if commands else ""
        out.append(f"  {jid}{cmd} {j.get('status')}"
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
                     max_chars: int = TEXT_MAX_CHARS,
                     free_text: bool = True,
                     goal_lines: list[str] | None = None) -> str:
    """The working set as text for the model, or "" when there is nothing
    to give back (no edits, tests, reads, jobs or plan). `free_text=False`
    keeps only paths, keys, test ids and job ids: no failure messages, no
    command lines (pass a `plan` built the same way). `goal_lines`
    (`ozagia.core.goals.brief_lines`: a title line, then one line per goal)
    go after the plan; they alone do not make the text non-empty."""
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
    jl = job_lines(jobs or [], session, commands=free_text)
    tl = failing_lines(view.tests, messages=free_text)
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
    cmd = (f": `{_short(lt.get('command'), 100)}`"
           if lt and free_text and lt.get("command") else "")
    test_title = "failing tests" + (
        f" (last pytest run {_clock(lt.get('ts'))}{cmd})" if lt else "") + ":"
    sections = [
        (test_title, tl),
        ("running jobs:", jl),
        ("plan:", [f"  {plan}"] if plan else []),
        (goal_lines[0] if goal_lines else "goals:", list(goal_lines or [])[1:]),
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


# What a persisted test record keeps: no failure messages, no command lines
# (they go to later sessions, possibly of another harness).
_RECORD_TEST_FIELDS = ("outcome", "params", "ts", "since")
_RECORD_LAST_FIELDS = ("ts", "failed", "complete")
# Recent runs kept in the record (newest last), for `recorded_outcome`: when a
# test last ran, and whether it passed (`ozagia.core.goals`).
MAX_RECORD_RUNS = 50
# Per run: selectors, failed and errored test keys kept. A run with more
# selectors is kept as filtered (it decides no pass); one with more failed
# or errored tests as incomplete (a dropped failure must not read as a pass).
MAX_RUN_KEYS = 200
MAX_RUN_KEY_CHARS = 300
# A run's selector is kept only when it looks like a path or node id.
_SAFE_SELECTOR = re.compile(r"[A-Za-z0-9_./@+\-\[\]:]{1,300}")
RUN_SOURCES = ("captured", "compound", "reported")
# A run's failure module is kept only when it is a dotted module name.
_MODULE_NAME = re.compile(r"[A-Za-z_][\w.]{0,99}")


def _run_keys(value: Any) -> tuple[list[str], bool]:
    """Test keys of a run's `failed`/`errors` list, sorted and capped, and
    whether any was dropped."""
    if not isinstance(value, list):
        return [], value is not None
    keys = sorted({k for k in value if isinstance(k, str) and k.startswith("test:")
                   and len(k) <= MAX_RUN_KEY_CHARS})
    dropped = len(keys) != len(set(map(str, value))) or len(keys) > MAX_RUN_KEYS
    return keys[:MAX_RUN_KEYS], dropped


def _record_run(run: Any) -> dict[str, Any] | None:
    """A run as the record keeps it: its time (the start, for a captured
    run with a fingerprint), the selectors it ran (paths and node ids
    only), the test keys it failed and those that errored, whether it
    completed, was filtered or stopped early, its `source`, for a captured
    run its `failure_class`, `failure_module` and `failure_items` (when
    known; records written before them have none) and, for a captured run, the
    working tree's fingerprint at its start (`ozagia.core.fingerprint`).
    No command line, no messages.

    A selector that is not kept (not path-like, too long, over
    `MAX_RUN_KEYS`) makes the run `filtered`: an empty or shorter list
    must not widen or narrow what it is taken to have run. Failed or
    errored keys over the caps make it incomplete."""
    if not isinstance(run, dict):
        return None
    ts = run.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    sels = run.get("selectors")
    filtered = run.get("filtered") is True
    complete = run.get("complete") is True
    if isinstance(sels, list):
        kept = [s for s in sels if isinstance(s, str) and _SAFE_SELECTOR.fullmatch(s)]
        if len(kept) != len(sels) or len(kept) > MAX_RUN_KEYS:
            filtered = True
        kept = kept[:MAX_RUN_KEYS]
    else:
        kept, filtered = [], filtered or sels is not None
    failed, f_drop = _run_keys(run.get("failed"))
    errors, e_drop = _run_keys(run.get("errors"))
    if f_drop or e_drop:
        complete = False
    source = run.get("source") if run.get("source") in RUN_SOURCES else "reported"
    out: dict[str, Any] = {"ts": float(ts), "selectors": kept, "failed": failed,
                           "errors": errors, "complete": complete,
                           "filtered": filtered, "stops": run.get("stops") is True,
                           "source": source}
    if source == "captured":
        from ozagia.core.failures import CLASSES
        if run.get("failure_class") in CLASSES:
            out["failure_class"] = run["failure_class"]
            mod = run.get("failure_module")
            if isinstance(mod, str) and _MODULE_NAME.fullmatch(mod):
                out["failure_module"] = mod
            items = _record_items(run.get("failure_items"))
            if items:
                out["failure_items"] = items
    if source == "captured" and run.get("tree") is not None:
        from ozagia.core.fingerprint import clean_fingerprint
        tree = clean_fingerprint(run.get("tree"))
        if tree is not None:
            out["tree"] = tree
    return out


def _record_items(value: Any) -> list[dict[str, Any]]:
    """A run's `failure_items` as the record keeps them: known classes,
    test or file keys within `MAX_RUN_KEY_CHARS`, dotted modules, at most
    `MAX_RUN_KEYS`."""
    from ozagia.core.failures import CLASSES
    out: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("class") not in CLASSES:
            continue
        key = item.get("key")
        if not (isinstance(key, str) and key.startswith(("test:", "file:"))
                and len(key) <= MAX_RUN_KEY_CHARS):
            continue
        row = {"key": key, "class": item["class"]}
        mod = item.get("module")
        if isinstance(mod, str) and _MODULE_NAME.fullmatch(mod):
            row["module"] = mod
        out.append(row)
        if len(out) >= MAX_RUN_KEYS:
            break
    return out


def run_record(run: TestRun) -> dict[str, Any]:
    """The record's entry for a pytest run (see `_record_run`)."""
    failed = [k for k, v in run.failures.items() if v.get("outcome") != "error"]
    errors = [k for k, v in run.failures.items() if v.get("outcome") == "error"]
    return _record_run({"ts": run.ts, "selectors": list(run.selectors),
                        "failed": failed, "errors": errors,
                        "complete": run.complete, "filtered": run.filtered,
                        "stops": run.stops, "source": run.source,
                        "tree": run.tree, "failure_class": run.failure_class,
                        "failure_module": run.failure_module,
                        "failure_items": run.failure_items}) or {}


def recorded_outcome(record: dict[str, Any], key: str, *,
                     evidence: bool = False) -> dict[str, Any]:
    """What the record says about test `key`: `{"outcome": "passed" |
    "failed" | "error" | "unknown", "ts": run time or None, "reason",
    "run": the deciding run or None}`.

    The newest run that failed the test (or had it error), or that
    selected it, completed, was not filtered and did not stop early,
    decides; an older failure still in the failing set (a run evicted
    from `runs`) counts when no run decides.

    With `evidence` (goal criteria), only `captured` runs count, the
    failing set is not consulted (it also holds reported runs), and when
    the newest captured run that selected the test unfiltered did not
    finish (a collection or import error, a timeout, `--co`), the outcome
    is `unknown`."""
    best: dict[str, Any] | None = None
    for run in reversed(record.get("runs") or []):
        if evidence and run.get("source") != "captured":
            continue
        if key in (run.get("errors") or []):
            best = {"outcome": "error", "ts": run.get("ts"), "run": run,
                    "reason": "the newest run that selected it had it error "
                              "(setup, fixture or collection), not fail"}
            break
        if key in (run.get("failed") or []):
            best = {"outcome": "failed", "ts": run.get("ts"), "run": run,
                    "reason": "the newest run that selected it failed it"}
            break
        if not _selected(key, run.get("selectors") or []):
            continue
        failures = run.get("failed") or run.get("errors")
        if (run.get("complete") and not run.get("filtered")
                and not (run.get("stops") and failures)):
            best = {"outcome": "passed", "ts": run.get("ts"), "run": run,
                    "reason": "the newest complete run that selected it did "
                              "not fail it"}
            break
        if evidence and not run.get("complete") and not run.get("filtered"):
            return {"outcome": "unknown", "ts": run.get("ts"), "run": run,
                    "reason": "the newest captured run that selected it did not "
                              "finish (collection or import error, timeout, or "
                              "an option with which pytest runs no test)"}
    if evidence:
        if best is not None:
            return best
        return {"outcome": "unknown", "ts": None, "run": None,
                "reason": "no complete pytest run that ozagia captured "
                          "selected this test"}
    failing = (record.get("tests") or {}).get(key)
    if isinstance(failing, dict):
        fts = failing.get("ts")
        if best is None or (isinstance(fts, (int, float))
                            and fts >= float(best.get("ts") or 0)):
            return {"outcome": "failed", "ts": fts, "run": None,
                    "reason": "in the record's failing tests"}
    if best is not None:
        return best
    return {"outcome": "unknown", "ts": None, "run": None,
            "reason": "no recorded complete pytest run selected this test"}


def _record_test(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: rec[k] for k in _RECORD_TEST_FIELDS if k in rec}
    if out.get("outcome") not in ("failed", "error"):
        out["outcome"] = "failed"
    params = out.get("params")
    out["params"] = [p for p in params if isinstance(p, str)] if isinstance(
        params, list) else []
    out["prefix"] = safe_prefix(rec.get("prefix"))
    return out


def _record_last(last: Any) -> dict[str, Any] | None:
    if not isinstance(last, dict):
        return None
    return {k: last[k] for k in _RECORD_LAST_FIELDS if k in last}


def load_test_record(root: str | Path) -> dict[str, Any]:
    """`{"tests": {key: rec}, "last": {...}}` from the state dir; empty
    when missing or unreadable. Only the fields a record keeps are read
    (`_RECORD_TEST_FIELDS` and the rerun prefix)."""
    try:
        data = json.loads(_record_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tests": {}, "last": None, "runs": []}
    if not isinstance(data, dict) or not isinstance(data.get("tests"), dict):
        return {"tests": {}, "last": None, "runs": []}
    tests = {k: _record_test(v) for k, v in data["tests"].items()
             if isinstance(k, str) and k.startswith("test:") and isinstance(v, dict)}
    raw_runs = data.get("runs") if isinstance(data.get("runs"), list) else []
    runs = [r for r in (_record_run(x) for x in raw_runs) if r]
    return {"tests": tests, "last": _record_last(data.get("last")),
            "runs": runs[-MAX_RECORD_RUNS:]}


def save_test_record(root: str | Path, record: dict[str, Any]) -> None:
    path = _record_path(root)
    runs = [r for r in (_record_run(x) for x in record.get("runs") or []) if r]
    data = {"tests": {k: _record_test(v) for k, v in record.get("tests", {}).items()},
            "last": _record_last(record.get("last")),
            "runs": runs[-MAX_RECORD_RUNS:]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="tests.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


class TestRecord:
    """The root's failing tests across sessions, persisted on change. It
    holds only what is persisted: test ids, outcome, times and the rerun
    prefix (no messages or command lines)."""
    __test__ = False

    def __init__(self, root: str | Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        rec = load_test_record(root)
        self.tests: OrderedDict[str, dict[str, Any]] = OrderedDict(
            sorted(rec["tests"].items(), key=lambda kv: kv[1].get("ts", 0)))
        self.last = rec["last"]
        self.runs: list[dict[str, Any]] = list(rec.get("runs") or [])

    def apply(self, run: TestRun) -> None:
        """Apply `run` to the failing tests and add it to the recent runs;
        the record is written every time (each run is a new entry)."""
        with self.lock:
            apply_test_run(self.tests, run)
            for key in list(self.tests):
                self.tests[key] = _record_test(self.tests[key])
            self.last = {"ts": run.ts, "failed": len(run.failures),
                         "complete": run.complete}
            entry = run_record(run)
            if entry:
                self.runs.append(entry)
                del self.runs[:-MAX_RECORD_RUNS]
            save_test_record(self.root, {"tests": dict(self.tests),
                                         "last": self.last, "runs": self.runs})

    def view(self) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any] | None]:
        with self.lock:
            return [(k, dict(v)) for k, v in self.tests.items()], (
                dict(self.last) if self.last else None)


# --------------------------------------------------------------------------- #
# DevSession plan step
# --------------------------------------------------------------------------- #
def devsession_line(root: str | Path, scan: int = 20, *,
                    free_text: bool = True) -> str | None:
    """The current step of the newest open `DevSession` in the root (best
    effort; None when there is none or it cannot be read). With
    `free_text=False`, its id and step numbers without the task and step
    titles."""
    def title(t: Any) -> str:
        return f" \"{_short(t, 80)}\"" if free_text else ""
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
            part = f"DevSession {st['session_id']}{title(st.get('task'))}"
            if cur:
                part += (f": step {cur.get('plan_index') or len(done)}/{len(steps) or '?'}"
                         f"{title(cur.get('title'))} ({cur.get('status')})")
            elif steps:
                part += f": plan of {len(steps)} steps, none started"
            nxt = len(done) if cur and cur.get("status") == "passed" else None
            if nxt is not None and nxt < len(steps):
                t = steps[nxt].get("title") if isinstance(steps[nxt], dict) else steps[nxt]
                part += f"; next{title(t)}" if free_text else f"; next step {nxt + 1}"
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


def _reference_status_lines(root: Path, *, schedule_sync: bool = False) -> list[str]:
    """Render bounded reference freshness and optionally schedule its update."""
    try:
        from ozagia.core import federation
        reference_report = federation.reference_status(root)
        references = (reference_report.get("references")
                      or reference_report.get("reference_repos") or [])
        reference_errors = reference_report.get("errors") or []
        if not isinstance(references, list):
            references = []
        if not references and not reference_errors:
            return []
        request_errors: list[Any] = []
        if schedule_sync:
            # This API schedules a bounded background worker and returns
            # immediately; the brief never waits for network or checkout work.
            request_report = federation.request_reference_sync(root, limit=6)
            request_errors = request_report.get("errors", [])
            reference_report = federation.reference_status(root)
            references = (reference_report.get("references")
                          or reference_report.get("reference_repos") or references)
            reference_errors = reference_report.get("errors") or reference_errors
        with federation.without_reference_sync():
            report = federation.status(root)
    except Exception:  # noqa: BLE001 - session brief must remain available
        return ["linked reference status unavailable; verify before relying on it"]

    root_status: dict[Path, dict[str, Any]] = {}
    for member in report.get("members", []):
        if not isinstance(member, dict) or not isinstance(member.get("root"), str):
            continue
        try:
            root_status[Path(member["root"]).expanduser().resolve()] = member
        except (OSError, RuntimeError, ValueError):
            continue

    lines = ["linked reference snapshot status (updates run in bounded background work):"]
    if reference_errors or request_errors:
        count = len(reference_errors) + len(request_errors)
        lines.append(f"  {count} reference manifest or update error(s); verify declared paths")
    omitted = max(0, len(references) - 6)
    for index, reference in enumerate(references[:6], 1):
        if not isinstance(reference, dict):
            lines.append(f"  reference {index}: invalid manifest entry")
            continue
        raw_path = reference.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            lines.append(f"  reference {index}: path missing; snapshot unknown")
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            member = root_status.get(path.resolve())
        except (OSError, RuntimeError, ValueError):
            member = None
        snapshot_state = str(member.get("state", "unknown")) if member else "missing"
        sync = reference.get("sync")
        sync_state = (sync.get("state") if isinstance(sync, dict)
                      else reference.get("state"))
        pin = reference.get("pin")
        head = (sync.get("head") if isinstance(sync, dict) else None)
        if not isinstance(head, str) or not head:
            head = member.get("head") if member else None
        if isinstance(pin, str) and pin:
            if isinstance(head, str) and head:
                pin_state = "match" if head == pin else "mismatch"
            elif sync_state == "pinned":
                pin_state = "match"
            else:
                pin_state = "unknown"
        else:
            pin_state = "not pinned"
        label = member.get("name") if member else None
        if not isinstance(label, str) or not label.strip():
            label = f"reference {index}"
        label = re.sub(r"[^A-Za-z0-9 ._-]", "", " ".join(label.split()))[:40]
        label = label or f"reference {index}"
        parts = [f"snapshot {snapshot_state}", f"pin {pin_state}"]
        if sync_state == "error":
            parts.append("last sync failed")
        elif sync_state == "updated" or (isinstance(sync, dict) and sync.get("updated")):
            parts.append("index stale until reindexed")
        elif sync_state == "pending":
            parts.append("sync pending")
        elif sync_state == "not_checked":
            parts.append("sync not checked")
        lines.append(f"  {label}: {', '.join(parts)}")
    if omitted:
        lines.append(f"  ... {omitted} more reference checkout(s)")
    return lines


def _dirty_paths(dirty: Iterable[str]) -> list[str]:
    """The paths named by `git status --porcelain` lines.

    A rename reports its new path; status codes and the separating space are
    dropped. Git quotes paths with special characters, so one pair of outer
    quotes is removed. Only extensions are inspected later, so an odd quoting
    edge case costs a tools line, not correctness.
    """
    paths: list[str] = []
    for line in dirty:
        rest = line[3:] if len(line) > 3 else ""
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] == '"':
            rest = rest[1:-1]
        if rest:
            paths.append(rest)
    return paths


def tool_brief_lines(root: str | Path, paths: Iterable[str], *,
                     max_lines: int = BRIEF_TOOL_LINES) -> list[str]:
    """A bounded `[ozagia] tools:` block for paths that name a capability.

    The capability inference is the toolchain registry's own
    (`ozagia.core.toolchain.infer_capabilities`), so ordinary code paths add
    nothing. No version or sudo probe runs, and nothing is installed: the
    plan's install options are previews. A broken config or plan omits the
    block rather than failing the brief. `[tools] brief = false` opts out.
    """
    if max_lines < 1:
        return []
    try:
        from ozagia.core import toolchain as T
        capabilities, _unmatched = T.infer_capabilities(list(paths))
        if not capabilities or not T.brief_enabled(root):
            return []
        plan = T.tool_plan(root, capabilities=capabilities,
                           probe_versions=False, probe_sudo=False)
        brief = T.brief_lines(plan)
    except Exception:  # noqa: BLE001 - the brief goes without the tools block
        return []
    # brief_lines' own header/trailers describe the full report; the session
    # brief keeps one line per capability (its indented remote-provider
    # detail is left out to stay small).
    body = [
        line for line in brief
        if not line.startswith(("[ozagia] tools: ", "keep the provider count:",
                                "not checked:"))
        and not line.startswith(" ")
    ]
    shown = capabilities[:max_lines - 1]
    if not body or not shown:
        return []
    header = "[ozagia] tools: " + ", ".join(shown)
    if len(capabilities) > len(shown):
        header += f", +{len(capabilities) - len(shown)} more"
    header += " in the changed files; advisory, nothing installed"
    return [header, *body[:max_lines - 1]]


def session_brief(root: str | Path, max_chars: int = BRIEF_MAX_CHARS, *,
                  tests: list[tuple[str, dict[str, Any]]] | None = None,
                  last_test: dict[str, Any] | None = None,
                  jobs: list[dict[str, Any]] | None = None,
                  jobs_enabled: bool | None = None,
                  goal_lines: list[str] | None = None,
                  schedule_reference_sync: bool = False) -> str:
    """Dirty files, the last commits, the tests still failing in the root's
    test record (each with the time of the first run that failed it), a
    bounded `[ozagia] tools:` block when the dirty files name a non-code
    capability (PDF, images, office, media, ...; no version or sudo probes),
    then the open and claimed goals with unmet criteria
    (`ozagia.core.goals.brief_lines`; `goal_lines` replaces them), running
    jobs, and the MCP tools to prefer.

    Over `max_chars`, the MCP tools line stays and the goal lines keep
    their reserved share (at most `max_chars // BRIEF_GOAL_SHARE`) when they
    fit; the tools block is dropped first, then running jobs, then the goal
    lines, and the text before them (header, git, failing tests) is cut at
    its end. `tests` defaults to the persisted record
    (`load_test_record`); `last_test` is accepted and unused (each test
    carries its own time)."""
    root = Path(root)
    share = max_chars // BRIEF_GOAL_SHARE
    if goal_lines is None:
        try:
            from ozagia.core.goals import BRIEF_CHARS, brief_lines
            goal_lines = brief_lines(root, max_chars=min(BRIEF_CHARS, share))
        except Exception:  # noqa: BLE001 - the brief goes without goals
            goal_lines = []
    # Lines past the reserved share are left out (whole lines).
    goal_block: list[str] = []
    size = 0
    for line in goal_lines:
        if size + len(line) + 1 > share:
            break
        goal_block.append(line)
        size += len(line) + 1
    if len(goal_block) < 2:
        goal_block = []
    lines = [BRIEF_HEADER]
    lines.extend(_reference_status_lines(root,
                                         schedule_sync=schedule_reference_sync))
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    dirty: list[str] = []
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
    tool_block = (tool_brief_lines(
        root, _dirty_paths(dirty[:BRIEF_DIRTY_FILES])) if dirty else [])
    log = _git(root, "log", f"-{BRIEF_COMMITS}", "--format=%h %s")
    if log and log.strip():
        lines.append("recent commits:")
        lines.extend(f"  {x[:120]}" for x in log.splitlines() if x.strip())
    if tests is None:
        rec = load_test_record(root)
        tests = sorted(((k, v) for k, v in rec["tests"].items() if isinstance(v, dict)),
                       key=lambda kv: kv[1].get("ts", 0))
    if tests:
        lines.append("failing tests (ozagia's record of earlier pytest runs in "
                     "this root; rerun to confirm):")
        lines.extend(failing_lines(tests, BRIEF_TESTS))
    job_block: list[str] = []
    if jobs:
        job_block = ["running jobs:", *job_lines(jobs, None, BRIEF_JOBS)]
    if jobs_enabled is None:
        from ozagia.daemon.jobs import jobs_enabled as _enabled
        jobs_enabled = _enabled(root)
    tools = MCP_TOOLS_LINE + (" " + JOBS_LINE if jobs_enabled else "")
    text = "\n".join([*lines, *tool_block, *goal_block, *job_block, tools])
    if len(text) <= max_chars:
        return text
    # Keep the goals' reserved share and the tools line; cut the text
    # before them at its end. The tools block goes first, then jobs.
    tool_text = "".join(x + "\n" for x in tool_block)
    goals_text = "".join(x + "\n" for x in goal_block)
    jobs_text = "".join(x + "\n" for x in job_block)
    sep = "\n...\n"
    keep = (max_chars - len(tools) - len(goals_text) - len(tool_text)
            - len(jobs_text) - len(sep))
    if keep < min(200, max_chars // 4):
        jobs_text = ""
        keep = (max_chars - len(tools) - len(goals_text) - len(tool_text)
                - len(sep))
    if keep < 0:
        tool_text = ""
        keep = max_chars - len(tools) - len(goals_text) - len(sep)
    if keep < 0:
        goals_text = ""
        keep = max_chars - len(tools) - len(sep)
    head_text = "\n".join(lines)
    return (head_text[:max(keep, 0)].rstrip() + sep + tool_text + goals_text
            + jobs_text + tools)[:max_chars]


__all__ = ["BRIEF_HEADER", "BRIEF_MAX_CHARS", "HEADER", "JOBS_LINE", "MAX_FILES",
           "MAX_JOBS", "MAX_KEYS", "MAX_READ_FILES", "MAX_READ_RANGES",
           "MAX_TESTS", "MCP_TOOLS_LINE", "TEXT_MAX_CHARS", "TestRecord",
           "TestRun", "View", "WorkingSet", "apply_test_run", "devsession_line",
           "job_lines", "key_file", "load_test_record", "merge_views",
           "failing_lines", "parse_failures", "pytest_head", "rerun_command",
           "run_record", "session_brief", "tool_brief_lines", "pytest_run",
           "recorded_outcome", "text_meta", "working_set_text"]
