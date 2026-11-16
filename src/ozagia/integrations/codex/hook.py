"""The Codex hook: graph context for Codex's shell and patch calls.

Codex (0.169) runs this as a command hook (see `install.py`). It reads one
hook event as JSON on stdin and answers on stdout, following the Codex hooks
reference (https://learn.chatgpt.com/docs/hooks, see docs/codex.md).

- `PreToolUse` for `Bash`: refuse ref-moving git commands with
  `permissionDecision: "deny"`. Local; no daemon needed.
- `PreToolUse` for `Bash`, a `pgrep` or `ps -p` command: when a daemon
  answers and the command is about one of its background jobs
  (`ozagia.daemon.jobs.capture_ps`), deny it with the answer (the
  processes, and the job's status) as the reason. No daemon is started
  for this.
- `PostToolUse` for `Bash`: send `after` as `shell` ({command, workdir})
  with the command's output, and return the daemon's `append` as
  `additionalContext`.
- `PostToolUse` for `apply_patch`: send `after` as `patch` ({patch}); the
  daemon re-ingests the changed files and returns the graph delta.
- `SessionStart`: a short brief (dirty files, recent commits, the ozagia MCP
  tools to prefer) as `additionalContext`, and a start of the daemon so it
  is loaded by the first tool call. Outside a git work tree it prints
  nothing.

The daemon is started on demand (`ozagia.daemon.autostart.ensure`, a few
seconds at most) when nothing answers on its socket, and only for a git
work tree, `$OZAGIA_ROOT` or a root with a graph snapshot
(`hookcore.autostart_allowed`). The hook stops itself after
`HOOK_ALARM_S`, a few seconds before Codex's handler timeout. It never blocks
the agent because ozagia failed: any error prints nothing and exits 0, and
is logged as one JSON line to `<state dir>/codex_hook.log`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, TextIO

from ozagia.integrations import hookcore

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
BRIEF_MAX_CHARS = 1500
BRIEF_DIRTY_FILES = 15
BRIEF_COMMITS = 3
# SessionStart only kicks the daemon off; it does not wait for the load.
SESSION_START_BUDGET_S = 0.5
GIT_TIMEOUT_S = 3
# After the alarm fired during the SessionStart daemon start, the brief gets
# this long (its git calls take GIT_TIMEOUT_S each at most); HOOK_ALARM_S
# plus this stays under HOOK_TIMEOUT_S.
BRIEF_ALARM_S = 3

MCP_TOOLS_LINE = (
    "Prefer the ozagia MCP tools to reading whole files: dev_symbol / dev_node "
    "(one symbol by key, sym:path::name), dev_outline, dev_impact before "
    "changing a shared module, dev_context to start a task. Long commands: "
    "job_start, then job_wait (blocks until exit or a log line, returns only "
    "new output) instead of polling write_stdin, sleep or ps.")


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


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


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
        return _context("SessionStart", session_brief(root))

    if event == "PreToolUse" and tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        sub = hookcore.git_ref_moving(command)
        if sub is not None:
            return _deny(hookcore.git_refusal_reason(sub))
        answer = job_ps_answer(root, command, session)
        if answer is not None:
            return _deny(answer)
        return None

    if event == "PostToolUse":
        mapped = map_tool(tool_name, tool_input, cwd if isinstance(cwd, str) else None)
        if mapped is None:
            return None
        tool, inp = mapped
        resp = hookcore.daemon_request_autostart(root, {
            "id": 1, "op": "after", "harness": HARNESS, "session": session,
            "tool": tool, "input": inp,
            "output": output_text(payload.get("tool_response"))},
            timeout=DAEMON_TIMEOUT_S)
        append = resp.get("append")
        if not isinstance(append, str) or not append:
            return None
        return _context("PostToolUse", append)
    return None


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run one hook event. Always returns 0."""
    return hookcore.run_hook(handle, log_name=LOG_NAME, timeout_s=HOOK_ALARM_S,
                             stdin=stdin, stdout=stdout)
