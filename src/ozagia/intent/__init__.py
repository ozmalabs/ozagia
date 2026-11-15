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
falls back to the built-in. See `docs/intent.md` for every format.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from ozagia.intent.common import (
    IntentError,
    Keyer,
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


def handle(memory: Any, tool: str, input: dict[str, Any], *,
           harness: str = "opencode", session: str | None = None
           ) -> dict[str, Any]:
    """Carry out one captured tool call (see the module docstring)."""
    t0 = time.perf_counter()
    meta: dict[str, Any] = {"intent": str(tool), "keys": []}
    body: dict[str, Any]
    try:
        handler = HANDLERS.get(tool)
        if handler is None:
            raise Unhandled(f"{tool} is left to the built-in")
        if not isinstance(input, dict):
            raise IntentError("tool input must be an object")
        res = handler(memory, input)
        body = {"handled": True, "output": res["output"], "is_error": False}
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
    return cls["class"] not in ("read", "grep", "git_read", "git_refs"), cls


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
        if (cls.get("class") == "git_read"
                and cls.get("subcommand") in ("diff", "show")
                and isinstance(output, str)):
            touched = touched_keys(memory, output)
            if touched:
                lines.append(tag("touched: " + ", ".join(touched)))
                meta["keys"].extend(touched)
            meta["intent"] = "after_git_touched"
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


__all__ = ["HANDLERS", "MAX_REFRESH_FILES", "after", "classify", "handle",
           "handle_edit", "handle_glob", "handle_grep", "handle_read",
           "handle_shell", "handle_write", "touched_keys"]
