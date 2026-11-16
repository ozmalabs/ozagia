"""Carry out a compound shell command (`shell.plan_compound`) step by step.

`run_compound(memory, cls, inp, wd)` runs the steps of a `compound`
classification in order with shell semantics:

- `&&` runs the next pipeline when the status is 0, `||` when it is not,
  and `;` always runs it. A leading `cd DIR` changes the directory of the
  steps after it.
- A pipeline's status is its last command's: 0 after a `| tail`/`| head`
  filter, else the step's own status. pytest takes its exit code, grep is 1
  when nothing matches, read and git errors are 1 / git's code, and echo,
  pwd and cd are 0.
- One timeout covers the whole command: `input.timeout` (ms), else
  `run.DEFAULT_TIMEOUT_S`, clamped to `_deadline_s_remaining`.

Everything that could make the command passthrough (a directory or file
outside the root, a missing file, an interpreter that cannot be found) is
checked before the first step runs, so a passthrough never repeats work. A
failure while a step runs becomes that step's error text and status 1.

Output: each step's text in order, filters applied to the step's own text
(not to its `[ozagia]` lines), then its `[ozagia]` lines. pytest, read and
grep steps give the same lines as when they run alone; a `git diff`/`git
show` step adds `[ozagia] touched: <keys>`. A non-zero final status adds
`[ozagia] exit <n>`. The text is capped at `MAX_OUTPUT_CHARS`.

Locking: steps that read the graph run under `inp["_lock"]` when the caller
set one (the daemon runs a compound with a pytest step outside its global
lock and passes the lock here).
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import time
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


def _filtered(text: str, filters: list[tuple]) -> tuple[str, int]:
    """`text` through the pipeline's filters (`shell._filter`), and the
    pipeline's status: 0, or for a final grep 1 when no line matched."""
    import re
    lines = text.splitlines()
    status = 0
    for f in filters:
        kind = f[0]
        status = 0
        if kind == "head":
            lines = lines[:f[1]]
        elif kind == "tail":
            lines = lines[-f[1]:] if f[1] else []
        elif kind == "from":
            lines = lines[f[1] - 1:]
        elif kind == "lines":
            lines = lines[f[1] - 1:f[2]]
        elif kind == "grep":
            rx, invert, before, after = re.compile(f[1]), f[2], f[3], f[4]
            hits = [i for i, line in enumerate(lines)
                    if bool(rx.search(line)) != invert]
            if not hits:
                lines, status = [], 1
                continue
            if not before and not after:
                lines = [lines[i] for i in hits]
                continue
            keep: list[int] = []
            for i in hits:
                keep.extend(range(max(0, i - before), min(len(lines), i + after + 1)))
            out: list[str] = []
            last = None
            for i in sorted(set(keep)):
                if last is not None and i != last + 1:
                    out.append("--")
                out.append(lines[i])
                last = i
            lines = out
    return "\n".join(lines), status


def _select(lines: list[str], filters: list[tuple]) -> tuple[list[int], int]:
    """The indices of `lines` the pipeline's filters keep, and its status.
    Like `_filtered` but by position, so a read or grep step can render
    the kept lines in its own format (grep's `--` group separators are not
    lines here)."""
    import re
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


def _grep_line(cls: dict[str, Any], wd: Path, found: tuple[str, int, str]) -> str:
    """One match as grep itself prints it (`path:n:text` and its variants),
    for the filters that follow it in a pipeline."""
    path, n, text = found
    parts = []
    if cls.get("with_filename"):
        parts.append(os.path.relpath(path, wd))
    if cls.get("line_numbers"):
        parts.append(str(n))
    parts.append(text)
    return ":".join(parts)


class _Step:
    """One prepared step: `run()` gives (text, tags, status, keys)."""

    def __init__(self, step: dict[str, Any],
                 run: Callable[[float], tuple[str, list[str], int, list[str]]]
                 ) -> None:
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


