"""Bounded, opt-in discovery for offline infrastructure source analysis.

This module never executes a project or an infrastructure tool. State, input
variable files, credential files, symlinks, and repository state are excluded.
Callers must not persist or return raw contents: IaC source itself can contain
secrets, even in otherwise ordinary configuration files.
"""
from __future__ import annotations

import os
import stat
import fnmatch
from pathlib import Path
from typing import Iterator

from ozagia.config import (
    DEFAULT_IAC_SKIP_GLOBS,
    load_project_config,
    state_rel,
)

MAX_FILES = 5000
MAX_FILE_BYTES = 1024 * 1024
_EXTRA_SKIP = frozenset({".terraform", ".terragrunt-cache", ".pulumi", ".kube"})
DEFAULT_SKIP_GLOBS = DEFAULT_IAC_SKIP_GLOBS
_STATUS_PATH_LIMIT = 500
_STATUS_SCAN_LIMIT = 50_000


def _skip_patterns(root: Path) -> tuple[str, ...]:
    cfg = load_project_config(root)
    return cfg.iac_skip_globs


def _matches_skip(rel: str, patterns: tuple[str, ...]) -> bool:
    name = Path(rel).name
    for pattern in patterns:
        if (fnmatch.fnmatchcase(rel, pattern)
                or fnmatch.fnmatchcase(name, pattern)):
            return True
        if pattern.endswith("/**"):
            directory = pattern[:-3].rstrip("/")
            if rel == directory or rel.startswith(directory + "/"):
                return True
    return False


def skipped_summary(root: str | Path) -> dict[str, object]:
    """Return bounded names/counts of excluded IaC paths, never file contents."""
    base = Path(root).resolve()
    cfg = load_project_config(base)
    patterns = _skip_patterns(base)
    count = 0
    visited = 0
    paths: list[str] = []
    truncated = False

    def record(rel: str) -> None:
        nonlocal count, truncated
        count += 1
        if len(paths) < _STATUS_PATH_LIMIT:
            paths.append(rel)
        else:
            truncated = True

    for current, dirs, files in os.walk(base, followlinks=False):
        visited += 1 + len(dirs) + len(files)
        if visited > _STATUS_SCAN_LIMIT:
            truncated = True
            break
        directory = Path(current)
        rel_dir = directory.relative_to(base).as_posix()
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if (name in cfg.memory_skip_dirs or name in _EXTRA_SKIP
                    or _matches_skip(rel, patterns)
                    or (directory / name).is_symlink()):
                if name not in cfg.memory_skip_dirs or name in _EXTRA_SKIP \
                        or _matches_skip(rel, patterns):
                    record(rel)
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if _matches_skip(rel, patterns):
                record(rel)
    return {"count": count, "paths": paths, "truncated": truncated}


def is_skipped_path(root: str | Path, rel: str) -> bool:
    """Check a repository-relative path against IaC's merged skip policy."""
    base = Path(root).resolve()
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        return True
    cfg = load_project_config(base)
    parts = path.parts
    if any(part in cfg.memory_skip_dirs or part in _EXTRA_SKIP for part in parts):
        return True
    patterns = _skip_patterns(base)
    return any(_matches_skip(Path(*parts[:end]).as_posix(), patterns)
               for end in range(1, len(parts) + 1))


def source_kind(name: str) -> str | None:
    """Classify only source formats; state and variable files never qualify."""
    value = name.lower()
    if (value in {"plan.json", "tfplan.json", "terraform-plan.json"}
            or (value.startswith("pulumi.") and value.endswith((".yaml", ".yml"))
                and value not in {"pulumi.yaml", "pulumi.yml"})
            or value.startswith((".env", "kubeconfig", "terraform.tfstate"))
            or ".tfstate" in value or value.endswith((".tfvars", ".tfvars.json", ".pem", ".key"))):
        return None
    if value.endswith((".tf", ".tf.json", ".tofu", ".tofu.json")):
        return "terraform"
    if value.endswith((".bicep", ".arm")):
        return "arm"
    if value.endswith((".yaml", ".yml", ".json")):
        return "manifest"
    return None


def iter_candidate_paths(root: str | Path, *, max_files: int = MAX_FILES
                         ) -> Iterator[tuple[str, str]]:
    """List eligible paths without opening their contents."""
    base = Path(root).resolve()
    cfg = load_project_config(base)
    state = state_rel(base)
    patterns = cfg.iac_skip_globs
    count = 0
    for current, dirs, files in os.walk(base, followlinks=False):
        directory = Path(current)
        rel_dir = directory.relative_to(base).as_posix()
        dirs[:] = sorted(d for d in dirs if d not in cfg.memory_skip_dirs
                         and d not in _EXTRA_SKIP
                         and not _matches_skip(
                             d if rel_dir == "." else f"{rel_dir}/{d}", patterns)
                         and not (directory / d).is_symlink()
                         and (not state or (Path(rel_dir) / d).as_posix() != state))
        for name in sorted(files):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if _matches_skip(rel, patterns):
                continue
            kind = source_kind(name)
            if not kind:
                continue
            count += 1
            if count > max(0, max_files):
                return
            path = directory / name
            if not path.is_symlink():
                yield path.relative_to(base).as_posix(), kind


def read_source(base: str | Path, rel: str, *,
                max_bytes: int = MAX_FILE_BYTES) -> str | None:
    """Read one discovered source without following symlinks or blocking.

    Returns ``None`` for special files, symlinks, oversized files and invalid
    UTF-8. Callers must not persist or return the raw text.
    """
    root = Path(base).resolve()
    path = root / rel
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(
            os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                return None
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        return None


def iter_sources(root: str | Path, *, max_files: int = MAX_FILES
                 ) -> Iterator[tuple[str, str, str]]:
    """Yield bounded UTF-8 source, excluding symlinks and special files.

    Parsing and selecting safe fields are the responsibility of the consumer.
    """
    base = Path(root).resolve()
    for rel, kind in iter_candidate_paths(base, max_files=max_files):
        text = read_source(base, rel)
        if text is not None:
            yield rel, kind, text
