"""File-system answers for captured shell commands.

- `run_find`: a `find` class (`shell._find`) evaluated as GNU find does it
  without `-L`: entries in directory order (`os.scandir`, the `readdir`
  order find uses), depth first, the start path first, symlinks not
  followed, `-prune` stopping the descent, `-maxdepth`/`-mindepth`, and the
  implicit `-print` when the expression has no `-print`. Paths are printed
  as find prints them: the start path as written, then `/name` (no extra
  slash after a start path ending in `/`). `.git` and the state directory
  are walked like any other directory.
- `rg_files_argv`: the argv for an `rg --files` class. It runs rg itself
  with the command's own filters, so ignore files, globs and types behave
  exactly as rg's, sorted by path (`--sort path`: rg's own output order is
  that of its parallel walk, so a sorted list is one of the orders it can
  print).

Both raise `Unhandled` for anything the capture cannot reproduce (a start
path that is a symlink or outside the root, a missing path, a permission
error, more than `FIND_MAX_VISITS` entries).
"""
from __future__ import annotations

import functools
import os
import re
import stat
from pathlib import Path
from typing import Any

from ozagia.intent.common import Unhandled

FIND_MAX_VISITS = 20000
# Lines a find step may print when nothing filters it.
FIND_MAX_OUTPUT = 1000


def _inside(root: Path, p: Path) -> Path:
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"{p} is outside the project root")
    return real


@functools.lru_cache(maxsize=256)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def prepare_find(root: Path, wd: Path, cls: dict[str, Any]
                 ) -> dict[str, Any]:
    """The start paths and `-newer` reference times of a find class, checked
    before anything runs."""
    starts: list[tuple[str, Path]] = []
    for p in cls["paths"]:
        q = Path(p)
        candidate = q if q.is_absolute() else wd / q
        if candidate.is_symlink():
            raise Unhandled("find capture does not follow a symlink start path")
        real = _inside(root, candidate)
        if not os.path.lexists(real):
            raise Unhandled(f"find root does not exist: {p}")
        starts.append((p, real))
    refs: dict[str, int] = {}
    for ref in cls.get("newer") or []:
        q = Path(ref)
        candidate = q if q.is_absolute() else wd / q
        if candidate.is_symlink():
            raise Unhandled("find -newer with a symlink is not mapped")
        real = _inside(root, candidate)
        try:
            refs[ref] = real.stat().st_mtime_ns
        except OSError as e:
            raise Unhandled(f"find -newer {ref}: {e}") from None
    return {"starts": starts, "refs": refs}


def _matches(e: tuple, disp: str, name: str, st: os.stat_result,
             refs: dict[str, int], out: list[str], state: dict[str, bool]
             ) -> bool:
    kind = e[0]
    if kind == "and":
        return (_matches(e[1], disp, name, st, refs, out, state)
                and _matches(e[2], disp, name, st, refs, out, state))
    if kind == "or":
        return (_matches(e[1], disp, name, st, refs, out, state)
                or _matches(e[2], disp, name, st, refs, out, state))
    if kind == "not":
        return not _matches(e[1], disp, name, st, refs, out, state)
    if kind == "name":
        return _rx(e[1]).fullmatch(name) is not None
    if kind == "path":
        return _rx(e[1]).fullmatch(disp) is not None
    if kind == "type":
        test = {"f": stat.S_ISREG, "d": stat.S_ISDIR, "l": stat.S_ISLNK}[e[1]]
        return test(st.st_mode)
    if kind == "newer":
        return st.st_mtime_ns > refs[e[1]]
    if kind == "print":
        out.append(disp)
        return True
    if kind == "prune":
        state["prune"] = True
        return True
    if kind == "const":
        return bool(e[1])
    raise Unhandled(f"find primary {kind} is not mapped")


def run_find(cls: dict[str, Any], prepared: dict[str, Any], *,
             max_output: int = FIND_MAX_OUTPUT) -> list[str]:
    """The lines `find` prints for `cls` (see the module docstring)."""
    expr = cls["expr"]
    implicit = not cls.get("prints")
    maxdepth, mindepth = cls.get("maxdepth"), cls.get("mindepth")
    refs = prepared["refs"]
    out: list[str] = []
    visits = 0

    def visit(disp: str, path: str, name: str, depth: int,
              st: os.stat_result) -> None:
        nonlocal visits
        visits += 1
        if visits > FIND_MAX_VISITS:
            raise Unhandled("find traversal exceeds capture limit")
        state = {"prune": False}
        if mindepth is None or depth >= mindepth:
            if _matches(expr, disp, name, st, refs, out, state) and implicit:
                out.append(disp)
            if len(out) > max_output:
                raise Unhandled("find result exceeds capture limit")
        if (not stat.S_ISDIR(st.st_mode) or state["prune"]
                or (maxdepth is not None and depth >= maxdepth)):
            return
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as e:
            raise Unhandled(f"find traversal failed: {e}") from None
        for ent in entries:
            child = disp + ent.name if disp.endswith("/") else f"{disp}/{ent.name}"
            try:
                cst = ent.stat(follow_symlinks=False)
            except OSError as e:
                raise Unhandled(f"find traversal failed: {e}") from None
            visit(child, os.path.join(path, ent.name), ent.name, depth + 1, cst)

    for written, real in prepared["starts"]:
        try:
            st = os.lstat(real)
        except OSError as e:
            raise Unhandled(f"find traversal failed: {e}") from None
        stripped = written.rstrip("/") or "/"
        visit(written, str(real), stripped.rsplit("/", 1)[-1] or "/", 0, st)
    return out


def rg_files_argv(rg: str, root: Path, wd: Path, cls: dict[str, Any]
                  ) -> list[str]:
    """The rg argv for an `rg --files` class, its paths checked."""
    for p in cls["paths"]:
        q = Path(p)
        real = _inside(root, q if q.is_absolute() else wd / q)
        if not real.exists():
            raise Unhandled(f"no such path: {p}")
    argv = [rg, "--files", "--sort", "path", "--color", "never",
            *cls["options"]]
    if cls["paths"]:
        argv += ["--", *cls["paths"]]
    return argv


__all__ = ["FIND_MAX_OUTPUT", "FIND_MAX_VISITS", "prepare_find",
           "rg_files_argv", "run_find"]
