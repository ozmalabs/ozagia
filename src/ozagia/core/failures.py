"""Deterministic failure classes for pytest (and generic command) results.

One classifier for the eval harness's task checks, the agent's own pytest
runs in a transcript, the daemon's captured pytest runs and the test
record that goals read. It reads only what it is given (exit code,
timeout flag, stdout, stderr, the command, the interpreter, the root) and
runs nothing; the same input always gives the same class.

`classify(...)` returns `{"class", "evidence": [lines], "detail", ...}`.
The rules are tried in this order and the first that matches decides:

1. `harness`: the runner itself failed. The program the command runs or
   the interpreter is missing or not executable (`FileNotFoundError` /
   `PermissionError` naming it, `<name>: command not found`, exit 126 or
   127, `can't open file`), or the working directory (`root`) is missing.
2. `timeout`: the run was cut off (`timed_out`), whatever it printed.
3. `environment`: something the code needs is not installed where it ran.
   `ModuleNotFoundError` / `ImportError` for a module that is not
   first-party (third-party or a stdlib module the interpreter lacks), a
   missing shared library, `No module named pytest`, `unrecognized
   arguments` (a pytest plugin that is not installed), or a missing
   executable other than the runner. The result names the `module`, the
   `interpreter` and, when `ozagia.core.environments.who_has` exists (W20),
   the interpreters that have it (`available_in`).
4. `collection`: pytest could not collect what it was asked to run, for a
   reason in the project: an import error of a first-party module, a
   syntax error during collection, `fixture 'x' not found`, `found no
   collectors` / `not found` for a named node id, no tests collected, or
   a collection error with no recognized cause.
5. `assertion`: tests failed, with assertion output (`E   assert`,
   `AssertionError`, `Failed:`).
6. `error`: tests failed or errored with exceptions that are not
   assertions (setup, teardown, test bodies), pytest's internal or usage
   errors, or any other non-zero exit.
7. `passed`: exit 0, or (exit code unknown) a pytest summary with passes
   and no failures or errors.

`unknown` is the class when nothing is known: no exit code, no timeout and
no output that any rule reads. Exit 0 decides `passed` before rules 3 to 6
(a passing run's output may mention import errors in captured logs).

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
`TAIL_CHARS` characters, each evidence line is cut to `EVIDENCE_CHARS` and
at most `MAX_EVIDENCE` lines are returned. Standard library only.
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
# check has one is invalid, and a goal criterion it decides is unknown.
NOT_EVIDENCE = frozenset({"harness", "environment"})
HEAD_CHARS = 16_000
TAIL_CHARS = 48_000
EVIDENCE_CHARS = 240
MAX_EVIDENCE = 4
# Entries of a directory read when listing first-party names.
FIRST_PARTY_SCAN = 2000
WHO_HAS_MAX = 5

_IMPORT_LINE = re.compile(
    r"^(?:E\s+|.*?(?:: | - ))?(ModuleNotFoundError|ImportError): (.*)$")
_NO_MODULE = re.compile(r"No module named '?([A-Za-z_][\w.]*)'?")
_CANNOT_IMPORT = re.compile(
    r"cannot import name '?[\w.]+'? from (?:partially initialized module )?"
    r"'([A-Za-z_][\w.]*)'")
_SHARED_LIB = re.compile(r"(\S+\.so(?:\.[\w.]+)?): cannot open shared object file")
_NO_PYTEST = re.compile(r"(?::\s*|^)No module named '?pytest'?\s*$")
_UNRECOGNIZED = re.compile(r"error: unrecognized arguments: (.+)$")
_FILE_NOT_FOUND = re.compile(
    r"(FileNotFoundError|PermissionError): \[Errno \d+\] [^:']*: '([^']+)'")
# A shell's "not found": `bash: foo: command not found`, `sh: 1: foo: not
# found`, `/bin/sh: line 1: foo: command not found`.
_NOT_FOUND = re.compile(
    r"^(?:\S*sh: (?:\d+: |line \d+: )?)?([\w./+-]+): (?:command not found"
    r"|not found)\s*$")
_CANT_OPEN = re.compile(r"^\S*python[\d.]*: can't open file '([^']+)'")
_SYNTAX = re.compile(r"(?:^E\s+|: |\s- |^)(SyntaxError|IndentationError|TabError)\b")
_FIXTURE = re.compile(r"fixture '([^']+)' not found")
_NO_COLLECTORS = re.compile(
    r"ERROR: (?:found no collectors for|not found:|file or directory not "
    r"found:) (\S+)")
_COLLECTING = re.compile(
    r"ERROR collecting |errors? during collection|\[ozagia\] collection error"
    r"|\d+ collection errors?")
_NO_TESTS = re.compile(r"\bno tests ran\b|collected 0 items|no tests collected")
_ASSERTION = re.compile(
    r"^E\s+(?:AssertionError|assert\b|Failed:)|AssertionError\b"
    r"|^\[ozagia\] failed test:\S+: (?:assert\b|AssertionError)")
_FAILED_LINE = re.compile(r"^(?:FAILED \S|\[ozagia\] failed test:)")
_ERROR_LINE = re.compile(r"^(?:ERROR \S+::|\[ozagia\] error test:)")
_INTERNAL = re.compile(r"^INTERNALERROR>")
_FRAME_PATH = re.compile(
    r"(?:^|\s)([A-Za-z_][\w-]*(?:/[\w.-]+)*\.py)(?::\d+|\s|$)")
# pytest's final summary (`3 failed, 10 passed in 1.2s`) and the daemon's
# captured head (`[ozagia] pytest failed: exit 1, 0.1 s, 1 passed, ...`).
_SUMMARY = re.compile(
    r"(?:^|\W)(\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
    r"(?:, \d+ \w+)*) in [\d.]+s\b")
_HEAD = re.compile(r"^\[ozagia\] pytest \w+(?: \([^)]*\))?: exit (-?\d+|None)")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")


def bounded(text: Any) -> str:
    """`text` cut to its first `HEAD_CHARS` and last `TAIL_CHARS`
    characters, with a marker line between them."""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    if len(s) <= HEAD_CHARS + TAIL_CHARS:
        return s
    cut = len(s) - HEAD_CHARS - TAIL_CHARS
    return f"{s[:HEAD_CHARS]}\n... ({cut} characters not read) ...\n{s[-TAIL_CHARS:]}"


def _has_py(path: str) -> bool:
    if os.path.isfile(os.path.join(path, "__init__.py")):
        return True
    try:
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i >= FIRST_PARTY_SCAN:
                    return False
                if e.name.endswith(".py") and e.is_file():
                    return True
    except OSError:
        return False
    return False


def first_party_names(root: str | Path, import_roots: Iterable[str] = ()) -> set[str]:
    """The top-level module names of the project at `root`: Python files
    and directories holding Python files (a package or a namespace
    package) in the root and in each import root (default `src` when it
    exists). Hidden directories and `__pycache__` are skipped; at most
    `FIRST_PARTY_SCAN` entries of each directory are read."""
    base = Path(root)
    roots = [r for r in import_roots if isinstance(r, str) and r]
    if not roots and (base / "src").is_dir():
        roots = ["src"]
    out: set[str] = set()
    for d in [base, *(base / r for r in roots)]:
        try:
            with os.scandir(d) as it:
                entries = [e for _, e in zip(range(FIRST_PARTY_SCAN), it)]
        except OSError:
            continue
        for e in entries:
            name = e.name
            if name.startswith(".") or name == "__pycache__":
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


def who_has(root: str | Path | None, module: str) -> list[str] | None:
    """The interpreters that can import `module`, from W20's
    `ozagia.core.environments.who_has(root, module)` when that module
    exists; None when it does not or fails. Items may be paths or dicts
    with a `path`; at most `WHO_HAS_MAX` are kept."""
    try:
        env = importlib.import_module("ozagia.core.environments")
        fn = getattr(env, "who_has")
    except Exception:  # noqa: BLE001 - W20 absent: no detail
        return None
    try:
        got = fn(root, module)
    except Exception:  # noqa: BLE001 - an optional detail
        return None
    out: list[str] = []
    for item in got or []:
        path = item if isinstance(item, str) else (
            item.get("path") if isinstance(item, dict) else None)
        if isinstance(path, str) and path and path not in out:
            out.append(path)
    return out[:WHO_HAS_MAX]


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


def _cut(line: str) -> str:
    s = line.strip()
    return s if len(s) <= EVIDENCE_CHARS else s[:EVIDENCE_CHARS - 3] + "..."


def _counts(lines: list[str]) -> dict[str, int] | None:
    """Passed/failed/error counts of the last pytest summary or captured
    head line, or None."""
    for line in reversed(lines):
        s = line.strip()
        if _HEAD.match(s) or _SUMMARY.search(s):
            got = {"passed": 0, "failed": 0, "error": 0}
            for n, word in _COUNT.findall(s):
                got["error" if word.startswith("error") else word] += int(n)
            return got
    return None


def _result(cls: str, evidence: list[str], detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"class": cls,
                           "evidence": [_cut(e) for e in evidence if e][:MAX_EVIDENCE],
                           "detail": detail}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def classify(returncode: int | None, timed_out: bool, stdout: str | None,
             stderr: str | None, *, command: Any = None,
             first_party: Iterable[str] | None = None,
             interpreter: str | None = None,
             root: str | Path | None = None) -> dict[str, Any]:
    """The failure class of one command result (see the module doc for the
    rules and their order). `command`: the command line (string or argv);
    `first_party`: the project's top-level module names; `interpreter`:
    the Python that ran it; `root`: the project root (its working
    directory; also where first-party names and `who_has` look)."""
    text = bounded(stdout) + "\n" + bounded(stderr)
    lines = [x.rstrip() for x in text.splitlines()]
    stripped = [x.strip() for x in lines]
    rc = returncode if isinstance(returncode, int) and not isinstance(
        returncode, bool) else None
    exit_ev = f"exit {rc}" if rc is not None else ""

    # 1. harness
    runner = _runner(command)
    runner_names = {n for n in (runner, interpreter) if n}
    runner_names |= {os.path.basename(n) for n in list(runner_names)}
    if root is not None:
        runner_names.add(str(root))
    for s in stripped:
        m = _FILE_NOT_FOUND.search(s)
        if m and (m.group(2) in runner_names
                  or os.path.basename(m.group(2)) in runner_names - {str(root)}):
            what = "working directory" if m.group(2) == str(root) else "runner"
            return _result("harness", [s, exit_ev],
                           f"the {what} {m.group(2)} is missing or not executable")
        m = _NOT_FOUND.search(s)
        if m and (m.group(1) in runner_names
                  or os.path.basename(m.group(1)) in runner_names):
            return _result("harness", [s, exit_ev],
                           f"the runner {m.group(1)} was not found")
        m = _CANT_OPEN.search(s)
        if m:
            return _result("harness", [s, exit_ev],
                           f"the interpreter cannot open {m.group(1)}")
    unstarted = rc in (126, 127) or (rc is None and not timed_out)
    if unstarted and root is not None and not os.path.isdir(str(root)):
        return _result("harness", [exit_ev], f"the working directory {root} is missing")
    if unstarted and interpreter and not os.path.exists(interpreter):
        return _result("harness", [exit_ev], f"the interpreter {interpreter} does "
                       "not exist")
    if rc in (126, 127) and not any(_NOT_FOUND.search(s) for s in stripped):
        return _result("harness", [exit_ev] + [s for s in stripped if s][-1:],
                       f"exit {rc}: the runner could not be started")

    # 2. timeout
    if timed_out:
        last = [s for s in stripped if s][-1:]
        return _result("timeout", ["timed out", *last], "the run timed out")

    # 7 (early). exit 0
    if rc == 0:
        summary = [s for s in stripped if _SUMMARY.search(s) or _HEAD.match(s)][-1:]
        return _result("passed", [exit_ev, *summary], "exit 0")

    # 3. environment, 4. collection (import errors)
    if first_party is not None:
        fp = {str(x) for x in first_party}
        fp_source = "given"
    elif root is not None:
        fp = first_party_names(root)
        fp_source = "root"
    else:
        fp = _names_from_output(lines)
        fp_source = "output"
    first_party_import: tuple[str, str] | None = None
    for s in stripped:
        if _NO_PYTEST.search(s):
            return _environment("pytest", [s, exit_ev], interpreter, root,
                                "pytest is not installed")
        m = _IMPORT_LINE.match(s)
        if not m:
            continue
        msg = m.group(2)
        mm = _NO_MODULE.search(msg) or _CANNOT_IMPORT.search(msg)
        if mm:
            module = mm.group(1)
            top = module.split(".")[0]
            if top in fp:
                first_party_import = first_party_import or (module, s)
                continue
            how = ("missing module" if mm.re is _NO_MODULE
                   else "cannot import from")
            return _environment(top, [s, exit_ev], interpreter, root,
                                f"{how} {module} (third-party)", full=module)
        lib = _SHARED_LIB.search(msg)
        if lib:
            return _environment(None, [s, exit_ev], interpreter, root,
                                f"missing shared library {lib.group(1)}")
    for s in stripped:
        m = _UNRECOGNIZED.search(s)
        if m:
            return _environment(None, [s, exit_ev], interpreter, None,
                                f"unrecognized arguments {m.group(1).strip()} "
                                "(a pytest plugin not installed?)")
        # A missing program (a bare name, not a path: a data file a test
        # cannot find is the test's error).
        m = _FILE_NOT_FOUND.search(s)
        name = m.group(2) if m and m.group(1) == "FileNotFoundError" else None
        if name is None:
            n = _NOT_FOUND.search(s)
            name = n.group(1) if n else None
        if name and "/" not in name and "." not in name:
            return _environment(None, [s, exit_ev], interpreter, None,
                                f"missing executable {name}", executable=name)

    # 4. collection
    collecting = [s for s in stripped if _COLLECTING.search(s)]
    if first_party_import is not None:
        module, line = first_party_import
        return _result("collection", [line, *collecting[:1], exit_ev],
                       f"import error of first-party module {module}",
                       module=module, first_party_from=fp_source)
    if collecting:
        syn = [s for s in stripped if _SYNTAX.search(s)]
        if syn:
            return _result("collection", [syn[0], collecting[0], exit_ev],
                           "syntax error during collection")
    for s in stripped:
        m = _FIXTURE.search(s)
        if m:
            return _result("collection", [s, exit_ev], f"fixture {m.group(1)} not found")
    for s in stripped:
        m = _NO_COLLECTORS.search(s)
        if m:
            return _result("collection", [s, exit_ev],
                           f"no tests collected for {m.group(1)}")
    if collecting:
        return _result("collection", [collecting[0], exit_ev], "collection error")
    no_tests = [s for s in stripped if _NO_TESTS.search(s)]
    if rc == 5 or no_tests:
        return _result("collection", [*no_tests[:1], exit_ev], "no tests collected")

    # 5. assertion, 6. error
    counts = _counts(lines)
    failed = [s for s in stripped if _FAILED_LINE.match(s)]
    errored = [s for s in stripped if _ERROR_LINE.match(s)]
    n_failed = (counts or {}).get("failed", 0) or len(failed)
    n_error = (counts or {}).get("error", 0) or len(errored)
    if n_failed:
        asserts = [s for s in stripped if _ASSERTION.search(s)]
        if asserts:
            return _result("assertion", [asserts[0], *failed[:1], exit_ev],
                           f"{n_failed} failed with assertion output")
        return _result("error", [*failed[:2], exit_ev],
                       f"{n_failed} failed with exceptions, not assertions")
    if n_error:
        return _result("error", [*errored[:2], exit_ev],
                       f"{n_error} errored (setup, teardown or test body)")
    internal = [s for s in stripped if _INTERNAL.match(s)]
    if internal:
        return _result("error", [internal[-1], exit_ev], "pytest internal error")
    if rc is not None:
        last = [s for s in stripped if s][-1:]
        return _result("error", [exit_ev, *last], f"exit {rc} with no recognized "
                       "pytest result")

    # 7. passed (exit code unknown)
    if counts is not None and counts["passed"] and not counts["failed"] \
            and not counts["error"]:
        return _result("passed", [s for s in stripped if _SUMMARY.search(s)
                                  or _HEAD.match(s)][-1:],
                       "a pytest summary with passes only")
    return _result("unknown", [], "no exit code and no recognized output")


def _environment(module: str | None, evidence: list[str], interpreter: str | None,
                 root: str | Path | None, detail: str, *,
                 full: str | None = None, executable: str | None = None
                 ) -> dict[str, Any]:
    available = who_has(root, module) if module and root is not None else None
    if available is not None and interpreter:
        available = [p for p in available if p != interpreter]
    text = detail
    if interpreter:
        text += f" under {interpreter}"
    if available:
        text += "; available in " + ", ".join(available)
    return _result("environment", evidence, text, module=module,
                   module_path=full if full and full != module else None,
                   executable=executable, interpreter=interpreter,
                   available_in=available)


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


__all__ = ["CLASSES", "NOT_EVIDENCE", "bounded", "classify", "first_party_names",
           "head_note", "who_has"]
