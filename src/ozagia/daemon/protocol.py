"""The graph daemon protocol: how a harness plugin hands tool calls to ozagia.

A daemon serves one target repository. It listens on a Unix socket, by default
`<state dir>/daemon.sock` (`$OZAGIA_DAEMON_SOCK` overrides it). Unix socket
paths are limited to about 100 bytes, so a longer default path is replaced by
`<tmp dir>/ozagia-<sha256(root)[:16]>.sock`. Messages are
UTF-8 JSON objects, one per line, in both directions. Each request carries an
`id`; the response repeats it. A client may send several requests on one
connection; responses come back in request order. The OpenCode plugin opens
one connection per tool call, so a slow call never queues others behind it.

Requests (`op`):

    {"id": 1, "op": "ping"}
      -> {"id": 1, "ok": true, "version": PROTOCOL_VERSION, "root": "...",
          "pid": 123}

    {"id": 2, "op": "tool", "harness": "opencode", "session": "ses_...",
     "tool": "read", "input": {...the tool input exactly as the model sent it}}
      -> {"id": 2, "ok": true, "handled": true, "output": "<text for the
          model>", "is_error": false, "meta": {"intent": "...", "keys": [...],
          "ms": 1.2, ...}}
      -> {"id": 2, "ok": true, "handled": false, "reason": "...",
          "meta": {...}}
      `meta` always has `intent` (a short name of what was done), `keys`
      (the node keys involved) and `ms` (handler time); `ozagia.intent`
      documents the intents and output formats. A handled answer also
      carries `permission`: the request the harness's own tool would make
      for this call (`{"action", "resources", "save"}`, see
      `ozagia.intent.permission_request`), or null.

    {"id": 2, "op": "tool", ..., "preflight": true}
      For edit, write and shell: run every check but change nothing. If the
      daemon would carry the call out:
      -> {"id": 2, "ok": true, "handled": false, "preflight": true,
          "reason": "permission required", "permission": {...}, "meta": {...}}
      and the plugin asks the harness for `permission` before sending the
      call again without `preflight`. A call that would be passthrough or
      a tool error gets its usual answer (it changes nothing). Other tools
      ignore `preflight`; the plugin asks for their `permission` after the
      answer and before showing the output.

    {"id": 3, "op": "after", "harness": "opencode", "session": "...",
     "tool": "shell", "input": {...}, "output": "<the built-in's output>"}
      -> {"id": 3, "ok": true, "append": "<text to append, or empty>",
          "meta": {"intent": "...", "keys": [...], "ms": 1.2}}

    {"id": 4, "op": "hello", "harness": "opencode", "plugin": "ozagia",
     "version": "0.1.0", "session": "...", "phase": "setup"|"session",
     "tools": {"<name>": {"codemode": false}, ...}, "permission_assert": true}
      -> {"id": 4, "ok": true}
      A plugin sends `hello` when it loads (`phase: "setup"`, no session)
      and once for each new session (`phase: "session"`). The daemon
      records it, so a session whose plugin never loaded is visible
      (`stats.hellos`). `tools` reports the options of the tools it
      replaced (`stats.codemode_tools` lists those not `codemode: false`)
      and `permission_assert` whether the harness lets it ask permission.

    {"id": 5, "op": "stats"}   -> {"id": 5, "ok": true, "stats": {...}}
    {"id": 6, "op": "shutdown"} -> {"id": 6, "ok": true}

    {"id": 7, "op": "session_event", "harness": "codex", "session": "...",
     "event": "compact"|"start"|"end"}
      -> {"id": 7, "ok": true, "cleared": <ledgers touched>, "meta": {...}}
      The harness compacted the session's context (`compact`), started or
      cleared it (`start`), or ended it (`end`). `compact` and `start`
      clear the read ledger of every `sub` of (harness, session); `end`
      drops them. Sent by the OpenCode plugin's `compaction` session hook,
      Codex `PreCompact`/`SessionStart`/`SessionEnd` and Claude Code
      `SessionStart`/`SessionEnd`.

    {"id": 8, "op": "read_check", "harness": "claude", "session": "...",
     "sub": "...", "tool": "read"|"shell", "input": {...}}
      -> {"id": 8, "ok": true, "repeat": bool, "policy": "stub"|"note"|"off",
          "stub": "<one line>"|null, "meta": {...}}
      For a hook's PreToolUse: does this read, which the harness is about
      to run itself, repeat an unchanged range the session already read?
      `stub` is set only under the `stub` policy; the hook then denies the
      call with it as the reason. Nothing is recorded (the call's `after`
      records it). Takes the graph lock and honours `deadline_ms`.

Sessions. `tool`, `after` and `read_check` with a `session` use that
session's read ledger (`ozagia.intent.ledger`), keyed by (harness, session,
`sub`). `sub` is optional: agents that share a session id send something
that tells them apart (Claude Code subagents: `agent_id`; Codex:
`transcript_path`, plus `agent_id` when present). A served read that
repeats an unchanged range gets a stub or a note by the harness's repeat
policy (`meta.repeat`, `meta.bytes_saved`; see `docs/intent.md`).

Ledger fields on `tool`, `after`, `read_check` and `session_event` (all
optional):
    acks             tokens (`meta.ack` of earlier served reads) whose output
                     reached the model; a served read is recorded only once
                     acked. The OpenCode plugin sends them on its next
                     request for the session.
    reset_pending    true: a `session_event` for the session failed earlier;
                     applied as `compact` before the request (after `acks`).
    compaction_hook  true: the sender reports compaction for this session
                     (the plugin's `compaction` hook registered; also in
                     `hello`), so a configured `stub` may apply.
    delivered        `after` only: `{"start": n, "lines": k}`, the lines the
                     harness says it showed (Claude Code Read's
                     `file.startLine`/`numLines`). `after` records only the
                     lines `output` shows in any case.

Deadlines. Every `tool` and `after` request may carry `"deadline_ms"`: an
absolute Unix epoch time in milliseconds, after which the sender no longer
waits for the answer (the plugin sends `Date.now() + its timeout`). The
daemon checks it when the request's turn comes (after taking the graph
lock, before the handler runs). An expired request does nothing:

    tool  -> {"id": ..., "ok": true, "handled": false, "reason": "expired",
              "expired": true, "meta": {...}}
    after -> {"id": ..., "ok": true, "append": "", "expired": true,
              "meta": {...}}

A request that started before its deadline runs to the end; a handled
answer that finished after it carries `"late": true`. A plugin that gave up
on an `edit`, `write` or `shell` it had already sent cannot know whether it
ran, so it does not run the built-in: it reports an "outcome unknown" tool
error and sends `after` with `meta.fallback = "unknown_outcome"`.

`after` may carry `"meta": {"fallback": "<why>"}`: the plugin ran the
built-in (or gave up) because the daemon failed or timed out. The daemon
counts these (`stats.plugin_fallbacks`).

Reserved input fields. Keys of `input` that start with `_` are reserved for
the daemon and in-process callers (`ozagia.intent` documents them); the
daemon drops any a client sends.

A handled `tool` response with `"is_error": true` is a tool error: the plugin
reports `output` to the model as the tool's error (for OpenCode, by throwing
an Error with that message), not as a successful result.

`after` applies to every tool, handled or not by a capture plugin: a
hook-only harness (Claude Code) runs every built-in itself and sends `after`
for each, so the daemon re-ingests what changed and returns context (graph
headers for a read range, grouping for grep matches, the graph delta and
broken references for an edit) in `append`.

`handled: false` means "run the original built-in": the plugin calls it, then
sends `after` with its output so the daemon can update the graph (for example
re-ingest files a shell command changed) and add context. A daemon error is
`{"ok": false, "error": "..."}` (with `"id": null` when the request line was
not a JSON object; a line longer than MAX_MESSAGE_BYTES gets that error and
the connection is closed); the plugin then runs the built-in and reports
a fallback. The plugin never fails a tool call because the daemon failed,
except an `edit`, `write` or `shell` whose request was sent and then failed
or timed out (outcome unknown, see Deadlines). A daemon that cannot be
reached at all (connection refused) gets the built-in for every tool.

Command line: `python -m ozagia.daemon serve --root <repo> [--dump <graph>]
[--journal <jsonl>] [--sock <path>] [--parent-pid <pid>]` loads (or, if
missing, builds) the snapshot, listens, and prints one line `ready <socket
path>` on stdout when it accepts connections. It exits on `shutdown`,
SIGTERM or SIGINT, or (with `--parent-pid`) within `PARENT_POLL_S` seconds
of that process going away, and removes its socket.

Tool names are the harness-neutral names below. A plugin maps its harness's
names onto them (OpenCode 2.3: read, grep, glob, edit, write, patch, shell;
Claude Code: Read, Grep, Glob, Edit, Write, MultiEdit, Bash; Codex: Bash as
shell, apply_patch as patch).

`input` field names follow OpenCode 2.3 (verified): read {path, offset?,
limit?}, grep {pattern, path?, include?, literal?}, glob {pattern, path?},
edit {path, oldString, newString, replaceAll?}, write {path, content}, shell
{command, workdir?, timeout?, description?}, patch {patch} (the patch text;
`after` only: the daemon re-ingests what it changed). A plugin for another harness
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
HARNESSES = ("opencode", "claude", "codex", "native")
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
