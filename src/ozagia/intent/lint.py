"""ozagia.intent.lint — lint the lines an edit changed.

After a handled edit, key edit or write (`ozagia.intent.edit`), and in
`after` for a built-in edit, write or patch (`ozagia.intent.after`), the
project's linter runs on the changed files only, and the findings that fall
on the changed lines are reported:

    [ozagia] lint <rule> L<n> <key>: <message>
    [ozagia] lint: <k> more findings on the changed lines
    [ozagia] lint: <linter> timed out after <s> s

At most `MAX_LINES` finding lines are shown. Linters:

- ruff (`ruff check --output-format json --no-fix --no-cache
  --force-exclude`) for `.py` files, when ruff is configured: a
  `pyproject.toml` with `[tool.ruff]`, or a `ruff.toml`/`.ruff.toml`, in the
  file's directory or one above it up to the root, or `[lint] python =
  "ruff"` in `.ozagia.toml`. The executable is `ruff` on PATH.
- rustfmt (`rustfmt --check --edition <edition> --color never`, the file's
  text on stdin, run in the file's directory so its `rustfmt.toml` applies
  and out-of-line child modules are not checked) for `.rs` files, when a
  `Cargo.toml` is in the file's directory or above it (its `[package]
  edition`, or the workspace's for `edition.workspace = true`; Cargo's
  default 2015 when unset), or `[lint] rust = "rustfmt"`; rustfmt must be
  on PATH. A hunk rustfmt would change is reported as `lint rustfmt L<a>
  <key>: rustfmt would reformat L<a>-<b>`.

Trust: lint runs on edit permission alone, so it never executes a program
from inside the repository unless the user opts in outside it. A linter
whose path or real path lies inside the root is skipped (PATH entries that
are relative or empty are ignored), and `<root>/.venv/bin/ruff` is not
considered. `OZAGIA_LINT_TRUST_VENV=1` in the daemon's environment, or
`[lint] trust_project_venv = true` in the user-level
`$XDG_CONFIG_HOME/ozagia/config.toml` (default `~/.config/ozagia/`; ignored
when it lies inside the root), lifts that: `<root>/.venv/bin/ruff` is then
preferred and in-root PATH entries are accepted. A repository's own
`.ozagia.toml` cannot enable it. Linters run with the environment of a
captured pytest (`_test_env`, else this process's environment without
`OZAGIA_*`, `PYTHONPATH` and `COLUMNS`).

`.ozagia.toml`: `[lint] enabled = false` turns this off; `enabled = true`
lints Python and Rust files even without a ruff or Cargo configuration
(`python = "off"` / `rust = "off"` still exclude a language). A
configuration file that does not load turns lint off. `timeout_s` (default
3, at most `MAX_TIMEOUT_S` = 10 whatever the file says) is the budget of
all linter runs of one call; it is further cut to what is left before the
caller stops waiting (`_reply_deadline`, `_deadline_s_remaining`) less
`DEADLINE_MARGIN_S`. Under the daemon the linters run with the graph lock
released (`_unlock`); the lock is taken again for the node keys. Linters
never modify files and write no cache. A missing linter, a linter that
fails or output that does not parse adds nothing; nothing here fails the
edit.

Changed lines: a handled call diffs the file's text before and after
(`changed_lines`); a new file counts as changed everywhere. In `after`
(`after_changes`): a write lints the whole file; an edit the lines its
`newString` occupies now when it occurs exactly once in the file (every
occurrence for `replaceAll`, up to `MAX_OCCURRENCES`). Before that, a
single edit is undone at each occurrence of its `newString`; when exactly
one result has the sha256 the graph had for the file before the refresh,
the edit's changed lines are exact. Otherwise the line ranges
of the added and changed keys of the graph delta (narrowed to the
occurrences inside them, when there are any); a Codex patch the lines its
chunks added (located like Codex's own matcher) and every line of an added
file. Where those cannot be found, the graph delta's ranges stand in. A
finding counts when any line of its span (`location` to `end_location`)
is a changed line.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ozagia.intent.common import Keyer, one_line, read_text, root_of, split_lines, tag

# Finding lines shown per call.
MAX_LINES = 10
# Files linted per call.
MAX_FILES = 50
# Occurrences of an edit's newString located in `after` (replaceAll).
MAX_OCCURRENCES = 20
DEFAULT_TIMEOUT_S = 3.0
# The most a call's linters may take, whatever `[lint] timeout_s` says.
MAX_TIMEOUT_S = 10.0
# Time left for the rest of the answer when the budget follows a deadline.
DEADLINE_MARGIN_S = 1.0
# Below this budget no linter starts.
MIN_BUDGET_S = 0.05
# How long to wait for a killed linter's pipes to close.
KILL_WAIT_S = 1.0
# ruff's own precedence within one directory.
RUFF_CONFIGS = (".ruff.toml", "ruff.toml")
TRUST_ENV = "OZAGIA_LINT_TRUST_VENV"
_RUSTFMT_HEAD_RE = re.compile(r"^Diff in (.+?)(?::(\d+):| at line (\d+):)\s*$")


# --------------------------------------------------------------------------- #
# Configuration and trust
# --------------------------------------------------------------------------- #
def _config(root: Path) -> tuple[Any, bool]:
    """(project config, loaded); a config that does not load is (None,
    False), which turns lint off."""
    from ozagia.config import load_project_config
    try:
        return load_project_config(root), True
    except Exception:  # noqa: BLE001 - a broken config: lint off
        return None, False


def _within(root: Path, path: str | Path) -> bool:
    real = Path(os.path.realpath(path))
    root_real = Path(os.path.realpath(root))
    return real == root_real or root_real in real.parents


def user_config_path() -> Path:
    """`$XDG_CONFIG_HOME/ozagia/config.toml` (default `~/.config/...`)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "ozagia" / "config.toml"


