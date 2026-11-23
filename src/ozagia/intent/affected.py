"""Affected-test selection and a test-result cache for a captured pytest.

Both are off by default and turned on in the project configuration:

    [tests]
    select = "affected"   # default "all"
    cache = true          # default false

`select` keeps its original meaning: `all` runs what the command names and
`affected` runs only the linked tests. When `[tests] select` is not set, the
interactive default comes from the typed settings registry
(`testing.captured`, default `all`). An explicit `[tests] select` wins over
`testing.captured`, because it is the more specific setting and its meaning
does not change. `testing.captured = "off"` (or `"none"`) disables selection;
`smoke` runs the affected subset and `package` runs all, because the affected
selector is narrower; see :func:`ozagia.core.testing.captured_select`.

Selection stays off by default because changing what a command runs is
surprising: an agent that types `pytest -q` expects the whole suite, and a
partial run reported as a pass could hide a regression. With `all` and
`cache = false` a captured pytest runs exactly as before and this module
writes nothing. `OZAGIA_TESTS_ALL=1` always runs the command as typed.

Which commands: a captured pytest (alone or as a compound step) with no test
selector, or whose selectors are all directories (`pytest`, `pytest -q`,
`python -m pytest -x tests/`), and whose options only change reporting
(`-q`, `-v`, `-x`, `-s`, `-l`, `--tb`, `-r`, `--maxfail`, `--durations`,
`--color`, `--no-header`, `--disable-warnings`, `--strict-*`, ...). Anything
else (`-k`, `-m`, `-p`, `--deselect`, `--doctest-modules`, a pytest config
whose `addopts` has `--doctest`) runs as before. `OZAGIA_TESTS_ALL=1` on the
command (or in the caller's environment) runs everything.

Selection (`select = "affected"`). A suite is the interpreter, the directory
selectors, the environment assignments and the options that can change a
result. For each suite the state keeps its last fully green run: a run of the
command as typed that exited 0, with the git HEAD, the hashes of the files
that were dirty then, and the test files with their test counts. The files
changed since then are `git diff --no-renames <head>` plus untracked files
plus the files dirty at that run, less those whose content is what it was.
The tests linked to them are:

- test files that changed (a new test file only when its directory already
  holds a test file of the green run);
- test files whose transitive project imports reach a changed file. Imports
  are resolved here, not from the graph's `imports` edges, because those do
  not resolve `import a.b` or `from pkg import submodule`: every project file
  whose dotted path ends with the imported name counts (over-inclusion only
  runs more tests). A file depends on its package `__init__.py` files, a test
  file on its conftest chain, and any file on project files named by its
  string literals (`"pkg.mod"`, `"tests/fixtures/x.py"`);
- the graph's covering tests (`intent.common.covering_tests`) of a changed
  file;
- a test file whose import closure holds a dynamic import that cannot be
  resolved (`importlib.import_module(name)`, `__import__`, `exec`, entry
  points, ...) is linked to every change.

Everything runs, as typed, when the graph cannot decide: no green run yet,
the green run's commit is gone, not a git work tree, the root is below the
git top level (changes outside it are not seen), the interpreter, its
installed distributions or the environment differ from the green run's, a
conftest.py changed, a non-Python file changed (tests may read it; this
includes pytest and packaging config), a changed Python file no test is
linked to, too many changed or untracked files, no file changed at all,
every test file is affected (named: the files whose dynamic imports link
every change), or planning took longer than `MAX_PLAN_S` (or
`PLAN_SHARE` of `_deadline_s_remaining`). The output then says
`[ozagia] ran all tests: <reason>`. Test files that failed in the suite's
latest run are always selected. A subset run says so in the head line and
after it:

    [ozagia] pytest passed (affected subset: 2 of 3 tests run, 1 not run): exit 0, ...
    [ozagia] ran N affected tests of M; not run: K tests in F files (a, b, +n more); run all: OZAGIA_TESTS_ALL=1 <command>

Only a run of the command as typed that exits 0 moves the green run forward,
and only when HEAD, the dirty files and the interpreter are the same after
the run as before it; an affected run never does, so changes accumulate
until the next full pass.

Files: git's list (`ls-files -c -o --exclude-standard`), less tool caches
(`__pycache__`, `.pytest_cache`, ...) at any depth and the other
`GENERIC_SKIP_DIRS` names (`build`, `dist`, `venv`, ...) at the top level
only, since below it they are ordinary package names.

Cache (`cache = true`). Each test node's last outcome is stored with the key
of its test file: a hash of the test file, its conftest chain, the project
files of its import closure (conftest closures included), the root pytest
config files, the interpreter (version and installed distributions), the
environment (less a few per-terminal variables), the result-changing
options, and the size and mtime of every non-Python file git does not
ignore. A test file whose closure has a dynamic import has no key. The key
is taken before the run and again after it; results are stored only when
both agree. Of the tests that would run, a node whose stored key equals the
current one with outcome passed (or skipped), and that is not flaky, is not
run and is reported as `[ozagia] cached pass <file>: ...`. A failure or an
error is never served from the cache; a node seen with no stored outcome
under the current key (the file's node list is stored only by a run of the
whole file that was not cut short by `-x`) makes the whole file run. With
`select = "all"` the cache applies to every test file of the green run.
`pytest not run: every selected test has a cached pass` needs at least one
cached node; an empty selection runs the command as typed.

Slow (> `SLOW_S`) and flaky (passed and failed under one key) tests are
remembered while either feature is on and listed at the end of the output.

State: `<state dir>/tests/results.json` (bounded: `MAX_NODES` nodes,
`MAX_FILES` files, `MAX_SUITES` suites, `MAX_SLOW`/`MAX_FLAKY`). Entries of
the wrong shape are dropped on load; a failed write resets the file.

Known limits: tests that pass alone but fail with others (order or shared
state) can pass in a subset run; files git ignores (generated data, the
virtualenv beyond its distribution list) are not seen as changes. A captured
`cargo test` uses the same `[tests]` settings through
:mod:`ozagia.intent.affected_cargo` (target-level selection and cache).
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from ozagia.config import GENERIC_SKIP_DIRS, state_path, state_rel

ENV_ALL = "OZAGIA_TESTS_ALL"
SLOW_S = 5.0
VERSION = 2
MAX_NODES = 20000
MAX_FILES = 5000
MAX_SUITES = 16
MAX_SLOW = 500
MAX_FLAKY = 500
MAX_PY_FILES = 20000
MAX_CHANGED = 500
MAX_UNTRACKED = 5000
MAX_SELECTOR_CHARS = 100_000
MAX_LISTED = 5
MAX_CACHED_LINES = 10
GIT_TIMEOUT_S = 20
# Planning time: at most this, and at most PLAN_SHARE of the caller's
# remaining time; past it the command runs as typed.
MAX_PLAN_S = 30.0
PLAN_SHARE = 0.25
# Tool caches, skipped at any depth. Other `GENERIC_SKIP_DIRS` names
# (`build`, `dist`, `target`, `venv`, ...) are ordinary package names below
# the top level, so they are skipped only there.
_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache",
                         ".ruff_cache", ".hypothesis", ".git"})

PYTEST_CONFIG_FILES = ("pytest.ini", ".pytest.ini", "pyproject.toml",
                       "setup.cfg", "tox.ini")

# Options that only change what pytest reports, or when it stops.
_REPORT_FLAGS = frozenset({
    "-q", "-v", "-x", "-l", "--quiet", "--verbose", "--exitfirst",
    "--showlocals", "--no-header", "--no-summary", "--disable-warnings",
    "--full-trace",
})
_REPORT_VALUE_OPTS = frozenset({"--tb", "-r", "--maxfail", "--durations",
                                "--durations-min", "--color"})
# Options that may change a result but not which tests run.
_RESULT_FLAGS = frozenset({"-s", "--capture=no", "--strict-markers",
                           "--strict-config", "--strict"})
_EARLY_STOP = ("-x", "--exitfirst", "--maxfail")
# Variables that differ per terminal, not per test environment.
_VOLATILE_ENV = frozenset({
    "_", "SHLVL", "OLDPWD", "PWD", "COLUMNS", "LINES", "TERM_SESSION_ID",
    "WINDOWID", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_CLIENT",
    "SSH_CONNECTION", "SSH_TTY", "XDG_SESSION_ID", "TMUX", "TMUX_PANE",
    "STY", "WINDOW", "KITTY_WINDOW_ID", "GPG_AGENT_INFO", "PYTEST_CURRENT_TEST",
    ENV_ALL,
})
_DYNAMIC_CALLS = frozenset({
    "import_module", "__import__", "spec_from_file_location", "run_path",
    "run_module", "load_source", "load_module", "iter_modules",
    "walk_packages", "entry_points", "exec", "find_spec", "module_from_spec",
})
_SAFE_NODEID = re.compile(r"^[\w./:\[\]-]+$")

_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# options and configuration
# --------------------------------------------------------------------------- #

def _split_cluster(tok: str) -> str | None:
    """The result-changing letters of a short-flag cluster (`-xvs` -> "s"),
    or None when `tok` is not a cluster of known letters."""
    if tok.startswith("--") or len(tok) < 2 or not set(tok[1:]) <= set("vqxsl"):
        return None
    return "".join(c for c in tok[1:] if c == "s")


def _args(extra: list[str]) -> tuple[list[str], str | None, bool]:
    """(result-changing options, why the options are not eligible, whether
    an option stops the run early)."""
    keep: list[str] = []
    early = False
    i = 0
    while i < len(extra):
        tok = extra[i]
        name = tok.split("=", 1)[0]
        if tok in _REPORT_FLAGS:
            early |= tok in _EARLY_STOP
        elif tok in _RESULT_FLAGS:
            keep.append(tok)
        elif name in _REPORT_VALUE_OPTS and "=" in tok:
            early |= name in _EARLY_STOP
        elif tok in _REPORT_VALUE_OPTS:
            early |= tok in _EARLY_STOP
            i += 1
        elif not tok.startswith("--") and tok[:2] == "-r" and len(tok) > 2:
            pass
        elif (cl := _split_cluster(tok)) is not None:
            early |= "x" in tok
            if cl:
                keep.append("-s")
        else:
            return [], f"option {tok} is not a reporting option", early
        i += 1
    return sorted(set(keep)), None, early


def _config_reason(root: Path) -> str | None:
    """Why the pytest configuration rules selection out, or None."""
    for name in PYTEST_CONFIG_FILES:
        p = root / name
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("addopts") and "--doctest" in s:
                return f"{name} addopts collects doctests"
    return None


def _explicit_tests_select(cfg: Any) -> str | None:
    """``[tests] select`` when the project file sets it explicitly, else None."""
    source = getattr(cfg, "source", None)
    if source is None:
        return None
    try:
        data = tomllib.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    tests = data.get("tests")
    if isinstance(tests, dict) and "select" in tests:
        return cfg.tests_select
    return None


def _captured_default(root: Path) -> str:
    """``testing.captured`` from the registry, or the safe ``all`` default."""
    try:
        from ozagia.core import settings as S
        return str(S.get(root, "testing.captured")["value"])
    except (S.SettingsError, OSError, ValueError):
        return "all"


def _settings(root: Path) -> tuple[str, bool]:
    """(captured selector, cache) for a captured pytest.

    Precedence: an explicit ``[tests] select`` in the project config, then
    ``testing.captured`` from the settings registry, then ``all``. The
    selector is one of the two the executor implements: ``all`` or
    ``affected``. ``OZAGIA_TESTS_ALL=1`` is handled by :func:`plan` and still
    runs the command as typed.
    """
    from ozagia.config import ConfigError, load_project_config
    from ozagia.core.testing import captured_select
    try:
        cfg = load_project_config(root)
    except ConfigError:
        return "all", False
    select = _explicit_tests_select(cfg)
    if select is None:
        select = captured_select(_captured_default(root))
    return select, cfg.tests_cache


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #

_SHA_CACHE: dict[str, tuple[int, int, int, str]] = {}
_SHA_CACHE_MAX = 50000


def _sha(path: Path) -> str | None:
    """The content sha256 of a file, or None when it is missing."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    k = str(path)
    hit = _SHA_CACHE.get(k)
    if hit and hit[:3] == (st.st_mtime_ns, st.st_size, st.st_ino):
        return hit[3]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    if len(_SHA_CACHE) >= _SHA_CACHE_MAX:
        _SHA_CACHE.clear()
    _SHA_CACHE[k] = (st.st_mtime_ns, st.st_size, st.st_ino, digest)
    return digest


