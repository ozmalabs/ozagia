"""Carry out a compound shell command (`shell.plan_compound`) step by step.

`run_compound(memory, cls, inp, wd)` runs the steps of a `compound`
classification in order with shell semantics:

- `&&` runs the next item when the status is 0, `||` when it is not,
  and `;` always runs it. A leading `cd DIR` (also `cd ~`, `cd ~/DIR`
  when that is inside the root) changes the directory of the steps after
  it. A subshell `( ... )` runs its own sequence; a `cd` inside it ends
  with it, and its status is that of its last command.
- A pipeline's status is its last command's: 0 after a `| tail`/`| head`/
  `| sed`/`| wc`/`| sort`/`| uniq`/`| cut` filter, 1 after a `| grep`
  that keeps no line, else the step's own status. pytest takes its exit
  code, grep is 1 when nothing matches, read and git errors are 1 / git's
  code, `rg --files` takes rg's code, and echo, pwd, find and cd are 0.
- One timeout covers the whole command: `input.timeout` (ms), else
  `run.DEFAULT_TIMEOUT_S`, clamped to `_deadline_s_remaining`. A step's
  own `timeout N` wrapper limits a git, ls or `rg --files` subprocess to N
  seconds (status 124 when it expires; the partial output a killed command
  may have printed is not reproduced); an emulated step that takes longer
  than N gets status 124 and no output.

Everything that could make the command passthrough (a directory or file
outside the root, a missing file, an interpreter that cannot be found, a
`sort` in a locale whose order is not byte order) is checked before the
first step runs, so a passthrough never repeats work. A find step is
evaluated then too, unless a pytest step could change the tree first. A
failure while a step runs becomes that step's error text and status 1.

Filters (`_filter_text`) follow the text as the shell passes it: a last
line without a newline stays so through `head`, `tail` and `sed` (so
`wc -l` counts as wc does), and grep, sort, uniq and cut end every line.
`sort` compares bytes (C/POSIX/C.UTF-8 order; other locales collate
differently, so the command falls back), `-n` reads a leading number after
blanks (`-`, digits, `.` fraction; anything else is 0) and orders equal
numbers by bytes unless `-u`, `-r` reverses both, and `-u` keeps the first
line of each run of equal keys. `uniq` compares bytes; `uniq -c` prints
`%7d `. `cut -f` selects fields in file order, prints a line without the
delimiter whole (dropped with `-s`). `wc -w` splits at ASCII white space,
and in a UTF-8 locale also at the Unicode spaces glibc and coreutils
count.

Output: each step's text in order, filters applied to the step's own text
(not to its `[ozagia]` lines), then its `[ozagia]` lines. pytest, read and
grep steps give the same lines as when they run alone; a `git diff`/`git
show` step adds `[ozagia] touched: <keys>`. A pipeline through `wc`,
`sort`, `uniq` or `cut` shows the command's own text without graph lines.
A non-zero final status adds `[ozagia] exit <n>`. The text is capped at
`MAX_OUTPUT_CHARS`.

Locking: steps that read the graph run under `inp["_lock"]` when the caller
set one (the daemon runs a compound with a pytest step outside its global
lock and passes the lock here).
"""
from __future__ import annotations

import contextlib
import functools
import os
import re
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from ozagia.intent.common import (
    IntentError,
    Unhandled,
    one_line,
    preflight_stop,
    root_of,
    tag,
)

MAX_OUTPUT_CHARS = 30000
_TAG = "[ozagia] "


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def _split(text: str) -> tuple[list[str], bool]:
    """Lines of `text` and whether the last one ends with a newline."""
    if not text:
        return [], True
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
        return parts, True
    return parts, False


def _join(lines: list[str], final: bool) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + ("\n" if final else "")


def _locale(env: dict[str, str], category: str) -> str:
    return env.get("LC_ALL") or env.get(category) or env.get("LANG") or "POSIX"


def _c_like(name: str) -> bool:
    return name in ("C", "POSIX") or name.lower() in ("c.utf-8", "c.utf8")


