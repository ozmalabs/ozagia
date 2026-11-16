"""`grep` and `glob`: the real search, in OpenCode's shape, grouped by node.

grep output:

    [ozagia] defined: <keys>              (identifier pattern with graph defs)
    Found <N> matches[ (showing first 100)]
    <abs path>:
    [ozagia] <enclosing key>
      Line <n>: <text>
      ...
                                          (blank line between files)
    (Results are truncated: showing 100 of <N> matches. ...)

Files are ordered newest first (OpenCode's order), matches in line order.
Every match line is kept up to the 100-match cap; a line longer than 2000
characters is cut with "..." as OpenCode does. The search is ripgrep
(`--hidden`, `.gitignore` respected, `.git` and the state dir excluded), or a
Python walk with `re` when `rg` is missing. Files larger than
`MAX_READ_BYTES` (2 MiB, the size above which `read` leaves a file to the
built-in) are not searched: on a repository with gigabytes of result files a
search must not read them on every call.

glob output: matching absolute paths, newest first, at most 100, each graph
file followed by `  [ozagia] <n> symbols, <m> tests`.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ozagia.intent.common import (
    MAX_READ_BYTES,
    IntentError,
    Keyer,
    Unhandled,
    file_summary,
    resolve,
    role_note,
    root_of,
    tag,
)

MAX_MATCHES = 100
MAX_GLOB_RESULTS = 100
MAX_LINE_CHARS = 2000
# Definition keys listed on the `defined:` line.
MAX_DEFINED_KEYS = 10
SEARCH_TIMEOUT_S = 60
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SKIP = {".git"}
TRUNCATED = ("(Results are truncated: showing {shown} of {total} matches. "
             "Consider using a more specific path or pattern.)")
GLOB_TRUNCATED = ("(Results are truncated: showing {shown} of {total} files. "
                  "Consider using a more specific path or pattern.)")


def rg_binary() -> str | None:
    found = shutil.which("rg")
    if found:
        return found
    return "/usr/bin/rg" if os.access("/usr/bin/rg", os.X_OK) else None


def _state_excludes(root: Path) -> tuple[Path | None, list[str]]:
    from ozagia.config import state_dir
    sd = Path(os.path.realpath(state_dir(root)))
    try:
        sd.relative_to(root)
    except ValueError:
        return None, []
    return sd, ["--glob", f"!{sd.name}"]


def _under(path: Path, d: Path | None) -> bool:
    return d is not None and (path == d or d in path.parents)


# --------------------------------------------------------------------------- #
# The search
# --------------------------------------------------------------------------- #
def _rg_grep(rg: str, root: Path, base: Path, pattern: str, *,
             include: str | None, literal: bool, ignore_case: bool
             ) -> list[tuple[str, int, str]]:
    sd, excl = _state_excludes(root)
    cmd = [rg, "-n", "-H", "--no-heading", "--null", "--color", "never",
           "--hidden", "--no-messages", "--max-filesize", str(MAX_READ_BYTES),
           "--glob", "!.git", *excl]
    if literal:
        cmd.append("-F")
    if ignore_case:
        cmd.append("-i")
    if include:
        cmd += ["--glob", include]
    cmd += ["-e", pattern, "--", str(base)]
    proc = subprocess.run(cmd, capture_output=True, timeout=SEARCH_TIMEOUT_S,
                          cwd=str(root), stdin=subprocess.DEVNULL)
    out: list[tuple[str, int, str]] = []
    for raw in proc.stdout.split(b"\n"):
        if not raw:
            continue
        path_b, sep, rest = raw.partition(b"\0")
        if not sep:
            continue
        num, _, text = rest.partition(b":")
        if not num.isdigit():
            continue
        path = os.fsdecode(path_b)
        if _under(Path(path), sd):
            continue
        line = text.decode("utf-8", errors="replace")
        out.append((path, int(num), line[:-1] if line.endswith("\r") else line))
    if proc.returncode == 2 and not out:
        msg = proc.stderr.decode("utf-8", errors="replace").strip()
        if msg:
            raise IntentError(f"ripgrep failed: {msg}")
    return out


def _walk(root: Path, base: Path) -> list[Path]:
    from ozagia.config import GENERIC_SKIP_DIRS
    sd, _ = _state_excludes(root)
    if base.is_file():
        return [base]
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP and d not in GENERIC_SKIP_DIRS
                             and not _under(here / d, sd))
        found.extend(here / f for f in sorted(filenames))
    return found


def _glob_match(pattern: str, rel: str) -> bool:
    if "/" not in pattern:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)
    from ozagia.core.memory import _glob_regex
    return bool(_glob_regex(pattern).match(rel))


def _py_grep(root: Path, base: Path, pattern: str, *, include: str | None,
             literal: bool, ignore_case: bool) -> list[tuple[str, int, str]]:
    try:
        rx = re.compile(re.escape(pattern) if literal else pattern,
                        re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise IntentError(f"invalid regex: {e}") from None
    out = []
    for p in _walk(root, base):
        if include and not _glob_match(include, p.relative_to(base).as_posix()
                                       if p != base else p.name):
            continue
        try:
            if p.stat().st_size > MAX_READ_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for i, line in enumerate(text.split("\n"), start=1):
            line = line[:-1] if line.endswith("\r") else line
            if rx.search(line):
                out.append((str(p), i, line))
    return out


def _mtime(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# Graph additions
# --------------------------------------------------------------------------- #
def _name_index(memory: Any) -> dict[str, list[str]]:
    """Symbol/Test name -> keys, rebuilt when the graph's node set changes."""
    gen = (len(memory.nodes), getattr(memory, "_next_id", 0))
    cached = getattr(memory, "_intent_name_index", None)
    if cached is not None and cached[0] == gen:
        return cached[1]
    index: dict[str, list[str]] = {}
    for nid, n in memory.nodes.items():
        if n.get("type") not in ("Symbol", "Test"):
            continue
        name = (n.get("attrs") or {}).get("name")
        key = memory.key_of(nid)
        if isinstance(name, str) and key:
            index.setdefault(name, []).append(key)
    for keys in index.values():
        keys.sort()
    memory._intent_name_index = (gen, index)
    return index


