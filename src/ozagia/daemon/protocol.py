"""The graph daemon protocol: how a harness plugin hands tool calls to ozagia.

A daemon serves one target repository. It listens on a Unix socket, by default
`<state dir>/daemon.sock` (`$OZAGIA_DAEMON_SOCK` overrides it). Unix socket
paths are limited to about 100 bytes, so a longer default path is replaced by
`<tmp dir>/ozagia-<sha256(root)[:16]>.sock`. Messages are
UTF-8 JSON objects, one per line, in both directions. Each request carries an
`id`; the response repeats it. A client may send several requests on one
connection; responses come back in request order.

Requests (`op`):

    {"id": 1, "op": "ping"}
      -> {"id": 1, "ok": true, "version": PROTOCOL_VERSION, "root": "...",
          "pid": 123}

    {"id": 2, "op": "tool", "harness": "opencode", "session": "ses_...",
     "tool": "read", "input": {...the tool input exactly as the model sent it}}
      -> {"id": 2, "ok": true, "handled": true, "output": "<text for the
          model>", "meta": {"intent": "...", "keys": [...], ...}}
      -> {"id": 2, "ok": true, "handled": false, "reason": "..."}

    {"id": 3, "op": "after", "harness": "opencode", "session": "...",
     "tool": "shell", "input": {...}, "output": "<the built-in's output>"}
      -> {"id": 3, "ok": true, "append": "<text to append, or empty>",
          "meta": {...}}

    {"id": 4, "op": "stats"}   -> {"id": 4, "ok": true, "stats": {...}}
    {"id": 5, "op": "shutdown"} -> {"id": 5, "ok": true}

`handled: false` means "run the original built-in": the plugin calls it, then
sends `after` with its output so the daemon can update the graph (for example
re-ingest files a shell command changed) and add context. A daemon error is
`{"ok": false, "error": "..."}`; the plugin then runs the built-in and reports
a fallback. The plugin never fails a tool call because the daemon failed.

Tool names are the harness-neutral names below. A plugin maps its harness's
names onto them (OpenCode 2.3: read, grep, glob, edit, write, patch, shell;
Claude Code: Read, Grep, Glob, Edit, Write, MultiEdit, Bash).

`input` field names follow OpenCode 2.3 (verified): read {path, offset?,
limit?}, grep {pattern, path?, include?, literal?}, glob {pattern, path?},
edit {path, oldString, newString, replaceAll?}, write {path, content}, shell
{command, workdir?, timeout?, description?}. A plugin for another harness
converts its inputs to these names before sending.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from ozagia.config import state_path

PROTOCOL_VERSION = 1
ENV_SOCK = "OZAGIA_DAEMON_SOCK"
SOCK_NAME = "daemon.sock"
TOOLS = ("read", "grep", "glob", "edit", "write", "patch", "shell")
HARNESSES = ("opencode", "claude", "native")
# The largest request or response line a peer must accept, in bytes.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
# Unix socket paths longer than this fall back to the temp dir.
MAX_SOCK_PATH = 100


def socket_path(root: str | Path) -> Path:
    """The daemon socket for `root`."""
    env = os.environ.get(ENV_SOCK)
    if env:
        return Path(env)
    path = state_path(root, SOCK_NAME)
    if len(str(path)) <= MAX_SOCK_PATH:
        return path
    digest = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"ozagia-{digest}.sock"
