"""Deterministic failure classes for pytest (and generic command) results.

One classifier for the eval harness's task checks, the agent's own pytest
runs in a transcript, the daemon's captured pytest runs and the test
record that goals read. It reads only what it is given (exit code,
timeout flag, stdout, stderr, the command, the interpreter, the root) and
runs nothing; the same input always gives the same class.

`classify(...)` returns `{"class", "evidence": [lines], "detail", "items",
...}`. The output is first parsed into structure, and only the positions
where pytest states a cause are read:

- collection-error blocks (`ERROR collecting <path>`) and the
  `ImportError while loading conftest` block, by their `E` lines;
- each failed or errored test's block (`FAILURES`/`ERRORS` sections), by
  its exception `E` line: the first exception line of the last exception
  in the block (after the last `During handling ...`/`The above exception
  ...` separator); deeper-indented `E` lines after it (assertion diffs,
  multi-line messages) are continuations and are not read;
- the reason after ` - ` in the short summary's `FAILED`/`ERROR` lines;
- pytest's own startup and usage errors at column 0 (`No module named
  pytest` from the interpreter, `ERROR: usage:` with `<prog>: error: ...`,
  `ERROR: found no collectors for ...`, `INTERNALERROR>`);
- the daemon's own lines (`[ozagia] failed test:`, `error test:`,
  `collection error`, `error:`) when they follow its `[ozagia] pytest ...`
  head and run to the end of the text (a head with anything else after
  it was printed by a test and is ordinary text).

`Captured stdout/stderr/log` sections, warnings and anything a test
prints outside those positions are skipped. Lines before the first header
(a tail of the output) or after the `bounded` marker form a fragment
read like a block.

Each failing item (a test, a collection error, a conftest error) gets a
class from its cause: `environment` (an import error of a module that is
not first-party, a missing shared library, a `FileNotFoundError` for a
bare program name raised through `subprocess`), `collection` (in a
collection block: a first-party import error, a syntax error, an
unrecognized cause; anywhere: `fixture 'x' not found`), `assertion`
(`assert`, `AssertionError`, `Failed:`) or `error` (any other exception,
or no cause stated). `items` lists them: `[{"node", "class", "module"?}]`,
at most `MAX_ITEMS`.

The run's class is decided in this order:

1. `harness`: the runner itself failed. Only when the exit code is None,
   126 or 127, or nothing shows pytest started, and never for exit 0: the
   program the command runs or the interpreter is missing or not
   executable (`FileNotFoundError` / `PermissionError` naming it, `<name>:
   command not found`, `can't open file`, exit 126/127 with no output of
   pytest), or the working directory (`root`) is missing.
2. `timeout`: the run was cut off (`timed_out`), whatever it printed.
3. `passed`: exit 0.
4. Startup (nothing started): `No module named pytest` or `unrecognized
   arguments` (a plugin that is not installed) is `environment`; another
   usage error is `error`.
5. Failing items: `environment` only when every item has an environment
   cause; else `collection` when an item is `collection`, else
   `assertion` when an item is `assertion`, else `error`.
6. Without items: `ERROR: found no collectors`/`not found` and `no tests
   ran` (exit 5) are `collection`; `INTERNALERROR>` and any other non-zero
   exit are `error`; with no exit code, a summary with passes only is
   `passed`, else `unknown`.

For `environment` the result names the `module` (or `executable`), the
`interpreter` and, with a `root` and W20's
`ozagia.core.environments.module_environment`, the interpreters that have
it (`available_in`) and the manifests that declare it (`declared_in`, `[]`
when none does). W20 memoizes that answer per root and module and follows
its trust rule: project-shipped interpreters are read statically unless
the user trusts them, so a captured pytest never runs one.

First-party versus third-party: a module is first-party when its top-level
name is in `first_party` (callers pass the project's top-level module
names from the graph or `first_party_names(root, import_roots)`). Without
`first_party`, the names are computed from `root` (`first_party_names`);
without a root either, they are the top-level directories and modules of
the relative paths in the output's traceback frames and collection lines
(`tests/test_x.py:6:` gives `tests`; `src/` is skipped). A module whose
top-level name is not first-party is third-party (stdlib included: an
interpreter that lacks a stdlib module is an environment problem).

Bounds: each stream is read as its first `HEAD_CHARS` and last
`TAIL_CHARS` characters, each evidence line is cut to `EVIDENCE_CHARS`,
at most `MAX_EVIDENCE` lines are returned, `detail` is cut to
`DETAIL_CHARS`, module names to `MODULE_CHARS` and item nodes to
`NODE_CHARS`. Standard library only.
"""
from __future__ import annotations

import importlib
import os
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

CLASSES = ("harness", "timeout", "environment", "collection", "assertion",
           "error", "passed", "unknown")
