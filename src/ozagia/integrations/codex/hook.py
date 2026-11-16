"""The Codex hook: graph context for Codex's shell and patch calls.

Codex (0.169) runs this as a command hook (see `install.py`). It reads one
hook event as JSON on stdin and answers on stdout, following the Codex hooks
reference (https://learn.chatgpt.com/docs/hooks, see docs/codex.md).

- `PreToolUse` for `Bash`: refuse ref-moving git commands with
  `permissionDecision: "deny"`. Local; no daemon needed.
- `PostToolUse` for `Bash`: send `after` as `shell` ({command, workdir})
  with the command's output, and return the daemon's `append` as
  `additionalContext`.
- `PostToolUse` for `apply_patch`: send `after` as `patch` ({patch}); the
  daemon re-ingests the changed files and returns the graph delta.
- `SessionStart`: a short brief (dirty files, recent commits, the ozagia MCP
  tools to prefer) as `additionalContext`, and a start of the daemon so it
  is loaded by the first tool call.

The daemon is started on demand (`ozagia.daemon.autostart.ensure`, a few
seconds at most) when nothing answers on its socket. The hook never blocks
the agent because ozagia failed: any error prints nothing and exits 0, and
is logged as one JSON line to `<state dir>/codex_hook.log`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, TextIO

from ozagia.integrations import hookcore

# The hook finishes within this many seconds; install.py sets the same value
# as the handlers' `timeout` in hooks.json.
HOOK_TIMEOUT_S = 20
LOG_NAME = "codex_hook.log"
HARNESS = "codex"
# The SessionStart brief stays under this many characters.
BRIEF_MAX_CHARS = 1500
BRIEF_DIRTY_FILES = 15
BRIEF_COMMITS = 3
# SessionStart only kicks the daemon off; it does not wait for the load.
SESSION_START_BUDGET_S = 0.5
GIT_TIMEOUT_S = 3

MCP_TOOLS_LINE = (
    "Prefer the ozagia MCP tools over reading whole files: dev_symbol / "
    "dev_node to read one symbol by key (sym:path::name), dev_outline for a "
    "file's outline, dev_impact before changing a widely used module, and "
    "dev_context to start a task.")


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
def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def session_brief(root: Path, max_chars: int = BRIEF_MAX_CHARS) -> str:
    """Dirty files, the last commits and the MCP tools to prefer."""
    lines = ["[ozagia] session brief"]
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    if status is not None:
        dirty = [x for x in status.splitlines() if x.strip()]
        head = f"branch {branch.strip()}; " if branch else ""
        if dirty:
            shown = dirty[:BRIEF_DIRTY_FILES]
            more = len(dirty) - len(shown)
            lines.append(f"{head}{len(dirty)} changed file(s):")
            lines.extend(f"  {x}" for x in shown)
            if more:
                lines.append(f"  ... and {more} more")
        else:
            lines.append(f"{head}working tree clean")
    log = _git(root, "log", f"-{BRIEF_COMMITS}", "--format=%h %s")
    if log and log.strip():
        lines.append("recent commits:")
        lines.extend(f"  {x[:120]}" for x in log.splitlines() if x.strip())
    lines.append(MCP_TOOLS_LINE)
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    # Keep the tools line; cut the middle.
    keep = max_chars - len(MCP_TOOLS_LINE) - len("\n...\n")
    return text[:max(keep, 0)].rstrip() + "\n...\n" + MCP_TOOLS_LINE


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def _context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def handle(payload: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """The hook's JSON answer for one event, or None to print nothing."""
    event = payload.get("hook_event_name")
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session = payload.get("session_id") or ""
    cwd = payload.get("cwd")

    if event == "SessionStart":
        try:
            hookcore.ensure_daemon(root, SESSION_START_BUDGET_S)
        except Exception as exc:  # the brief does not need the daemon
            hookcore.log_failure(root, LOG_NAME, payload, exc)
        return _context("SessionStart", session_brief(root))

    if event == "PreToolUse" and tool_name == "Bash":
        sub = hookcore.git_ref_moving(str(tool_input.get("command") or ""))
        if sub is None:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason":
                                           hookcore.git_refusal_reason(sub)}}

    if event == "PostToolUse":
        mapped = map_tool(tool_name, tool_input, cwd if isinstance(cwd, str) else None)
        if mapped is None:
            return None
        tool, inp = mapped
        resp = hookcore.daemon_request_autostart(root, {
            "id": 1, "op": "after", "harness": HARNESS, "session": session,
            "tool": tool, "input": inp,
            "output": output_text(payload.get("tool_response"))})
        append = resp.get("append")
        if not isinstance(append, str) or not append:
            return None
        return _context("PostToolUse", append)
    return None


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run one hook event. Always returns 0."""
    return hookcore.run_hook(handle, log_name=LOG_NAME, timeout_s=HOOK_TIMEOUT_S,
                             stdin=stdin, stdout=stdout)
