"""`shell`: the mapped command classes (see `ozagia.intent.shell`).

pytest output: pytest's own output tail (verbatim, `ozagia.check`'s last
`OUTPUT_TAIL_LINES` lines), then

    [ozagia] pytest <status>: exit <code>, <s> s, <p> passed, <f> failed, <e> error, <k> skipped
    [ozagia] failed test:<rel>::<qual>[<param>]: <message>
    [ozagia]   frame sym:<rel>::<qual> <file>:<line>
    [ozagia] collection error file:<rel>: <message>

A read-like command becomes a read of the same range; a search-like command
becomes a grep; a ref-moving git command is refused.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ozagia.intent.common import (
    IntentError,
    Unhandled,
    preflight_stop,
    one_line,
    root_of,
    tag,
)
from ozagia.intent.shell import classify

# The timeout when the model gives none: OpenCode's built-in shell default
# (2 min) and the native loop's `harness.tools.DEFAULT_SHELL_TIMEOUT_MS`, so
# a captured pytest is cut off when the built-in would have been.
DEFAULT_TIMEOUT_S = 120
# The shortest timeout a deadline clamps a captured pytest to (the native
# loop's plain shell uses the same floor).
MIN_TIMEOUT_S = 0.1


def refusal(command: str) -> str:
    """The tool error for a ref-moving git command (`git_refs`)."""
    return f"Refused by ozagia: {one_line(command)} changes git refs"


def _workdir(root: Path, wd: Any) -> Path:
    if not wd:
        return root
    p = Path(str(wd))
    if not p.is_absolute():
        p = root / p
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"workdir {wd} is outside the project root")
    if not real.is_dir():
        raise Unhandled(f"workdir {wd} is not a directory")
    return real


def _inside(root: Path, p: Path) -> Path:
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"{p} is outside the project root")
    return real


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _python_for(cls: dict[str, Any], wd: Path) -> str:
    """The interpreter the command names, or Unhandled."""
    tok = cls.get("python")
    if tok:
        if "/" in tok:
            p = Path(tok) if Path(tok).is_absolute() else wd / tok
            if not _executable(p):
                raise Unhandled(f"{tok} is not an executable")
            return str(p)
        found = shutil.which(tok)
        if not found:
            raise Unhandled(f"{tok} is not on PATH")
        return found
    script = cls.get("script") or "pytest"
    if "/" in script:
        sp = Path(script) if Path(script).is_absolute() else wd / script
    else:
        found = shutil.which(script)
        if not found:
            raise Unhandled(f"{script} is not on PATH")
        sp = Path(found)
    try:
        with open(sp, "rb") as fh:
            first = fh.readline(512).decode("utf-8", errors="replace").strip()
    except OSError:
        raise Unhandled(f"cannot read {script}") from None
    if not first.startswith("#!"):
        raise Unhandled(f"{script} has no interpreter line")
    parts = first[2:].split()
    if len(parts) == 2 and os.path.basename(parts[0]) == "env":
        interp = shutil.which(parts[1]) or ""
    elif len(parts) == 1:
        interp = parts[0]
    else:
        raise Unhandled(f"{script}: interpreter line {first!r} is not mapped")
    if not os.path.basename(interp).startswith("python") or not _executable(
            Path(interp)):
        raise Unhandled(f"{script}: interpreter {interp!r} is not a python")
    return interp


def _verbosity(extra: list[str]) -> list[str]:
    """`extra` with the verbosity flags folded into one, compensating for
    the `-q` that `check.run_tests` always passes."""
    net = 0
    kept: list[str] = []
    for tok in extra:
        if tok in ("-q", "--quiet"):
            net -= 1
        elif tok in ("-v", "--verbose"):
            net += 1
        elif (tok.startswith("-") and not tok.startswith("--") and len(tok) > 1
              and set(tok[1:]) <= set("vqxsl")):
            net += tok.count("v") - tok.count("q")
            rest = "".join(c for c in tok[1:] if c not in "vq")
            if rest:
                kept.append("-" + rest)
        else:
            kept.append(tok)
    need = net + 1
    if need > 0:
        kept.append("-" + "v" * need)
    elif need < 0:
        kept.append("-" + "q" * -need)
    return kept


def run_pytest(memory: Any, cls: dict[str, Any], inp: dict[str, Any],
               wd: Path) -> dict[str, Any]:
    from ozagia.check import OUTPUT_TAIL_LINES, run_tests
    root = root_of(memory)
    python = _python_for(cls, wd)
    if wd != root and cls.get("path_options"):
        raise Unhandled("path options with a workdir other than the root")
    selectors = []
    for s in cls["selectors"]:
        part, sep, rest = s.partition("::")
        p = Path(part)
        p = Path(os.path.normpath(p if p.is_absolute() else wd / p))
        _inside(root, p)
        rel = os.path.relpath(p, root)
        selectors.append(Path(rel).as_posix() + sep + rest)
    if not selectors and wd != root:
        selectors.append(Path(os.path.relpath(wd, root)).as_posix())
    timeout = inp.get("timeout")
    try:
        timeout_s = float(timeout) / 1000 if timeout else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    remaining = inp.get("_deadline_s_remaining")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        timeout_s = max(MIN_TIMEOUT_S, min(timeout_s, float(remaining)))
    env = inp.get("_test_env")
    if isinstance(cls.get("env"), dict) and cls["env"]:
        if wd != root:
            raise Unhandled("environment assignments with a workdir other "
                            "than the root")
        base_env = env if isinstance(env, dict) else dict(os.environ)
        env = {**base_env, **cls["env"]}
    preflight_stop(inp)
    res = run_tests(root, selectors, python=python, timeout_s=timeout_s,
                    extra_args=_verbosity(list(cls["extra_args"])),
                    tail_lines=int(OUTPUT_TAIL_LINES if cls.get("tail_lines") is None
                                   else cls["tail_lines"]),
                    **({"env": env, "inherit": False}
                       if isinstance(env, dict) else {}))
    c = res["counts"]
    out = res.get("output_tail") or ""
    filt = cls.get("output_filter")
    if filt and filt[0] in ("tail", "head") and filt[1] >= 0:
        rows = out.splitlines()
        rows = rows[-filt[1]:] if filt[0] == "tail" and filt[1] else (
            rows[:filt[1]] if filt[0] == "head" else [])
        out = "\n".join(rows)
    lines = [out]
    head = (f"pytest {res['status']}: exit {res['exit_code']}, "
            f"{res['seconds']} s, {c['passed']} passed, {c['failed']} failed, "
            f"{c['error']} error, {c['skipped']} skipped")
    if res["collection_errors"]:
        head += f", {len(res['collection_errors'])} collection errors"
    lines.append(tag(head))
    if res.get("error"):
        lines.append(tag(f"error: {res['error']}"))
    keys: list[str] = []

    def frames(rec: dict[str, Any]) -> None:
        seen: set[str] = set()
        for fr in rec.get("frames") or []:
            if fr["key"] in seen:
                continue
            seen.add(fr["key"])
            keys.append(fr["key"])
            lines.append(tag(f"  frame {fr['key']} {fr['file']}:{fr['line']}"))

    for err in res["collection_errors"]:
        keys.append(err["key"])
        lines.append(tag(f"collection error {err['key']}: "
                         f"{one_line(err['message'])}"))
        frames(err)
    for t in res["tests"]:
        if t["outcome"] not in ("failed", "error"):
            continue
        keys.append(t["key"])
        param = f"[{t['param']}]" if t.get("param") else ""
        lines.append(tag(f"{t['outcome']} {t['key']}{param}: "
                         f"{one_line(t['message'])}"))
        frames(t)
    return {"intent": "shell_pytest", "output": "\n".join(lines),
            "keys": list(dict.fromkeys(keys)), "exit_code": res["exit_code"]}


def handle_shell(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    from ozagia.intent.read import read_range
    from ozagia.intent.search import handle_grep
    command = inp.get("command")
    if not isinstance(command, str):
        raise IntentError("command must be a string")
    cls = classify(command)
    kind = cls["class"]
    if kind == "git_refs":
        raise IntentError(refusal(command), intent="shell_refused")
    if kind not in ("pytest", "read", "grep", "ls", "find", "compound"):
        raise Unhandled(cls.get("compound_reason") or cls.get("reason")
                        or kind)
    root = root_of(memory)
    wd = _workdir(root, inp.get("workdir"))
    if kind == "compound":
        from ozagia.intent.compound import run_compound
        return run_compound(memory, cls, inp, wd)
    if "cd" in cls:
        p = Path(cls["cd"])
        wd = _workdir(root, p if p.is_absolute() else wd / p)
    if kind == "pytest":
        return run_pytest(memory, cls, inp, wd)
    if kind == "read":
        p = Path(cls["file"])
        p = _inside(root, p if p.is_absolute() else wd / p)
        res = read_range(memory, p, cls["range"])
        preflight_stop(inp)
        return res
    if kind == "ls":
        p = Path(cls["path"])
        candidate = p if p.is_absolute() else wd / p
        target = _inside(root, candidate)
        if not target.is_dir():
            raise Unhandled("ls capture requires an existing directory")
        try:
            with os.scandir(target) as scan:
                names = sorted(entry.name for entry in scan
                               if not entry.name.startswith("."))
        except OSError as e:
            raise Unhandled(f"ls directory cannot be read: {e}") from None
        if len(names) > 2000:
            raise Unhandled("ls output exceeds capture limit")
        preflight_stop(inp)
        return {"intent": "shell_ls", "output": "\n".join(names), "keys": []}
    if kind == "find":
        import fnmatch
        p = Path(cls["path"])
        candidate = p if p.is_absolute() else wd / p
        if candidate.is_symlink():
            raise Unhandled("find capture does not follow a symlink start path")
        base = _inside(root, candidate)
        if not base.is_dir() and not base.is_file():
            raise Unhandled(f"find root does not exist: {base}")
        if base.name in (".git", ".ozagia"):
            raise Unhandled("find capture excludes repository state")
        if not cls["file_only"]:
            raise Unhandled("find capture requires -type f")
        found: list[str] = []
        visited = 0
        if base.is_file() and not base.is_symlink() and fnmatch.fnmatchcase(base.name, cls["pattern"]):
            found.append(cls["path"])
        def walk_error(error: OSError) -> None:
            raise Unhandled(f"find traversal failed: {error}") from None

        try:
            for current, dirs, files in os.walk(base, followlinks=False,
                                                onerror=walk_error):
                if any(d in (".git", ".ozagia") for d in dirs):
                    raise Unhandled("find capture would traverse repository state")
                dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
                visited += len(dirs) + len(files)
                if visited > 5000:
                    raise Unhandled("find traversal exceeds capture limit")
                if len(found) > 1000:
                    raise Unhandled("find result exceeds capture limit")
                for name in files:
                    item = Path(current) / name
                    if item.is_symlink():
                        continue
                    if fnmatch.fnmatchcase(name, cls["pattern"]):
                        rel = item.relative_to(base).as_posix()
                        # find prints the starting path as written, then paths
                        # relative to it; absolute starts remain absolute.
                        prefix = cls["path"].rstrip("/")
                        found.append((prefix + "/" if prefix not in ("", ".") else
                                      ("./" if prefix == "." else "")) + rel)
                        if len(found) > 1000:
                            raise Unhandled("find result exceeds capture limit")
        except OSError as e:
            raise Unhandled(f"find traversal failed: {e}") from None
        preflight_stop(inp)
        return {"intent": "shell_find", "output": "\n".join(found),
                "keys": []}
    base = wd
    if cls["path"] is not None:
        p = Path(cls["path"])
        p = _inside(root, p if p.is_absolute() else wd / p)
        if p.is_dir() and not cls["recursive"]:
            raise Unhandled("grep of a directory without -r")
        path = str(p)
    else:
        path = str(wd)
    preflight_stop(inp)
    try:
        res = handle_grep(memory, {"pattern": cls["pattern"], "path": path,
                                   "include": cls["include"],
                                   "literal": cls["literal"]},
                          base=base, ignore_case=cls["ignore_case"])
    except IntentError as e:
        raise Unhandled(str(e)) from None
    res["intent"] = "shell_grep"
    return res


__all__ = ["handle_shell", "refusal", "run_pytest"]
