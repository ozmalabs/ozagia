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


# --------------------------------------------------------------------------- #
# mapping Claude Code tools onto the protocol
# --------------------------------------------------------------------------- #
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

    if event == "PreToolUse" and tool_name == "Bash":
        sub = git_ref_moving(str(tool_input.get("command") or ""))
        if sub is None:
            return None
        return _pre_output("deny", hookcore.git_refusal_reason(sub))

    if event == "PreToolUse" and tool_name == "Read":
        key = node_key(tool_input.get("file_path"))
        if key is None:
            return None
        resp = daemon_request(protocol.socket_path(root), {
            "id": 1, "op": "tool", "harness": HARNESS, "session": session,
            "tool": "read", "input": {"path": key}})
        if not resp.get("handled"):
            return None
        label = ("ozagia read error for" if resp.get("is_error")
                 else "ozagia answered this Read from the code graph; the built-in Read did not run. Node")
        return _pre_output("deny", f"{label} {key}:\n{resp.get('output') or ''}")

    if event == "PostToolUse":
        mapped = map_tool(tool_name, tool_input, cwd)
        if mapped is None:
            return None
        tool, inp = mapped
        resp = daemon_request(protocol.socket_path(root), {
            "id": 1, "op": "after", "harness": HARNESS, "session": session,
            "tool": tool, "input": inp,
            "output": response_text(payload.get("tool_response"))})
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
