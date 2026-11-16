"""Bounded, opt-in discovery for offline infrastructure source analysis.

This module never executes a project or an infrastructure tool. State, input
variable files, credential files, symlinks, and repository state are excluded.
Callers must not persist or return raw contents: IaC source itself can contain
secrets, even in otherwise ordinary configuration files.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterator

from ozagia.config import load_project_config, state_rel

MAX_FILES = 5000
MAX_FILE_BYTES = 1024 * 1024
_EXTRA_SKIP = frozenset({".terraform", ".terragrunt-cache", ".pulumi", ".kube"})


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
    count = 0
    for current, dirs, files in os.walk(base, followlinks=False):
        directory = Path(current)
        rel_dir = directory.relative_to(base).as_posix()
        dirs[:] = sorted(d for d in dirs if d not in cfg.memory_skip_dirs
                         and d not in _EXTRA_SKIP
                         and not (directory / d).is_symlink()
                         and (not state or (Path(rel_dir) / d).as_posix() != state))
        for name in sorted(files):
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
