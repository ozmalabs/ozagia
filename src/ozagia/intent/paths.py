"""Candidates for a path that does not exist.

A read or edit of a missing path (a typo, a file that moved, a path of the
main checkout used in a worktree or the reverse) gets up to
`MAX_CANDIDATES` existing paths, in this order:

1. the same path inside this root, when an absolute path points into
   another worktree of the repository (`git worktree list --porcelain`);
2. the path with each missing component replaced by the closest name in
   its directory (`difflib`), so a typo in a directory name is found too;
3. close names in the same directory;
4. files of the graph with the same base name, those sharing the longest
   path suffix first;
5. the same relative path in the other worktrees (the main checkout
   included).

One line reports them (`suggestion_line`):

    [ozagia] no such file <path>; did you mean: <path> (<key>), ...

Paths inside the root are shown relative to it when the request was
relative, else absolute; each carries its `file:` key when the graph has
one. A path in another worktree is absolute, with `(worktree <branch>)`.
Nothing is ever read or written in place of the missing path: the caller
reports the candidates and the model chooses.

`missing_paths(output)` finds the paths of "No such file or directory"
messages in a shell command's output (`cat`, `sed`, `head`, `nl`, `ls`,
`rg`, `grep`, `cd`, Python).
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ozagia.intent.common import TAG, IntentError, Unhandled

MAX_CANDIDATES = 5
GIT_TIMEOUT_S = 2
# `git worktree list` is cached this long per root.
WORKTREE_TTL_S = 30.0
# difflib ratios: a whole name, and a directory name on the way.
NAME_CUTOFF = 0.6
DIR_CUTOFF = 0.75
SAME_DIR_MATCHES = 3
BASENAME_MATCHES = 3
# Directories with more entries than this are not listed for fuzzy names.
MAX_DIR_ENTRIES = 5000
# Paths taken from one shell output.
MAX_SHELL_PATHS = 3

_wt_cache: dict[str, tuple[float, list[tuple[Path, str | None]]]] = {}


@dataclass(frozen=True)
class Candidate:
    path: Path          # absolute
    rel: str | None     # root-relative POSIX path, None outside the root
    why: str            # "this checkout", "close name", "same name",
    #                     "worktree <branch>"
    key: str | None     # the graph's file key, if it has one


def clear_cache() -> None:
    _wt_cache.clear()


def worktrees(root: str | Path) -> list[tuple[Path, str | None]]:
    """(path, branch) of every worktree of `root`'s repository, as git lists
    them (the main checkout first); [] outside git. Cached briefly."""
    key = str(root)
    now = time.monotonic()
    hit = _wt_cache.get(key)
    if hit is not None and now - hit[0] < WORKTREE_TTL_S:
        return hit[1]
    out: list[tuple[Path, str | None]] = []
    try:
        proc = subprocess.run(["git", "-C", key, "worktree", "list", "--porcelain"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        cur: list[Any] | None = None
        rows: list[list[Any]] = []
        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                cur = [Path(os.path.realpath(line[len("worktree "):])), None]
                rows.append(cur)
            elif cur is not None and line.startswith("branch "):
                cur[1] = line[len("branch "):].removeprefix("refs/heads/")
            elif cur is not None and line == "detached":
                cur[1] = "detached"
            elif cur is not None and line == "bare":
                rows.remove(cur)
                cur = None
        out = [(p, b) for p, b in rows]
    _wt_cache[key] = (now, out)
    return out


def _rel(base: Path, path: Path) -> str | None:
    try:
        rel = path.relative_to(base).as_posix()
    except ValueError:
        return None
    return "" if rel == "." else rel


def _real_prefix(p: Path) -> Path:
    """`p` with symlinks resolved in its longest existing ancestor."""
    tail: list[str] = []
    cur = p
    while not cur.exists() and cur != cur.parent:
        tail.append(cur.name)
        cur = cur.parent
    real = Path(os.path.realpath(cur))
    for name in reversed(tail):
        real = real / name
    return real


def _listdir(d: Path, *, dirs_only: bool = False) -> list[str]:
    try:
        with os.scandir(d) as it:
            names = []
            for i, e in enumerate(it):
                if i >= MAX_DIR_ENTRIES:
                    return []
                if e.name in (".git",):
                    continue
                if dirs_only and not e.is_dir():
                    continue
                names.append(e.name)
            return names
    except OSError:
        return []


def _repair(root: Path, target: Path) -> Path | None:
    """`target` with each missing component (below `root`) replaced by the
    closest name in its directory, if every one has a close name."""
    rel = _rel(root, target)
    if not rel:
        return None
    parts = rel.split("/")
    cur = root
    for i, part in enumerate(parts):
        nxt = cur / part
        if nxt.exists():
            cur = nxt
            continue
        last = i == len(parts) - 1
        names = _listdir(cur, dirs_only=not last)
        got = difflib.get_close_matches(part, names, n=1,
                                        cutoff=NAME_CUTOFF if last else DIR_CUTOFF)
        if not got:
            return None
        cur = cur / got[0]
    return cur if cur != target and cur.exists() else None


def _graph_files(memory: Any) -> list[str]:
    index = getattr(memory, "_key_to_id", None)
    if not isinstance(index, dict):
        return []
    return [k[5:] for k in index if isinstance(k, str) and k.startswith("file:")]


def _suffix_len(a: list[str], b: list[str]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _file_key(memory: Any, rel: str | None) -> str | None:
    if rel is None or memory is None:
        return None
    key = f"file:{rel}"
    try:
        return key if memory.node_by_key(key) is not None else None
    except Exception:  # noqa: BLE001 - a key is only a label here
        return None


def candidates(root: str | Path, raw: Any, *, base: str | Path | None = None,
               memory: Any = None, limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """Existing paths the missing path `raw` (absolute, or relative to `base`,
    default the root) may have meant; see the module docstring. [] when
    `raw` exists or nothing is close."""
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        return []
    root = Path(os.path.realpath(root))
    p = Path(os.path.expanduser(raw.strip()))
    if not p.is_absolute():
        p = Path(base or root) / p
    p = _real_prefix(Path(os.path.normpath(p)))
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(path: Path, why: str) -> None:
        if len(out) >= limit or path == p:
            return
        s = str(path)
        if s in seen or not path.exists():
            return
        seen.add(s)
        rel = _rel(root, path)
        out.append(Candidate(path, rel, why, _file_key(memory, rel)))

    trees = worktrees(root)
    # The deepest worktree holding the path (worktrees may nest).
    owner: Path | None = None
    for w, _b in trees:
        if _rel(w, p) is not None and (owner is None or len(w.parts) > len(owner.parts)):
            owner = w
    rel_in_root = _rel(root, p)
    if owner is not None and owner != root:
        rel = _rel(owner, p)
        if rel:
            add(root / rel, "this checkout")
    else:
        rel = rel_in_root
    if rel:
        target = root / rel
        fixed = _repair(root, target)
        if fixed is not None:
            add(fixed, "close name")
        if target.parent.is_dir():
            names = [n for n in _listdir(target.parent) if (target.parent / n).is_file()]
            for name in difflib.get_close_matches(target.name, names,
                                                  n=SAME_DIR_MATCHES, cutoff=NAME_CUTOFF):
                add(target.parent / name, "close name")
    if memory is not None and p.name:
        want = (rel or p.as_posix()).split("/")
        same = [f for f in _graph_files(memory) if f.rsplit("/", 1)[-1] == p.name]
        same.sort(key=lambda f: (-_suffix_len(f.split("/"), want), len(f), f))
        for f in same[:BASENAME_MATCHES]:
            add(root / f, "same name")
    if rel:
        for w, branch in trees:
            if w not in (root, owner):
                add(w / rel, f"worktree {branch or w.name}")
    return out


def suggestion_line(raw: str, cands: list[Candidate]) -> str:
    """`[ozagia] no such file <raw>; did you mean: ...`, or "" for none."""
    if not cands:
        return ""
    relative = not os.path.isabs(raw)
    parts = []
    for c in cands:
        if c.rel is not None:
            shown = c.rel if relative else str(c.path)
            parts.append(f"{shown} ({c.key})" if c.key else shown)
        else:
            parts.append(f"{c.path} ({c.why})")
    return f"{TAG}no such file {raw}; did you mean: " + ", ".join(parts)


def suggest(root: str | Path, raw: Any, *, base: str | Path | None = None,
            memory: Any = None) -> str:
    """The suggestion line for a missing `raw`, or "" (it exists, or no
    candidate)."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    p = Path(os.path.expanduser(raw.strip()))
    if not p.is_absolute():
        p = Path(base or root) / p
    if p.exists():
        return ""
    return suggestion_line(raw, candidates(root, raw, base=base, memory=memory))