# Classes that say nothing about the code under test: an eval run whose
# check has one may be invalid (`ozagia.eval.agent.check_validity`), and a
# goal criterion a test with one decides is unknown.
NOT_EVIDENCE = frozenset({"harness", "environment"})
HEAD_CHARS = 16_000
TAIL_CHARS = 48_000
EVIDENCE_CHARS = 240
MAX_EVIDENCE = 4
DETAIL_CHARS = 300
MODULE_CHARS = 100
NODE_CHARS = 300
MAX_ITEMS = 50
# Entries of a directory read when listing first-party names, and how deep
# a directory is searched for Python code (a namespace package's code may
# sit in subdirectories only).
FIRST_PARTY_SCAN = 2000
FIRST_PARTY_DEPTH = 3
WHO_HAS_MAX = 5

# --- causes ----------------------------------------------------------------
_NO_MODULE = re.compile(r"No module named '?([A-Za-z_][\w.]*)'?")
_CANNOT_IMPORT = re.compile(
    r"cannot import name '?[\w.]+'? from (?:partially initialized module )?"
    r"'([A-Za-z_][\w.]*)'")
_SHARED_LIB = re.compile(r"(\S+\.so(?:\.[\w.]+)?): cannot open shared object file")
_IMPORT_MSG = re.compile(r"^(?:[\w.]+\.)?(ModuleNotFoundError|ImportError): (.*)$")
_ASSERT_MSG = re.compile(r"^(?:assert\b|(?:[\w.]+\.)?(?:AssertionError|Failed)\b)")
_SYNTAX_MSG = re.compile(r"^(?:[\w.]+\.)?(SyntaxError|IndentationError|TabError)\b")
_FIXTURE = re.compile(r"fixture '([^']+)' not found")
_FNF_MSG = re.compile(
    r"^(?:[\w.]+\.)?FileNotFoundError: \[Errno \d+\] [^:']*: '([^']+)'")
# The first line of an exception in an `E` block.
_EXC_LINE = re.compile(
    r"^(?:[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failed)\b"
    r"|Failed\b|assert\b|fixture '[^']*' not found)")
# The last line of a long or short traceback: `tests/test_a.py:12: AssertionError`.
_LOCATION = re.compile(r"^[\w./-]+\.py:\d+: ([A-Za-z_][\w.]*)$")
_WRAPPED = re.compile(r'^failed on (?:setup|teardown|call) with "(.*)"$')
_CHAIN = ("The above exception was the direct cause of the following exception",
          "During handling of the above exception, another exception occurred")

# --- structure -------------------------------------------------------------
_SECTION = re.compile(r"^(?:={3,}|!{3,})(?: .*)?$")
_ITEM_HEAD = re.compile(r"^_{3,} (.+?) _{3,}$")
_DASHED = re.compile(r"^-{3,} (.+?) -{3,}$")
_MARKER = re.compile(r"^\.\.\. \(\d+ characters not read\) \.\.\.$")
_SUMMARY_ITEM = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$")
_PROGRESS = re.compile(r"^[.FEsxX]+(?:\s+\[\s*\d+%\])?$")
_COLLECTED = re.compile(r"^(?:collected \d+ items?|collecting \.\.\.)")
_CONFTEST = re.compile(r"^ImportError while loading conftest '([^']+)'")

# --- startup and harness (column 0, outside captured sections) --------------
_NO_PYTEST = re.compile(r"^\S*python[\w.-]*: No module named '?pytest'?$")
_USAGE = re.compile(r"^ERROR: usage: ")
_ARG_ERROR = re.compile(
    r"^(?:\S*/)?(?:pytest|py\.test|__main__\.py|pytest-[\w.]+): error: (.+)$")
_UNRECOGNIZED = re.compile(r"^unrecognized arguments: (.+)$")
_NO_COLLECTORS = re.compile(
    r"^ERROR: (?:found no collectors for|not found:|file or directory not "
    r"found:) (\S+)")
_INTERNAL = re.compile(r"^INTERNALERROR>")
_FILE_NOT_FOUND = re.compile(
    r"^(FileNotFoundError|PermissionError): \[Errno \d+\] [^:']*: '([^']+)'")
# A shell's "not found": `bash: foo: command not found`, `sh: 1: foo: not
# found`, `/bin/sh: line 1: foo: command not found`.
_NOT_FOUND = re.compile(
    r"^(?:\S*sh: (?:\d+: |line \d+: )?)?([\w./+-]+): (?:command not found"
    r"|not found)\s*$")
_CANT_OPEN = re.compile(r"^\S*python[\d.]*: can't open file '([^']+)'")
_NO_TESTS = re.compile(r"\bno tests ran\b|^collected 0 items|no tests collected")
_COLLECTING = re.compile(r"errors? during collection|^\d+ collection errors?")

# --- the daemon's lines ----------------------------------------------------
_HEAD = re.compile(r"^\[ozagia\] pytest \w+(?: \([^)]*\))?: exit (-?\d+|None)")
_D_TEST = re.compile(
    r"^\[ozagia\] (failed|error) (test:\S+?)(\[[^\]]*\])?(?:: (.*))?$")
