"""Bounded, root-relative non-following source reads for offline analyzers."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def read_text(path: Path, limit: int, *, root: Path | None = None) -> tuple[str, int]:
    """Read a regular file via non-following directory descriptors below root."""
    if (limit < 0 or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd):
        raise OSError("safe bounded read unavailable")
    root = Path(root) if root is not None else path.parent
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise OSError("path outside scan root") from exc
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("invalid scan path")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(root, dir_flags)
    try:
        for component in parts[:-1]:
            child = os.open(component, dir_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        child = os.open(parts[-1], file_flags, dir_fd=fd)
    finally:
        os.close(fd)
    with os.fdopen(child, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("not a regular file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file exceeds read limit")
    return raw.decode("utf-8"), len(raw)
