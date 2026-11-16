"""The Claude Code hook: hand each built-in tool call to the ozagia daemon.

Claude Code runs this as a command hook (see `install.py`). It reads one hook
event as JSON on stdin and answers on stdout, following the Claude Code hooks
reference (https://code.claude.com/docs/en/hooks, see docs/claude_code.md for
the version implemented against).

- `PostToolUse`: map the Claude tool and its input onto the protocol's neutral
  names (see `ozagia.daemon.protocol`), send `after` with the built-in's
  result, and return the daemon's `append` text as `additionalContext`.
- `PreToolUse` for `Bash`: refuse ref-moving git commands with
  `permissionDecision: "deny"`. Local; no daemon needed.
- `PreToolUse` for `Read` of a node key (`sym:`, `test:`, `doc:`, `text:`):
  ask the daemon with the `tool` op and, when it handles the read, deny the
  built-in with the daemon's output as the reason, which Claude reads.
  Experimental.
- `PreToolUse` for `Read` of a path and for a shell read in `Bash`: ask the
  daemon's `read_check`. Under the `stub` repeat policy (opt-in; the
  default is `note`, `ozagia.intent.ledger`) a repeat of an unchanged
  range this session already read is denied with the one-line stub as the
  reason; otherwise the built-in runs, and `PostToolUse` records the lines
  its output shows (a Read sends `file.startLine`/`numLines` as
  `delivered`). A shell read with a `cd` is not tracked: Claude Code's
  shell keeps its directory, so PreToolUse and PostToolUse would resolve
  it differently.
- `PreToolUse` for `Read` of a path that does not exist: deny with the
  nearest existing paths (`[ozagia] no such file <p>; did you mean: ...`)
  when there are any; otherwise as below.
- `PreToolUse` for `Edit`, `MultiEdit` and `Write`: the warnings about
  another live session's uncommitted edits of the file (`owner_check`) as
  `additionalContext`. Never denies.
- `PostToolUse` and `PostToolUseFailure` for `WebFetch` (neutral
  `webfetch`, input `{url}`): `after` with the result or the error; when it
  shows a failed fetch of a paper the daemon appends a `research_fetch`
  hint, returned as `additionalContext`. The hook never fetches.
- `SessionStart`: `session_event` `compact` (source "compact") or `start`
  (startup, resume, clear), with `compaction_hook: true`, which clears the
  session's read ledger and confirms that compaction is reported;
  `SessionEnd`: `end`. With source "compact" the session's working set
  (`ozagia.core.working_set`: files and keys it edited, failing tests and
  their rerun command, jobs, recent reads; from a running daemon, at most
  `WORKING_SET_MAX_CHARS`) is returned as `additionalContext`; otherwise
  nothing is printed. A failed event leaves a marker,
  and the session's next ledger request carries `reset_pending`.

Requests carry `sub` = the payload's `agent_id` when a subagent makes the
call (subagents share the parent's session id), so a subagent's ledger is
its own. A configured `stub` stays `note` until an `agent_id` has been
seen in the session (an older Claude Code that sends none would merge a
subagent into its parent's ledger).

The hook never blocks the agent because ozagia failed: any error prints
nothing and exits 0, and is logged as one JSON line to
`<state dir>/claude_hook.log`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, TextIO

from ozagia.daemon import protocol
from ozagia.integrations import hookcore
from ozagia.integrations.hookcore import (  # noqa: F401 - part of this module's API
    DAEMON_TIMEOUT_S,
    KEY_PREFIXES,
    REF_MOVING_GIT,
    HookError,
    daemon_request,
    git_ref_moving,
    node_key,
    response_text,
)

# The hook finishes within this many seconds; install.py sets the same value
# as the hook's `timeout` in the Claude Code settings.
HOOK_TIMEOUT_S = 20
LOG_NAME = "claude_hook.log"
HARNESS = "claude"
# SessionStart after a compaction: the working set, at most this long.
WORKING_SET_MAX_CHARS = 7000


# --------------------------------------------------------------------------- #
# mapping Claude Code tools onto the protocol
# --------------------------------------------------------------------------- #
def delivered_lines(tool_name: str, tool_response: Any) -> dict[str, int] | None:
    """`{"start", "lines"}` from a Read's `tool_response.file.startLine` /
    `numLines` (the lines Claude Code showed the model), or None."""
    if tool_name != "Read" or not isinstance(tool_response, dict):
        return None
    file = tool_response.get("file")
    if not isinstance(file, dict):
        return None
    start, count = file.get("startLine"), file.get("numLines")
    if (isinstance(start, int) and isinstance(count, int)
            and not isinstance(start, bool) and not isinstance(count, bool)
            and start >= 1 and count >= 0):
        return {"start": start, "lines": count}
    return None

def _put(out: dict[str, Any], key: str, src: dict[str, Any], name: str) -> None:
    if src.get(name) is not None:
        out[key] = src[name]


def map_tool(tool_name: str, tool_input: dict[str, Any],
             cwd: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """`(neutral tool, protocol input)` for a Claude Code call, or None."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    out: dict[str, Any] = {}
    if tool_name == "Read":
        _put(out, "path", ti, "file_path")
        _put(out, "offset", ti, "offset")
        _put(out, "limit", ti, "limit")
        return "read", out
    if tool_name == "Grep":
        _put(out, "pattern", ti, "pattern")
        _put(out, "path", ti, "path")
        _put(out, "include", ti, "glob")
        # Claude Code's Grep is always a ripgrep regex; it has no fixed-strings flag.
        out["literal"] = False
        return "grep", out
    if tool_name == "Glob":
        _put(out, "pattern", ti, "pattern")
        _put(out, "path", ti, "path")
        return "glob", out
    if tool_name == "Edit":
        _put(out, "path", ti, "file_path")
        _put(out, "oldString", ti, "old_string")
        _put(out, "newString", ti, "new_string")
        out["replaceAll"] = bool(ti.get("replace_all", False))
        return "edit", out
    if tool_name == "MultiEdit":
        # One `after` for the file; the daemon re-ingests it and reports the delta.
        _put(out, "path", ti, "file_path")
        edits = ti.get("edits")
        if isinstance(edits, list):
            out["edits"] = [{"oldString": e.get("old_string"),
                             "newString": e.get("new_string"),
                             "replaceAll": bool(e.get("replace_all", False))}
                            for e in edits if isinstance(e, dict)]
        return "edit", out
    if tool_name == "Write":
        _put(out, "path", ti, "file_path")
        _put(out, "content", ti, "content")
        return "write", out
    if tool_name == "WebFetch":
        # `after` only: a failed fetch of a paper gets a research_fetch hint.
        _put(out, "url", ti, "url")
        return "webfetch", out
    if tool_name == "Bash":
        _put(out, "command", ti, "command")
        if cwd:
            out["workdir"] = cwd
        _put(out, "timeout", ti, "timeout")
        _put(out, "description", ti, "description")
        return "shell", out
    return None


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def _pre_output(decision: str, reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def handle(payload: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """The hook's JSON answer for one event, or None to print nothing."""
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    session = payload.get("session_id") or ""
    cwd = payload.get("cwd")
    # Subagent calls carry `agent_id` (and the parent's session id).
    sub = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""

    if event == "SessionStart":
        # The working set first (it survives the ledger reset anyway).
        text = (hookcore.working_set(root, HARNESS, session, sub,
                                     max_chars=WORKING_SET_MAX_CHARS)
                if payload.get("source") == "compact" else None)
        hookcore.session_event(root, HARNESS, session,
                               hookcore.start_event(payload.get("source")))
        if not text:
            return None
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": text}}
    if event == "SessionEnd":
        hookcore.session_event(root, HARNESS, session, "end")
        return None

    if event == "PreToolUse" and tool_name == "Bash":
        sub_cmd = git_ref_moving(str(tool_input.get("command") or ""))
        if sub_cmd is not None:
            return _pre_output("deny", hookcore.git_refusal_reason(sub_cmd))
        mapped = map_tool(tool_name, tool_input, cwd)
        stub = hookcore.repeat_stub(root, HARNESS, session, sub, *mapped) if mapped else None
        return _pre_output("deny", stub) if stub else None

    if event == "PreToolUse" and tool_name == "Read":
        key = node_key(tool_input.get("file_path"))
        if key is None:
            line = hookcore.missing_path_line(
                root, tool_input.get("file_path"),
                cwd if isinstance(cwd, str) else None)
            if line:
                return _pre_output("deny", "The file does not exist, so the Read "
                                           "did not run. Pick one of these paths "
                                           "(or check the name):\n" + line)
            mapped = map_tool(tool_name, tool_input, cwd)
            stub = (hookcore.repeat_stub(root, HARNESS, session, sub, *mapped)
                    if mapped else None)
            return _pre_output("deny", stub) if stub else None
        msg = {"id": 1, "op": "tool", "harness": HARNESS, "session": session,
               "tool": "read", "input": {"path": key}}
        if sub:
            msg["sub"] = sub
        resp = daemon_request(protocol.socket_path(root), msg)
        if not resp.get("handled"):
            return None
        label = ("ozagia read error for" if resp.get("is_error")
                 else "ozagia answered this Read from the code graph; the built-in Read did not run. Node")
        return _pre_output("deny", f"{label} {key}:\n{resp.get('output') or ''}")

    if event == "PreToolUse" and tool_name in ("Edit", "MultiEdit", "Write"):
        path = tool_input.get("file_path")
        warnings = hookcore.owner_warnings(
            root, HARNESS, session, [path] if isinstance(path, str) and path else [],
            cwd if isinstance(cwd, str) else None)
        if not warnings:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": "\n".join(warnings)}}

    if event == "PostToolUseFailure" and tool_name == "WebFetch":
        # A WebFetch that raised (404, 403, a type it can't read): its error
        # is the output `after` checks.
        mapped = map_tool(tool_name, tool_input, cwd)
        if mapped is None:
            return None
        msg = {"id": 1, "op": "after", "harness": HARNESS, "session": session,
               "tool": mapped[0], "input": mapped[1],
               "output": str(payload.get("error") or ""),
               "meta": {"builtin_error": True}}
        if sub:
            msg["sub"] = sub
        resp = daemon_request(protocol.socket_path(root), msg)
        append = resp.get("append")
        if not isinstance(append, str) or not append:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                       "additionalContext": append}}

    if event == "PostToolUse":
        mapped = map_tool(tool_name, tool_input, cwd)
        if mapped is None:
            return None
        tool, inp = mapped
        msg = {"id": 1, "op": "after", "harness": HARNESS, "session": session,
               "tool": tool, "input": inp,
               "output": response_text(payload.get("tool_response"))}
        if sub:
            msg["sub"] = sub
        shown = delivered_lines(tool_name, payload.get("tool_response"))
        if shown is not None:
            msg["delivered"] = shown
        resp = hookcore.ledger_request(
            root, msg, lambda m: daemon_request(protocol.socket_path(root), m))
        append = resp.get("append")
        if not isinstance(append, str) or not append:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                       "additionalContext": append}}
    return None


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run one hook event. Always returns 0."""
    return hookcore.run_hook(handle, log_name=LOG_NAME, timeout_s=HOOK_TIMEOUT_S,
                             stdin=stdin, stdout=stdout)