def _prepare(memory: Any, step: dict[str, Any], inp: dict[str, Any],
             wd: Path, lock: Any) -> _Step:
    from ozagia.intent import touched_keys
    from ozagia.intent.read import read_range
    from ozagia.intent.run import _python_for, run_pytest
    from ozagia.intent.search import handle_grep
    root = root_of(memory)
    kind = step["kind"]
    env = inp.get("_test_env") if isinstance(inp.get("_test_env"), dict) else None
    cls = step.get("cls") or {}

    if kind == "echo":
        text = step["text"]
        return _Step(step, lambda _t: (text, [], 0, []))
    if kind == "pwd":
        return _Step(step, lambda _t: (str(wd), [], 0, []))
    if kind == "ls":
        for p in step["paths"]:
            q = Path(p)
            _inside(root, q if q.is_absolute() else wd / q)

        def run_ls(timeout: float) -> tuple:
            text, err, code = _subprocess(step["argv"], cwd=wd,
                                          env=dict(env or os.environ),
                                          timeout=timeout)
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
            text, err, code = _subprocess(step["argv"], cwd=wd,
                                          env=_git_env(env), timeout=timeout)
            tags: list[str] = []
            keys: list[str] = []
            if sub in ("diff", "show") and code == 0 and text:
                with lock:
                    keys = touched_keys(memory, text)
                if keys:
                    tags.append(tag("touched: " + ", ".join(keys)))
            return text, tags, code, keys, err
        return _Step(step, run_git)
    if kind == "read":
        p = Path(cls["file"])
        p = _inside(root, p if p.is_absolute() else wd / p)
        if not p.is_file():
            raise Unhandled(f"not a regular file: {cls['file']}")
        from ozagia.intent.common import read_text, split_lines
        total = len(split_lines(read_text(p)))
        if total and _range_bounds(cls["range"], total)[0] > total:
            raise Unhandled("range starts after the end of the file")

        def run_read(_t: float) -> tuple[str, list[str], int, list[str]]:
            from ozagia.intent.common import Keyer, read_text, split_lines
            try:
                if not step["filters"]:
                    with lock:
                        res = read_range(memory, p, cls["range"])
                    body, tags = _split_tags(res["output"])
                    return body, tags, 0, list(res.get("keys") or [])
                lines = split_lines(read_text(p))
                a, b = _range_bounds(cls["range"], len(lines))
                idx, status = _select(lines[a - 1:b], step["filters"])
                nums = [a + i for i in idx]
                if not nums:
                    return "", [], status, []
                if nums == list(range(nums[0], nums[-1] + 1)):
                    with lock:
                        res = read_range(memory, p, ("lines", nums[0], nums[-1]))
                    body, tags = _split_tags(res["output"])
                    return body, tags, status, list(res.get("keys") or [])
                rel = p.relative_to(root).as_posix()
                with lock:
                    keyer = Keyer(memory, rel)
                    keys = list(dict.fromkeys(keyer.key(n) for n in nums))
                body = "\n".join(lines[n - 1] for n in nums)
                return body, [tag("lines in: " + ", ".join(keys[:20]))], status, keys
            except (Unhandled, IntentError, OSError) as e:
                return f"{cls.get('tool', 'read')}: {e}", [], 1, []
        step["_filters_inside"] = True
        return _Step(step, run_read)
    if kind == "grep":
        if cls["path"] is not None:
            p = Path(cls["path"])
            p = _inside(root, p if p.is_absolute() else wd / p)
            if not p.exists():
                raise Unhandled(f"no such path: {cls['path']}")
            if p.is_dir() and not cls["recursive"]:
                raise Unhandled("grep of a directory without -r")
            path = str(p)
        else:
            path = str(wd)

        def run_grep(_t: float) -> tuple[str, list[str], int, list[str]]:
            status = [0]

            def select(found: list[tuple[str, int, str]]) -> list:
                raws = [_grep_line(cls, wd, f) for f in found]
                idx, status[0] = _select(raws, step["filters"])
                return [found[i] for i in idx]
            try:
                with lock:
                    res = handle_grep(
                        memory, {"pattern": cls["pattern"], "path": path,
                                 "include": cls["include"],
                                 "literal": cls["literal"]},
                        base=wd, ignore_case=cls["ignore_case"],
                        select=select if step["filters"] else None)
            except (Unhandled, IntentError) as e:
                return f"{cls['tool']}: {e}", [], 2, []
            body, tags = _split_tags(res["output"])
            if step["filters"]:
                if body.strip() == "No files found":
                    body = ""
                return body, tags, status[0], list(res.get("keys") or [])
            st = 1 if body.strip() == "No files found" else 0
            return body, tags, st, list(res.get("keys") or [])
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
            tail = _pytest_tail_lines(step["filters"])
            try:
                res = run_pytest(memory, {**cls, "output_filter": None,
                                          **({"tail_lines": tail}
                                             if tail is not None else {})},
                                 sub, wd)
            except (Unhandled, IntentError) as e:
                return f"pytest: {e}", [], 1, []
            body, tags = _split_tags(res["output"])
            code = res.get("exit_code")
            status = code if isinstance(code, int) and code >= 0 else 1
            return body, tags, status, list(res.get("keys") or [])
        return _Step(step, run_tests)
    raise Unhandled(f"step {kind} is not mapped")


