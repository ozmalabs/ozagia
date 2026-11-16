"""The Codex hook: graph context for Codex's shell and patch calls.

Codex (0.169) runs this as a command hook (see `install.py`). It reads one
hook event as JSON on stdin and answers on stdout, following the Codex hooks
reference (https://learn.chatgpt.com/docs/hooks, see docs/codex.md).

- `PreToolUse` for `Bash`: refuse ref-moving git commands with
  `permissionDecision: "deny"`. Local; no daemon needed. A shell read
  (`sed -n`, `cat`, `nl -ba F | sed -n ...`) is checked with the daemon's
  `read_check`: under the `stub` repeat policy a repeat of an unchanged
  range is denied with the one-line stub as the reason. The default
  policy is `note` (`ozagia.intent.ledger`), so this denies only when
  `[reads]` opts in. Codex is a poor candidate: subagent hooks carry the
  parent's session id, and exec output is truncated (the ledger records
  only the lines the output shows). A command with a `cd` is not tracked.
- `PreToolUse` for `apply_patch`: check the patch against the current files
  as Codex would apply it (`ozagia.intent.patch.check_patch`, local). If
  every hunk matches, print nothing. If a hunk would fail ("Failed to find
  expected lines"), deny with the nearest region of each failing hunk (its
  current text with line numbers, range, node key, file sha256), so the
  agent can fix the patch without reading the file again. On a local miss
  only, a running daemon (never started for this) is asked too, for its
  graph keys and to count the miss; it has `PATCH_DAEMON_TIMEOUT_S`. A patch
  that matches never waits for the daemon. A patch that does not parse, one
  with `*** Environment ID:` (for another environment's files), or any
  checker error, prints nothing.
- `PreToolUse` for `Bash`, a `pgrep` or `ps -p` command: when a daemon
  answers and the command is about one of its background jobs
  (`ozagia.daemon.jobs.capture_ps`), deny it with the answer (the
  processes, and the job's status) as the reason. No daemon is started
  for this.
- `PostToolUse` for `Bash`: send `after` as `shell` ({command, workdir})
  with the command's output, and return the daemon's `append` as
  `additionalContext` (with the ledger's `unchanged since read N` note).
- `PostToolUse` for `apply_patch`: send `after` as `patch` ({patch}); the
  daemon re-ingests the changed files and returns the graph delta.
- `SessionStart`: a short brief "since the last session" (dirty files,
  recent commits, tests failing at the last pytest run, running jobs, the
  ozagia MCP tools to prefer; `ozagia.core.working_set.session_brief`) as
  `additionalContext`, and a start of the daemon so it is loaded by the
  first tool call. With source "compact", the session's working set
  instead (files and keys it edited, failing tests and their rerun
  command, jobs, recent reads; at most `WORKING_SET_MAX_CHARS`, under
  Codex's 2,500-token hook output cap), or the brief when the daemon has
  none. Outside a git work tree it prints nothing. It also sends `session_event`: `compact` for source "compact",
  `start` otherwise, which clears the session's read ledger; it carries
  `compaction_hook: true` (PreCompact is installed with it).
- `PreCompact`: `session_event` `compact`; `SessionEnd`: `end`. A failed
  compact or start leaves a marker; the session's next ledger request
  (`after`, `read_check`) carries `reset_pending`.

Ledger requests carry `sub`: the payload's `transcript_path` (and
`agent_id` when present), so threads sharing a session id are kept apart
where Codex tells them apart.

The daemon is started on demand (`ozagia.daemon.autostart.ensure`, a few
seconds at most) when nothing answers on its socket, and only for a git
work tree, `$OZAGIA_ROOT` or a root with a graph snapshot
(`hookcore.autostart_allowed`). The hook stops itself after
`HOOK_ALARM_S`, a few seconds before Codex's handler timeout. It never blocks
the agent because ozagia failed: any error prints nothing and exits 0, and
is logged as one JSON line to `<state dir>/codex_hook.log`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, TextIO

from ozagia.daemon import protocol
from ozagia.core import working_set as W
from ozagia.integrations import hookcore
from ozagia.intent.patch import check_patch, patch_text

# Codex's handler timeout, which install.py writes to hooks.json.
HOOK_TIMEOUT_S = 20
# The hook stops itself (prints nothing) after this many seconds, before
# Codex kills it.
HOOK_ALARM_S = 15
# One daemon request: with AUTOSTART_BUDGET_S (3 s) and the root lookup it
# stays under HOOK_ALARM_S.
DAEMON_TIMEOUT_S = 10
# A PreToolUse `ps`/`pgrep` check asks a running daemon for this long.
PS_TIMEOUT_S = 3
LOG_NAME = "codex_hook.log"
HARNESS = "codex"
# The SessionStart brief stays under this many characters.
BRIEF_MAX_CHARS = W.BRIEF_MAX_CHARS
# SessionStart only kicks the daemon off; it does not wait for the load.
SESSION_START_BUDGET_S = 0.5
# After the alarm fired during the SessionStart daemon start, the brief gets
# this long (its git calls take GIT_TIMEOUT_S each at most); HOOK_ALARM_S
# plus this stays under HOOK_TIMEOUT_S.
BRIEF_ALARM_S = 3
# SessionStart's `session_event` (the ledger reset) waits this long at most.
SESSION_EVENT_TIMEOUT_S = 1.0
# PreToolUse apply_patch: how long the hook waits for the daemon's keyed
# answer, and the largest deny reason. Codex shows the model about 2,500
# tokens of hook output; 7,000 characters of code and prose stay under it.
PATCH_DAEMON_TIMEOUT_S = 1.5
PATCH_REPLY_MAX_CHARS = 7000

# The brief's lines on the MCP tools and jobs (`ozagia.core.working_set`).
MCP_TOOLS_LINE = W.MCP_TOOLS_LINE
JOBS_LINE = W.JOBS_LINE
# SessionStart after a compaction: the working set, at most this many
# characters (about 2,000 tokens; Codex shows 2,500 tokens of hook output
# without `additionalContextLimit`), asked of the daemon for this long.
WORKING_SET_MAX_CHARS = W.TEXT_MAX_CHARS
WORKING_SET_TIMEOUT_S = 3.0


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #
def output_text(tool_response: Any) -> str:
    """The tool's result as text. Codex sends a local tool's model-facing
    output, normally a string; an object with an `output` string is taken
    as that string, anything else as in `hookcore.response_text`."""
    if isinstance(tool_response, dict) and isinstance(tool_response.get("output"), str):
        return tool_response["output"]
    return hookcore.response_text(tool_response)


def map_tool(tool_name: str, tool_input: Any,
             cwd: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """`(neutral tool, protocol input)` for a Codex call, or None."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    command = ti.get("command")
    if tool_name == "Bash":
        if not isinstance(command, str):
            return None
        out: dict[str, Any] = {"command": command}
        if cwd:
            out["workdir"] = cwd
        return "shell", out
    if tool_name == "apply_patch":
        text = command if isinstance(command, str) else ti.get("patch")
        if not isinstance(text, str):
            return None
        return "patch", {"patch": text}
    return None