_D_COLLECT = re.compile(r"^\[ozagia\] collection error (\S+?)(?:: (.*))?$")
_D_ERROR = re.compile(r"^\[ozagia\] error: (.*)$")

_FRAME_PATH = re.compile(
    r"(?:^|\s)([A-Za-z_][\w-]*(?:/[\w.-]+)*\.py)(?::\d+|\s|$)")
# pytest's final summary (`3 failed, 10 passed in 1.2s`).
_SUMMARY = re.compile(
    r"(?:^|\W)(\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
    r"(?:, \d+ \w+)*) in [\d.]+s\b")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
_MODULE_NAME = re.compile(r"[A-Za-z_][\w.]*")


def bounded(text: Any) -> str:
    """`text` cut to its first `HEAD_CHARS` and last `TAIL_CHARS`
    characters, with a marker line between them."""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    if len(s) <= HEAD_CHARS + TAIL_CHARS:
        return s
    cut = len(s) - HEAD_CHARS - TAIL_CHARS
    return f"{s[:HEAD_CHARS]}\n... ({cut} characters not read) ...\n{s[-TAIL_CHARS:]}"


# --------------------------------------------------------------------------- #
# first-party names
# --------------------------------------------------------------------------- #
def _entries(path: str | Path) -> list[os.DirEntry]:
    """At most `FIRST_PARTY_SCAN` entries of a directory, sorted by name
    (deterministic when the directory is under the cap)."""
    try:
        with os.scandir(path) as it:
            got = [e for _, e in zip(range(FIRST_PARTY_SCAN), it)]
    except OSError:
        return []
    return sorted(got, key=lambda e: e.name)


def _has_py(path: str, depth: int = FIRST_PARTY_DEPTH) -> bool:
    """Whether a directory holds Python code: `__init__.py`, a `.py` file,
    or (down to `depth` levels) a subdirectory that does (a namespace
    package such as `acme/proj/__init__.py`)."""
    if os.path.isfile(os.path.join(path, "__init__.py")):
        return True
    subdirs: list[str] = []
    for e in _entries(path):
        try:
            if e.is_file() and e.name.endswith(".py"):
                return True
            if (depth > 1 and e.is_dir() and e.name.isidentifier()
                    and not e.name.startswith(".")):
                subdirs.append(e.path)
        except OSError:
            continue
    return any(_has_py(d, depth - 1) for d in subdirs)


def first_party_names(root: str | Path, import_roots: Iterable[str] = ()) -> set[str]:
    """The top-level module names of the project at `root`: Python files
    and directories holding Python code (a package, or a namespace package
    whose code sits in subdirectories, searched `FIRST_PARTY_DEPTH` levels
    deep) in the root and in each import root (default `src` when it
    exists). Hidden directories and `__pycache__` are skipped; at most
    `FIRST_PARTY_SCAN` entries of each directory are read."""
    base = Path(root)
    roots = [r for r in import_roots if isinstance(r, str) and r]
    if not roots and (base / "src").is_dir():
        roots = ["src"]
    out: set[str] = set()
    # An import root (`src`) is a directory of packages, not a package.
    root_tops = {Path(r).parts[0] for r in roots if Path(r).parts}
    for d in [base, *(base / r for r in roots)]:
        for e in _entries(d):
            name = e.name
            if name.startswith(".") or name == "__pycache__":
                continue
            if d == base and name in root_tops:
                continue
            try:
                if e.is_file() and name.endswith(".py"):
                    if name[:-3].isidentifier():
                        out.add(name[:-3])
                elif e.is_dir() and name.isidentifier() and _has_py(e.path):
                    out.add(name)
            except OSError:
                continue
    return out


def _names_from_output(lines: list[str]) -> set[str]:
    """The fallback first-party names: top-level parts of the relative
    `.py` paths the output names (frames, collection lines)."""
    out: set[str] = set()
    for line in lines:
        for m in _FRAME_PATH.finditer(line):
            rel = m.group(1)
            if "site-packages" in rel or "dist-packages" in rel:
                continue
            parts = rel.split("/")
            if parts[0] == "src" and len(parts) > 1:
                parts = parts[1:]
            top = parts[0][:-3] if len(parts) == 1 else parts[0]
            if top.isidentifier():
                out.add(top)
    return out


def _env_module() -> Any:
    try:
        return importlib.import_module("ozagia.core.environments")
    except Exception:  # noqa: BLE001 - W20 absent: no detail
        return None


def _paths(items: Any) -> list[str]:
    """Interpreter paths from W20's items (`{"interpreter", "version"}`;
    `path` and plain strings accepted too), at most `WHO_HAS_MAX`."""
    out: list[str] = []
    for item in items or []:
        if isinstance(item, str):
            path = item
        elif isinstance(item, dict):
            path = item.get("interpreter") or item.get("path")
        else:
            path = None
        if isinstance(path, str) and path and path not in out:
            out.append(path)
    return out[:WHO_HAS_MAX]


