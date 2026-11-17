"""The launch status file the TUI sidebar shows while the graph starts.

The interactive `ozagia agent` session does not wait for the snapshot: it
starts the build/refresh and the daemon in the background so the TUI appears
at once. `GraphSession` writes each phase here, and the TUI profile sidebar
(`ozagia-profile`, whose summary is `OzAGIa G:... D:...`) reads the path from
`OZAGIA_LAUNCH_STATUS` in OpenCode's environment and shows `G:building 4s`
while the graph is not ready.

One launch writes one file, under that run's log directory (a new launch has a
new file, so concurrent sessions do not fight over it). Writes replace the
file atomically, so a reader never sees half a document. Every function is
best effort: a launch never fails because its status file could not be
written or read.

Phases: `preparing` (the session is deciding whether to build), `building` and
`refreshing` (written by the prep process), `daemon` (snapshot ready, daemon
starting), `ready`, and `failed`. `action`, `nodes` and `seconds` repeat the
snapshot report; `error` names a failure.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

ENV_LAUNCH_STATUS = "OZAGIA_LAUNCH_STATUS"
VERSION = 1
PHASES = ("preparing", "building", "refreshing", "daemon", "ready", "failed")
# Bounds: a status document is a panel line, not a log.
MAX_BYTES = 64 * 1024
MAX_TEXT = 500


def read(path: str | Path | None) -> dict[str, Any] | None:
    """The current status document, or None when it is absent or malformed."""
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
        if len(text) > MAX_BYTES:
            return None
        doc = json.loads(text)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("version") == VERSION else None


def write(path: str | Path | None, **fields: Any) -> dict[str, Any] | None:
    """Merge `fields` into the file at `path` and replace it atomically.

    Fields another writer put there are kept: the prep process reports
    `building`/`refreshing` over the session's `preparing` document, and the
    session then reports `daemon`/`ready` over the prep's. Returns the
    document that was written, or None when the file could not be updated.
    """
    if path is None:
        return None
    p = Path(path)
    doc = read(p) or {}
    doc.update(fields)
    doc["version"] = VERSION
    doc["updated_at"] = time.time()
    for key in ("detail", "error"):
        value = doc.get(key)
        if isinstance(value, str) and len(value) > MAX_TEXT:
            doc[key] = value[:MAX_TEXT] + "…"
    try:
        payload = json.dumps(doc, sort_keys=True, default=str) + "\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        # A unique name per writer: two processes may hold a document at once
        # (the prep process and the session), and neither may see a copy that
        # the other is still writing.
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)
    except (OSError, TypeError, ValueError):
        return None
    return doc


__all__ = ["ENV_LAUNCH_STATUS", "MAX_BYTES", "MAX_TEXT", "PHASES", "VERSION",
           "read", "write"]
