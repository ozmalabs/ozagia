"""The Kiro hook: git refusal, node-key reads and graph refresh for Kiro CLI.

Kiro runs this as a command hook (see `install.py` and docs/kiro.md). It
reads one hook event as JSON on stdin (`hook_event_name`, `cwd`,
`session_id`, and for tool events `tool_name`, `tool_input` and, after the
tool, `tool_response`). It answers in plain text: stdout with exit 0, or
stderr with exit 2 to block a tool before it runs.

kiro-cli 2.37 has three engines. V3 (the KAS engine, `kiro-cli --v3` or
`chat.agentEngine = "v3"`) loads standalone `.kiro/hooks/*.json` files and
agent-profile hooks, and sends PascalCase event names. V1 and V2 (the Rust
engines) load hooks only from agent profiles and send the CLI 2.x
lowerCamel names (`agentSpawn`, `preToolUse`, `postToolUse`). The hook
accepts both.

- `SessionStart` / `agentSpawn`: in a git work tree, a short brief (dirty
  files, recent commits, the ozagia MCP tools to prefer) on stdout, which
  Kiro adds to the agent's context; the daemon is started so it is loaded
  by the first tool call.
- `PreToolUse` for a shell tool: a ref-moving git command is refused with
  exit 2 and the reason on stderr, which Kiro gives to the model. Local; no
  daemon needed.
- `PreToolUse` for a read tool whose paths are all node keys (`sym:`,
  `test:`, `doc:`, `text:`, `json:`, `yaml:`, `toml:`): the daemon answers
  the read (`tool` op) and the hook blocks the built-in with the answer on
  stderr, so the model reads the node instead of a "file not found".
- `PreToolUse` for a read or directory listing whose every path is missing
  under the event's cwd: blocked (exit 2) with the nearest existing paths
  (`[ozagia] no such file <p>; did you mean: ...`), when there are any;
  otherwise the read runs.
- `PreToolUse` for a write or edit tool: the warnings about another live
  session's uncommitted edits of the file (`owner_check`), on stdout. Never
  blocks.
- `PostToolUse` for read, write, edit, shell, grep and glob tools: `after`
  with the neutral tool and input; the daemon re-ingests changed files and
  counts the call. A non-empty `append` is printed on stdout. (Kiro 2.37
  does not pass PostToolUse output to the model in any engine; see
  docs/kiro.md. The refresh still keeps the graph current.)
- `PostCompact` (CLI V3 3.1.0 and later only): the session's working set
  (`compaction_context`, at most 3,000 characters on stdout) and a
  `session_event` `compact`. install.py registers it in the V3 hook file
  only (2.37's V3 loader skips a trigger it does not know; the CLI 2.x
  profile validator rejects one). `PreCompact` does nothing.

The hook stops itself after `HOOK_ALARM_S`, a few seconds before the
handler timeout install.py writes. It never blocks the agent because
ozagia failed: any error prints nothing, exits 0, and is logged as one JSON
line to `<state dir>/kiro_hook.log`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TextIO

from ozagia.integrations import hookcore
from ozagia.integrations.hookcore import session_brief

# The handler timeout install.py writes (`timeout` in seconds in a hook
# file; `timeout_ms` in a CLI 2.x agent profile).
HOOK_TIMEOUT_S = 20
# The hook stops itself (prints nothing) after this many seconds.
HOOK_ALARM_S = 15
# One daemon request: with AUTOSTART_BUDGET_S (3 s) it stays under HOOK_ALARM_S.
DAEMON_TIMEOUT_S = 10
LOG_NAME = "kiro_hook.log"
HARNESS = "kiro"
# SessionStart only kicks the daemon off; it does not wait for the load.
SESSION_START_BUDGET_S = 0.5
# After the alarm fired during the SessionStart daemon start, the brief gets
# this long; HOOK_ALARM_S plus this stays under HOOK_TIMEOUT_S.
BRIEF_ALARM_S = 3
# Kiro adds at most 3,000 characters of a PostCompact hook's stdout.
COMPACTION_MAX_CHARS = 3000
# PostCompact's `session_event` and `working_set` requests wait this long.
COMPACTION_TIMEOUT_S = 3.0
# A read tool asking for more node keys than this runs as usual.
MAX_KEY_READS = 8

KEY_PREFIXES = hookcore.KEY_PREFIXES + hookcore.STRUCTURED_KEY_PREFIXES

# Event names: V3 sends PascalCase; the CLI 2.x engines send lowerCamel.
EVENTS = {
    "SessionStart": "SessionStart", "sessionStart": "SessionStart",
    "agentSpawn": "SessionStart", "AgentSpawn": "SessionStart",
    "PreToolUse": "PreToolUse", "preToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse", "postToolUse": "PostToolUse",
    "PreCompact": "PreCompact", "PostCompact": "PostCompact",
}

# Tool IDs: V3 built-ins (read_file, read_files, fs_write, fs_append,
# str_replace, execute_bash, grep_search, file_search, list_directory) and
# the CLI 2.x names (fs_read, fs_write, execute_bash, grep, glob; the V2 TUI
# shows them as read, write, shell).
SHELL_TOOLS = frozenset({"execute_bash", "execute_pwsh", "execute_cmd", "shell"})
READ_TOOLS = frozenset({"read_file", "readFile", "read_files", "readFiles",
                        "fs_read", "read"})
LIST_TOOLS = frozenset({"list_directory"})
EDIT_TOOLS = frozenset({"str_replace"})
WRITE_TOOLS = frozenset({"fs_write", "write"})
APPEND_TOOLS = frozenset({"fs_append"})
GREP_TOOLS = frozenset({"grep_search", "grep"})
GLOB_TOOLS = frozenset({"glob"})
# A path substring search; sent as the glob `**/*<query>*`.
FILE_SEARCH_TOOLS = frozenset({"file_search"})
# Web fetch (`{url, mode?, searchPhrase?}`): `after` only, for the
# research_fetch hint after a failed fetch of a paper.
WEB_FETCH_TOOLS = frozenset({"web_fetch", "webFetch"})


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #
def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _read_request(path: Any, offset: Any = None, limit: Any = None) -> dict[str, Any] | None:
    """A protocol read for one Kiro read. Kiro's `offset` counts lines from
    0; the protocol's (OpenCode's) from 1."""
    p = _str(path)
    if p is None:
        return None
    out: dict[str, Any] = {"path": p}
    off = _int(offset)
    if off is not None and off >= 0:
        out["offset"] = off + 1
    lim = _int(limit)
    if lim is not None and lim > 0:
        out["limit"] = lim
    return out


def read_requests(tool_input: Any) -> list[dict[str, Any]] | None:
    """The protocol reads one Kiro read call asks for, or None when it is not
    a plain file or directory read (an image read, an unknown shape).

    Shapes: `{path, offset?, limit?}` (read_file), `{paths: [...]}` or
    `{files: [path | {path, offset?, limit?}]}` (read_files), and
    `{operations: [{mode, path, offset?, limit?}]}` (CLI 2.x fs_read)."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    out: list[dict[str, Any] | None] = []
    ops = ti.get("operations")
    if isinstance(ops, list):
        for op in ops:
            if not isinstance(op, dict) or op.get("mode", "Line") not in ("Line", "Directory"):
                return None
            out.append(_read_request(op.get("path"), op.get("offset"), op.get("limit")))
    elif isinstance(ti.get("paths"), list):
        out = [_read_request(p) for p in ti["paths"]]
    elif isinstance(ti.get("files"), list):
        for f in ti["files"]:
            if isinstance(f, dict):
                out.append(_read_request(f.get("path"), f.get("offset"), f.get("limit")))
            else:
                out.append(_read_request(f))
    elif "path" in ti:
        out = [_read_request(ti.get("path"), ti.get("offset"), ti.get("limit"))]
    if not out or any(r is None for r in out):
        return None
    return [r for r in out if r is not None]