def who_has(root: str | Path | None, module: str) -> list[str] | None:
    """The interpreters that can import `module`, from W20's
    `ozagia.core.environments.who_has(root, module)` (items
    `{"interpreter", "version", "probe"}`) when that module exists; None
    when it does not or fails. At most `WHO_HAS_MAX` paths are kept."""
    env = _env_module()
    fn = getattr(env, "who_has", None) if env is not None else None
    if fn is None:
        return None
    try:
        got = fn(root, module)
    except Exception:  # noqa: BLE001 - an optional detail
        return None
    return _paths(got)


def module_environment(root: str | Path | None, module: str, *,
                       cache_mode: str = "rw"
                       ) -> tuple[list[str] | None, list[dict[str, Any]] | None]:
    """(available_in, declared_in) for a missing module: the interpreters
    that have it and the manifests that declare it (`[]`: none does), from
    W20's `environments.module_environment` (memoized there, trust
    respected: untrusted project interpreters are read, not run); with only
    a `who_has`, declared_in is None; (None, None) without W20 or when it
    fails. `cache_mode` is W20's probe cache mode (`read` writes nothing
    under the root's state dir)."""
    env = _env_module()
    if env is None:
        return None, None
    fn = getattr(env, "module_environment", None)
    if fn is None:
        return who_has(root, module), None
    try:
        got = fn(root, module, cache_mode=cache_mode)
    except Exception:  # noqa: BLE001 - an optional detail
        return None, None
    if not isinstance(got, dict):
        return None, None
    declared = got.get("declared_in")
    if isinstance(declared, list):
        declared = [{"file": str(d.get("file")), "optional": bool(d.get("optional")),
                     "group": d.get("group")}
                    for d in declared if isinstance(d, dict) and d.get("file")
                    ][:WHO_HAS_MAX]
    else:
        declared = None
    return _paths(got.get("available_in")), declared


def _runner(command: Any) -> str | None:
    """The program a command line runs (after `VAR=value` assignments)."""
    if isinstance(command, (list, tuple)):
        toks = [str(t) for t in command]
    elif isinstance(command, str):
        try:
            toks = shlex.split(command)
        except ValueError:
            toks = command.split()
    else:
        return None
    for t in toks:
        if re.match(r"^[A-Za-z_]\w*=", t):
            continue
        if t in ("cd", "&&", ";"):
            return None
        return t
    return None


def _cut(line: str, limit: int = EVIDENCE_CHARS) -> str:
    s = line.strip()
    return s if len(s) <= limit else s[:limit - 3] + "..."


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
class _Block:
    """One cause block: a test's failure or error, a collection error, the
    conftest error, or a fragment of one (`kind` None)."""
    __slots__ = ("name", "kind", "stmts", "subprocess", "location", "_indent",
                 "_prev_e")

    def __init__(self, name: str | None, kind: str | None) -> None:
        self.name = name
        self.kind = kind
        self.stmts: list[str] = []      # `E` statement lines of the last exception
        self.subprocess = False
        self.location: str | None = None    # the exception type of the last frame
        self._indent: int | None = None
        self._prev_e = False

    def feed(self, line: str) -> None:
        if line == "E" or line.startswith("E "):
            body = line[2:]
            text = body.strip()
            indent = len(body) - len(body.lstrip(" "))
            if not text:
                return                  # a blank `E` line keeps the statement
            if self._prev_e and self._indent is not None and indent > self._indent:
                return                  # a continuation (assertion diff, message)
            self._indent, self._prev_e = indent, True
            self.stmts.append(text)
            return
        self._prev_e, self._indent = False, None
        if any(c in line for c in _CHAIN):
            self.stmts = []
        if "subprocess" in line:
            self.subprocess = True
        m = _LOCATION.match(line)
        if m:
            self.location = m.group(1)

    def cause(self) -> str | None:
        """The exception line: the first statement that names an exception
        (or `assert`), else the last statement, else the exception type of
        the traceback's last location line (a tail that cut the `E` lines),
        else None."""
        for s in self.stmts:
            if _EXC_LINE.match(s):
                return s
        return self.stmts[-1] if self.stmts else self.location


def _block_for(header: str) -> _Block:
    if header.startswith("ERROR collecting "):
        return _Block(header[len("ERROR collecting "):].strip(), "collection")
    m = re.match(r"^ERROR at (?:setup|teardown) of (.+)$", header)
    if m:
        return _Block(m.group(1).strip(), "test")
    return _Block(header.strip(), "test")


def _node_name(node: str) -> str:
    """The name a block header uses for a node id: `tests/x.py::C::t[1]` is
    `C.t[1]`; a path stays a path."""
    path, sep, rest = node.partition("::")
    return rest.replace("::", ".") if sep else path


class _Parsed:
    __slots__ = ("blocks", "summary", "visible", "started", "no_pytest", "usage",
                 "arg_error", "no_collectors", "internal", "summaries")

    def __init__(self) -> None:
        self.blocks: list[_Block] = []
        self.summary: list[dict[str, Any]] = []
        self.visible: list[str] = []    # lines outside captured sections
        self.started = False
        self.no_pytest: str | None = None
        self.usage = False
        self.arg_error: str | None = None
        self.no_collectors: list[tuple[str, str]] = []
        self.internal: str | None = None
        self.summaries: list[str] = []  # pytest's final summary lines


