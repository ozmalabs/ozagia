"""ozagia.intent — carry out an agent's tool call on the repository graph.

`handle(memory, tool, input)` takes one captured call (the harness-neutral
tool names and OpenCode 2.3 input fields of `ozagia.daemon.protocol`) and
returns the body of the protocol's `tool` response:

    {"handled": True, "output": "<text>", "is_error": False, "meta": {...}}
    {"handled": True, "output": "<error text>", "is_error": True, "meta": {...}}
    {"handled": False, "reason": "<why>", "meta": {...}}

`after(memory, tool, input, output)` runs after a built-in (or any) call and
returns `{"append": "<text or empty>", "meta": {...}}`. `meta` always has
`intent` (a short name), `keys` (node keys involved) and `ms`.

Output has the shape of OpenCode's own tools. Source lines are the file's
exact text after a `<n>: ` prefix; graph additions are separate lines that
start with `[ozagia] `. A handler that raises anything other than a tool
error returns `handled: False` with the error as `reason`, so the plugin
falls back to the built-in. The edit and write handlers never raise once
the file is written: a later failure (journal, reingest) becomes an
`[ozagia] warning: <error>` line in a handled result. See `docs/intent.md`
for every format.

Reserved input fields. Keys that start with `_` are set by the caller that
runs the intent layer, never by the model (the daemon drops any it
receives), and never appear in outputs:

    _deadline_s_remaining   seconds left in the caller's run, measured when
                            the input was built; a captured pytest's timeout
                            is clamped to it (at least 0.1 s).
    _test_env               the environment (dict) for a captured pytest. It
                            replaces the inherited environment, so the tests
                            see what the agent's own shell would.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from ozagia.intent.common import (
    IntentError,
    Keyer,
    Preflight,
    Unhandled,
    delta_keys,
    delta_lines,
    tag,
)
from ozagia.intent.edit import handle_edit, handle_write
from ozagia.intent.read import handle_read
from ozagia.intent.run import handle_shell
from ozagia.intent.search import handle_glob, handle_grep
from ozagia.intent.shell import classify

# `after` skips the graph refresh when more files than this changed.
MAX_REFRESH_FILES = 200

HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "read": handle_read,
    "grep": handle_grep,
    "glob": handle_glob,
    "edit": handle_edit,
    "write": handle_write,
    "shell": handle_shell,
}
# Tools whose built-in run can change files.
_WRITING_TOOLS = frozenset({"edit", "write", "patch"})
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


# Tools whose handled call changes files or runs commands: a preflight runs
# their handler up to that point (see `handle`).
MUTATING_TOOLS = frozenset({"edit", "write", "shell"})


def permission_request(memory: Any, tool: str, inp: dict[str, Any]
                       ) -> dict[str, Any] | None:
    """The permission the harness's own tool would ask for this call, in
    OpenCode 2.3's terms: `{"action", "resources", "save"}`. read, edit,
    write: the path relative to the root (`read`, `edit`). grep, glob: the
    pattern. shell: one resource per simple command (`shell`, without
    `cd`), and as `save` the same commands. None when there is nothing to
    ask (an unknown tool or input)."""
    import os
    from ozagia.intent.common import root_of
    from ozagia.intent.shell import command_resources
    if not isinstance(inp, dict):
        return None
    if tool in ("read", "edit", "write"):
        path = inp.get("path", inp.get("filePath"))
        if not isinstance(path, str) or not path:
            return None
        root = root_of(memory)
        p = path if os.path.isabs(path) else os.path.join(str(root), path)
        p = os.path.normpath(p)
        rel = os.path.relpath(p, root)
        res = p if rel.startswith("..") else (rel.replace(os.sep, "/") or ".")
        if res == ".":
            res = "."
        return {"action": "read" if tool == "read" else "edit",
                "resources": [res], "save": ["*"]}
    if tool in ("grep", "glob"):
        pattern = inp.get("pattern")
        if not isinstance(pattern, str):
            return None
        return {"action": tool, "resources": [pattern], "save": ["*"]}
    if tool == "shell":
        command = inp.get("command")
        if not isinstance(command, str):
            return None
        cmds = command_resources(command)
        if not cmds and command.strip():
            cmds = [command.strip()]
        # `save` (what "always allow" stores) is the exact commands: never
        # wider than what the user was shown.
        return {"action": "shell", "resources": cmds, "save": list(cmds)}
    return None


def handle(memory: Any, tool: str, input: dict[str, Any], *,
           harness: str = "opencode", session: str | None = None,
           preflight: bool = False) -> dict[str, Any]:
    """Carry out one captured tool call (see the module docstring).

    With `preflight`, a mutating tool's handler (edit, write, shell) runs
    every check but stops before its first change. If it would carry the
    call out, the answer is `{"handled": False, "preflight": True,
    "permission": {...}}`: the caller asks for that permission and sends the
    call again without `preflight`. A call that would be passthrough or a
    tool error is answered as usual (it changes nothing). Other tools ignore
    `preflight`; every handled answer carries `permission` (or None)."""
    t0 = time.perf_counter()
    meta: dict[str, Any] = {"intent": str(tool), "keys": []}
    body: dict[str, Any]
    try:
        handler = HANDLERS.get(tool)
        if handler is None:
            raise Unhandled(f"{tool} is left to the built-in")
        if not isinstance(input, dict):
            raise IntentError("tool input must be an object")
        if preflight and tool in MUTATING_TOOLS:
            extra: dict[str, Any] | None = None
            try:
                handler(memory, {**input, "_preflight": True})
            except Preflight as p:  # (a handler that returns also stops here)
                extra = p.metadata
            meta["intent"] = f"{tool}_preflight"
            meta["ms"] = _ms(t0)
            perm = permission_request(memory, tool, input)
            if perm is not None and extra:
                perm["metadata"] = extra
            return {"handled": False, "preflight": True,
                    "reason": "permission required", "permission": perm,
                    "meta": meta}
        res = handler(memory, input)
        body = {"handled": True, "output": res["output"], "is_error": False,
                "permission": permission_request(memory, tool, input)}
        meta.update(intent=res["intent"], keys=list(res.get("keys") or []))
    except IntentError as e:
        body = {"handled": True, "output": str(e), "is_error": True}
        meta.update(intent=e.intent or f"{tool}_error", keys=e.keys)
    except Unhandled as e:
        body = {"handled": False, "reason": str(e)}
        meta["intent"] = f"{tool}_passthrough"
    except Exception as e:  # noqa: BLE001 - the plugin falls back
        body = {"handled": False, "reason": f"{type(e).__name__}: {e}"}
        meta.update(intent=f"{tool}_exception", error=True)
    meta["ms"] = _ms(t0)
    body["meta"] = meta
    return body


def _unquote(path: str) -> str:
    path = path.strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def touched_keys(memory: Any, diff: str) -> list[str]:
    """The keys enclosing each changed line of a unified diff, on the
    current files: `+` lines at their new line, `-` lines at the line just
    before the point of deletion. A deleted file maps to its `file:` key."""
    keys: list[str] = []
    rel: str | None = None
    old: str | None = None
    keyer: Keyer | None = None
    line: int | None = None

    def add(k: str) -> None:
        if k not in keys:
            keys.append(k)

    for raw in diff.splitlines():
        if raw.startswith("diff --git ") or raw.startswith("diff --cc "):
            rel, old, keyer, line = None, None, None, None
        elif raw.startswith("--- ") and line is None:
            p = _unquote(raw[4:])
            old = p[2:] if p.startswith("a/") else (None if p == "/dev/null"
                                                     else p)
        elif raw.startswith("+++ ") and line is None:
            p = _unquote(raw[4:])
            if p == "/dev/null":
                if old:
                    add(f"file:{old}")
                rel, keyer = None, None
            else:
                rel = p[2:] if p.startswith("b/") else p
                keyer = Keyer(memory, rel)
        elif raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            line = int(m.group(1)) if (m and keyer is not None) else None
        elif line is not None and keyer is not None:
            if raw.startswith("+"):
                add(keyer.key(line))
                line += 1
            elif raw.startswith("-"):
                # The deleted lines sat after the new line `line - 1`.
                add(keyer.key(max(line - 1, 1)))
            elif raw.startswith(" ") or raw == "":
                line += 1
            elif not raw.startswith("\\"):
                line = None
    return keys


def _may_change_files(tool: str, input: dict[str, Any]) -> tuple[bool, dict]:
    if tool in _WRITING_TOOLS:
        return True, {}
    if tool != "shell":
        return False, {}
    cls = classify(str(input.get("command") or ""))
    if cls["class"] == "compound":
        return any(s["kind"] == "pytest" for s in cls["steps"]), cls
    return cls["class"] not in ("read", "grep", "ls", "find", "git_read",
                                "git_refs"), cls


# pytest's short summary lines: `FAILED tests/x.py::test_y[p] - message`.
_PYTEST_SUMMARY_RE = re.compile(
    r"^(FAILED|ERROR) (\S+?\.py)::(\S+?)(\[[^\]]*\])?(?: - (.*))?$")


def _is_pytest(cmd: list[str]) -> bool:
    import os
    base = os.path.basename(cmd[0])
    if base in ("pytest", "py.test"):
        return True
    return (bool(re.match(r"^python(?:3(?:\.\d+)?)?$", base))
            and cmd[1:3] == ["-m", "pytest"])


def _is_git_diff(cmd: list[str]) -> bool:
    import os
    if os.path.basename(cmd[0]) != "git":
        return False
    rest = [a for a in cmd[1:] if not a.startswith("-")]
    return bool(rest) and rest[0] in ("diff", "show")


def failed_test_lines(memory: Any, output: str, wd: Any = None
                      ) -> tuple[list[str], list[str]]:
    """`[ozagia] failed|error test:<key>: <message>` lines from pytest's
    short summary in `output`, for tests the graph knows, and their keys.
    Paths are taken relative to `wd` (default: the root)."""
    import os
    from ozagia.intent.common import one_line, root_of
    root = root_of(memory)
    base = os.path.realpath(str(wd)) if wd else str(root)
    lines: list[str] = []
    keys: list[str] = []
    for raw in output.splitlines():
        m = _PYTEST_SUMMARY_RE.match(raw.strip())
        if not m:
            continue
        rel = os.path.relpath(os.path.normpath(os.path.join(base, m.group(2))),
                              root)
        if rel.startswith(".."):
            continue
        name = m.group(3)
        key = f"test:{rel}::{name}"
        if memory.node_by_key(key) is None:
            continue
        if key not in keys:
            keys.append(key)
        word = "failed" if m.group(1) == "FAILED" else "error"
        msg = f": {one_line(m.group(5))}" if m.group(5) else ""
        lines.append(tag(f"{word} {key}{m.group(4) or ''}{msg}"))
    return lines, keys


def _cd_dir(inp: dict[str, Any]) -> Any:
    """The directory a command starting `cd DIR &&` runs in, else its
    workdir (best effort, for reading paths in its output)."""
    import os
    command = str(inp.get("command") or "")
    wd = inp.get("workdir")
    m = re.match(r"^\s*cd\s+(\S+)\s*&&", command)
    if m and "$" not in m.group(1) and "~" not in m.group(1):
        d = m.group(1).strip("'\"")
        return d if os.path.isabs(d) or not wd else os.path.join(str(wd), d)
    return wd


def after(memory: Any, tool: str, input: dict[str, Any], output: Any, *,
          harness: str = "opencode", session: str | None = None
          ) -> dict[str, Any]:
    """Graph context after a tool ran: the delta of any files it changed,
    and for `git diff`/`git show` the keys its hunks touch."""
    t0 = time.perf_counter()
    meta: dict[str, Any] = {"intent": "after_none", "keys": []}
    lines: list[str] = []
    try:
        inp = input if isinstance(input, dict) else {}
        changes, cls = _may_change_files(tool, inp)
        heads: list[list[str]] = []
        if cls.get("class") in ("complex", "compound") and isinstance(output, str):
            from ozagia.intent.shell import command_heads
            heads = command_heads(str(inp.get("command") or ""))
        if isinstance(output, str) and (
                (cls.get("class") == "git_read"
                 and cls.get("subcommand") in ("diff", "show"))
                or any(_is_git_diff(h) for h in heads)):
            touched = touched_keys(memory, output)
            if touched:
                lines.append(tag("touched: " + ", ".join(touched)))
                meta["keys"].extend(touched)
            meta["intent"] = "after_git_touched"
        if heads and any(_is_pytest(h) for h in heads):
            failed, fkeys = failed_test_lines(memory, output, _cd_dir(inp))
            if failed:
                lines.extend(failed)
                meta["keys"].extend(k for k in fkeys if k not in meta["keys"])
                meta["intent"] = "after_pytest_summary"
        if changes:
            meta["intent"] = "after_refresh"
            pending = memory.pending_changes()
            meta["changed_files"] = len(pending)
            if len(pending) > MAX_REFRESH_FILES:
                lines.append(tag("many files changed; graph refresh deferred"))
            elif pending:
                delta = memory.reingest(pending)
                lines.extend(delta_lines(delta))
                meta["keys"].extend(delta_keys(delta))
    except Exception as e:  # noqa: BLE001 - the tool already ran
        meta.update(intent="after_exception", error=f"{type(e).__name__}: {e}")
    meta["ms"] = _ms(t0)
    return {"append": "\n".join(lines), "meta": meta}


__all__ = ["HANDLERS", "MAX_REFRESH_FILES", "MUTATING_TOOLS", "Preflight",
           "after", "classify", "handle", "permission_request",
           "handle_edit", "handle_glob", "handle_grep", "handle_read",
           "handle_shell", "handle_write", "touched_keys"]