# --------------------------------------------------------------------------- #
# SessionStart brief
# --------------------------------------------------------------------------- #
def session_brief(root: Path, max_chars: int = BRIEF_MAX_CHARS) -> str:
    """Dirty files, the last commits, the tests failing at the last pytest
    run, running jobs and the MCP tools to prefer (`hookcore.session_brief`:
    from the daemon when one answers, else from git and the state dir)."""
    return hookcore.session_brief(root, max_chars)


# --------------------------------------------------------------------------- #
# PreToolUse apply_patch: patch miss recovery
# --------------------------------------------------------------------------- #
def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _daemon_check(root: Path, payload: dict[str, Any], text: str,
                  cwd: str | None) -> dict[str, Any] | None:
    """The daemon's `patch_check` answer (graph keys, and the counters), or
    None when no daemon answers within PATCH_DAEMON_TIMEOUT_S. Never starts
    one."""
    deadline = int((time.time() + PATCH_DAEMON_TIMEOUT_S) * 1000)
    inp: dict[str, Any] = {"patch": text, "max_chars": PATCH_REPLY_MAX_CHARS}
    if cwd:
        inp["workdir"] = cwd
    try:
        resp = hookcore.daemon_request(protocol.socket_path(root), {
            "id": 1, "op": "tool", "harness": HARNESS,
            "session": payload.get("session_id") or "", "tool": "patch_check",
            "input": inp, "deadline_ms": deadline},
            timeout=PATCH_DAEMON_TIMEOUT_S)
    except (OSError, ValueError, hookcore.HookError):
        return None
    if not resp.get("handled") or resp.get("is_error"):
        return None
    return resp


