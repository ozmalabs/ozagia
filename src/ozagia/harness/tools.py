"""The native loop's built-in tools: read, grep, glob, edit, write, shell.

Inputs use OpenCode 2.3 field names (see `ozagia.daemon.protocol`):

    read  {path, offset?, limit?}
    grep  {pattern, path?, include?, caseSensitive?}
    glob  {pattern, path?}
    edit  {path, oldString, newString, replaceAll?}
    write {path, content}
    shell {command, workdir?, timeout? (ms), description?}

Each tool returns the text the model sees, or raises `ToolError` with the
error text. Outputs follow OpenCode's shapes, without any graph context:

    read   Read file <abs path>, lines <a>-<b>[ of <total>]
           <n>: <line>
           (a directory: `Read directory <abs path>, entries <a>-<b>[ of <n>]`,
           then one name per line, `/` after a directory name)
    grep   Found <N> matches[ (showing first 100)]
           <abs path>:
             Line <n>: <text>
           (blank line between files; `No files found` when nothing matches)
    glob   absolute paths, newest first, at most 100
    edit   Edit applied successfully.
    write  Wrote file successfully.
    shell  combined stdout and stderr; the last MAX_SHELL_OUTPUT_CHARS kept

File paths are absolute or relative to the workdir and must stay inside it
(after resolving symlinks). The search itself (ripgrep, or a Python `re`
walk when `rg` is missing) is the one `ozagia.intent.search` uses, so both
modes of the native loop search the same files.
"""
from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

MAX_SHELL_OUTPUT_CHARS = 30000
DEFAULT_SHELL_TIMEOUT_MS = 120_000
DEFAULT_READ_LIMIT = 2000
MAX_GLOB_RESULTS = 100
# How long a shell's process group gets between SIGTERM and SIGKILL.
SHELL_KILL_GRACE_S = 2.0
BINARY_SNIFF_BYTES = 8192

TOOL_NAMES = ("read", "grep", "glob", "edit", "write", "shell")


class ToolError(Exception):
    """A tool failed; the message goes back to the model as the tool's error."""


