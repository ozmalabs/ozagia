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

    {"id": 9, "op": "path_check", "path": "...", "cwd"?: "..."}
      -> {"id": 9, "ok": true, "exists": bool, "line": "[ozagia] no such
          file ...; did you mean: ..." | "", "candidates": [{"path", "rel",
          "key", "why"}], "graph"?: bool, "meta": {...}}
      For a hook's PreToolUse read of a path: the nearest existing paths
      when it is missing (`ozagia.intent.paths`). Changes nothing. Never
      waits on the graph lock: while it is held (a reindex), the answer
      has no graph candidates or keys (`graph: false`).

    {"id": 10, "op": "owner_check", "harness", "session", "paths": [...],
     "cwd"?: "..."}
      -> {"id": 10, "ok": true, "warnings": ["[ozagia] warning: ..."],
          "meta": {...}}
      Before a hook-run edit: the files another live session edited and
      left uncommitted (`ozagia.core.owners`). Never blocks anything.

    {"id": 11, "op": "who", "path": "..."}
      -> {"id": 11, "ok": true, "who": {"path", "dirty", "last_commit",
          "sessions": [{"harness", "session", "edits", "first", "last",
          "last_age", "live", "committed"}]}}

    Any request with a `session` (and `hello`) marks that session live.

    {"id": 12, "op": "working_set", "harness": "codex", "session": "...",
     "sub"?: "...", "max_chars"?: 7000, "since"?: false,
     "free_text"?: true}
      -> {"id": 12, "ok": true, "text": "<[ozagia] working set ...>" | "",
          "meta": {"intent": "working_set"|"working_set_empty"|"session_brief",
                   "keys", "harness", "session", "resolved", "files",
                   "edited_keys", "tests", "reads", "jobs", "chars"}}
      The session's working set (`ozagia.core.working_set`): files and keys
      it edited, tests failing in its last pytest runs with a rerun
      command, running jobs, the newest open DevSession's step, recent
      reads; at most `max_chars` (default and maximum 7,000). It survives
      `session_event` `compact`/`start`. With `sub`, that sub-key's state
      when it has one, else all of the session's. Without `session`, the
      session whose working set changed last (of `harness`, when given;
      `meta.resolved: "latest"`): the MCP tool `dev_working_set` uses this,
      since an MCP server does not know the harness's session id. `text` is
      empty when the daemon has nothing for the session. With
      `since: true`, the session-start brief instead (git status, recent
      commits, the root's failing tests, running jobs; at most 1,500).
      `free_text: false` keeps only paths, keys, test ids and job ids: no
      failure messages, command lines or DevSession titles (the OpenCode
      plugin, which puts the text in the system prompt).

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

Background jobs (`ozagia.daemon.jobs` documents the fields and semantics).
Each takes optional `session`/`harness`, and `reader` (default: `session`,
else "default"), whose log position `job_wait` and `job_log` use:

    {"op": "job_start", "command": "<shell line>" | "argv": [...], "cwd"?,
     "env"?: {...}, "detach"?: bool}
      -> {"ok": true, "job": {"id": "j1", "pid", "pgid", "status", "log", ...}}
    {"op": "job_wait", "job": "j1" | "any", "until"?: "exit" | "file:<path>" |
     "log:<regex>", "deadline_s"?: 45, "cursor"?: <byte offset>}
      -> {"ok": true, "job", "met", "status", "exit_code", "elapsed_s",
          "waited_s", "output": "<new lines>", "cursor", "skipped_lines",
          "skipped_bytes", "match"?, "note"?}
    {"op": "job_list"} -> {"ok": true, "jobs": [...], "running": n}
    {"op": "job_log", "job", "cursor"?, "tail"?}
      -> {"ok": true, "job", "status", ..., "output", "cursor"}
    {"op": "job_stop", "job", "signal"?: "TERM", "grace_s"?: 3}
      -> {"ok": true, "job", "status", "exit_code", "signal", ...}
      (`job_list` and `job_start` give job records with `id`; replies that
      are one job name it `job`.)
    {"op": "job_events", "reader", "after_seq"?: n, "deadline_s"?: 300}
      -> {"ok": true, "events": [{"seq", "job", "command", "status",
          "exit_code", "elapsed_s", "session", "harness"}], "running": n,
          "seq": <the latest event's seq>}
      Job ends after `after_seq` (default: the last `seq` this reader was
      given; a new reader: from now). Blocks while a job runs and there is
      none (the OpenCode plugin's completion announcements). While a job
      runs, `tool` and `after` answers carry `meta.jobs_running` and
      `meta.job_seq` (the `seq` to watch from).

    {"op": "job_ps", "command": "ps -p 123"}
      -> {"ok": true, "output": "<what the command prints>" | null}
      The shell capture of `pgrep`/`ps -p` about a job (null: not one), for
      hooks that answer before the command runs (Codex PreToolUse).

A bad job request is `{"ok": false, "error": "..."}`; so is `job_start`
while the project does not enable jobs (`ozagia.daemon.jobs.jobs_enabled`),
with an error that says how to. A job record may carry `leader_exited` and
`group_alive` (the command exited, other processes of its group run; the
job is still `running`) and `stopped_on_load`. A `job_wait` holds its
connection until it returns: give the client a timeout of `deadline_s` plus
a margin.

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
shell, apply_patch as patch; Kiro: see `ozagia.integrations.kiro.hook`).

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
TOOLS = ("read", "grep", "glob", "edit", "write", "patch", "shell", "replace")
HARNESSES = ("opencode", "claude", "codex", "kiro", "native")
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