def _excluded(rel: str, state: str | None) -> bool:
    parts = PurePosixPath(rel).parts
    if len(parts) > 1 and parts[0] in GENERIC_SKIP_DIRS:
        return True
    if any(p in _CACHE_DIRS for p in parts[:-1]) or rel.endswith((".pyc", ".pyo")):
        return True
    return bool(state) and (rel == state or rel.startswith(state + "/"))


class _OutOfTime(Exception):
    pass


def _check_time(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise _OutOfTime


def _git(root: Path, *args: str) -> bytes | None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
           "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _zsplit(out: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def _head(root: Path) -> str | None:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    return out.decode().strip() if out else None


def _below_toplevel(root: Path) -> str | None:
    """The git top level when `root` is a directory below it, else None
    (also when `root` is not in a work tree)."""
    out = _git(root, "rev-parse", "--show-toplevel")
    if not out:
        return None
    top = Path(out.decode("utf-8", "surrogateescape").strip())
    try:
        same = top.resolve() == root.resolve()
    except OSError:
        return None
    return None if same else top.as_posix()


def _git_files(root: Path, state: str | None) -> list[str] | None:
    """The files git tracks or does not ignore under `root`, less tool
    caches, top-level build output and the state dir; None outside git."""
    out = _git(root, "ls-files", "-z", "-c", "-o", "--exclude-standard")
    if out is None:
        return None
    return sorted({r for r in _zsplit(out) if not _excluded(r, state)})


def _walk_py(root: Path, state: str | None,
             deadline: float | None) -> list[str]:
    """The Python files under `root` when git cannot list them."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        _check_time(deadline)
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(
            d for d in dirnames
            if not _excluded(f"{rel_dir}/{d}/_" if rel_dir else f"{d}/_", state))
        out.extend(f"{rel_dir}/{n}" if rel_dir else n
                   for n in filenames if n.endswith(".py"))
        if len(out) > MAX_PY_FILES:
            break
    return sorted(out)


def _py_files(root: Path, state: str | None, files: list[str] | None,
              deadline: float | None = None) -> list[str]:
    if files is None:
        return _walk_py(root, state, deadline)
    return [r for r in files if r.endswith(".py") and (root / r).is_file()]


def _dirty_since(root: Path, base: str) -> tuple[list[str], list[str]] | None:
    """(tracked files that differ from `base`, untracked files), relative to
    `root`, or None when git cannot tell."""
    tracked = _git(root, "diff", "--name-only", "--no-renames", "--relative",
                   "-z", base, "--")
    untracked = _git(root, "ls-files", "-z", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return None
    return _zsplit(tracked), _zsplit(untracked)


def _nonpy_fingerprint(root: Path, state: str | None,
                       files: list[str] | None = None) -> str | None:
    """A hash of the size and mtime of every non-Python file git does not
    ignore (tests may read any of them)."""
    if files is None:
        files = _git_files(root, state)
    if files is None:
        return None
    h = hashlib.sha256()
    for rel in files:
        if rel.endswith(".py"):
            continue
        try:
            st = (root / rel).stat()
        except OSError:
            h.update(f"{rel}\0-\n".encode("utf-8", "surrogateescape"))
            continue
        h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode(
            "utf-8", "surrogateescape"))
    return h.hexdigest()


_PY_FP: dict[tuple[str, int], tuple[str, list[str]]] = {}


def _python_fingerprint(python: str) -> str | None:
    """The interpreter's version and installed distributions, or None."""
    try:
        real = os.path.realpath(python)
        k = (real, os.stat(real).st_mtime_ns)
    except OSError:
        return None
    hit = _PY_FP.get((python, k[1]))
    if hit is None:
        code = ("import sys, sysconfig; p = sysconfig.get_paths(); "
                "print(sys.version.replace(chr(10), ' ')); "
                "print(p['purelib']); print(p['platlib'])")
        try:
            proc = subprocess.run([python, "-c", code], capture_output=True,
                                  text=True, timeout=30, check=False,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        rows = proc.stdout.splitlines()
        if proc.returncode != 0 or len(rows) < 3:
            return None
        hit = (rows[0], sorted(set(rows[1:3])))
        _PY_FP[(python, k[1])] = hit
    h = hashlib.sha256(hit[0].encode())
    for d in hit[1]:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for n in names:
            if n.endswith((".dist-info", ".egg-info", ".pth", ".egg-link")):
                try:
                    m = os.stat(os.path.join(d, n)).st_mtime_ns
                except OSError:
                    m = 0
                h.update(f"{d}/{n}\0{m}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _env_hash(env: dict[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(env):
        if k in _VOLATILE_ENV:
            continue
        h.update(f"{k}={env[k]}\0".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Python imports
# --------------------------------------------------------------------------- #

@dataclass
class _PyInfo:
    sha: str | None
    imports: list[tuple[str, int, tuple[str, ...]]] = field(default_factory=list)
    literals: list[str] = field(default_factory=list)
    opaque: bool = False


_PARSE_CACHE: dict[str, tuple[int, int, _PyInfo]] = {}


def _stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (-1, -1)
    return (st.st_mtime_ns, st.st_size)


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _parse_py(path: Path) -> _PyInfo:
    try:
        st = path.stat()
    except OSError:
        return _PyInfo(sha=None, opaque=True)
    k = str(path)
    hit = _PARSE_CACHE.get(k)
    if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    try:
        data = path.read_bytes()
    except OSError:
        return _PyInfo(sha=None, opaque=True)
    info = _PyInfo(sha=hashlib.sha256(data).hexdigest())
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError):
        info.opaque = True
        tree = None
    if tree is not None:
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    info.imports.append((a.name, 0, ()))
            elif isinstance(n, ast.ImportFrom):
                info.imports.append((n.module or "", int(n.level or 0),
                                     tuple(a.name for a in n.names)))
            elif isinstance(n, ast.Call):
                name = _call_name(n.func)
                if name not in _DYNAMIC_CALLS:
                    continue
                arg = n.args[0] if n.args else None
                if (name in ("import_module", "__import__")
                        and isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and not arg.value.startswith(".")):
                    info.imports.append((arg.value, 0, ()))
                elif (name in ("run_path", "spec_from_file_location",
                               "load_source")
                      and all(isinstance(a, ast.Constant)
                              and isinstance(a.value, str) for a in n.args)):
                    info.literals.extend(a.value for a in n.args)
                else:
                    info.opaque = True
            elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and 0 < len(n.value) <= 300
                  and ("." in n.value or "/" in n.value)
                  and not any(c.isspace() for c in n.value)):
                info.literals.append(n.value)
    if len(_PARSE_CACHE) >= MAX_PY_FILES:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[k] = (st.st_mtime_ns, st.st_size, info)
    return info


class _Project:
    """The project's Python files and their dependencies."""

    def __init__(self, root: Path, state: str | None,
                 extra: set[str] = frozenset(),
                 files: list[str] | None = None,
                 deadline: float | None = None) -> None:
        self.root = root
        self.state = state
        self.files: dict[str, _PyInfo] = {}
        self.stamps: dict[str, tuple[int, int]] = {}
        self.too_many = False
        rels = _py_files(root, state, files, deadline)
        if len(rels) > MAX_PY_FILES:
            self.too_many = True
            return
        for i, rel in enumerate(rels):
            if i % 256 == 0:
                _check_time(deadline)
            self.stamps[rel] = _stamp(root / rel)
            self.files[rel] = _parse_py(root / rel)
        # Deleted files still resolve, so their importers are linked.
        names = set(self.files) | {r for r in extra if r.endswith(".py")}
        self.by_dotted: dict[str, set[str]] = {}
        self.by_tail: dict[str, set[str]] = {}
        for rel in names:
            parts = list(PurePosixPath(rel).parts)
            parts[-1] = parts[-1][:-3]
            if parts[-1] == "__init__":
                parts.pop()
            for i in range(len(parts)):
                self.by_dotted.setdefault(".".join(parts[i:]), set()).add(rel)
            self.by_tail.setdefault(PurePosixPath(rel).name, set()).add(rel)
        self.names = names
        self._deps: dict[str, set[str]] = {}

    def unchanged(self, files: list[str] | None) -> bool:
        """Whether the project's Python files are the same set with the same
        size and mtime as when this was built (then it can be reused)."""
        if self.too_many:
            return False
        rels = _py_files(self.root, self.state, files)
        if set(rels) != set(self.stamps):
            return False
        return all(_stamp(self.root / r) == self.stamps[r] for r in rels)

    def opaque_in(self, files: set[str]) -> set[str]:
        return {r for r in files
                if (info := self.files.get(r)) is not None and info.opaque}

    def _rel_module(self, rel: str, module: str, level: int) -> list[str]:
        base = PurePosixPath(rel).parent
        for _ in range(level - 1):
            base = base.parent
        parts = [p for p in module.split(".") if p]
        d = base.joinpath(*parts) if parts else base
        return [f"{d.as_posix()}.py" if parts else "",
                (d / "__init__.py").as_posix()]

    def _absolute(self, module: str) -> set[str]:
        out: set[str] = set()
        parts = module.split(".")
        for i in range(1, len(parts) + 1):
            out |= self.by_dotted.get(".".join(parts[:i]), set())
        return out

    def deps(self, rel: str) -> set[str]:
        """Project files `rel` depends on directly."""
        got = self._deps.get(rel)
        if got is not None:
            return got
        out: set[str] = set()
        info = self.files.get(rel)
        p = PurePosixPath(rel).parent
        while p.as_posix() not in ("", "."):
            init = (p / "__init__.py").as_posix()
            if init in self.names and init != rel:
                out.add(init)
            p = p.parent
        if info is not None:
            for module, level, names in info.imports:
                if level:
                    cands = [c for c in self._rel_module(rel, module, level) if c]
                    pkg = PurePosixPath(cands[-1]).parent
                    for n in names:
                        cands += [(pkg / f"{n}.py").as_posix(),
                                  (pkg / n / "__init__.py").as_posix()]
                    out |= {c for c in cands if c in self.names}
                else:
                    out |= self._absolute(module)
                    for n in names:
                        if n != "*":
                            out |= self.by_dotted.get(f"{module}.{n}", set())
            for s in info.literals:
                s = s.strip().removeprefix("./")
                if "/" in s:
                    if s in self.names:
                        out.add(s)
                    elif s.endswith(".py"):
                        out |= {r for r in self.by_tail.get(PurePosixPath(s).name, ())
                                if r.endswith("/" + s)}
                else:
                    out |= self.by_dotted.get(s, set())
        out.discard(rel)
        self._deps[rel] = out
        return out

    def conftests(self, rel: str) -> list[str]:
        out = []
        parts = PurePosixPath(rel).parts[:-1]
        for i in range(len(parts) + 1):
            c = "/".join([*parts[:i], "conftest.py"])
            if c in self.files:
                out.append(c)
        return out

    def closure(self, test_rel: str) -> tuple[set[str], bool]:
        """(files the test file depends on, transitively, with its conftest
        chain; whether any of them has an unresolvable dynamic import)."""
        todo = [test_rel, *self.conftests(test_rel)]
        seen = set(todo)
        opaque = False
        while todo:
            r = todo.pop()
            info = self.files.get(r)
            if info is not None and info.opaque:
                opaque = True
            for d in self.deps(r):
                if d not in seen:
                    seen.add(d)
                    todo.append(d)
        return seen, opaque

    def key(self, test_rel: str, common: str) -> str | None:
        files, opaque = self.closure(test_rel)
        if opaque:
            return None
        h = hashlib.sha256(common.encode())
        for r in sorted(files):
            info = self.files.get(r)
            sha = info.sha if info is not None else None
            if sha is None:
                return None
            h.update(f"{r}\0{sha}\n".encode("utf-8", "surrogateescape"))
        return h.hexdigest()


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

def _empty() -> dict[str, Any]:
    return {"version": VERSION, "suites": {}, "files": {}, "nodes": {},
            "flaky": {}, "slow": {}, "failing": {}, "cargo": {}}


def _state_file(root: Path) -> Path:
    return state_path(root, "tests", "results.json")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _str_map(v: Any, ok: Any) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and ok(x)
                                       for k, x in v.items())


def _t_ok(rec: dict[str, Any]) -> bool:
    return "t" not in rec or _num(rec["t"])


def _valid_suite(r: Any) -> bool:
    return (isinstance(r, dict) and _t_ok(r)
            and isinstance(r.get("head"), str)
            and _str_map(r.get("dirty"), lambda x: x is None or isinstance(x, str))
            and _str_map(r.get("files"), lambda x: isinstance(x, int)
                         and not isinstance(x, bool))
            and all(r.get(k) is None or isinstance(r.get(k), str)
                    for k in ("py", "env")))


def _valid_file(r: Any) -> bool:
    return (isinstance(r, dict) and _t_ok(r) and isinstance(r.get("k"), str)
            and isinstance(r.get("nodes"), list)
            and all(isinstance(n, str) for n in r["nodes"]))


def _valid_node(r: Any) -> bool:
    return (isinstance(r, dict) and _t_ok(r) and isinstance(r.get("k"), str)
            and isinstance(r.get("o"), str)
            and ("s" not in r or _num(r["s"])))


def _valid_flaky(r: Any) -> bool:
    return (isinstance(r, dict) and _t_ok(r)
            and ("n" not in r or (isinstance(r["n"], int)
                                  and not isinstance(r["n"], bool))))


def _valid_slow(r: Any) -> bool:
    return isinstance(r, dict) and _t_ok(r) and _num(r.get("s"))


def _valid_failing(r: Any) -> bool:
    return (isinstance(r, dict) and _t_ok(r)
            and _str_map(r.get("files"), _num))


def _valid_cargo_suite(r: Any) -> bool:
    return (isinstance(r, dict) and _t_ok(r)
            and isinstance(r.get("head"), str)
            and _str_map(r.get("dirty"), lambda x: x is None or isinstance(x, str))
            and _str_map(r.get("targets"), lambda x: isinstance(x, int)
                         and not isinstance(x, bool))
            and _str_map(r.get("passed"), lambda x: isinstance(x, int)
                         and not isinstance(x, bool))
            and _str_map(r.get("ignored"), lambda x: isinstance(x, int)
                         and not isinstance(x, bool))
            and _str_map(r.get("keys"), lambda x: isinstance(x, str))
            and all(r.get(k) is None or isinstance(r.get(k), str)
                    for k in ("cargo", "env", "manifests", "features")))


_VALID = {"suites": _valid_suite, "files": _valid_file, "nodes": _valid_node,
          "flaky": _valid_flaky, "slow": _valid_slow, "failing": _valid_failing,
          "cargo": _valid_cargo_suite}


def load_state(root: Path) -> dict[str, Any]:
    """The stored results; entries of the wrong shape are dropped, so a
    hand-edited or older file degrades to full runs instead of an error."""
    try:
        data = json.loads(_state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return _empty()
    out = _empty()
    for name, ok in _VALID.items():
        table = data.get(name)
        if isinstance(table, dict):
            out[name] = {k: v for k, v in table.items()
                         if isinstance(k, str) and ok(v)}
    return out


def _prune(table: dict[str, Any], limit: int) -> None:
    if len(table) <= limit:
        return

    def t(k: str) -> float:
        v = table[k].get("t") if isinstance(table[k], dict) else None
        return float(v) if _num(v) else 0.0
    order = sorted(table, key=t)
    for k in order[:len(table) - limit]:
        del table[k]


def _write_state(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[dict[str, Any]]:
    """The state under a lock, written back after the block. When the
    block or the write fails, the state is reset to empty (full runs
    from then on) rather than left as it was, and the error is raised."""
    path = _state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(path.parent / ".lock", "a+") as lk:
        try:
            import fcntl
            fcntl.flock(lk, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        data = load_state(root)
        try:
            yield data
            _prune(data["suites"], MAX_SUITES)
            _prune(data["files"], MAX_FILES)
            _prune(data["nodes"], MAX_NODES)
            _prune(data["flaky"], MAX_FLAKY)
            _prune(data["slow"], MAX_SLOW)
            _prune(data["failing"], MAX_SUITES)
            _prune(data["cargo"], MAX_SUITES)
            _write_state(path, data)
        except Exception:
            try:
                _write_state(path, _empty())
            except Exception:  # noqa: BLE001 - the original error is raised
                with contextlib.suppress(OSError):
                    path.unlink()
            raise


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #

@dataclass
class Plan:
    """What a captured pytest runs, and what to record after it."""
    root: Path
    select: str
    cache: bool
    suite: str
    run_all_cmd: str
    early_stop: bool
    # None: the command as typed.
    selectors: list[str] | None = None
    reason: str | None = None            # why everything runs
    forced: bool = False                 # selection was asked for, all ran
    selected: list[str] = field(default_factory=list)   # test files
    not_run: dict[str, int] = field(default_factory=dict)
    cached: dict[str, dict[str, int]] = field(default_factory=dict)
    cached_since: dict[str, float] = field(default_factory=dict)
    whole_files: set[str] = field(default_factory=set)
    keys: dict[str, str] = field(default_factory=dict)
    key_inputs: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    python: str = ""
    env_hash: str = ""
    # Test files that failed in this suite's latest run; always selected.
    refailed: list[str] = field(default_factory=list)
    # The tree before a run of the command as typed: the green record is
    # written only when the tree after the run is the same.
    snapshot: dict[str, Any] | None = None
    can_be_green: bool = True

    @property
    def nothing_to_run(self) -> bool:
        """Every selected node is served from the cache (at least one)."""
        return (self.selectors is not None and not self.selectors
                and bool(self.cached))


def _run_all_cmd(inp: dict[str, Any], cls: dict[str, Any]) -> str:
    command = str(inp.get("command") or "pytest")
    raw = cls.get("_raw")
    prefix = f"{ENV_ALL}=1 "
    if isinstance(raw, str) and raw and raw in command:
        return command.replace(raw, prefix + raw.lstrip(), 1)
    if "cd" in cls and "&&" in command:
        left, right = command.split("&&", 1)
        return f"{left}&& {prefix}{right.strip()}"
    return prefix + command


def _suite_id(python: str, scope: list[str], cls: dict[str, Any],
              args: list[str]) -> str:
    env = {k: v for k, v in (cls.get("env") or {}).items() if k != ENV_ALL}
    blob = json.dumps([python, sorted(scope), sorted(env.items()), args])
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _is_test_name(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name.endswith(".py") and (name.startswith("test_")
                                     or name.endswith("_test.py"))


def _in_scope(rel: str, scope: list[str]) -> bool:
    return not scope or any(s in (".", "") or rel == s or rel.startswith(s.rstrip("/") + "/")
                            for s in scope)


def _plan_budget(inp: dict[str, Any]) -> float:
    """Seconds planning may take: `MAX_PLAN_S`, and at most `PLAN_SHARE` of
    the caller's remaining time."""
    budget = MAX_PLAN_S
    remaining = inp.get("_deadline_s_remaining")
    if _num(remaining):
        budget = min(budget, max(0.0, float(remaining)) * PLAN_SHARE)
    return budget


def _run_as_typed(p: Plan, reason: str | None, *, keep_keys: bool) -> None:
    p.selectors = None
    p.reason = reason
    p.forced = p.select == "affected"
    p.selected, p.not_run, p.refailed = [], {}, []
    p.cached, p.cached_since, p.whole_files = {}, {}, set()
    if not keep_keys:
        p.keys, p.key_inputs = {}, {}


def plan(memory: Any, cls: dict[str, Any], inp: dict[str, Any], *,
         root: Path, python: str, selectors: list[str],
         env: dict[str, str] | None) -> Plan | None:
    """The plan for a captured pytest, or None when neither feature is on or
    the command is not one this module changes (see the module docstring)."""
    select, cache = _settings(root)
    if select == "all" and not cache:
        return None
    scope = []
    for s in selectors:
        if "::" in s or not (root / s).is_dir():
            return None
        scope.append(s.rstrip("/") or ".")
    args, why, early = _args(list(cls.get("extra_args") or []))
    if why is not None:
        return None
    eff_env = dict(env if isinstance(env, dict) else os.environ)
    p = Plan(root=root, select=select, cache=cache,
             suite=_suite_id(python, scope, cls, args),
             run_all_cmd=_run_all_cmd(inp, cls), early_stop=early,
             python=python, env_hash=_env_hash(eff_env))
    p.state = load_state(root)
    budget = _plan_budget(inp)
    try:
        _plan(p, memory, cls, inp, scope, args, eff_env,
              time.monotonic() + budget)
    except _OutOfTime:
        _run_as_typed(p, f"planning took longer than {budget:g} s",
                      keep_keys=False)
    if sum(len(s) + 1 for s in p.selectors or []) > MAX_SELECTOR_CHARS:
        _run_as_typed(p, "too many tests to name on the command line",
                      keep_keys=True)
    if p.selectors is None and p.can_be_green:
        p.snapshot = _snapshot(root, python)
    return p


def _plan(p: Plan, memory: Any, cls: dict[str, Any], inp: dict[str, Any],
          scope: list[str], args: list[str], eff_env: dict[str, str],
          deadline: float) -> None:
    root, select, cache, state = p.root, p.select, p.cache, p.state
    srel = state_rel(root)
    _check_time(deadline)
    top = _below_toplevel(root)
    if top is not None:
        p.can_be_green = False
        return _run_as_typed(
            p, f"the root is below the git top level {top}, so changes "
               "outside it are not seen", keep_keys=False)
    files = _git_files(root, srel)

    def everything(reason: str, proj: _Project | None = None) -> None:
        _run_as_typed(p, reason, keep_keys=False)
        if cache:
            # Keys for every test file, so this run fills the cache.
            if proj is None:
                proj = _Project(root, srel, files=files, deadline=deadline)
            if not proj.too_many:
                p.selected = sorted(
                    f for f in proj.files
                    if (_is_test_name(f) or f in state["files"])
                    and _in_scope(f, scope))
                _key_inputs(p, proj, eff_env, args, files)
                p.key_inputs["project"] = proj
                p.selected = []

    if (cls.get("env") or {}).get(ENV_ALL) == "1" or eff_env.get(ENV_ALL) == "1":
        return everything(f"{ENV_ALL}=1")
    cfg_why = _config_reason(root)
    if cfg_why:
        return everything(cfg_why)
    base = state["suites"].get(p.suite)
    if base is None:
        return everything("no fully green run of this suite recorded yet")
    head = base["head"]
    if _git(root, "cat-file", "-e", f"{head}^{{commit}}") is None:
        return everything("the commit of the last green run is not available")
    pyfp = _python_fingerprint(p.python)
    if pyfp is None or base.get("py") != pyfp:
        return everything("the interpreter or its installed packages changed "
                          "since the last green run")
    if base.get("env") != p.env_hash:
        return everything("the environment changed since the last green run")
    _check_time(deadline)
    dirty = _dirty_since(root, head)
    if dirty is None:
        return everything("git cannot list the changed files")
    tracked, untracked = dirty
    if len(untracked) > MAX_UNTRACKED:
        return everything(f"more than {MAX_UNTRACKED} untracked files")
    recorded: dict[str, str | None] = base["dirty"]
    changed: list[str] = []
    for rel in sorted(set(tracked) | set(untracked) | set(recorded)):
        if _excluded(rel, srel):
            continue
        now = _sha(root / rel)
        if rel in recorded and recorded[rel] == now:
            continue
        changed.append(rel)
    if len(changed) > MAX_CHANGED:
        return everything(f"{len(changed)} files changed since the last green run")
    for rel in changed:
        if PurePosixPath(rel).name == "conftest.py":
            return everything(f"conftest.py changed: {rel}")
    for rel in changed:
        if not rel.endswith(".py"):
            return everything(f"non-Python file changed: {rel}")

    _check_time(deadline)
    gone = {r for r in changed if not (root / r).exists()}
    proj = _Project(root, srel, extra=gone, files=files, deadline=deadline)
    if proj.too_many:
        return everything(f"more than {MAX_PY_FILES} Python files", proj)
    green_files: dict[str, int] = dict(base["files"])
    green_dirs = {PurePosixPath(f).parent.as_posix() for f in green_files}
    candidates = {f for f in green_files if f in proj.files}
    for rel in changed:
        if rel in proj.files and _is_test_name(rel) and rel not in candidates:
            if (PurePosixPath(rel).parent.as_posix() in green_dirs
                    and _in_scope(rel, scope)):
                candidates.add(rel)
            else:
                return everything(f"new test file outside the known test "
                                  f"directories: {rel}", proj)
    failing = (state["failing"].get(p.suite) or {}).get("files") or {}
    refailed = {f for f in failing if f in proj.files and _in_scope(f, scope)}
    candidates |= refailed
    changed_py = [r for r in changed if r.endswith(".py")]
    linked: set[str] = set()
    affected: set[str] = set()
    opaque_closures: set[str] = set()
    for i, t in enumerate(sorted(candidates)):
        if i % 64 == 0:
            _check_time(deadline)
        files_t, opaque = proj.closure(t)
        hit = files_t.intersection(changed_py)
        if hit or (opaque and changed_py):
            affected.add(t)
            linked |= hit
        if opaque:
            opaque_closures |= files_t
    # A captured pytest runs outside the daemon's graph lock; the graph is
    # read under it (`_lock`, set by the daemon for lock-free calls).
    with inp.get("_lock") or contextlib.nullcontext():
        graph = _graph_tests(memory, changed_py)
    for rel, tests in graph.items():
        got = tests & candidates
        if got:
            affected |= got
            linked.add(rel)
    for rel in changed_py:
        if rel in linked or rel in candidates:
            continue
        if rel in gone and rel in green_files:
            continue
        return everything(f"no test is linked to {rel}", proj)
    affected |= refailed

    if select == "all" or not affected:
        if select == "affected":
            reason = ("no files changed since the last green run"
                      if not changed else "no test is affected by the changes")
            if not cache:
                return everything(reason, proj)
            p.forced, p.reason = True, reason
        affected = set(candidates)

    p.selected = sorted(affected)
    p.refailed = sorted(refailed)
    p.not_run = {f: n for f, n in green_files.items()
                 if f not in affected and f in proj.files}
    if cache:
        _key_inputs(p, proj, eff_env, args, files)
        _apply_cache(p, proj)
    else:
        p.selectors = list(p.selected)
        p.whole_files = set(p.selected)
    p.key_inputs["project"] = proj
    # Every candidate runs, whole, and none is cached: that is the command
    # as typed, so run it as typed (a full run; the green run can move).
    if (not p.cached and not p.not_run and p.whole_files == set(p.selected)
            and set(p.selectors or []) == p.whole_files):
        reason = p.reason
        if select == "affected" and not p.forced:
            opaque = sorted(proj.opaque_in(opaque_closures))
            reason = "every test file is affected by the changes"
            if opaque:
                reason += (f" (an unresolved dynamic import in "
                           f"{_listed(opaque)} links every change to the "
                           "tests that load it)")
        elif not p.selected and reason is None:
            reason = "no test file of the last green run is left"
        _run_as_typed(p, reason, keep_keys=True)
    return None


def _snapshot(root: Path, python: str) -> dict[str, Any] | None:
    """HEAD, the dirty files' hashes and the interpreter fingerprint, or
    None when git cannot tell."""
    head = _head(root)
    if head is None:
        return None
    dirty = _dirty_since(root, head)
    if dirty is None:
        return None
    srel = state_rel(root)
    rels = sorted(r for r in set(dirty[0]) | set(dirty[1]) if not _excluded(r, srel))
    if len(rels) > MAX_CHANGED or len(dirty[1]) > MAX_UNTRACKED:
        return None
    return {"head": head, "dirty": {r: _sha(root / r) for r in rels},
            "py": _python_fingerprint(python)}


def _graph_tests(memory: Any, changed: list[str]) -> dict[str, set[str]]:
    """The test files of the graph's covering tests of each changed file."""
    out: dict[str, set[str]] = {}
    try:
        from ozagia.intent.common import covering_tests, file_nid
        for rel in changed:
            fid = file_nid(memory, rel)
            if fid is None:
                continue
            files = set()
            for tid in covering_tests(memory, fid):
                f = (memory.node(tid) or {}).get("file")
                if f:
                    files.add(str(f))
            out[rel] = files
    except Exception:  # noqa: BLE001 - the import walk decides alone
        return {}
    return out


def _common_key(root: Path, python: str, env: dict[str, str],
                args: list[str], state: str | None,
                files: list[str] | None = None) -> str | None:
    pyfp = _python_fingerprint(python)
    nonpy = _nonpy_fingerprint(root, state, files)
    if pyfp is None or nonpy is None:
        return None
    cfg = [(n, _sha(root / n) or "-") for n in PYTEST_CONFIG_FILES]
    return json.dumps([VERSION, pyfp, nonpy, cfg, _env_hash(env), args])


def _key_inputs(p: Plan, proj: _Project, env: dict[str, str],
                args: list[str], files: list[str] | None) -> None:
    python = p.python
    p.key_inputs.update(python=python, env=env, args=args)
    common = _common_key(p.root, python, env, args, state_rel(p.root), files)
    p.key_inputs["common"] = common
    if common is None:
        return
    names = set(p.selected) | {f for f in p.state["files"] if f in proj.files}
    for f in names:
        k = proj.key(f, common)
        if k is not None:
            p.keys[f] = k


def _apply_cache(p: Plan, proj: _Project) -> None:
    nodes = p.state["nodes"]
    flaky = p.state["flaky"]
    sel: list[str] = []
    for f in p.selected:
        k = p.keys.get(f)
        entry = p.state["files"].get(f)
        if (k is None or not isinstance(entry, dict) or entry.get("k") != k
                or not isinstance(entry.get("nodes"), list)):
            sel.append(f)
            p.whole_files.add(f)
            continue
        cached = {"passed": 0, "skipped": 0}
        rerun: list[str] = []
        since = 0.0
        for nid in entry["nodes"]:
            rec = nodes.get(nid)
            if (isinstance(rec, dict) and rec.get("k") == k
                    and rec.get("o") in ("passed", "skipped") and nid not in flaky):
                cached[rec["o"]] += 1
                since = max(since, float(rec.get("t") or 0))
            else:
                rerun.append(nid)
        if rerun and not all(_SAFE_NODEID.match(n) for n in rerun):
            sel.append(f)
            p.whole_files.add(f)
            continue
        sel.extend(rerun)
        if cached["passed"] or cached["skipped"]:
            p.cached[f] = cached
            p.cached_since[f] = since
    p.selectors = sel


# --------------------------------------------------------------------------- #
# after the run
# --------------------------------------------------------------------------- #

def _listed(files: list[str]) -> str:
    shown = ", ".join(files[:MAX_LISTED])
    more = f", +{len(files) - MAX_LISTED} more" if len(files) > MAX_LISTED else ""
    return f"{shown}{more}"


def _record(p: Plan, res: dict[str, Any] | None, *, full: bool) -> list[str]:
    """Store the run's outcomes; return the slow and flaky lines."""
    from ozagia.intent.common import tag
    now = time.time()
    lines: list[str] = []
    if res is None:
        return lines
    usable = res.get("status") in ("passed", "failed")
    tests = res.get("tests") or []
    bad_files = {e.get("file") for e in res.get("collection_errors") or []}
    proj: _Project | None = p.key_inputs.get("project")
    post: dict[str, str] = {}
    if p.cache and usable and proj is not None and p.key_inputs.get("common"):
        srel = state_rel(p.root)
        files = _git_files(p.root, srel)
        fresh = proj if proj.unchanged(files) else _Project(p.root, srel,
                                                            files=files)
        common = _common_key(p.root, p.key_inputs["python"],
                             p.key_inputs["env"], p.key_inputs["args"], srel,
                             files)
        if common is not None and common == p.key_inputs["common"]:
            for f in {t["file"] for t in tests}:
                k = fresh.key(f, common)
                if k is not None and p.keys.get(f) == k:
                    post[f] = k
    passed_as_typed = (full and res.get("exit_code") == 0
                       and res.get("status") == "passed")
    green: dict[str, Any] | None = None
    if passed_as_typed and p.snapshot is not None:
        after = _snapshot(p.root, p.python)
        if after == p.snapshot:
            green = after
        else:
            lines.append(tag("green run not recorded: files or the interpreter "
                             "changed during the run"))
    complete = not (p.early_stop and res.get("exit_code") != 0)
    slow_now: list[tuple[str, float]] = []
    flaky_now: list[str] = []
    with _locked(p.root) as data:
        by_file: dict[str, list[str]] = {}
        for t in tests:
            nid = t.get("nodeid")
            if not nid:
                continue
            secs = float(t.get("seconds") or 0)
            if secs > SLOW_S:
                data["slow"][nid] = {"s": round(secs, 2), "t": now}
                slow_now.append((nid, secs))
            k = post.get(t["file"])
            if k is None or t["file"] in bad_files:
                continue
            by_file.setdefault(t["file"], []).append(nid)
            old = data["nodes"].get(nid)
            out = t["outcome"]
            if (isinstance(old, dict) and old.get("k") == k
                    and {old.get("o"), out} in ({"passed", "failed"},
                                                {"passed", "error"})):
                prev = data["flaky"].get(nid) or {}
                data["flaky"][nid] = {"k": k, "t": now,
                                      "n": int(prev.get("n") or 0) + 1}
            data["nodes"][nid] = {"k": k, "o": out,
                                  "s": round(float(t.get("seconds") or 0), 3),
                                  "t": now}
            if nid in data["flaky"]:
                flaky_now.append(nid)
        for f, nids in by_file.items():
            if complete and (p.selectors is None or f in p.whole_files):
                data["files"][f] = {"k": post[f], "nodes": nids, "t": now}
        if usable:
            _failing(p, data, res, complete, passed_as_typed, now)
        if green is not None:
            _green(p, data, tests, now, green)
    for nid, secs in sorted(slow_now, key=lambda x: -x[1])[:MAX_LISTED]:
        lines.append(tag(f"slow test (> {SLOW_S:g} s): {nid} {secs:.1f} s"))
    if len(slow_now) > MAX_LISTED:
        lines.append(tag(f"slow tests: +{len(slow_now) - MAX_LISTED} more"))
    for nid in flaky_now[:MAX_LISTED]:
        lines.append(tag(f"flaky test: {nid} (passed and failed with the "
                         "same inputs; never served from the cache)"))
    if len(flaky_now) > MAX_LISTED:
        lines.append(tag(f"flaky tests: +{len(flaky_now) - MAX_LISTED} more"))
    return lines


def _failing(p: Plan, data: dict[str, Any], res: dict[str, Any],
             complete: bool, passed_as_typed: bool, now: float) -> None:
    """Remember the suite's test files that failed in this run, and forget
    those that ran to the end without a failure."""
    tests = res.get("tests") or []
    bad = {t.get("file") for t in tests if t.get("outcome") in ("failed", "error")}
    bad |= {e.get("file") for e in res.get("collection_errors") or []}
    bad = {f for f in bad if isinstance(f, str) and f}
    old = (data["failing"].get(p.suite) or {}).get("files") or {}
    fails: dict[str, float] = {} if passed_as_typed else dict(old)
    if complete:
        for f in {t.get("file") for t in tests} - bad:
            fails.pop(f, None)
    for f in bad:
        fails[f] = now
    if fails:
        data["failing"][p.suite] = {"files": fails, "t": now}
    else:
        data["failing"].pop(p.suite, None)


def _green(p: Plan, data: dict[str, Any], tests: list[dict[str, Any]],
           now: float, snap: dict[str, Any]) -> None:
    files: dict[str, int] = {}
    for t in tests:
        if t.get("file"):
            files[t["file"]] = files.get(t["file"], 0) + 1
    data["suites"][p.suite] = {**snap, "env": p.env_hash, "files": files,
                               "t": now}


def _tally(p: Plan, res: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """(tests run, cached passes, cached skips, tests not run)."""
    ran = len((res or {}).get("tests") or [])
    cached_p = sum(c["passed"] for c in p.cached.values())
    cached_s = sum(c["skipped"] for c in p.cached.values())
    return ran, cached_p, cached_s, sum(p.not_run.values())


def scope(p: Plan | None, res: dict[str, Any] | None) -> str:
    """The head line's scope, `" (affected subset: 2 of 4 tests run, 2 not
    run)"`, or "" for a run of the command as typed."""
    if p is None or p.selectors is None:
        return ""
    ran, cached_p, cached_s, not_run = _tally(p, res)
    kind = ("affected" if p.select == "affected" and not p.forced
            else "cached")
    parts = [f"{ran} of {ran + cached_p + cached_s + not_run} tests run"]
    if cached_p or cached_s:
        parts.append(f"{cached_p + cached_s} cached")
    if not_run:
        parts.append(f"{not_run} not run")
    return f" ({kind} subset: {', '.join(parts)})"


def finish(p: Plan, res: dict[str, Any] | None) -> tuple[list[str], list[str],
                                                         dict[str, Any]]:
    """(lines after the pytest head, lines at the end, meta) for a run made
    by `p`; `res` is `check.run_tests`'s result, None when nothing ran."""
    from ozagia.intent.common import tag
    head: list[str] = []
    counts = {"affected_runs": 0, "tests_skipped_unaffected": 0,
              "cached_passes": 0, "full_runs_forced": 0}
    full = p.selectors is None
    if full:
        if p.forced:
            counts["full_runs_forced"] = 1
        if p.select == "affected" or p.reason:
            head.append(tag(f"ran all tests: {p.reason}"))
    else:
        ran, cached_p, cached_s, not_run = _tally(p, res)
        total = ran + cached_p + cached_s + not_run
        word = ("affected tests" if p.select == "affected" and not p.forced
                else "tests")
        parts = [f"ran {ran} {word} of {total}"]
        if cached_p or cached_s:
            parts.append(f"cached: {cached_p} passed"
                         + (f", {cached_s} skipped" if cached_s else ""))
        if not_run:
            nf = len(p.not_run)
            parts.append(f"not run: {not_run} test{'s' * (not_run != 1)} in "
                         f"{nf} file{'s' * (nf != 1)} "
                         f"({_listed(sorted(p.not_run))})")
        parts.append(f"run all: {p.run_all_cmd}")
        head.append(tag("; ".join(parts)))
        if p.forced and p.reason:
            head.append(tag(f"selection off for this run: {p.reason}"))
        if p.refailed:
            head.append(tag("run again, failed in the last run: "
                            f"{_listed(p.refailed)}"))
        for f in sorted(p.cached)[:MAX_CACHED_LINES]:
            c = p.cached[f]
            since = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(p.cached_since.get(f) or 0))
            head.append(tag(
                f"cached pass {f}: {c['passed']} passed"
                + (f", {c['skipped']} skipped" if c["skipped"] else "")
                + f" (inputs unchanged since {since})"))
        if len(p.cached) > MAX_CACHED_LINES:
            head.append(tag(f"cached pass: +{len(p.cached) - MAX_CACHED_LINES}"
                            " more files"))
        if p.select == "affected" and not p.forced:
            counts["affected_runs"] = 1
        counts["tests_skipped_unaffected"] = not_run
        counts["cached_passes"] = cached_p
    try:
        tail = _record(p, res, full=full)
    except Exception as e:  # noqa: BLE001 - the run's own lines still stand
        tail = [tag(f"warning: test results not stored: {e}")]
    return head, tail, {"tests": counts}


def merge_meta(metas: list[dict[str, Any]]) -> dict[str, Any]:
    """The `tests` counters of several runs (a compound's pytest steps)."""
    out: dict[str, int] = {}
    for m in metas:
        for k, v in ((m or {}).get("tests") or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    return {"tests": out} if out else {}


__all__ = ["ENV_ALL", "Plan", "finish", "load_state", "merge_meta", "plan",
           "scope"]