def trust_project_venv(root: Path) -> bool:
    """Whether the user allowed linters from inside the repository:
    `OZAGIA_LINT_TRUST_VENV=1` in this process's environment, or `[lint]
    trust_project_venv = true` in the user-level config (not one inside the
    root)."""
    if os.environ.get(TRUST_ENV, "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        path = user_config_path()
        if not path.is_file() or _within(root, path):
            return False
    except (OSError, RuntimeError):
        return False
    data = _toml(path) or {}
    table = data.get("lint")
    return isinstance(table, dict) and table.get("trust_project_venv") is True


def find_executable(root: Path, name: str, *, trusted: bool,
                    venv: bool = False) -> str | None:
    """The first `name` on PATH (absolute entries only) that is an
    executable file and, unless `trusted`, neither lies nor resolves inside
    the root; with `trusted` and `venv`, `<root>/.venv/bin/<name>` first.
    The path is returned as found (a rustup proxy needs its own name)."""
    cands: list[Path] = []
    if trusted and venv:
        cands.append(root / ".venv" / "bin" / name)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.isabs(d):
            cands.append(Path(d) / name)
    for c in cands:
        try:
            real = os.path.realpath(c)
            if not (os.path.isfile(real) and os.access(real, os.X_OK)):
                continue
            if not trusted and (_within(root, real) or _within(root, c.parent)):
                continue
        except OSError:
            continue
        return str(c)
    return None


def lint_env(inp: dict[str, Any] | None) -> dict[str, str]:
    """The linters' environment: the caller's `_test_env`, else this
    process's without the variables meant for ozagia itself."""
    env = (inp or {}).get("_test_env")
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    from ozagia.daemon.server import captured_test_env
    return captured_test_env()


def _ancestors(root: Path, rel: str) -> list[Path]:
    """The file's directory and each one above it, up to the root."""
    out: list[Path] = []
    d = root.joinpath(*PurePosixPath(rel).parent.parts)
    while True:
        out.append(d)
        if d == root or root not in d.parents:
            break
        d = d.parent
    return out


def _toml(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def ruff_config(root: Path, rel: str) -> Path | None:
    """The nearest ruff configuration of a file (see the module docstring)."""
    for d in _ancestors(root, rel):
        for name in RUFF_CONFIGS:
            if (d / name).is_file():
                return d / name
        py = d / "pyproject.toml"
        if py.is_file():
            data = _toml(py)
            if data and isinstance(data.get("tool"), dict) and "ruff" in data["tool"]:
                return py
    return None


def cargo_edition(root: Path, rel: str) -> str | None:
    """The edition of the nearest Cargo.toml above a file, or None if none."""
    manifest = None
    for d in _ancestors(root, rel):
        if (d / "Cargo.toml").is_file():
            manifest = d / "Cargo.toml"
            break
    if manifest is None:
        return None
    data = _toml(manifest) or {}
    pkg = data.get("package") if isinstance(data.get("package"), dict) else {}
    ed = pkg.get("edition")
    if isinstance(ed, str):
        return ed
    if isinstance(ed, dict) and ed.get("workspace") is True:
        d = manifest.parent.parent
        while d == root or root in d.parents:
            ws = d / "Cargo.toml"
            if ws.is_file():
                wdata = _toml(ws) or {}
                wpkg = ((wdata.get("workspace") or {}).get("package") or {})
                if isinstance(wpkg, dict) and isinstance(wpkg.get("edition"), str):
                    return wpkg["edition"]
            if d == root:
                break
            d = d.parent
    if not pkg:                 # a virtual workspace manifest
        ws_pkg = ((data.get("workspace") or {}).get("package") or {})
        if isinstance(ws_pkg, dict) and isinstance(ws_pkg.get("edition"), str):
            return ws_pkg["edition"]
    return "2015"


def ruff_executable(root: Path) -> str | None:
    """The ruff lint runs (see "Trust" in the module docstring)."""
    return find_executable(root, "ruff", trusted=trust_project_venv(root),
                           venv=True)


class Plan:
    """Which linter applies to which file of one root."""

    def __init__(self, root: Path):
        self.root = root
        cfg, loaded = _config(root)
        self.enabled = getattr(cfg, "lint_enabled", None) if loaded else False
        timeout = float(getattr(cfg, "lint_timeout_s", DEFAULT_TIMEOUT_S)
                        or DEFAULT_TIMEOUT_S)
        self.timeout_s = min(timeout, MAX_TIMEOUT_S)
        self.python = getattr(cfg, "lint_python", None)
        self.rust = getattr(cfg, "lint_rust", None)
        self._ruff: str | None | bool = False
        self._rustfmt: str | None | bool = False

    def ruff(self) -> str | None:
        if self._ruff is False:
            self._ruff = ruff_executable(self.root)
        return self._ruff  # type: ignore[return-value]

    def rustfmt(self) -> str | None:
        if self._rustfmt is False:
            self._rustfmt = find_executable(
                self.root, "rustfmt", trusted=trust_project_venv(self.root))
        return self._rustfmt  # type: ignore[return-value]

    def linter(self, rel: str) -> tuple[str, str | None] | None:
        """("ruff", None) or ("rustfmt", edition) for a file, or None."""
        if self.enabled is False:
            return None
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in (".py", ".pyi"):
            if self.python == "off":
                return None
            if (self.python == "ruff" or self.enabled is True
                    or ruff_config(self.root, rel) is not None):
                return ("ruff", None) if self.ruff() else None
            return None
        if suffix == ".rs":
            if self.rust == "off":
                return None
            edition = cargo_edition(self.root, rel)
            if edition is None and not (self.rust == "rustfmt"
                                        or self.enabled is True):
                return None
            return ("rustfmt", edition or "2021") if self.rustfmt() else None
        return None


def applies(memory: Any, rel: str) -> bool:
    """Whether a linter would run on `rel` (cheap: no linter runs)."""
    return Plan(root_of(memory)).linter(rel) is not None


# --------------------------------------------------------------------------- #
# Changed lines
# --------------------------------------------------------------------------- #
def changed_lines(before: str | None, after: str) -> set[int] | None:
    """1-based lines of `after` that differ from `before`; None (every line)
    for a new file. A deletion marks the lines on both sides of it."""
    if before is None:
        return None
    import difflib
    a, b = split_lines(before), split_lines(after)
    lo = 0
    while lo < len(a) and lo < len(b) and a[lo] == b[lo]:
        lo += 1
    hi_a, hi_b = len(a), len(b)
    while hi_a > lo and hi_b > lo and a[hi_a - 1] == b[hi_b - 1]:
        hi_a -= 1
        hi_b -= 1
    out: set[int] = set()
    sm = difflib.SequenceMatcher(None, a[lo:hi_a], b[lo:hi_b], autojunk=False)
    for op, _i1, _i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if j2 > j1:
            out.update(range(lo + j1 + 1, lo + j2 + 1))
        else:
            out.update(n for n in (lo + j1, lo + j1 + 1) if 1 <= n <= len(b))
    return out


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _occurrences(text: str, new: str, limit: int) -> list[set[int]]:
    """The lines of each occurrence of `new` in `text` (at most `limit`)."""
    out: list[set[int]] = []
    start = 0
    while len(out) < limit:
        i = text.find(new, start)
        if i < 0:
            break
        end = i + len(new)
        last = end - 1 if new.endswith("\n") and end > i else end
        out.append(set(range(_line_of(text, i), _line_of(text, max(last, i)) + 1)))
        start = end
    return out


def _patch_text(inp: dict[str, Any]) -> str | None:
    from ozagia.intent.patch import patch_text
    if isinstance(inp.get("patchText"), str):
        return inp["patchText"]
    return patch_text(inp)


def _patch_changes(root: Path, inp: dict[str, Any]
                   ) -> dict[str, set[int] | None]:
    """Per file of a Codex patch, the lines its chunks put there (as far
    as they can be found in the file as it is now)."""
    from ozagia.intent.patch import _resolve, parse_patch, seek_sequence, split_source
    text = _patch_text(inp)
    if not text:
        return {}
    ops = parse_patch(text).ops
    wd = inp.get("workdir") or inp.get("cwd")
    base = Path(str(wd)) if wd and Path(str(wd)).is_absolute() else root
    out: dict[str, set[int] | None] = {}
    for op in ops:
        if op.kind == "delete":
            continue
        _real, rel, why = _resolve(root, base, op.move_to or op.path)
        if why or rel is None:
            continue
        if op.kind == "add":
            out[rel] = None
            continue
        try:
            current = read_text(root / rel)
        except Exception:  # noqa: BLE001 - not a text file now
            continue
        lines = [t for t, _ in split_source(current)]
        got: set[int] = set()
        cursor, found = 0, True
        for ch in op.chunks:
            if ch.context is not None:
                r = seek_sequence(lines, [ch.context], cursor, False)
                if r is not None:
                    cursor = r[0] + 1
            if not ch.new:
                continue
            r = seek_sequence(lines, ch.new, cursor, ch.eof)
            if r is None:
                found = False
                continue
            at = r[0]
            ctx = {j for _i, j in ch.ctx_idx}
            added = [k for k in range(len(ch.new)) if k not in ctx]
            if added:
                got.update(at + k + 1 for k in added)
            else:                   # only deletions: the lines around them
                got.update(range(at + 1, at + len(ch.new) + 1))
            cursor = at + len(ch.new)
        if got or found:
            prev = out.get(rel, set())
            out[rel] = None if prev is None else prev | got
    return out


def _delta_lines(memory: Any, delta: Any) -> dict[str, set[int]]:
    """Line ranges of the added and changed keys of a graph delta."""
    if delta is None:
        return {}
    d = delta.to_dict() if hasattr(delta, "to_dict") else dict(delta)
    out: dict[str, set[int]] = {}
    for key in [*(d.get("added") or []), *(d.get("changed") or [])]:
        nid = memory.node_by_key(key)
        attrs = ((memory.node(nid) or {}).get("attrs") or {}) if nid is not None else {}
        rel, a, b = attrs.get("file"), attrs.get("start_line"), attrs.get("end_line")
        if isinstance(rel, str) and isinstance(a, int) and isinstance(b, int):
            out.setdefault(rel, set()).update(range(a, b + 1))
    return out


def file_shas(memory: Any, rels: Iterable[str]) -> dict[str, str]:
    """The sha256 the graph has for each file (taken before a refresh: the
    text a built-in edit started from)."""
    out: dict[str, str] = {}
    for rel in rels:
        try:
            nid = memory.node_by_key(f"file:{rel}")
            sha = ((memory.node(nid) or {}).get("attrs") or {}).get("sha256") \
                if nid is not None else None
        except Exception:  # noqa: BLE001 - no hash for this file
            sha = None
        if isinstance(sha, str) and sha:
            out[rel] = sha
    return out


def _verified_lines(data: bytes, old: str, new: str, replace_all: bool,
                    before_sha: str) -> set[int] | None:
    """The changed lines of a built-in edit (`old` -> `new`) when exactly
    one way of undoing it gives the text whose sha256 the graph had before
    the edit; None otherwise."""
    import hashlib
    old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
    cands: list[bytes] = []
    if replace_all:
        cands.append(data.replace(new_b, old_b))
    else:
        start = 0
        while len(cands) < MAX_OCCURRENCES:
            i = data.find(new_b, start)
            if i < 0:
                break
            cands.append(data[:i] + old_b + data[i + len(new_b):])
            start = i + len(new_b)
    hits = [c for c in cands if hashlib.sha256(c).hexdigest() == before_sha]
    if len(hits) != 1:
        return None
    try:
        return changed_lines(hits[0].decode("utf-8"), data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def after_changes(memory: Any, tool: str, inp: dict[str, Any],
                  files: Iterable[str], delta: Any = None,
                  before_sha: dict[str, str] | None = None
                  ) -> dict[str, set[int] | None]:
    """The changed lines of the files a built-in edit, write or patch
    changed (`files`: what the graph refresh found changed), for the files
    a linter applies to (see the module docstring). `before_sha`: the
    graph's sha256 of the files before the refresh (`file_shas`)."""
    from ozagia.intent.common import resolve
    root = root_of(memory)
    plan = Plan(root)
    pending = [f for f in files if plan.linter(f) is not None]
    if not pending:
        return {}
    out: dict[str, set[int] | None] = {}
    unknown: list[str] = []
    if tool in ("edit", "write"):
        try:
            rel = resolve(root, inp.get("path", inp.get("filePath"))).rel
        except Exception:  # noqa: BLE001 - no path: nothing to lint
            return {}
        if rel not in pending:
            return {}
        if tool == "write":
            out[rel] = None
        else:
            edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
            try:
                text = read_text(root / rel)
            except Exception:  # noqa: BLE001 - not a text file now
                return {}
            one = [e for e in edits if isinstance(e, dict)]
            sha = (before_sha or {}).get(rel)
            if (sha and len(one) == 1 and isinstance(one[0].get("oldString"), str)
                    and isinstance(one[0].get("newString"), str)
                    and one[0]["oldString"] and one[0]["newString"]):
                try:
                    exact = _verified_lines((root / rel).read_bytes(),
                                            one[0]["oldString"],
                                            one[0]["newString"],
                                            one[0].get("replaceAll") is True, sha)
                except OSError:
                    exact = None
                if exact is not None:
                    return {rel: exact} if exact else {}
            got: set[int] = set()
            whole = False
            vague = False           # some newString is not located exactly
            spots: set[int] = set()     # every occurrence of those
            for e in edits:
                if not isinstance(e, dict):
                    continue
                old, new = e.get("oldString"), e.get("newString")
                if old == "" and isinstance(new, str):
                    whole = True
                elif isinstance(new, str) and new:
                    if e.get("replaceAll") is True:
                        occ = _occurrences(text, new, MAX_OCCURRENCES + 1)
                        if 0 < len(occ) <= MAX_OCCURRENCES:
                            for s in occ:
                                got |= s
                            continue
                    else:
                        occ = _occurrences(text, new, 2)
                        if len(occ) == 1:
                            got |= occ[0]
                            continue
                    vague = True
                    for s in _occurrences(text, new, MAX_OCCURRENCES):
                        spots |= s
            if whole:
                out[rel] = None
            elif vague:
                by_delta = _delta_lines(memory, delta).get(rel) or set()
                narrowed = by_delta & spots
                lines = got | (narrowed or by_delta)
                if lines:
                    out[rel] = lines
            elif got:
                out[rel] = got
            else:
                unknown.append(rel)
    elif tool == "patch":
        got_patch = _patch_changes(root, inp)
        for rel, lines in got_patch.items():
            if rel not in pending:
                continue
            if lines is None or lines:
                out[rel] = lines
            else:
                unknown.append(rel)
    if unknown:
        by_delta = _delta_lines(memory, delta)
        for rel in unknown:
            if by_delta.get(rel):
                out[rel] = by_delta[rel]
    return out


# --------------------------------------------------------------------------- #
# Running the linters
# --------------------------------------------------------------------------- #
def run_bounded(argv: list[str], *, cwd: Path, env: dict[str, str] | None,
                timeout_s: float, stdin: bytes | None = None,
                merge_stderr: bool = False) -> tuple[int | None, bytes, bool]:
    """Run `argv` in a new session: (exit code, stdout, timed out). On
    timeout the whole process group is killed and the output so far is
    collected for at most `KILL_WAIT_S` (a descendant that left the group
    may still hold the pipes); the exit code is then None. Raises OSError
    when the program cannot start."""
    p = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        start_new_session=True)
    try:
        out, _err = p.communicate(stdin, timeout=max(timeout_s, MIN_BUDGET_S))
        return p.returncode, out or b"", False
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        p.kill()
    try:
        out, _err = p.communicate(timeout=KILL_WAIT_S)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        out = b""
        for f in (p.stdin, p.stdout, p.stderr):
            with contextlib.suppress(OSError, ValueError):
                if f is not None:
                    f.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=KILL_WAIT_S)
    return None, out or b"", True


def _run(argv: list[str], cwd: Path, timeout_s: float, env: dict[str, str],
         stdin: bytes | None = None) -> tuple[str | None, bool]:
    """(stdout, timed out); stdout None when the linter could not run or
    timed out."""
    try:
        _rc, out, timed_out = run_bounded(argv, cwd=cwd, env=env,
                                          timeout_s=timeout_s, stdin=stdin)
    except OSError:
        return None, False
    if timed_out:
        return None, True
    return out.decode("utf-8", errors="replace"), False


def _rel(root: Path, path: str, cwd: Path) -> str | None:
    p = Path(path)
    p = Path(os.path.realpath(p if p.is_absolute() else cwd / p))
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return None


def _ruff_findings(root: Path, stdout: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(stdout)
    except ValueError:
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        loc = d.get("location") or {}
        end = d.get("end_location") or {}
        rel = _rel(root, str(d.get("filename") or ""), root)
        row = loc.get("row") if isinstance(loc, dict) else None
        if rel is None or not isinstance(row, int):
            continue
        end_row = end.get("row") if isinstance(end, dict) else None
        if not isinstance(end_row, int) or isinstance(end_row, bool) or end_row < row:
            end_row = row
        out.append({"rel": rel, "line": row, "end": end_row,
                    "rule": str(d.get("code") or "syntax"),
                    "message": str(d.get("message") or "")})
    return out


def rustfmt_hunks(stdout: str, root: Path, cwd: Path,
                  stdin_rel: str | None = None) -> list[dict[str, Any]]:
    """`{rel, start, end}` per change of `rustfmt --check` output: each run
    of changed lines between context lines, as lines of the file as it is
    (the lines rustfmt would replace, or the line an insertion goes
    before). With `stdin_rel` (the file was piped through stdin), every
    hunk belongs to that file."""
    out: list[dict[str, Any]] = []
    rel: str | None = None
    group: list[int] = []
    line = 0
    replacing = False

    def close() -> None:
        if rel is not None and group:
            out.append({"rel": rel, "start": min(group), "end": max(group)})
        group.clear()

    for raw in stdout.splitlines():
        m = _RUSTFMT_HEAD_RE.match(raw)
        if m:
            close()
            rel = stdin_rel if stdin_rel is not None else _rel(root, m.group(1), cwd)
            line = int(m.group(2) or m.group(3))
            replacing = False
            continue
        if rel is None:
            continue
        if raw.startswith("-"):
            group.append(line)
            replacing = True
            line += 1
        elif raw.startswith("+"):
            if not replacing:       # a pure insertion: before this line
                group.append(line)
        else:
            close()
            replacing = False
            line += 1
    close()
    return out


def lint_budget(timeout_s: float, inp: dict[str, Any] | None) -> float:
    """`timeout_s` cut to what is left before the caller stops waiting
    (`_reply_deadline`, a `time.monotonic()` value, and
    `_deadline_s_remaining`), less `DEADLINE_MARGIN_S`."""
    budget = min(timeout_s, MAX_TIMEOUT_S)
    inp = inp or {}
    at = inp.get("_reply_deadline")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        budget = min(budget, float(at) - time.monotonic() - DEADLINE_MARGIN_S)
    left = inp.get("_deadline_s_remaining")
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        budget = min(budget, float(left) - DEADLINE_MARGIN_S)
    return budget


def lint_changes(memory: Any, changes: dict[str, set[int] | None], *,
                 inp: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the linters on the files of `changes` (rel -> changed lines, None
    for all) and return `{"lines": [...], "runs", "findings", "timeouts"}`
    (see the module docstring). `inp` is the call's input, for its reserved
    fields: `_test_env`, `_unlock` (a context manager that releases the
    caller's graph lock while the linters run), `_reply_deadline` and
    `_deadline_s_remaining`."""
    root = root_of(memory)
    plan = Plan(root)
    res: dict[str, Any] = {"lines": [], "runs": 0, "findings": 0, "timeouts": 0}
    ruff_files: list[str] = []
    rust_files: list[tuple[str, str, bytes]] = []    # (rel, edition, text)
    for rel in list(changes)[:MAX_FILES]:
        lines = changes[rel]
        if lines is not None and not lines:
            continue
        if not (root / rel).is_file():
            continue
        got = plan.linter(rel)
        if got is None:
            continue
        if got[0] == "ruff":
            ruff_files.append(rel)
        else:
            try:
                text = (root / rel).read_bytes()
            except OSError:
                continue
            rust_files.append((rel, str(got[1]), text))
    if not ruff_files and not rust_files:
        return res
    budget = lint_budget(plan.timeout_s, inp)
    shown = f"{round(max(budget, 0.0), 1):g}"
    if budget < MIN_BUDGET_S:
        res["timeouts"] += 1
        res["lines"].append(tag("lint: not run; the request deadline is too near"))
        return res
    ruff_exe, rustfmt_exe = plan.ruff(), plan.rustfmt()
    env = lint_env(inp)
    deadline = time.monotonic() + budget
    findings: list[dict[str, Any]] = []
    notes: list[str] = []

    def left() -> float:
        return deadline - time.monotonic()

    unlock = (inp or {}).get("_unlock")
    # The linters read files only: the graph lock is free meanwhile.
    with (unlock() if callable(unlock) else contextlib.nullcontext()):
        if ruff_files and ruff_exe:
            argv = [ruff_exe, "check", "--output-format", "json", "--no-fix",
                    "--no-cache", "--force-exclude", "--", *ruff_files]
            stdout, timed_out = _run(argv, root, left(), env)
            res["runs"] += 1
            if timed_out:
                res["timeouts"] += 1
                notes.append(tag(f"lint: ruff timed out after {shown} s"))
            elif stdout:
                findings.extend(_ruff_findings(root, stdout))
        for rel, edition, text in rust_files if rustfmt_exe else []:
            if left() < MIN_BUDGET_S:
                res["timeouts"] += 1
                notes.append(tag(f"lint: rustfmt not run on {rel}; the {shown} s "
                                 f"lint budget is spent"))
                continue
            # The file on stdin: only it is checked, not its child modules.
            # Its directory as cwd: rustfmt finds its rustfmt.toml from there.
            argv = [rustfmt_exe, "--check", "--edition", edition,
                    "--color", "never"]
            cwd = root.joinpath(*PurePosixPath(rel).parent.parts)
            stdout, timed_out = _run(argv, cwd, left(), env, stdin=text)
            res["runs"] += 1
            if timed_out:
                res["timeouts"] += 1
                notes.append(tag(f"lint: rustfmt timed out after {shown} s"))
                continue
            for h in rustfmt_hunks(stdout or "", root, cwd, stdin_rel=rel):
                findings.append({"rel": rel, "line": h["start"], "end": h["end"],
                                 "rule": "rustfmt",
                                 "message": (f"rustfmt would reformat "
                                             f"L{h['start']}-{h['end']}")})
    kept = []
    for f in findings:
        lines = changes.get(f["rel"])
        span = set(range(f["line"], f.get("end", f["line"]) + 1))
        if lines is None or span & lines:
            kept.append(f)
    kept.sort(key=lambda f: (f["rel"], f["line"], f["rule"]))
    res["findings"] = len(kept)
    keyers: dict[str, Keyer] = {}
    for f in kept[:MAX_LINES]:
        keyer = keyers.setdefault(f["rel"], Keyer(memory, f["rel"]))
        try:
            key = keyer.key(f["line"])
        except Exception:  # noqa: BLE001 - the file key instead
            key = f"file:{f['rel']}"
        res["lines"].append(tag(f"lint {f['rule']} L{f['line']} {key}: "
                                f"{one_line(f['message'])}"))
    if len(kept) > MAX_LINES:
        res["lines"].append(tag(f"lint: {len(kept) - MAX_LINES} more findings "
                                f"on the changed lines"))
    res["lines"].extend(notes)
    return res


def lint_meta(got: dict[str, Any]) -> dict[str, int]:
    """The counters of a `lint_changes` result for a response's meta."""
    if not got.get("runs") and not got.get("timeouts"):
        return {}
    return {"lint_runs": int(got["runs"]), "lint_findings": int(got["findings"]),
            "lint_timeouts": int(got["timeouts"])}


__all__ = ["MAX_LINES", "MAX_TIMEOUT_S", "Plan", "after_changes", "applies",
           "cargo_edition", "changed_lines", "find_executable", "lint_budget",
           "lint_changes", "lint_env", "lint_meta", "ruff_config",
           "ruff_executable", "run_bounded", "rustfmt_hunks",
           "trust_project_venv", "user_config_path"]