# --------------------------------------------------------------------------- #
# paths and arguments
# --------------------------------------------------------------------------- #
def resolve(workdir: Path, path: Any, *, field: str = "path") -> Path:
    """`path` (absolute or workdir-relative) as an absolute path inside the
    workdir. Symlinks are resolved before the check."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError(f"{field} is required")
    root = Path(os.path.realpath(workdir))
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise ToolError(f"{path} is outside the working directory {root}")
    return real


def _opt_int(inp: dict[str, Any], name: str) -> int | None:
    value = inp.get(name)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ToolError(f"{name} must be an integer, got {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer, got {value!r}") from None
    if n < 1:
        raise ToolError(f"{name} must be at least 1, got {n}")
    return n


def _header(kind: str, display: str, a: int, b: int, total: int,
            unit: str) -> str:
    if total == 0:
        return f"Read {kind} {display}, 0 {unit}"
    head = f"Read {kind} {display}, {unit} {a}-{b}"
    if a > 1 or b < total:
        head += f" of {total}"
    return head


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def tool_read(workdir: Path, inp: dict[str, Any], **_: Any) -> str:
    target = resolve(workdir, inp.get("path"))
    offset = _opt_int(inp, "offset") or 1
    limit = _opt_int(inp, "limit") or DEFAULT_READ_LIMIT
    if target.is_dir():
        entries = [n + "/" if (target / n).is_dir() else n
                   for n in sorted(os.listdir(target))]
        return _render(entries, "directory", str(target), offset, limit,
                       "entries", numbered=False)
    if not target.is_file():
        raise ToolError(f"File not found: {target}")
    data = target.read_bytes()
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        raise ToolError(f"Cannot read binary file: {target}")
    text = data.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    return _render(lines, "file", str(target), offset, limit, "lines",
                   numbered=True)


def _render(items: list[str], kind: str, display: str, offset: int,
            limit: int, unit: str, *, numbered: bool) -> str:
    total = len(items)
    if total and offset > total:
        raise ToolError(f"Offset {offset} is out of range for this {kind} "
                        f"({total} {unit})")
    b = min(total, offset + limit - 1)
    out = [_header(kind, display, offset, b, total, unit)]
    for i in range(offset, b + 1):
        out.append(f"{i}: {items[i - 1]}" if numbered else items[i - 1])
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# grep and glob
# --------------------------------------------------------------------------- #
def tool_grep(workdir: Path, inp: dict[str, Any], **_: Any) -> str:
    from ozagia.intent.common import IntentError
    from ozagia.intent.search import (
        MAX_LINE_CHARS,
        MAX_MATCHES,
        TRUNCATED,
        _mtime,
        _py_grep,
        _rg_grep,
        rg_binary,
    )
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        raise ToolError("pattern is required")
    root = Path(os.path.realpath(workdir))
    base = resolve(workdir, inp["path"]) if inp.get("path") else root
    if not base.exists():
        raise ToolError(f"No such file or directory: {base}")
    include = inp.get("include") or None
    literal = bool(inp.get("literal"))
    ignore_case = inp.get("caseSensitive") is False
    rg = rg_binary()
    try:
        if rg:
            found = _rg_grep(rg, root, base, pattern, include=include,
                             literal=literal, ignore_case=ignore_case)
        else:
            found = _py_grep(root, base, pattern, include=include,
                             literal=literal, ignore_case=ignore_case)
    except IntentError as e:
        raise ToolError(str(e)) from None
    except subprocess.TimeoutExpired:
        raise ToolError("grep timed out") from None
    if not found:
        return "No files found"
    by_file: dict[str, list[tuple[int, str]]] = {}
    for p, n, text in found:
        by_file.setdefault(p, []).append((n, text))
    total = len(found)
    truncated = total > MAX_MATCHES
    lines = [f"Found {total} matches"
             + (f" (showing first {MAX_MATCHES})" if truncated else "")]
    shown = 0
    for p in sorted(by_file, key=lambda p: (-_mtime(p), p)):
        if shown >= MAX_MATCHES:
            break
        if shown:
            lines.append("")
        lines.append(f"{os.path.abspath(p)}:")
        for n, text in sorted(by_file[p]):
            if shown >= MAX_MATCHES:
                break
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + "..."
            lines.append(f"  Line {n}: {text}")
            shown += 1
    if truncated:
        lines += ["", TRUNCATED.format(shown=MAX_MATCHES, total=total)]
    return "\n".join(lines)


def tool_glob(workdir: Path, inp: dict[str, Any], **_: Any) -> str:
    from ozagia.intent.search import (
        GLOB_TRUNCATED,
        SEARCH_TIMEOUT_S,
        _mtime,
        _walk,
        rg_binary,
    )
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern is required")
    root = Path(os.path.realpath(workdir))
    base = resolve(workdir, inp["path"]) if inp.get("path") else root
    if not base.is_dir():
        raise ToolError(f"Not a directory: {base}")
    rg = rg_binary()
    if rg:
        try:
            proc = subprocess.run(
                [rg, "--files", "--hidden", "--no-messages", "--color", "never",
                 "--glob", "!.git", "--glob", pattern, "--", str(base)],
                capture_output=True, timeout=SEARCH_TIMEOUT_S, cwd=str(root),
                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ToolError("glob timed out") from None
        files = [os.fsdecode(b) for b in proc.stdout.split(b"\n") if b]
    else:
        files = [str(p) for p in _walk(root, base)
                 if _glob_match(pattern, p.relative_to(base).as_posix())]
    files = sorted((os.path.abspath(f) for f in files),
                   key=lambda p: (-_mtime(p), p))
    if not files:
        return "No files found"
    lines = files[:MAX_GLOB_RESULTS]
    if len(files) > MAX_GLOB_RESULTS:
        lines += ["", GLOB_TRUNCATED.format(shown=MAX_GLOB_RESULTS,
                                            total=len(files))]
    return "\n".join(lines)


def _glob_match(pattern: str, rel: str) -> bool:
    """ripgrep-like: a pattern without `/` matches the base name; `**/`
    matches any number of directories."""
    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)
    if pattern.startswith("**/"):
        tail = pattern[3:]
        parts = rel.split("/")
        return any(fnmatch.fnmatchcase("/".join(parts[i:]), tail)
                   for i in range(len(parts)))
    return fnmatch.fnmatchcase(rel, pattern)


# --------------------------------------------------------------------------- #
# edit and write
# --------------------------------------------------------------------------- #
def tool_edit(workdir: Path, inp: dict[str, Any], **_: Any) -> str:
    target = resolve(workdir, inp.get("path"))
    old, new = inp.get("oldString"), inp.get("newString")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("oldString and newString are required strings")
    if old == new:
        raise ToolError("oldString and newString must be different")
    if old == "":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8")
        return "Edit applied successfully."
    if not target.is_file():
        raise ToolError(f"File {target} not found")
    text = target.read_text(encoding="utf-8", errors="surrogateescape")
    count = text.count(old)
    if count == 0:
        raise ToolError("oldString not found in content")
    if count > 1 and not inp.get("replaceAll"):
        raise ToolError("Found multiple matches for oldString. Provide more "
                        "surrounding lines in oldString to identify the "
                        "correct match.")
    text = text.replace(old, new) if inp.get("replaceAll") else text.replace(
        old, new, 1)
    target.write_text(text, encoding="utf-8", errors="surrogateescape")
    return "Edit applied successfully."


def tool_write(workdir: Path, inp: dict[str, Any], **_: Any) -> str:
    target = resolve(workdir, inp.get("path"))
    content = inp.get("content")
    if not isinstance(content, str):
        raise ToolError("content is required")
    if target.is_dir():
        raise ToolError(f"{target} is a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return "Wrote file successfully."


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #
def _killpg(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + SHELL_KILL_GRACE_S
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def truncate_output(text: str, limit: int = MAX_SHELL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return (f"(output truncated: showing the last {limit} of {len(text)} "
            f"characters)\n" + text[-limit:])


def tool_shell(workdir: Path, inp: dict[str, Any], *,
               env: dict[str, str] | None = None,
               max_timeout_s: float | None = None, **_: Any) -> str:
    """`bash -c command` in its own process group. On timeout the group gets
    SIGTERM, then SIGKILL after SHELL_KILL_GRACE_S."""
    command = inp.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command is required")
    cwd = Path(os.path.realpath(workdir))
    if inp.get("workdir"):
        w = Path(str(inp["workdir"]))
        cwd = Path(os.path.realpath(w if w.is_absolute() else cwd / w))
        if not cwd.is_dir():
            raise ToolError(f"workdir is not a directory: {inp['workdir']}")
    timeout_ms = _opt_int(inp, "timeout") or DEFAULT_SHELL_TIMEOUT_MS
    timeout_s = timeout_ms / 1000
    if max_timeout_s is not None:
        timeout_s = max(0.1, min(timeout_s, max_timeout_s))
    run_env = dict(env if env is not None else os.environ, PWD=str(cwd))
    proc = subprocess.Popen(["bash", "-c", command], cwd=str(cwd), env=run_env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    timed_out = False
    try:
        out, _err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _killpg(proc.pid)
        try:
            out, _err = proc.communicate(timeout=SHELL_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = b""
    text = truncate_output((out or b"").decode("utf-8", errors="replace"))
    if timed_out:
        text += (f"\n\n(Command timed out after {int(timeout_s * 1000)} ms; "
                 "its process group was killed)")
    elif proc.returncode:
        text += f"\n\n(exit code {proc.returncode})"
    return text if text.strip() else "(no output)"


TOOLS: dict[str, Callable[..., str]] = {
    "read": tool_read,
    "grep": tool_grep,
    "glob": tool_glob,
    "edit": tool_edit,
    "write": tool_write,
    "shell": tool_shell,
}


def run_tool(workdir: Path, name: str, inp: dict[str, Any], *,
             env: dict[str, str] | None = None,
             max_timeout_s: float | None = None) -> str:
    """Run one built-in tool. Raises ToolError for a tool error (including an
    unknown tool or an unexpected exception)."""
    fn = TOOLS.get(name)
    if fn is None:
        raise ToolError(f"unknown tool: {name}")
    if not isinstance(inp, dict):
        raise ToolError("tool input must be a JSON object")
    try:
        return fn(Path(workdir), inp, env=env, max_timeout_s=max_timeout_s)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001 - reported to the model
        raise ToolError(f"{type(e).__name__}: {e}") from None