def _parse(lines: list[str]) -> _Parsed:
    p = _Parsed()
    cur: _Block | None = None
    captured = False
    fresh = True                        # no header seen yet: a leading fragment
    for raw in lines:
        s = raw.rstrip()
        if _MARKER.match(s):
            cur = _Block(None, None)
            p.blocks.append(cur)
            captured, fresh = False, False
            continue
        m = _ITEM_HEAD.match(s)
        if m:
            cur = _block_for(m.group(1))
            p.blocks.append(cur)
            captured, fresh, p.started = False, False, True
            continue
        if _SECTION.match(s):
            cur, captured, fresh, p.started = None, False, False, True
            if _SUMMARY.search(s):
                p.summaries.append(s.strip("=! "))
            continue
        m = _DASHED.match(s)
        if m:
            captured = m.group(1).startswith("Captured ")
            continue
        if captured:
            continue
        p.visible.append(s)
        m = _SUMMARY_ITEM.match(s)
        if m and (".py" in m.group(2) or "::" in m.group(2)):
            node = m.group(2).strip()
            kind = "test" if "::" in node else "collection"
            p.summary.append({"node": node, "name": _node_name(node), "kind": kind,
                              "outcome": "failed" if m.group(1) == "FAILED" else "error",
                              "reason": (m.group(3) or "").strip() or None,
                              "line": s})
            p.started = True
            continue
        if _SUMMARY.search(s) and not s.startswith("E "):
            p.summaries.append(s)
            p.started = True
        if _PROGRESS.match(s) or _COLLECTED.match(s):
            p.started = True
        m = _CONFTEST.match(s)
        if m:
            cur = _Block(m.group(1), "startup")
            p.blocks.append(cur)
            fresh = False
            continue
        if _NO_PYTEST.match(s):
            p.no_pytest = p.no_pytest or s
        if _USAGE.match(s):
            p.usage = True
        m = _ARG_ERROR.match(s)
        if m:
            p.arg_error = p.arg_error or s
        m = _NO_COLLECTORS.match(s)
        if m:
            p.no_collectors.append((m.group(1), s))
        if _INTERNAL.match(s):
            p.internal = s
        if cur is None and fresh:
            cur = _Block(None, None)
            p.blocks.append(cur)
        if cur is not None:
            cur.feed(s)
    return p


def daemon_head_index(lines: list[str]) -> int | None:
    """The index of the daemon's head line: the last `[ozagia] pytest ...`
    head when every non-empty line after it is an `[ozagia]` line, else
    None (a head a test printed is followed by pytest's own output)."""
    at = max((i for i, x in enumerate(lines) if _HEAD.match(x.strip())), default=None)
    if at is None:
        return None
    for x in lines[at + 1:]:
        s = x.strip()
        if s and not s.startswith("[ozagia] "):
            return None
    return at