def run_compound(memory: Any, cls: dict[str, Any], inp: dict[str, Any],
                 wd: Path) -> dict[str, Any]:
    from ozagia.intent.run import DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S
    root = root_of(memory)
    lock = inp.get("_lock") or contextlib.nullcontext()
    timeout = inp.get("timeout")
    try:
        budget = float(timeout) / 1000 if timeout else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        budget = DEFAULT_TIMEOUT_S
    remaining = inp.get("_deadline_s_remaining")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        budget = max(MIN_TIMEOUT_S, min(budget, float(remaining)))

    steps = cls["steps"]
    ops = cls["ops"]
    prepared: list[_Step | None] = []
    cur = wd
    for step in steps:
        if step["kind"] == "cd":
            q = Path(step["dir"])
            q = q if q.is_absolute() else cur / q
            real = _inside(root, q)
            if not real.is_dir():
                raise Unhandled(f"cd: {step['dir']} is not a directory")
            cur = real
            prepared.append(None)
            continue
        prepared.append(_prepare(memory, step, inp, cur, lock))

    preflight_stop(inp)
    t0 = time.monotonic()
    chunks: list[str] = []
    keys: list[str] = []
    status = 0
    for i, (step, prep) in enumerate(zip(steps, prepared)):
        if i > 0:
            op = ops[i - 1]
            if (op == "&&" and status != 0) or (op == "||" and status == 0):
                continue
        if prep is None:                       # cd
            status = 0
            continue
        left = budget - (time.monotonic() - t0)
        if left <= 0:
            chunks.append(tag(f"timed out after {round(budget, 1)} s; "
                              f"not run: {one_line(step['raw'])}"))
            status = 124
            break
        try:
            got = prep.run(left)
        except Exception as e:  # noqa: BLE001 - one step failed
            got = ("", [], 1, [], f"{step['argv'][0]}: {type(e).__name__}: {e}")
        text, tags, code, step_keys = got[:4]
        err = got[4] if len(got) > 4 else ""
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
            text, code = _filtered(text, step["filters"])
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
    out = "\n".join(chunks)
    if len(out) > MAX_OUTPUT_CHARS:
        out = (out[:MAX_OUTPUT_CHARS] + "\n"
               + tag(f"output truncated: showing the first {MAX_OUTPUT_CHARS}"
                     f" of {len(out)} characters"))
    if status != 0:
        out = (out + "\n" if out else "") + tag(f"exit {status}")
    return {"intent": "shell_compound", "output": out, "keys": keys,
            "exit_code": status}


__all__ = ["MAX_OUTPUT_CHARS", "run_compound"]