def defined_keys(memory: Any, pattern: str) -> list[str]:
    if not _IDENT_RE.match(pattern or ""):
        return []
    return list(_name_index(memory).get(pattern, []))


def _rel(root: Path, path: str) -> str | None:
    try:
        return Path(os.path.realpath(path)).relative_to(root).as_posix()
    except ValueError:
        return None


def handle_grep(memory: Any, inp: dict[str, Any], *, base: Path | None = None,
                ignore_case: bool = False,
                select: Any = None) -> dict[str, Any]:
    """The `grep` tool. `select`, when given, is called with the matches as
    (path, line, text) sorted by path and line and returns the ones to show
    (a captured `grep ... | head -N` uses it)."""
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        raise IntentError("pattern is required")
    root = root_of(memory)
    path = inp.get("path")
    target = resolve(root, path, base=base) if path else resolve(
        root, str(base or root))
    if not target.path.exists():
        raise Unhandled(f"no such path: {path}")
    include = inp.get("include") or None
    literal = bool(inp.get("literal"))
    # OpenCode 2.3 grep sends caseSensitive: false for a case-insensitive search.
    ignore_case = ignore_case or inp.get("caseSensitive") is False
    rg = rg_binary()
    if rg:
        found = _rg_grep(rg, root, target.path, pattern, include=include,
                         literal=literal, ignore_case=ignore_case)
    else:
        found = _py_grep(root, target.path, pattern, include=include,
                         literal=literal, ignore_case=ignore_case)
    if select is not None:
        found = list(select(sorted(found, key=lambda m: (m[0], m[1]))))
    by_file: dict[str, list[tuple[int, str]]] = {}
    for p, n, text in found:
        by_file.setdefault(p, []).append((n, text))
    order = sorted(by_file, key=lambda p: (-_mtime(p), p))
    total = len(found)
    defined = defined_keys(memory, pattern)
    keys: list[str] = list(defined)
    lines: list[str] = []
    if defined:
        shown = ", ".join(defined[:MAX_DEFINED_KEYS])
        if len(defined) > MAX_DEFINED_KEYS:
            shown += f", +{len(defined) - MAX_DEFINED_KEYS} more"
        lines.append(tag(f"defined: {shown}"))
    if total == 0:
        lines.append("No files found")
        return {"intent": "grep", "output": "\n".join(lines), "keys": keys}
    truncated = total > MAX_MATCHES
    lines.append(f"Found {total} matches"
                 + (f" (showing first {MAX_MATCHES})" if truncated else ""))
    shown_n = 0
    for p in order:
        if shown_n >= MAX_MATCHES:
            break
        if shown_n:
            lines.append("")
        abs_p = os.path.abspath(p)
        lines.append(f"{abs_p}:")
        rel = _rel(root, abs_p)
        keyer = Keyer(memory, rel) if rel else None
        last = None
        for n, text in sorted(by_file[p]):
            if shown_n >= MAX_MATCHES:
                break
            if keyer is not None:
                k = keyer.key(n)
                if k != last:
                    lines.append(tag(k))
                    last = k
                    if k not in keys:
                        keys.append(k)
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + "..."
            lines.append(f"  Line {n}: {text}")
            shown_n += 1
    if truncated:
        lines.append("")
        lines.append(TRUNCATED.format(shown=MAX_MATCHES, total=total))
    return {"intent": "grep", "output": "\n".join(lines), "keys": keys}