def pre_apply_patch(payload: dict[str, Any], root: Path,
                    tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Check the patch against the current files (`ozagia.intent.patch`):
    print nothing when every hunk matches, deny with the nearest regions
    when one would fail. A patch that does not parse, or a checker error,
    prints nothing (Codex reports its own errors)."""
    text = patch_text(tool_input)
    if not text:
        return None
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None
    try:
        local = check_patch(root, text, cwd=cwd, max_chars=PATCH_REPLY_MAX_CHARS)
    except Exception as exc:  # noqa: BLE001 - fail open
        hookcore.log_failure(root, LOG_NAME, payload, exc)
        local = None
    if local is None or not local.get("parsed") or not local.get("misses"):
        return None
    # Only on a local miss: the daemon (if one runs) checks it again with the
    # graph's keys and counts it; its answer is used for the reason only.
    resp = _daemon_check(root, payload, text, cwd)
    reason = local["reply"]
    meta = (resp or {}).get("meta") or {}
    if (meta.get("intent") == "patch_check_miss"
            and isinstance(resp.get("output"), str) and resp["output"]):
        reason = resp["output"]
    return _deny(reason[:PATCH_REPLY_MAX_CHARS])


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def _context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def session_sub(payload: dict[str, Any]) -> str:
    """The ledger sub-key of a Codex event: its `transcript_path` and, when
    the payload has one, `agent_id`. Codex documents that subagent hooks
    carry the parent's session id; these keep threads apart where Codex
    sends them (see `ozagia.intent.ledger`)."""
    parts = [payload.get(k) for k in ("transcript_path", "agent_id")]
    return "|".join(p for p in parts if isinstance(p, str) and p)


def job_ps_answer(root: Path, command: str, session: str = "") -> str | None:
    """The daemon's answer to a `pgrep`/`ps -p` command about one of its
    jobs, as a deny reason, or None (no such form, no daemon, not a job)."""
    head = command.lstrip().split(None, 1)[:1]
    if head not in (["ps"], ["pgrep"]):
        return None
    from ozagia.daemon import protocol
    try:
        resp = hookcore.daemon_request(protocol.socket_path(root), {
            "id": 1, "op": "job_ps", "harness": HARNESS, "session": session,
            "command": command}, PS_TIMEOUT_S)
    except (OSError, ValueError, hookcore.HookError):
        return None
    out = resp.get("output")
    if not isinstance(out, str):
        return None
    return ("[ozagia] answered from the ozagia job table instead of running "
            f"`{command.strip()[:200]}` (same output):\n{out}")


def handle(payload: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """The hook's JSON answer for one event, or None to print nothing."""
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session = payload.get("session_id") or ""
    cwd = payload.get("cwd")
    sub = session_sub(payload)

    if event == "PreCompact":
        hookcore.session_event(root, HARNESS, session, "compact")
        return None
    if event == "SessionEnd":
        hookcore.session_event(root, HARNESS, session, "end")
        return None

    if event == "SessionStart":
        in_repo = hookcore.in_work_tree(root)
        try:
            hookcore.ensure_daemon(root, SESSION_START_BUDGET_S, in_repo=in_repo)
        except hookcore.HookTimeout as exc:
            # The alarm is spent; arm it again so the brief is bounded too.
            hookcore.log_failure(root, LOG_NAME, payload, exc)
            hookcore.rearm_alarm(BRIEF_ALARM_S)
        except Exception as exc:  # the brief does not need the daemon
            hookcore.log_failure(root, LOG_NAME, payload, exc)
        if not in_repo:
            return None
        # After a compaction, the session's working set (it survives the
        # ledger reset below); the brief when the daemon has none.
        text = None
        if payload.get("source") == "compact":
            text = hookcore.working_set(root, HARNESS, session, sub,
                                        max_chars=WORKING_SET_MAX_CHARS,
                                        timeout=WORKING_SET_TIMEOUT_S)
        # The brief first: a failed event writes a marker in the state dir,
        # which would show up in the brief's `git status`.
        brief = text or session_brief(root)
        try:
            hookcore.session_event(root, HARNESS, session,
                                   hookcore.start_event(payload.get("source")),
                                   timeout=SESSION_EVENT_TIMEOUT_S)
        except Exception as exc:  # the brief does not need the daemon
            hookcore.log_failure(root, LOG_NAME, payload, exc)
        return _context("SessionStart", brief)

    if event == "PreToolUse" and tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        sub_cmd = hookcore.git_ref_moving(command)
        if sub_cmd is not None:
            return _deny(hookcore.git_refusal_reason(sub_cmd))
        answer = job_ps_answer(root, command, session)
        if answer is not None:
            return _deny(answer)
        mapped = map_tool(tool_name, tool_input, cwd if isinstance(cwd, str) else None)
        if mapped is None:
            return None
        stub = hookcore.repeat_stub(root, HARNESS, session, sub, *mapped)
        return _deny(stub) if stub else None

    if event == "PreToolUse" and tool_name == "apply_patch":
        return pre_apply_patch(payload, root, tool_input)

    if event == "PostToolUse":
        mapped = map_tool(tool_name, tool_input, cwd if isinstance(cwd, str) else None)
        if mapped is None:
            return None
        tool, inp = mapped
        msg = {"id": 1, "op": "after", "harness": HARNESS, "session": session,
               "tool": tool, "input": inp,
               "output": output_text(payload.get("tool_response"))}
        if sub:
            msg["sub"] = sub
        resp = hookcore.ledger_request(root, msg, lambda m: hookcore.daemon_request_autostart(
            root, m, timeout=DAEMON_TIMEOUT_S))
        append = resp.get("append")
        if not isinstance(append, str) or not append:
            return None
        return _context("PostToolUse", append)
    return None


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run one hook event. Always returns 0."""
    return hookcore.run_hook(handle, log_name=LOG_NAME, timeout_s=HOOK_ALARM_S,
                             stdin=stdin, stdout=stdout)
