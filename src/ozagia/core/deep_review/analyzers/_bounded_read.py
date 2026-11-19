"""Bounded, non-following source reads for offline analyzers."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def read_text(path: Path, limit: int) -> tuple[str, int]:
    """Read at most limit bytes from a regular file without following its final symlink."""
    if limit < 0 or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("safe bounded read unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("not a regular file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file exceeds read limit")
    return raw.decode("utf-8"), len(raw)