def handle_glob(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    pattern = inp.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise IntentError("pattern is required")
    root = root_of(memory)
    path = inp.get("path")
    target = resolve(root, path) if path else resolve(root, str(root))
    if not target.path.is_dir():
        raise Unhandled(f"not a directory: {path}")
    rg = rg_binary()
    sd, excl = _state_excludes(root)
    files: list[str]
    if rg:
        proc = subprocess.run(
            [rg, "--files", "--hidden", "--no-messages", "--color", "never",
             "--glob", "!.git", *excl, "--glob", pattern, "--",
             str(target.path)],
            capture_output=True, timeout=SEARCH_TIMEOUT_S, cwd=str(root),
            stdin=subprocess.DEVNULL)
        files = [os.fsdecode(b) for b in proc.stdout.split(b"\n") if b]
    else:
        files = [str(p) for p in _walk(root, target.path)
                 if _glob_match(pattern, p.relative_to(target.path).as_posix())]
    files = [os.path.abspath(f) for f in files if not _under(Path(f), sd)]
    files.sort(key=lambda p: (-_mtime(p), p))
    if not files:
        return {"intent": "glob", "output": "No files found", "keys": []}
    keys: list[str] = []
    lines = []
    for f in files[:MAX_GLOB_RESULTS]:
        rel = _rel(root, f)
        info = file_summary(memory, rel) if rel else None
        if info is not None:
            keys.append(f"file:{rel}")
        lines.append(f + role_note(info))
    if len(files) > MAX_GLOB_RESULTS:
        lines.append("")
        lines.append(GLOB_TRUNCATED.format(shown=MAX_GLOB_RESULTS,
                                           total=len(files)))
    return {"intent": "glob", "output": "\n".join(lines), "keys": keys}


# --------------------------------------------------------------------------- #
# Captured shell searches (grep, rg, git grep classes of `intent.shell`)
# --------------------------------------------------------------------------- #
# A record is (kind, abs path, display path, line, text): kind ":" for a
# match, "-" for a context line, "--" for a group separator.
Record = tuple[str, str, str, int, str]


def _inside_root(root: Path, p: Path) -> Path:
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"{p} is outside the project root")
    return real


def shell_grep_resolve(root: Path, cls: dict[str, Any], wd: Path
                       ) -> dict[str, Any]:
    """Check a grep class against the file system before anything runs.
    Unhandled when the capture could differ from the command: a path
    outside the root or missing, a directory without `-r`, overlapping
    paths (the command would print their matches twice), grep `--include`
    with a file operand (grep filters it, rg does not), or rg features
    without rg installed. Returns the targets and whether file names are
    printed (several paths, a directory, no path, or `-H`)."""
    paths = list(cls.get("paths") or ([cls["path"]] if cls.get("path") else []))
    targets: list[tuple[str | None, Path]] = []
    for p in paths:
        q = Path(p)
        real = _inside_root(root, q if q.is_absolute() else wd / q)
        if not real.exists():
            raise Unhandled(f"no such path: {p}")
        if real.is_dir() and not cls["recursive"]:
            raise Unhandled("grep of a directory without -r")
        targets.append((p, real))
    if not targets:
        targets = [(None, wd)]
    reals = [t[1] for t in targets]
    for i, a in enumerate(reals):
        for j, b in enumerate(reals):
            if i != j and (a == b or a in b.parents):
                raise Unhandled("overlapping paths are not mapped")
    globs = list(cls.get("globs") or [])
    if cls["tool"] == "grep" and globs and any(r.is_file() for r in reals):
        raise Unhandled("grep --include with a file operand is not mapped")
    if rg_binary() is None and (cls["tool"] == "rg" or cls.get("types")
                                or len(globs) > 1):
        raise Unhandled("rg is not installed")
    wf = cls.get("with_filename")
    if wf is None:
        wf = not paths or len(targets) > 1 or any(r.is_dir() for r in reals)
    return {"targets": targets, "with_filename": bool(wf)}