def check_filters(filters: list[tuple], env: dict[str, str]) -> None:
    """Unhandled for a filter whose result depends on a locale the runner
    does not reproduce: `sort` outside C/POSIX/C.UTF-8 collation (and, for
    `-n`, numeric formatting), and `wc -w` in a locale that is neither
    C-like nor UTF-8."""
    for f in filters:
        if f[0] == "sort":
            fenv = {**env, **f[4]}
            for cat in ("LC_COLLATE",) + (("LC_NUMERIC",) if f[1] else ()):
                name = _locale(fenv, cat)
                if not _c_like(name):
                    raise Unhandled(f"sort under {cat}={name} is not mapped "
                                    "(byte order needs LC_ALL=C)")
        elif f[0] == "wc" and f[1] == "w":
            name = _locale({**env, **f[2]}, "LC_CTYPE")
            if name not in ("C", "POSIX") and not re.search(r"(?i)utf-?8", name):
                raise Unhandled(f"wc -w under LC_CTYPE={name} is not mapped")


# White space for `wc -w` in a UTF-8 locale: glibc's `space` class and the
# no-break spaces coreutils' wc also treats as separators.
_UTF8_SPACE = frozenset(" \t\n\v\f\r\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
                        "\u2006\u2008\u2009\u200a\u2028\u2029\u205f\u3000"
                        "\u00a0\u2007\u202f\u2060")
_ASCII_SPACE = frozenset(" \t\n\v\f\r")
_NUM_RE = re.compile(r"[ \t]*(-?)(\d*)(?:\.(\d*))?")


def _number(line: str) -> Decimal:
    m = _NUM_RE.match(line)
    if not m or not (m.group(2) or m.group(3)):
        return Decimal(0)
    return Decimal(f"{m.group(1)}{m.group(2) or '0'}.{m.group(3) or '0'}")


def _sort(lines: list[str], numeric: bool, reverse: bool, unique: bool
          ) -> list[str]:
    def cmp(a: str, b: str) -> int:
        if numeric:
            ka, kb = _number(a), _number(b)
        else:
            ka, kb = a.encode(), b.encode()        # type: ignore[assignment]
        d = (ka > kb) - (ka < kb)
        d = -d if reverse else d
        if d or unique or not numeric:
            return d
        ba, bb = a.encode(), b.encode()
        d = (ba > bb) - (ba < bb)
        return -d if reverse else d
    out = sorted(lines, key=functools.cmp_to_key(cmp))
    if not unique:
        return out
    kept: list[str] = []
    for line in out:
        if not kept or cmp(kept[-1], line) != 0:
            kept.append(line)
    return kept


def _cut(lines: list[str], delim: str, ranges: tuple, only: bool) -> list[str]:
    out = []
    for line in lines:
        if delim not in line:
            if not only:
                out.append(line)
            continue
        parts = line.split(delim)
        out.append(delim.join(p for i, p in enumerate(parts, 1)
                              if any(a <= i and (b is None or i <= b)
                                     for a, b in ranges)))
    return out


def _wc(text: str, mode: str, env: dict[str, str]) -> int:
    if mode == "l":
        return text.count("\n")
    if mode == "c":
        return len(text.encode("utf-8"))
    space = (_UTF8_SPACE if re.search(r"(?i)utf-?8", _locale(env, "LC_CTYPE"))
             else _ASCII_SPACE)
    if space is _ASCII_SPACE:
        data = text.encode("utf-8")
        # coreutils versions disagree on bytes that are neither printable
        # nor white space in the C locale (older ones start no word with
        # them): only count what every version counts the same.
        if any(not (0x20 <= b < 0x7f or b in (9, 10, 11, 12, 13)) for b in data):
            raise Unhandled("wc -w over non-ASCII or control bytes in the C "
                            "locale is not mapped")
        words, inword = 0, False
        for b in data:
            if chr(b) in space:
                inword = False
            elif not inword:
                words, inword = words + 1, True
        return words
    words, inword = 0, False
    for ch in text:
        if ch in space:
            inword = False
        elif not inword:
            words, inword = words + 1, True
    return words


def _grep_lines(lines: list[str], f: tuple) -> tuple[list[str], int]:
    rx, invert, before, after = re.compile(f[1]), f[2], f[3], f[4]
    hits = [i for i, line in enumerate(lines) if bool(rx.search(line)) != invert]
    if not hits:
        return [], 1
    if not before and not after:
        return [lines[i] for i in hits], 0
    keep: set[int] = set()
    for i in hits:
        keep.update(range(max(0, i - before), min(len(lines), i + after + 1)))
    out: list[str] = []
    last = None
    for i in sorted(keep):
        if last is not None and i != last + 1:
            out.append("--")
        out.append(lines[i])
        last = i
    return out, 0