def _daemon_items(lines: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """The daemon's failing items (its `failed test:`, `error test:` and
    `collection error` lines) and its `error:` messages."""
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for x in lines:
        s = x.strip()
        m = _D_TEST.match(s)
        if m:
            key, param = m.group(2), m.group(3) or ""
            _, _, qual = key.partition("::")
            items.append({"node": key, "name": qual + param, "kind": "test",
                          "outcome": m.group(1),
                          "reason": (m.group(4) or "").strip() or None,
                          "line": s})
            continue
        m = _D_COLLECT.match(s)
        if m:
            key = m.group(1)
            items.append({"node": key, "name": key.partition(":")[2] or key,
                          "kind": "collection", "outcome": "error",
                          "reason": (m.group(2) or "").strip() or None, "line": s})
            continue
        m = _D_ERROR.match(s)
        if m:
            errors.append(m.group(1))
    return items, errors


def _attach(items: list[dict[str, Any]], blocks: list[_Block], *,
            keep_unmatched: bool) -> list[dict[str, Any]]:
    """Give each item its block (by name, in order). A fragment goes to
    the only item without a block; other unmatched blocks become items of
    their own when `keep_unmatched` (conftest blocks always do)."""
    items = [dict(i, block=None) for i in items]
    loose: list[_Block] = []
    for b in blocks:
        if b.kind is None or b.kind == "startup":
            loose.append(b)
            continue
        target = next((i for i in items if i["block"] is None and i["name"] == b.name
                       and i["kind"] == b.kind), None)
        if target is not None:
            target["block"] = b
        elif keep_unmatched:
            items.append({"node": b.name, "name": b.name, "kind": b.kind,
                          "outcome": "error", "reason": None, "line": None,
                          "block": b})
    for b in loose:
        if b.cause() is None:
            continue
        if b.kind is None:
            free = [i for i in items if i["block"] is None]
            if len(free) == 1:
                free[0]["block"] = b
                continue
        items.append({"node": b.name, "name": b.name, "kind": b.kind or "test",
                      "outcome": "error", "reason": None, "line": None, "block": b})
    return items


def _cause_class(msg: str, *, collection: bool, subprocess: bool,
                 fp: set[str]) -> dict[str, Any]:
    """The class of one failing item from its cause message."""
    m = _WRAPPED.match(msg)
    if m:
        msg = m.group(1)
    m = _IMPORT_MSG.match(msg)
    if m:
        body = m.group(2)
        mm = _NO_MODULE.search(body) or _CANNOT_IMPORT.search(body)
        if mm:
            module = mm.group(1)
            if module.split(".")[0] in fp:
                return {"class": "collection" if collection else "error",
                        "module": module, "first_party": True,
                        "detail": f"import error of first-party module {module}"}
            how = "missing module" if mm.re is _NO_MODULE else "cannot import from"
            return {"class": "environment", "module": module,
                    "detail": f"{how} {module} (third-party)"}
        lib = _SHARED_LIB.search(body)
        if lib:
            return {"class": "environment",
                    "detail": f"missing shared library {lib.group(1)}"}
        return {"class": "collection" if collection else "error",
                "detail": "collection error (import error)" if collection
                else "import error"}
    if _SYNTAX_MSG.match(msg):
        return {"class": "collection" if collection else "error",
                "detail": "syntax error" + (" during collection" if collection else "")}
    m = _FIXTURE.search(msg)
    if m:
        return {"class": "collection", "detail": f"fixture {m.group(1)} not found"}
    if _ASSERT_MSG.match(msg) or (msg.endswith("...") and len(msg) > 5
                                  and "AssertionError".startswith(msg[:-3])):
        return {"class": "assertion", "detail": "assertion"}
    m = _FNF_MSG.match(msg)
    if m and subprocess and "/" not in m.group(1) and "." not in m.group(1):
        return {"class": "environment", "executable": m.group(1),
                "detail": f"missing executable {m.group(1)}"}
    if collection:
        return {"class": "collection", "detail": "collection error"}
    return {"class": "error", "detail": "exception"}


def _classify_item(item: dict[str, Any], fp: set[str]) -> dict[str, Any]:
    block: _Block | None = item.get("block")
    collection = item["kind"] in ("collection", "startup")
    cause = block.cause() if block is not None else None
    ev = f"E   {cause}" if cause else None
    if cause is None:
        cause = item.get("reason")
        ev = item.get("line")
    if not cause:
        got = {"class": "collection" if collection else "error",
               "detail": "collection error" if collection else "no cause stated"}
    else:
        got = _cause_class(cause, collection=collection,
                           subprocess=bool(block and block.subprocess), fp=fp)
    evidence = list(dict.fromkeys(x for x in (ev, item.get("line")) if x))
    return {**got, "node": item.get("node"), "kind": item["kind"],
            "outcome": item["outcome"], "evidence": evidence}


def _counts(p: _Parsed, head: str | None) -> dict[str, int] | None:
    """Passed/failed/error counts of the daemon's head or pytest's last
    summary line, or None."""
    src = head or (p.summaries[-1] if p.summaries else None)
    if src is None:
        return None
    got = {"passed": 0, "failed": 0, "error": 0}
    for n, word in _COUNT.findall(src):
        got["error" if word.startswith("error") else word] += int(n)
    return got


def _result(cls: str, evidence: list[str], detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"class": cls,
                           "evidence": [_cut(e) for e in evidence if e][:MAX_EVIDENCE],
                           "detail": _cut(str(detail), DETAIL_CHARS)}
    for k, v in extra.items():
        if v is None:
            continue
        if k in ("module", "module_path", "executable") and isinstance(v, str):
            v = v[:MODULE_CHARS]
        out[k] = v
    out.setdefault("items", [])
    return out


def _public_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i in items[:MAX_ITEMS]:
        row: dict[str, Any] = {"node": _cut(i["node"], NODE_CHARS) if i.get("node")
                               else None, "class": i["class"]}
        if i.get("module"):
            row["module"] = str(i["module"])[:MODULE_CHARS]
        out.append(row)
    return out


def classify(returncode: int | None, timed_out: bool, stdout: str | None,
             stderr: str | None, *, command: Any = None,
             first_party: Iterable[str] | None = None,
             interpreter: str | None = None,
             root: str | Path | None = None,
             env_cache: str = "rw") -> dict[str, Any]:
    """The failure class of one command result (see the module doc for the
    parsing, the rules and their order). `command`: the command line
    (string or argv); `first_party`: the project's top-level module names;
    `interpreter`: the Python that ran it; `root`: the project root (its
    working directory; also where first-party names and `who_has` look);
    `env_cache`: the probe cache mode for the environment detail (`read`
    leaves the root's state dir alone)."""
    text = bounded(stdout) + "\n" + bounded(stderr)
    lines = [x.rstrip() for x in text.splitlines()]
    rc = returncode if isinstance(returncode, int) and not isinstance(
        returncode, bool) else None
    exit_ev = f"exit {rc}" if rc is not None else ""
    head_at = daemon_head_index(lines)
    p = _parse(lines[:head_at] if head_at is not None else lines)
    head = lines[head_at].strip() if head_at is not None else None
    d_items, d_errors = _daemon_items(lines[head_at + 1:]) if head_at is not None \
        else ([], [])
    started = p.started or head is not None
    visible = [s.strip() for s in p.visible]

    # 1. harness: only when the run may not have started, never for exit 0.
    gate = rc != 0 and not timed_out and (rc in (None, 126, 127) or not started)
    runner = _runner(command)
    runner_names = {n for n in (runner, interpreter) if n}
    runner_names |= {os.path.basename(n) for n in list(runner_names)}
    if gate:
        for s in visible:
            m = _FILE_NOT_FOUND.match(s)
            if m and (m.group(2) in runner_names or (root is not None and m.group(2)
                                                     == str(root))
                      or os.path.basename(m.group(2)) in runner_names):
                what = ("working directory" if root is not None
                        and m.group(2) == str(root) else "runner")
                return _result("harness", [s, exit_ev],
                               f"the {what} {m.group(2)} is missing or not executable")
            m = _NOT_FOUND.match(s)
            if m and (m.group(1) in runner_names
                      or os.path.basename(m.group(1)) in runner_names):
                return _result("harness", [s, exit_ev],
                               f"the runner {m.group(1)} was not found")
            if not started:
                m = _CANT_OPEN.match(s)
                if m:
                    return _result("harness", [s, exit_ev],
                                   f"the interpreter cannot open {m.group(1)}")
        if rc in (None, 126, 127):
            if root is not None and not os.path.isdir(str(root)):
                return _result("harness", [exit_ev],
                               f"the working directory {root} is missing")
            if interpreter and not os.path.exists(interpreter):
                return _result("harness", [exit_ev], f"the interpreter {interpreter} "
                               "does not exist")
        if rc in (126, 127) and not started and not any(
                _NOT_FOUND.match(s) for s in visible):
            return _result("harness", [exit_ev] + [s for s in visible if s][-1:],
                           f"exit {rc}: the runner could not be started")

    # 2. timeout
    if timed_out:
        last = [s for s in visible if s][-1:]
        return _result("timeout", ["timed out", *last], "the run timed out")

    # 3. exit 0
    if rc == 0:
        summary = [head] if head else p.summaries[-1:]
        return _result("passed", [exit_ev, *summary], "exit 0")

    # first-party names
    if first_party is not None:
        fp = {str(x) for x in first_party}
        fp_source = "given"
    elif root is not None:
        fp = first_party_names(root)
        fp_source = "root"
    else:
        fp = _names_from_output(lines)
        fp_source = "output"

    # 4. startup and usage errors (nothing started)
    if not started:
        if p.no_pytest:
            return _environment("pytest", [p.no_pytest, exit_ev], interpreter, root,
                                "pytest is not installed", env_cache=env_cache)
        if p.arg_error:
            msg = _ARG_ERROR.match(p.arg_error).group(1)  # type: ignore[union-attr]
            m = _UNRECOGNIZED.match(msg)
            if m:
                return _environment(None, [p.arg_error, exit_ev], interpreter, None,
                                    f"unrecognized arguments {m.group(1).strip()} "
                                    "(a pytest plugin not installed?)")
            return _result("error", [p.arg_error, exit_ev], f"pytest usage error: {msg}")
        if (gate and rc == 127) or (rc is None and not p.blocks and not d_items):
            for s in visible:
                n = _NOT_FOUND.match(s)
                if n and "/" not in n.group(1):
                    return _environment(None, [s, exit_ev], interpreter, None,
                                        f"missing executable {n.group(1)}",
                                        executable=n.group(1))

    # 5. failing items
    if d_items:
        raw = _attach(d_items, p.blocks, keep_unmatched=False)
    else:
        raw = _attach(p.summary, p.blocks, keep_unmatched=True)
    items = [_classify_item(i, fp) for i in raw]
    if items:
        return _decide(items, p, head, rc, exit_ev, interpreter, root, fp_source,
                       env_cache=env_cache)

    # 6. no items
    for node, s in p.no_collectors:
        return _result("collection", [s, exit_ev], f"no tests collected for {node}")
    if p.internal:
        return _result("error", [p.internal, exit_ev], "pytest internal error")
    no_tests = [s for s in visible + d_errors if _NO_TESTS.search(s)]
    if rc == 5 or no_tests:
        return _result("collection", [*no_tests[:1], exit_ev], "no tests collected")
    collecting = [s for s in visible if _COLLECTING.search(s)]
    if collecting:
        return _result("collection", [collecting[0], exit_ev], "collection error")
    counts = _counts(p, head)
    if counts and (counts["failed"] or counts["error"]):
        n = counts["failed"] or counts["error"]
        what = "failed" if counts["failed"] else "errored"
        return _result("error", [*(([head] if head else p.summaries[-1:])), exit_ev],
                       f"{n} {what}; the output states no cause")
    if rc is not None:
        last = [s for s in visible if s][-1:]
        return _result("error", [exit_ev, *last], f"exit {rc} with no recognized "
                       "pytest result")

    # passed (exit code unknown)
    if counts is not None and counts["passed"]:
        return _result("passed", [head] if head else p.summaries[-1:],
                       "a pytest summary with passes only")
    return _result("unknown", [], "no exit code and no recognized output")


def _decide(items: list[dict[str, Any]], p: _Parsed, head: str | None,
            rc: int | None, exit_ev: str, interpreter: str | None,
            root: str | Path | None, fp_source: str, *,
            env_cache: str = "rw") -> dict[str, Any]:
    """The run's class from its classified failing items (rule 5)."""
    public = _public_items(items)
    env = [i for i in items if i["class"] == "environment"]
    rest = [i for i in items if i["class"] != "environment"]
    if not rest:
        first = env[0]
        module = first.get("module")
        top = module.split(".")[0] if module else None
        return _environment(top, [*first["evidence"], exit_ev], interpreter, root,
                            first["detail"], full=module,
                            executable=first.get("executable"), items=public,
                            env_cache=env_cache)
    also = ""
    if env:
        mods = sorted({str(i.get("module") or i.get("executable") or "?")
                       for i in env})
        also = (f"; {len(env)} more with an environment cause "
                f"({', '.join(mods[:3])}{', ...' if len(mods) > 3 else ''})")
    for cls in ("collection", "assertion", "error"):
        mine = [i for i in rest if i["class"] == cls]
        if not mine:
            continue
        first = mine[0]
        if cls == "collection":
            extra: dict[str, Any] = {}
            if first.get("first_party"):
                extra = {"module": first.get("module"), "first_party_from": fp_source}
            detail = first["detail"]
            if first["kind"] == "startup":
                detail = f"conftest: {detail}"
            return _result("collection", [*first["evidence"], exit_ev], detail + also,
                           items=public, **extra)
        counts = _counts(p, head) or {}
        # The summary's counts (the items may be a tail's), less the items
        # with an environment cause.
        n_failed = _count(counts.get("failed"), rest, env, "failed")
        if cls == "assertion":
            return _result("assertion", [*first["evidence"], exit_ev],
                           f"{n_failed} failed with assertion output" + also,
                           items=public)
        if n_failed:
            detail = f"{n_failed} failed with exceptions, not assertions"
        else:
            n = _count(counts.get("error"), rest, env, "error") or len(rest)
            detail = f"{n} errored (setup, teardown or test body)"
        return _result("error", [*first["evidence"], exit_ev], detail + also,
                       items=public)
    raise AssertionError("unreachable")  # pragma: no cover


def _count(total: int | None, rest: list[dict[str, Any]], env: list[dict[str, Any]],
           outcome: str) -> int:
    mine = sum(1 for i in rest if i["outcome"] == outcome)
    if not total:
        return mine
    return max(mine, total - sum(1 for i in env if i["outcome"] == outcome))


def _environment(module: str | None, evidence: list[str], interpreter: str | None,
                 root: str | Path | None, detail: str, *,
                 full: str | None = None, executable: str | None = None,
                 items: list[dict[str, Any]] | None = None,
                 env_cache: str = "rw") -> dict[str, Any]:
    if module is not None and not _MODULE_NAME.fullmatch(module):
        module = None
    available, declared = (module_environment(root, module, cache_mode=env_cache)
                           if module and root is not None else (None, None))
    if available is not None and interpreter:
        here = os.path.abspath(interpreter)
        available = [p for p in available if os.path.abspath(p) != here]
    text = detail
    if interpreter:
        text += f" under {interpreter}"
    if available:
        text += "; available in " + ", ".join(available)
    if declared is not None:
        text += ("; declared in " + ", ".join(
            d["file"] + (" (optional)" if d["optional"] else "") for d in declared)
            if declared else "; declared in no manifest")
    return _result("environment", evidence, text, module=module,
                   module_path=full if full and full != module else None,
                   executable=executable, interpreter=interpreter,
                   available_in=available, declared_in=declared, items=items)


def head_note(result: dict[str, Any]) -> str | None:
    """The one-line note after a captured pytest's head line (without the
    `[ozagia] ` tag), or None for `passed`/`unknown`:
    `failure class environment: missing module numpy (third-party) under
    /usr/bin/python3; available in /repo/.venv/bin/python`."""
    cls = result.get("class")
    if cls in (None, "passed", "unknown"):
        return None
    detail = str(result.get("detail") or "").replace("\n", " ")
    return f"failure class {cls}: {detail}"[:400]


__all__ = ["CLASSES", "NOT_EVIDENCE", "bounded", "classify", "daemon_head_index",
           "module_environment",
           "first_party_names",
           "head_note", "who_has"]