def _workdir(tool_input: dict[str, Any], cwd: str | None) -> str | None:
    wd = _str(tool_input.get("cwd")) or _str(tool_input.get("working_dir"))
    if wd is None:
        return cwd
    if os.path.isabs(wd) or not cwd:
        return wd
    return os.path.join(cwd, wd)


def _write_or_edit(ti: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """fs_write: V3 `{path, text}`; CLI 2.x `{command, path, ...}` with
    command create, strReplace (str_replace), insert or append."""
    path = _str(ti.get("path"))
    if path is None:
        return None
    command = ti.get("command")
    if command in ("strReplace", "str_replace"):
        return _edit(ti)
    if command in ("insert", "append"):
        return "write", {"path": path}
    if command not in (None, "create"):
        return None
    out: dict[str, Any] = {"path": path}
    for name in ("text", "content", "file_text"):
        if isinstance(ti.get(name), str):
            out["content"] = ti[name]
            break
    return "write", out


def _edit(ti: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    path = _str(ti.get("path"))
    if path is None:
        return None
    out: dict[str, Any] = {"path": path}
    for key, names in (("oldString", ("oldStr", "old_str")),
                       ("newString", ("newStr", "new_str"))):
        for name in names:
            if isinstance(ti.get(name), str):
                out[key] = ti[name]
                break
    out["replaceAll"] = bool(ti.get("replace_all", ti.get("replaceAll", False)))
    return "edit", out


def map_tool(tool_name: str, tool_input: Any,
             cwd: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """`(neutral tool, protocol input)` for a Kiro call, or None."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in SHELL_TOOLS:
        command = _str(ti.get("command"))
        if command is None:
            return None
        out: dict[str, Any] = {"command": command}
        wd = _workdir(ti, cwd)
        if wd:
            out["workdir"] = wd
        if _int(ti.get("timeout")) is not None:
            out["timeout"] = ti["timeout"]
        if _str(ti.get("description")):
            out["description"] = ti["description"]
        return "shell", out
    if tool_name in READ_TOOLS:
        reqs = read_requests(ti)
        # The protocol reads one path; a batch is not sent.
        return ("read", reqs[0]) if reqs and len(reqs) == 1 else None
    if tool_name in LIST_TOOLS:
        req = _read_request(ti.get("path"))
        return ("read", req) if req else None
    if tool_name in EDIT_TOOLS:
        return _edit(ti)
    if tool_name in WRITE_TOOLS:
        return _write_or_edit(ti)
    if tool_name in APPEND_TOOLS:
        path = _str(ti.get("path"))
        return ("write", {"path": path}) if path else None
    if tool_name in GREP_TOOLS:
        pattern = _str(ti.get("query")) or _str(ti.get("pattern"))
        if pattern is None:
            return None
        out = {"pattern": pattern}
        if _str(ti.get("path")):
            out["path"] = ti["path"]
        include = _str(ti.get("includePattern")) or _str(ti.get("include"))
        if include:
            out["include"] = include
        # Kiro's grep is a ripgrep regex.
        out["literal"] = False
        return "grep", out
    if tool_name in GLOB_TOOLS:
        pattern = _str(ti.get("pattern"))
        if pattern is None:
            return None
        out = {"pattern": pattern}
        if _str(ti.get("path")):
            out["path"] = ti["path"]
        return "glob", out
    if tool_name in FILE_SEARCH_TOOLS:
        query = _str(ti.get("query"))
        return ("glob", {"pattern": f"**/*{query}*"}) if query else None
    if tool_name in WEB_FETCH_TOOLS:
        url = _str(ti.get("url"))
        return ("webfetch", {"url": url}) if url else None
    return None


def output_text(tool_response: Any) -> str:
    """The tool's result as text. V3 sends the result message as a string;
    an object with a `result` list or string (CLI 2.x) is flattened, and
    anything else is as in `hookcore.response_text`."""
    if isinstance(tool_response, dict) and "result" in tool_response:
        result = tool_response["result"]
        if isinstance(result, str):
            return result
        if isinstance(result, list):
            return "\n".join(r if isinstance(r, str) else hookcore.response_text(r)
                             for r in result)
    return hookcore.response_text(tool_response)


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def compaction_context(event: str, payload: dict[str, Any], root: Path) -> str | None:
    """`PreCompact` / `PostCompact` (Kiro CLI V3 3.1.0 and later; 2.37 has
    neither, and its V3 loader skips a hook with a trigger it does not know).

    `PostCompact` (after the summary replaced the history): `session_event`
    `compact` (the read ledger is cleared), then the session's working set
    from a running daemon (`ozagia.core.working_set`), at most
    `COMPACTION_MAX_CHARS`, on stdout, which Kiro adds to the context.
    `PreCompact` does nothing: its stdout would be summarised away, and a
    non-zero exit would cancel the compaction, which ozagia must never do
    (`run_hook` exits 0 on any failure)."""
    if event != "PostCompact":
        return None
    session = payload.get("session_id")
    text = hookcore.working_set(root, HARNESS, session,
                                max_chars=COMPACTION_MAX_CHARS,
                                timeout=COMPACTION_TIMEOUT_S)
    hookcore.session_event(root, HARNESS, session, "compact",
                           timeout=COMPACTION_TIMEOUT_S)
    return text[:COMPACTION_MAX_CHARS] if text else None


def _pre_read(tool_name: str, tool_input: dict[str, Any], root: Path,
              session: str, cwd: str | None = None) -> hookcore.HookBlock | None:
    reqs = read_requests(tool_input)
    if not reqs or len(reqs) > MAX_KEY_READS:
        return None
    keys = [hookcore.node_key(r["path"], KEY_PREFIXES, cwd) for r in reqs]
    if any(k is None for k in keys):
        return None
    parts: list[str] = []
    handled = False
    answered = False
    for req, key in zip(reqs, keys):
        inp = {**req, "path": key}
        resp = hookcore.daemon_request_autostart(root, {
            "id": 1, "op": "tool", "harness": HARNESS, "session": session,
            "tool": "read", "input": inp}, timeout=DAEMON_TIMEOUT_S)
        if not resp.get("handled"):
            parts.append(f"ozagia could not answer {key}: "
                         f"{resp.get('reason') or 'not handled'}")
            continue
        handled = True
        answered = answered or not resp.get("is_error")
        label = "ozagia read error for" if resp.get("is_error") else "Node"
        parts.append(f"{label} {key}:\n{resp.get('output') or ''}")
    if not handled:
        return None
    if answered:
        head = (f"ozagia answered this {tool_name} from the code graph, so the "
                "built-in tool did not run. This is the result; do not retry the "
                "call.")
    else:
        head = (f"ozagia could not answer this {tool_name} from the code graph, so "
                "the built-in tool did not run. Check the key (the error below may "
                "suggest one); if you meant a file, read it by its real path.")
    return hookcore.HookBlock(head + "\n\n" + "\n\n".join(parts))


def _pre_missing(tool_input: dict[str, Any], root: Path,
                 cwd: str | None) -> hookcore.HookBlock | None:
    """Block a read whose every path is missing under the event's cwd, with
    the nearest existing paths (`hookcore.missing_path_line`). A read with
    any existing path, or with no candidate, runs as usual."""
    reqs = read_requests(tool_input)
    if not reqs or len(reqs) > MAX_KEY_READS:
        return None
    if any(hookcore.path_exists(r["path"], cwd, root)
           or hookcore.node_key(r["path"], KEY_PREFIXES, cwd) for r in reqs):
        return None
    lines = [x for x in (hookcore.missing_path_line(root, r["path"], cwd)
                         for r in reqs) if x]
    if not lines:
        return None
    return hookcore.HookBlock(
        "ozagia: the read did not run because the path does not exist. "
        "Pick one of these paths (or check the name) and read it:\n"
        + "\n".join(lines))


def handle(payload: dict[str, Any], root: Path) -> str | hookcore.HookBlock | None:
    """The hook's answer for one event: text for stdout, a `HookBlock`, or
    None to print nothing."""
    event = EVENTS.get(str(payload.get("hook_event_name") or ""))
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session = str(payload.get("session_id") or "")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None

    if event == "SessionStart":
        in_repo = hookcore.in_work_tree(root)
        try:
            hookcore.ensure_daemon(root, SESSION_START_BUDGET_S, in_repo=in_repo)
        except hookcore.HookTimeout as exc:
            hookcore.log_failure(root, LOG_NAME, payload, exc)
            hookcore.rearm_alarm(BRIEF_ALARM_S)
        except Exception as exc:  # the brief does not need the daemon
            hookcore.log_failure(root, LOG_NAME, payload, exc)
        return session_brief(root) if in_repo else None

    if event == "PreToolUse":
        if tool_name in SHELL_TOOLS:
            sub = hookcore.git_ref_moving(str(tool_input.get("command") or ""))
            return None if sub is None else hookcore.HookBlock(
                hookcore.git_refusal_reason(sub))
        if tool_name in READ_TOOLS:
            got = _pre_read(tool_name, tool_input, root, session, cwd)
            return got if got is not None else _pre_missing(tool_input, root, cwd)
        if tool_name in LIST_TOOLS:
            return _pre_missing(tool_input, root, cwd)
        if tool_name in EDIT_TOOLS | WRITE_TOOLS | APPEND_TOOLS:
            mapped = map_tool(tool_name, tool_input, cwd)
            path = mapped[1].get("path") if mapped else None
            warnings = hookcore.owner_warnings(root, HARNESS, session,
                                               [path] if path else [], cwd)
            return "\n".join(warnings) if warnings else None
        return None

    if event == "PostToolUse":
        mapped = map_tool(tool_name, tool_input, cwd)
        if mapped is None:
            return None
        tool, inp = mapped
        resp = hookcore.daemon_request_autostart(root, {
            "id": 1, "op": "after", "harness": HARNESS, "session": session,
            "tool": tool, "input": inp,
            "output": output_text(payload.get("tool_response"))},
            timeout=DAEMON_TIMEOUT_S)
        append = resp.get("append")
        return append if isinstance(append, str) and append else None

    if event in ("PreCompact", "PostCompact"):
        return compaction_context(event, payload, root)
    return None


def main(stdin: TextIO | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    """Run one hook event. Returns 2 when a tool is blocked, else 0."""
    return hookcore.run_hook(handle, log_name=LOG_NAME, timeout_s=HOOK_ALARM_S,
                             stdin=stdin, stdout=stdout, stderr=stderr)