def _filter_text(text: str, filters: list[tuple],
                 env: dict[str, str] | None = None) -> tuple[str, int]:
    """`text` through the pipeline's filters (`shell._filter`), exactly as
    the shell passes it, and the pipeline's status."""
    env = dict(os.environ) if env is None else env
    status = 0
    for f in filters:
        kind = f[0]
        status = 0
        lines, final = _split(text)
        if kind == "head":
            final = final or f[1] < len(lines)
            lines = lines[:f[1]]
        elif kind == "tail":
            lines = lines[-f[1]:] if f[1] else []
        elif kind == "from":
            lines = lines[f[1] - 1:]
        elif kind == "lines":
            final = final or (f[2] is not None and f[2] < len(lines))
            lines = lines[f[1] - 1:f[2]]
        elif kind == "grep":
            lines, status = _grep_lines(lines, f)
            final = True
        elif kind == "sort":
            lines, final = _sort(lines, f[1], f[2], f[3]), True
        elif kind == "uniq":
            out: list[str] = []
            counts: list[int] = []
            for line in lines:
                if out and out[-1] == line:
                    counts[-1] += 1
                else:
                    out.append(line)
                    counts.append(1)
            lines = ([f"{c:7d} {line}" for c, line in zip(counts, out)]
                     if f[1] else out)
            final = True
        elif kind == "cut":
            lines, final = _cut(lines, f[1], f[2], f[3]), True
        elif kind == "wc":
            text = f"{_wc(text, f[1], {**env, **f[2]})}\n"
            continue
        text = _join(lines, final)
    return text, status


def _select(lines: list[str], filters: list[tuple]) -> tuple[list[int], int]:
    """The indices of `lines` the pipeline's filters keep, and its status.
    Only for line-selecting filters (head, tail, sed, grep), so a read or
    grep step can render the kept lines in its own format (grep's `--`
    group separators are not lines here)."""
    idx = list(range(len(lines)))
    status = 0
    for f in filters:
        kind = f[0]
        status = 0
        if kind == "head":
            idx = idx[:f[1]]
        elif kind == "tail":
            idx = idx[-f[1]:] if f[1] else []
        elif kind == "from":
            idx = idx[f[1] - 1:]
        elif kind == "lines":
            idx = idx[f[1] - 1:f[2]]
        elif kind == "grep":
            rx, invert, before, after = re.compile(f[1]), f[2], f[3], f[4]
            hits = [k for k, i in enumerate(idx)
                    if bool(rx.search(lines[i])) != invert]
            if not hits:
                idx, status = [], 1
                continue
            keep = sorted({j for k in hits
                           for j in range(max(0, k - before),
                                          min(len(idx), k + after + 1))})
            idx = [idx[k] for k in keep]
    return idx, status


# Filters that keep lines by their position.
_LINE_SELECTING = frozenset({"head", "tail", "from", "lines"})


def _transforms(filters: list[tuple]) -> bool:
    from ozagia.intent.shell import TRANSFORMS
    return any(f[0] in TRANSFORMS for f in filters)


def _pytest_tail_lines(filters: list[tuple]) -> int | None:
    """How much pytest output a step keeps: None (the default tail), a
    number of last lines, or 0 for the whole output (`check.run_tests`)."""
    from ozagia.check import OUTPUT_TAIL_LINES
    if not filters:
        return None
    if len(filters) == 1 and filters[0][0] == "tail":
        return max(OUTPUT_TAIL_LINES, filters[0][1])
    return 0


def _split_tags(text: str) -> tuple[str, list[str]]:
    body, tags = [], []
    for line in text.splitlines():
        (tags if line.startswith(_TAG) else body).append(line)
    return "\n".join(body), tags


def _inside(root: Path, p: Path) -> Path:
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"{p} is outside the project root")
    return real


def _range_bounds(rng: tuple, total: int) -> tuple[int, int]:
    """The 1-based inclusive line range a shell read covers."""
    kind = rng[0]
    if kind == "all":
        return 1, total
    if kind == "head":
        return 1, min(total, rng[1])
    if kind == "tail":
        return max(1, total - rng[1] + 1), total
    if kind == "from":
        return rng[1], total
    return rng[1], total if rng[2] is None else min(total, rng[2])


