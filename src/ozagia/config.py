"""Where ozagia works: the target repository and its state directory.

The target repository is `$OZAGIA_ROOT` if set, else the Git top level of the
current directory, else the current directory. ozagia never assumes it is
installed inside the repository it works on.

State (graph snapshot, journal, tag cache, sessions, edit backups) lives in
`<root>/.ozagia/` unless `$OZAGIA_STATE_DIR` names another directory (an
absolute path, or one relative to the root). Add `.ozagia/` to the target's
`.gitignore`; the indexers skip it either way.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENV_ROOT = "OZAGIA_ROOT"
ENV_STATE_DIR = "OZAGIA_STATE_DIR"
STATE_DIRNAME = ".ozagia"


def repo_root(start: str | Path | None = None) -> Path:
    """The repository ozagia acts on (see the module docstring)."""
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).resolve()
    base = Path(start) if start is not None else Path.cwd()
    try:
        proc = subprocess.run(["git", "-C", str(base), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()
    return base.resolve()


def state_dir(root: str | Path) -> Path:
    """The state directory for `root`."""
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        p = Path(env)
        return (p if p.is_absolute() else Path(root) / p).resolve()
    return Path(root).resolve() / STATE_DIRNAME


def state_path(root: str | Path, *parts: str) -> Path:
    """A path inside the state directory of `root`."""
    return state_dir(root).joinpath(*parts)