def _display(written: str | None, real: Path, wd: Path, abs_p: str) -> str:
    """A path as grep and rg print it: relative to the working directory
    with no path, the operand as written for a file, the operand joined to
    the relative path for a directory."""
    if written is None:
        return os.path.relpath(abs_p, wd)
    if real.is_file():
        return written
    rel = os.path.relpath(abs_p, real)
    return written + ("" if written.endswith("/") else "/") + rel


def _shell_matches(root: Path, cls: dict[str, Any], wd: Path,
                   resolved: dict[str, Any]) -> list[tuple[str, str, int, str]]:
    """(abs path, display path, line, text) of every match, in operand
    order, then by display path and line."""
    rg = rg_binary()
    sd, excl = _state_excludes(root)
    out: list[tuple[str, str, int, str]] = []
    for written, real in resolved["targets"]:
        found: list[tuple[str, str, int, str]] = []
        if rg:
            cmd = [rg, "-n", "-H", "--no-heading", "--null", "--color", "never",
                   "--no-messages", "--max-filesize", str(MAX_READ_BYTES), *excl]
            if cls.get("hidden", True):
                cmd += ["--hidden", "--glob", "!.git"]
            if cls["literal"]:
                cmd.append("-F")
            if cls["ignore_case"]:
                cmd.append("-i")
            for g in cls.get("globs") or []:
                cmd += ["--glob", g]
            for opt, t in cls.get("types") or []:
                cmd += [opt, t]
            cmd += ["-e", cls["pattern"]]
            if written is not None:
                cmd += ["--", written]
            proc = subprocess.run(cmd, capture_output=True, cwd=str(wd),
                                  timeout=SEARCH_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL)
            for raw in proc.stdout.split(b"\n"):
                path_b, sep, rest = raw.partition(b"\0")
                num, _, text = rest.partition(b":")
                if not sep or not num.isdigit():
                    continue
                disp = os.fsdecode(path_b)
                abs_p = os.path.abspath(os.path.join(wd, disp))
                if _under(Path(abs_p), sd):
                    continue
                line = text.decode("utf-8", errors="replace")
                found.append((abs_p, disp, int(num),
                              line[:-1] if line.endswith("\r") else line))
            if proc.returncode == 2 and not found:
                msg = proc.stderr.decode("utf-8", errors="replace").strip()
                if msg:
                    raise IntentError(f"ripgrep failed: {msg}")
        else:
            include = (cls.get("globs") or [None])[0]
            for p, n, text in _py_grep(root, real, cls["pattern"],
                                       include=include,
                                       literal=cls["literal"],
                                       ignore_case=cls["ignore_case"]):
                abs_p = os.path.abspath(p)
                found.append((abs_p, _display(written, real, wd, abs_p), n, text))
        if cls["tool"] == "git grep":
            found = [(a, os.path.relpath(a, wd), n, t) for a, _d, n, t in found]
        found.sort(key=lambda m: (m[1], m[2]))
        out.extend(found)
    return out


def _file_lines(path: str) -> list[str]:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise IntentError(f"cannot read {path}: {e}") from None
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    out = []
    for raw in parts:
        line = raw.decode("utf-8", errors="replace")
        out.append(line[:-1] if line.endswith("\r") else line)
    return out