def missing_error(memory: Any, raw: Any, message: str, *,
                  base: str | Path | None = None,
                  intent: str = "path_missing") -> IntentError | Unhandled:
    """A tool error `message` with the suggestion line, or `Unhandled` (the
    built-in reports the miss) when there is no candidate."""
    from ozagia.intent.common import root_of
    line = suggest(root_of(memory), raw, base=base, memory=memory)
    if not line:
        return Unhandled(f"no such file: {raw}")
    return IntentError(f"{message}\n{line}", intent=intent,
                       meta={"path_suggestions": 1})


def with_suggestions(memory: Any, raw: Any, err: IntentError) -> IntentError:
    """`err` (a path refused as outside the root) with the suggestion line
    when the path is missing or lies in another worktree."""
    from ozagia.intent.common import root_of
    root = root_of(memory)
    if not isinstance(raw, str) or not raw.strip():
        return err
    line = suggestion_line(raw, candidates(root, raw, memory=memory))
    if not line:
        return err
    return IntentError(f"{err}\n{line}", keys=err.keys, intent=err.intent,
                       meta={**err.meta, "path_suggestions": 1})


_NSF = "No such file or directory"
_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"")
_LEAD = re.compile(r"^(?:can't read|cannot open|cannot access|cannot stat|"
                   r"can't open file|can't open|cannot read)\s+", re.I)


def missing_paths(output: Any, limit: int = MAX_SHELL_PATHS) -> list[str]:
    """Paths named by "No such file or directory" lines of `output`."""
    if not isinstance(output, str) or _NSF not in output:
        return []
    out: list[str] = []
    for line in output.splitlines():
        i = line.find(_NSF)
        if i < 0:
            continue
        head, tail = line[:i].rstrip().rstrip(":").rstrip(), line[i + len(_NSF):]
        got = None
        m = _QUOTED.search(head)
        if m:
            got = m.group(1) or m.group(2)
        elif tail.startswith(": "):
            m = _QUOTED.search(tail)
            if m:
                got = m.group(1) or m.group(2)
        if got is None and head:
            last = _LEAD.sub("", head.split(": ")[-1].strip())
            if last and not last.startswith("[") and " " not in last.strip():
                got = last
        if got and len(got) < 4096 and got not in out:
            out.append(got)
            if len(out) >= limit:
                break
    return out


__all__ = ["Candidate", "MAX_CANDIDATES", "candidates", "clear_cache",
           "missing_error", "missing_paths", "suggest", "suggestion_line",
           "with_suggestions", "worktrees"]