def _range_text(text: str, rng: tuple) -> str:
    """The text a cat/head/tail/sed read prints, newline-exact."""
    kind = rng[0]
    if kind == "all":
        return text
    flt = {"head": ("head", rng[1]), "tail": ("tail", rng[1]),
           "from": ("from", rng[1])}.get(kind, rng)
    return _filter_text(text, [flt])[0]


class _Step:
    """One prepared step: `run()` gives (text, tags, status, keys[, err])."""

    def __init__(self, step: dict[str, Any],
                 run: Callable[[float], tuple]) -> None:
        self.step = step
        self.run = run


def _git_env(base: dict[str, str] | None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", PAGER="cat",
               GIT_TERMINAL_PROMPT="0")
    return env


def _subprocess(argv: list[str], *, cwd: Path, env: dict[str, str],
                timeout: float) -> tuple[str, str, int]:
    """(stdout, stderr, status) of `argv`, run without a shell."""
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(timeout, 0.1), check=False)
    except subprocess.TimeoutExpired:
        return "", f"{argv[0]}: timed out after {round(timeout, 1)} s", 124
    except OSError as e:
        return "", f"{argv[0]}: {e}", 127
    return (proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"), proc.returncode)


def _limit(step: dict[str, Any], timeout: float) -> float:
    t = step.get("timeout")
    return min(timeout, float(t)) if t else timeout


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def _prepare(memory: Any, step: dict[str, Any], inp: dict[str, Any],
             wd: Path, lock: Any, *, pytest_first: bool = False) -> _Step:
    from ozagia.intent import touched_keys
    from ozagia.intent.read import byte_slice, nl_check, nl_lines, read_range
    from ozagia.intent.run import _python_for, run_pytest
    from ozagia.intent.search import (
        grep_raw_line,
        render_shell_grep,
        shell_grep_records,
        shell_grep_resolve,
    )
    root = root_of(memory)
    kind = step["kind"]
    env = inp.get("_test_env") if isinstance(inp.get("_test_env"), dict) else None
    base_env = dict(env if env is not None else os.environ)
    step_env = step.get("env") or {}
    cls = step.get("cls") or {}
    filters = step["filters"]
    check_filters(filters, base_env)
    raw = _transforms(filters)

    if kind == "echo":
        text = step["text"] + ("\n" if step.get("newline", True) else "")
        return _Step(step, lambda _t: (text, [], 0, []))
    if kind == "pwd":
        return _Step(step, lambda _t: (f"{wd}\n", [], 0, []))
    if kind == "ls":
        for p in step["paths"]:
            q = Path(p)
            _inside(root, q if q.is_absolute() else wd / q)

        def run_ls(timeout: float) -> tuple:
            text, err, code = _subprocess(step["argv"], cwd=wd,
                                          env={**base_env, **step_env},
                                          timeout=_limit(step, timeout))
            return text, [], code, [], err
        return _Step(step, run_ls)
    if kind == "git_read":
        from ozagia.intent.shell import git_step_dirs
        sub = cls.get("subcommand")
        gwd = wd
        for d in git_step_dirs(step["argv"]):
            q = Path(d)
            gwd = _inside(root, q if q.is_absolute() else gwd / q)
            if not gwd.is_dir():
                raise Unhandled(f"git -C {d}: not a directory")

        def run_git(timeout: float) -> tuple:
            text, err, code = _subprocess(
                step["argv"], cwd=wd, env={**_git_env(env), **step_env,
                                           "GIT_OPTIONAL_LOCKS": "0"},
                timeout=_limit(step, timeout))
            tags: list[str] = []
            keys: list[str] = []
            if sub in ("diff", "show") and code == 0 and text and not raw:
                with lock:
                    keys = touched_keys(memory, text)
                if keys:
                    tags.append(tag("touched: " + ", ".join(keys)))
            return text, tags, code, keys, err
        return _Step(step, run_git)
    if kind == "files":
        from ozagia.intent.search import rg_binary
        from ozagia.intent.shellfs import rg_files_argv
        rg = rg_binary()
        if rg is None:
            raise Unhandled("rg is not installed")
        argv = rg_files_argv(rg, root, wd, cls)

        def run_files(timeout: float) -> tuple:
            text, err, code = _subprocess(argv, cwd=wd,
                                          env={**base_env, **step_env},
                                          timeout=_limit(step, timeout))
            return text, [], code, [], err
        return _Step(step, run_files)
    if kind == "find":
        from ozagia.intent.shellfs import (
            FIND_MAX_OUTPUT,
            FIND_MAX_VISITS,
            prepare_find,
            run_find,
        )
        prepared = prepare_find(root, wd, cls, {**base_env, **step_env})
        cap = FIND_MAX_VISITS if filters else FIND_MAX_OUTPUT

        def find_text() -> str:
            lines = run_find(cls, prepared, max_output=cap)
            return "".join(line + "\n" for line in lines)
        early = None if pytest_first else find_text()

        def run_find_step(_t: float) -> tuple:
            try:
                text = early if early is not None else find_text()
            except Unhandled as e:
                return "", [], 1, [], f"find: {e}"
            return text, [], 0, []
        return _Step(step, run_find_step)
    if kind == "read":
        p = Path(cls["file"])
        p = _inside(root, p if p.is_absolute() else wd / p)
        if not p.is_file():
            raise Unhandled(f"not a regular file: {cls['file']}")
        from ozagia.intent.common import read_text, split_lines
        rng = cls["range"]
        number = cls.get("number")
        if rng[0].startswith("bytes"):
            chunk, _start, _total = byte_slice(p, rng)
            if not filters:
                with lock:
                    res = read_range(memory, p, rng)
                body, tags = _split_tags(res["output"])
                keys = list(res.get("keys") or [])
                return _Step(step, lambda _t: (body, tags, 0, keys))
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                raise Unhandled("tail -c starts inside a UTF-8 character") from None
            return _Step(step, lambda _t: (text, [], 0, []))
        total = len(split_lines(read_text(p)))
        if total and _range_bounds(rng, total)[0] > total:
            raise Unhandled("range starts after the end of the file")
        if number:
            nl_check(p)
        if raw:
            if number:
                text = "".join(f"{s}\n" for s in
                               nl_lines(split_lines(read_text(p)), number))
            else:
                text = _range_text(p.read_bytes().decode("utf-8", errors="replace"),
                                   rng)
            return _Step(step, lambda _t: (text, [], 0, []))

        def run_read(_t: float) -> tuple[str, list[str], int, list[str]]:
            from ozagia.intent.common import Keyer, read_text, split_lines
            try:
                if not filters:
                    with lock:
                        res = read_range(memory, p, rng, numbering=number)
                    body, tags = _split_tags(res["output"])
                    return body, tags, 0, list(res.get("keys") or [])
                lines = split_lines(read_text(p))
                shown = nl_lines(lines, number) if number else lines
                a, b = _range_bounds(rng, len(lines))
                idx, status = _select(shown[a - 1:b], filters)
                nums = [a + i for i in idx]
                if not nums:
                    return "", [], status, []
                if nums == list(range(nums[0], nums[-1] + 1)):
                    with lock:
                        res = read_range(memory, p, ("lines", nums[0], nums[-1]),
                                         numbering=number)
                    body, tags = _split_tags(res["output"])
                    return body, tags, status, list(res.get("keys") or [])
                rel = p.relative_to(root).as_posix()
                with lock:
                    keyer = Keyer(memory, rel)
                    keys = list(dict.fromkeys(keyer.key(n) for n in nums))
                body = "\n".join(shown[n - 1] for n in nums)
                return body, [tag("lines in: " + ", ".join(keys[:20]))], status, keys
            except (Unhandled, IntentError, OSError) as e:
                return f"{cls.get('tool', 'read')}: {e}", [], 1, []
        step["_filters_inside"] = True
        return _Step(step, run_read)
    if kind == "grep":
        resolved = shell_grep_resolve(root, cls, wd)
        line_opts = {"with_filename": resolved["with_filename"],
                     "line_numbers": bool(cls.get("line_numbers"))}
        if (cls.get("tool") == "grep"
                and any(r.is_dir() for _w, r in resolved["targets"])):
            # grep -r searches what the capture leaves out (ignored, hidden
            # under .git, binary and large files) and prints files in
            # readdir order; the capture's records are a subset, by path.
            if raw:
                raise Unhandled("grep -r over a directory piped into wc, sort, "
                                "uniq or cut is not mapped")
            if any(f[0] in _LINE_SELECTING or (f[0] == "grep" and (f[3] or f[4]))
                   for f in filters):
                raise Unhandled("grep -r over a directory piped into head, "
                                "tail or sed is not mapped")
        early: tuple | None = None
        if raw:
            # Run now, so a line the capture cannot print exactly (binary,
            # not UTF-8, a carriage return) makes the command passthrough.
            if pytest_first:
                raise Unhandled(f"{cls['tool']} after a pytest step piped into "
                                "wc, sort, uniq or cut is not mapped")
            try:
                with lock:
                    recs = shell_grep_records(memory, cls, wd, resolved, raw=True)
                early = ("".join(grep_raw_line(r, **line_opts) + "\n"
                                 for r in recs), [], 0 if recs else 1, [])
            except IntentError as e:
                early = (f"{cls['tool']}: {e}", [], 2, [])

        def run_grep(_t: float) -> tuple:
            if early is not None:
                return early
            try:
                with lock:
                    recs = shell_grep_records(memory, cls, wd, resolved)
                    status = 0 if recs else 1
                    if filters:
                        raws = [grep_raw_line(r, **line_opts) for r in recs]
                        idx, status = _select(raws, filters)
                        recs = [recs[i] for i in idx]
                    res = render_shell_grep(memory, cls["pattern"], recs)
            except (Unhandled, IntentError) as e:
                return f"{cls['tool']}: {e}", [], 2, []
            body, tags = _split_tags(res["output"])
            if body.strip() == "No files found":
                body = "" if filters else body
            return body, tags, status, list(res.get("keys") or [])
        if not raw:
            step["_filters_inside"] = True
        return _Step(step, run_grep)
    if kind == "pytest":
        _python_for(cls, wd)
        if wd != root and cls.get("path_options"):
            raise Unhandled("path options with a workdir other than the root")
        if wd != root and cls.get("env"):
            raise Unhandled("environment assignments with a workdir other "
                            "than the root")
        for s in cls["selectors"]:
            part = s.partition("::")[0]
            q = Path(part)
            _inside(root, q if q.is_absolute() else wd / q)

        def run_tests(timeout: float) -> tuple[str, list[str], int, list[str]]:
            sub = {**inp, "timeout": max(timeout, 0.1) * 1000,
                   "_deadline_s_remaining": max(timeout, 0.1)}
            tail = _pytest_tail_lines(filters)
            try:
                res = run_pytest(memory, {**cls, "output_filter": None,
                                          "_raw": step.get("raw"),
                                          **({"tail_lines": tail}
                                             if tail is not None else {})},
                                 sub, wd)
            except (Unhandled, IntentError) as e:
                return f"pytest: {e}", [], 1, []
            metas = inp.get("_tests_meta")
            if isinstance(metas, list) and res.get("meta"):
                metas.append(res["meta"])
            body, tags = _split_tags(res["output"])
            code = res.get("exit_code")
            status = code if isinstance(code, int) and code >= 0 else 1
            return body, tags, status, list(res.get("keys") or [])
        return _Step(step, run_tests)
    raise Unhandled(f"step {kind} is not mapped")


def _prepare_seq(memory: Any, steps: list[dict[str, Any]], inp: dict[str, Any],
                 wd: Path, lock: Any, *, seen: dict[str, bool],
                 home: str | None) -> list[Any]:
    """Prepared steps: a `_Step`, None for `cd`, or ("group", [...]).
    `seen["pytest"]` records that a pytest step comes before (in run
    order), so a later find step is evaluated when it runs."""
    root = root_of(memory)
    out: list[Any] = []
    cur = wd
    for step in steps:
        if step["kind"] == "cd":
            d = step["dir"]
            if step.get("tilde"):
                if not home:
                    raise Unhandled("cd ~: HOME is not set")
                d = home + d[1:]
            q = Path(d)
            q = q if q.is_absolute() else cur / q
            real = _inside(root, q)
            if not real.is_dir():
                raise Unhandled(f"cd: {step['dir']} is not a directory")
            cur = real
            out.append(None)
        elif step["kind"] == "group":
            out.append(("group", _prepare_seq(memory, step["steps"], inp, cur,
                                              lock, seen=seen, home=home)))
        else:
            out.append(_prepare(memory, step, inp, cur, lock,
                                pytest_first=seen["pytest"]))
            seen["pytest"] = seen["pytest"] or step["kind"] == "pytest"
    return out


def run_compound(memory: Any, cls: dict[str, Any], inp: dict[str, Any],
                 wd: Path) -> dict[str, Any]:
    from ozagia.intent.run import DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S
    lock = inp.get("_lock") or contextlib.nullcontext()
    timeout = inp.get("timeout")
    try:
        budget = float(timeout) / 1000 if timeout else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        budget = DEFAULT_TIMEOUT_S
    remaining = inp.get("_deadline_s_remaining")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        budget = max(MIN_TIMEOUT_S, min(budget, float(remaining)))

    env = inp.get("_test_env") if isinstance(inp.get("_test_env"), dict) else None
    home = (env or os.environ).get("HOME")
    test_metas: list[dict[str, Any]] = []
    inp = {**inp, "_tests_meta": test_metas}
    prepared = _prepare_seq(memory, cls["steps"], inp, wd, lock,
                            seen={"pytest": False}, home=home)

    preflight_stop(inp)
    t0 = time.monotonic()
    chunks: list[str] = []
    keys: list[str] = []
    stopped = [False]

    def run_seq(steps: list[dict[str, Any]], ops: list[str],
                preps: list[Any]) -> int:
        status = 0
        for i, (step, prep) in enumerate(zip(steps, preps)):
            if stopped[0]:
                break
            if i > 0:
                op = ops[i - 1]
                if (op == "&&" and status != 0) or (op == "||" and status == 0):
                    continue
            if prep is None:                       # cd
                status = 0
                continue
            if isinstance(prep, tuple):            # a subshell
                status = run_seq(step["steps"], step["ops"], prep[1])
                continue
            left = budget - (time.monotonic() - t0)
            if left <= 0:
                chunks.append(tag(f"timed out after {round(budget, 1)} s; "
                                  f"not run: {one_line(step['raw'])}"))
                stopped[0] = True
                return 124
            s0 = time.monotonic()
            try:
                got = prep.run(left)
            except Exception as e:  # noqa: BLE001 - one step failed
                got = ("", [], 1, [], f"{step['argv'][0]}: {type(e).__name__}: {e}")
            text, tags, code, step_keys = got[:4]
            err = got[4] if len(got) > 4 else ""
            limit = step.get("timeout")
            if (limit and code != 124 and step["kind"] in ("read", "grep", "find")
                    and time.monotonic() - s0 > float(limit)):
                text, tags, code, step_keys, err = "", [], 124, [], ""
            # fd 1 and fd 2 as the step's redirects left them (plan_compound):
            # stderr merged into the pipeline, dropped, or past the filters.
            if step["stdout_null"]:
                text = ""
            if step["stderr"] == "merge" and err:
                text = (text + ("" if not text or text.endswith("\n") else "\n")
                        + err)
                err = ""
            elif step["stderr"] == "null":
                err = ""
            if step["filters"] and not step.get("_filters_inside"):
                text, code = _filter_text(text, step["filters"],
                                          dict(env if env is not None
                                               else os.environ))
            text = text.rstrip("\n")
            if text:
                chunks.append(text)
            err = err.rstrip("\n")
            if err:
                chunks.append(err)
            chunks.extend(tags)
            for k in step_keys:
                if k not in keys:
                    keys.append(k)
            status = code
        return status

    status = run_seq(cls["steps"], cls["ops"], prepared)
    out = "\n".join(chunks)
    if len(out) > MAX_OUTPUT_CHARS:
        out = (out[:MAX_OUTPUT_CHARS] + "\n"
               + tag(f"output truncated: showing the first {MAX_OUTPUT_CHARS}"
                     f" of {len(out)} characters"))
    if status != 0:
        out = (out + "\n" if out else "") + tag(f"exit {status}")
    res: dict[str, Any] = {"intent": "shell_compound", "output": out,
                           "keys": keys, "exit_code": status}
    if test_metas:
        from ozagia.intent.affected import merge_meta
        res["meta"] = merge_meta(test_metas)
    return res


__all__ = ["MAX_OUTPUT_CHARS", "check_filters", "run_compound"]