def shell_grep_records(memory: Any, cls: dict[str, Any], wd: Path,
                       resolved: dict[str, Any]) -> list[Record]:
    """The lines a captured grep prints, as records: matches, the `-A/-B/-C`
    context around them (overlapping and adjacent windows merge), and a
    `--` record before each group after the first when the tool prints
    separators (`cls["separators"]`)."""
    root = root_of(memory)
    matches = _shell_matches(root, cls, wd, resolved)
    before, after = int(cls.get("before") or 0), int(cls.get("after") or 0)
    sep = bool(cls.get("separators"))
    by_file: dict[str, list[tuple[str, int, str]]] = {}
    for abs_p, disp, n, text in matches:
        by_file.setdefault(disp, []).append((abs_p, n, text))
    recs: list[Record] = []
    for disp, found in by_file.items():
        abs_p = found[0][0]
        texts = {n: t for _a, n, t in found}
        lines = _file_lines(abs_p) if (before or after) else []
        total = max(len(lines), max(texts))
        groups: list[list[int]] = []
        for n in sorted(texts):
            lo, hi = max(1, n - before), min(total, n + after)
            if groups and lo <= groups[-1][1] + 1:
                groups[-1][1] = max(groups[-1][1], hi)
            else:
                groups.append([lo, hi])
        for lo, hi in groups:
            if sep and recs:
                recs.append(("--", abs_p, disp, 0, "--"))
            for k in range(lo, hi + 1):
                if k in texts:
                    recs.append((":", abs_p, disp, k, texts[k]))
                else:
                    recs.append(("-", abs_p, disp, k, lines[k - 1]))
    return recs


def grep_raw_line(rec: Record, *, with_filename: bool, line_numbers: bool) -> str:
    """One record as the command prints it: `path:n:text` for a match,
    `path-n-text` for a context line, `--` for a separator."""
    kind, _abs, disp, n, text = rec
    if kind == "--":
        return "--"
    head = (disp + kind if with_filename else "") + (
        f"{n}{kind}" if line_numbers else "")
    return head + text


def render_shell_grep(memory: Any, pattern: str, recs: list[Record]
                      ) -> dict[str, Any]:
    """Records in the grep tool's format (`handle_grep`): matches as
    `  Line n: text`, context lines as `  Line n- text`, a separator inside
    a file as `  --` (one between files is the blank line)."""
    root = root_of(memory)
    total = sum(1 for r in recs if r[0] == ":")
    defined = defined_keys(memory, pattern)
    keys: list[str] = list(defined)
    lines: list[str] = []
    if defined:
        shown = ", ".join(defined[:MAX_DEFINED_KEYS])
        if len(defined) > MAX_DEFINED_KEYS:
            shown += f", +{len(defined) - MAX_DEFINED_KEYS} more"
        lines.append(tag(f"defined: {shown}"))
    if total == 0:
        lines.append("No files found")
        return {"intent": "grep", "output": "\n".join(lines), "keys": keys}
    by_file: dict[str, list[Record]] = {}
    for i, r in enumerate(recs):
        if r[0] == "--":
            prev, nxt = recs[i - 1] if i else None, recs[i + 1] if i + 1 < len(recs) else None
            if prev is None or nxt is None or prev[1] != nxt[1]:
                continue
        by_file.setdefault(r[1], []).append(r)
    order = sorted(by_file, key=lambda p: (-_mtime(p), p))
    truncated = total > MAX_MATCHES
    lines.append(f"Found {total} matches"
                 + (f" (showing first {MAX_MATCHES})" if truncated else ""))
    shown_n = 0
    for p in order:
        if shown_n >= MAX_MATCHES:
            break
        if shown_n:
            lines.append("")
        lines.append(f"{p}:")
        rel = _rel(root, p)
        keyer = Keyer(memory, rel) if rel else None
        last = None
        for kind, _abs, _disp, n, text in by_file[p]:
            if kind == ":" and shown_n >= MAX_MATCHES:
                break
            if kind == "--":
                lines.append("  --")
                continue
            if keyer is not None:
                k = keyer.key(n)
                if k != last:
                    lines.append(tag(k))
                    last = k
                    if k not in keys:
                        keys.append(k)
            if len(text) > MAX_LINE_CHARS:
                text = text[:MAX_LINE_CHARS] + "..."
            lines.append(f"  Line {n}{kind} {text}")
            shown_n += kind == ":"
    if truncated:
        lines.append("")
        lines.append(TRUNCATED.format(shown=MAX_MATCHES, total=total))
    return {"intent": "grep", "output": "\n".join(lines), "keys": keys}


__all__ = ["MAX_MATCHES", "defined_keys", "grep_raw_line", "handle_glob",
           "handle_grep", "render_shell_grep", "rg_binary",
           "shell_grep_records", "shell_grep_resolve"]
