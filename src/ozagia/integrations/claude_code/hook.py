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

import json
import os
import re
import shlex
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from ozagia.config import repo_root, state_path
from ozagia.daemon import protocol

# The hook finishes within this many seconds; install.py sets the same value
# as the hook's `timeout` in the Claude Code settings.
HOOK_TIMEOUT_S = 20
# The total time allowed for one daemon request, from connect to response.
DAEMON_TIMEOUT_S = 15
LOG_NAME = "claude_hook.log"
HARNESS = "claude"

KEY_PREFIXES = ("sym:", "test:", "doc:", "text:")
# Claude Code makes file paths absolute before hooks run, so a key the model
# passed as `file_path` may arrive as `<cwd>/sym:...`.
_KEY_IN_PATH = re.compile(r"(?:^|/)((?:sym|test|doc|text):.+)$")

REF_MOVING_GIT = frozenset({
    "commit", "push", "stash", "branch", "checkout", "switch", "reset",
    "rebase", "merge", "tag", "worktree",
})
# git options before the subcommand that take a separate value.
_GIT_OPTS_WITH_VALUE = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--config-env", "--exec-path",
})
_SHELL_PUNCT = ";&|()<>\n"


class HookError(Exception):
    """The daemon could not answer."""


class _Timeout(Exception):
    pass


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


def response_text(tool_response: Any) -> str:
    """The built-in's result as text for the protocol's `output`.

    PostToolUse passes the tool's structured output object (for example Bash
    `{stdout, stderr, interrupted, isImage}`); older versions and other tools
    may pass a string or a list of content blocks.
    """
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        texts = [b.get("text") for b in tool_response
                 if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if texts and len(texts) == len(tool_response):
            return "\n".join(texts)
        return json.dumps(tool_response, ensure_ascii=False)
    if isinstance(tool_response, dict):
        if "stdout" in tool_response or "stderr" in tool_response:
            parts = [str(tool_response.get(k) or "") for k in ("stdout", "stderr")]
            return "\n".join(p for p in parts if p)
        file = tool_response.get("file")
        if isinstance(file, dict) and isinstance(file.get("content"), str):
            return file["content"]
        if isinstance(tool_response.get("content"), str):
            return tool_response["content"]
        names = tool_response.get("filenames")
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            return "\n".join(names)
        # Edit and Write echo the whole original file; the daemon reads the file itself.
        slim = {k: v for k, v in tool_response.items() if k != "originalFile"}
        return json.dumps(slim, ensure_ascii=False)
    return str(tool_response)


# --------------------------------------------------------------------------- #
# local checks
# --------------------------------------------------------------------------- #
def _shell_tokens(command: str) -> list[str]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        return list(lex)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        return command.split()


def _simple_commands(command: str) -> list[list[str]]:
    cmds: list[list[str]] = [[]]
    for tok in _shell_tokens(command):
        if tok and all(c in _SHELL_PUNCT for c in tok):
            cmds.append([])
        else:
            cmds[-1].append(tok)
    return [c for c in cmds if c]


def _git_subcommand(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i]):
        i += 1  # leading VAR=value assignments
    if i >= len(argv) or os.path.basename(argv[i]) != "git":
        return None
    i += 1
    while i < len(argv):
        arg = argv[i]
        if arg in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg
    return None


def git_ref_moving(command: str) -> str | None:
    """The first ref-moving git subcommand in a shell command, or None."""
    for argv in _simple_commands(command or ""):
        sub = _git_subcommand(argv)
        if sub in REF_MOVING_GIT:
            return sub
    return None


def node_key(file_path: Any) -> str | None:
    """The node key a Read's `file_path` names, or None for a real path."""
    if not isinstance(file_path, str) or not file_path:
        return None
    if file_path.startswith(KEY_PREFIXES):
        return file_path
    m = _KEY_IN_PATH.search(file_path)
    if m and not os.path.exists(file_path):
        return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# daemon client
# --------------------------------------------------------------------------- #
def daemon_request(sock: Path, msg: dict[str, Any],
                   timeout: float = DAEMON_TIMEOUT_S) -> dict[str, Any]:
    """Send one protocol request and return the `ok` response."""
    deadline = time.monotonic() + timeout
    line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    buf = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock))
        s.sendall(line)
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HookError("daemon request timed out")
            s.settimeout(remaining)
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > protocol.MAX_MESSAGE_BYTES:
                raise HookError("daemon response too large")
    raw = bytes(buf).split(b"\n", 1)[0]
    if not raw:
        raise HookError("daemon closed the connection without a response")
    resp = json.loads(raw.decode("utf-8"))
    if not isinstance(resp, dict) or resp.get("id") != msg.get("id"):
        raise HookError("daemon response does not match the request")
    if not resp.get("ok"):
        raise HookError(f"daemon error: {resp.get('error')}")
    return resp


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
        return _pre_output("deny", (
            f"ozagia: `git {sub}` moves refs and is refused in this session. "
            "Read-only git (status, diff, log, show, blame) is allowed; leave "
            "commits, branches and stashes to the user."))

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


def _root_for(payload: Any) -> Path:
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return repo_root(cwd)
    return repo_root()


def _log(root: Path | None, payload: Any, error: BaseException) -> None:
    try:
        base = root if root is not None else repo_root()
        path = state_path(base, LOG_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        p = payload if isinstance(payload, dict) else {}
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "event": p.get("hook_event_name"), "tool": p.get("tool_name"),
                 "session": p.get("session_id"),
                 "error": f"{type(error).__name__}: {error}"}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _on_alarm(signum: int, frame: Any) -> None:
    raise _Timeout(f"hook exceeded {HOOK_TIMEOUT_S} s")


def main(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run one hook event. Always returns 0."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    payload: Any = None
    root: Path | None = None
    use_alarm = hasattr(signal, "SIGALRM")
    try:
        if use_alarm:
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(HOOK_TIMEOUT_S)
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        root = _root_for(payload)
        out = handle(payload, root)
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()
    except BaseException as exc:  # never block the agent because ozagia failed
        _log(root, payload, exc)
    finally:
        if use_alarm:
            signal.alarm(0)
    return 0
