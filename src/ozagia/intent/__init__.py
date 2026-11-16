"""ozagia.intent — carry out an agent's tool call on the repository graph.

`handle(memory, tool, input)` takes one captured call (the harness-neutral
tool names and OpenCode 2.3 input fields of `ozagia.daemon.protocol`) and
returns the body of the protocol's `tool` response:

    {"handled": True, "output": "<text>", "is_error": False, "meta": {...}}
    {"handled": True, "output": "<error text>", "is_error": True, "meta": {...}}
    {"handled": False, "reason": "<why>", "meta": {...}}

A handled answer of a non-mutating tool (read, grep, glob), error or not,
also carries `permission` (`permission_request`), which the plugin asks for
before it shows the output.

`after(memory, tool, input, output)` runs after a built-in (or any) call and
returns `{"append": "<text or empty>", "meta": {...}}`. `meta` always has
`intent` (a short name), `keys` (node keys involved) and `ms`. After a
failed web fetch (`webfetch`, input `{url}`) of a paper-like URL it appends
one `research_fetch` hint line (`ozagia.research.fallback`; no network) and
has `fetch_fallback_hints: 1`. After a
built-in edit, write or patch it also has the lint lines of the changed
lines (`ozagia.intent.lint`) and `lint_runs`, `lint_findings` and
`lint_timeouts` when a linter ran.

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
                            Linters after an edit run with it too.
    _reply_deadline         `time.monotonic()` when the caller stops waiting
                            for the answer; the lint budget is cut to it.
    _unlock                 a context manager that releases the caller's
                            graph lock and takes it again; lint runs its
                            linters inside it.
    _lock                   the graph lock, for a call the caller runs
                            without it (captured pytest, `ruff check`): the
                            handler takes it for the steps that read the
                            graph.
    _reason                 why a handled `write` (or `edit` that creates the
                            file) created it: a short label recorded in the
                            created-file ledger (`ozagia.core.files`). The
                            daemon keeps this one reserved field; anything
                            longer than one bounded line is truncated.
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
from ozagia.intent.patch import edit_miss_lines, handle_patch_check
from ozagia.intent.read import handle_read
from ozagia.intent.run import handle_shell
from ozagia.intent.search import handle_glob, handle_grep
from ozagia.intent.shell import classify

# `after` skips the graph refresh when more files than this changed.
MAX_REFRESH_FILES = 200
# Changed files named in an `after` refresh's `meta.changed_paths`.
MAX_CHANGED_PATHS = 20

HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "read": handle_read,
    "grep": handle_grep,
    "glob": handle_glob,
    "edit": handle_edit,
    "write": handle_write,
    "shell": handle_shell,
    # A check only: never writes (the Codex hook's PreToolUse on apply_patch).
    "patch_check": handle_patch_check,
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
        if (tool == "edit" and (not isinstance(path, str) or not path)
                and isinstance(inp.get("key"), str)):
            from ozagia.edit.key_edit import key_file
            try:
                path = key_file(inp["key"])
            except Exception:  # noqa: BLE001 - no file: nothing to ask
                return None
        if not isinstance(path, str) or not path:
            return None
        root = root_of(memory)
        if tool == "read":
            from ozagia.core.structured import parse_key
            skey = parse_key(path)
            if skey is not None and not os.path.exists(os.path.join(str(root), path)):
                path = skey[1]          # json:<rel>#<pointer> reads <rel>
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
           preflight: bool = False, state: Any = None,
           owners: Any = None) -> dict[str, Any]:
    """Carry out one captured tool call (see the module docstring).

    With `preflight`, a mutating tool's handler (edit, write, shell) runs
    every check but stops before its first change. If it would carry the
    call out, the answer is `{"handled": False, "preflight": True,
    "permission": {...}}`: the caller asks for that permission and sends the
    call again without `preflight`. A call that would be passthrough or a
    tool error is answered as usual (it changes nothing). Other tools ignore
    `preflight`; every handled answer carries `permission` (or None).

    `state` is the session's `ozagia.intent.ledger.SessionState` (or
    None): a read or shell read is staged in its ledger (`meta.ack` is the
    token; it counts once the caller acknowledges delivery, see
    `SessionState.ack`), and a repeat of an unchanged range gets a stub or
    a note by its policy (`meta.repeat` is "stub" or "note",
    `meta.bytes_saved` an estimate). A `write` replacing a file the ledger
    holds only part of gets an `[ozagia] warning:` line.

    Edits are journaled with (harness, session) (`ozagia.core.owners`);
    a handled edit or write names its file in `meta.edited_files`. With
    `owners` (`ozagia.core.owners.Owners`), an edit of a file another live
    session edited and left uncommitted gets an `[ozagia] warning:` line
    (`meta.ownership_warnings`). A read or edit of a missing path answers
    with the nearest existing paths when there are any
    (`ozagia.intent.paths`, `meta.path_suggestions`)."""
    t0 = time.perf_counter()
    meta: dict[str, Any] = {"intent": str(tool), "keys": []}
    # Created-file ledger counters (`ozagia.core.files`), merged into `meta`.
    ledger_meta: dict[str, int] = {}
    body: dict[str, Any]
    spec = prior = None
    if (state is not None and not preflight and tool in ("read", "shell")
            and state.policy != "off" and isinstance(input, dict)):
        from ozagia.intent import ledger as L
        spec = L.read_spec(memory, tool, input, served=True,
                           no_cd=state.harness in L.HOOK_HARNESSES)
        prior = state.find(spec) if spec is not None else None
        if prior is not None and state.policy == "stub":
            try:
                stub, keys = L.stub_text(memory, spec, prior)
            except Exception:  # noqa: BLE001 - serve the text instead
                spec = prior = None
            else:
                state.tick()
                meta.update(intent=("read_stub" if tool == "read"
                                    else "shell_read_stub"),
                            keys=keys, repeat="stub",
                            bytes_saved=L.stub_bytes_saved(spec, stub),
                            ms=_ms(t0))
                return {"handled": True, "output": stub, "is_error": False,
                        "permission": permission_request(memory, tool, input),
                        "meta": meta}
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
        partial = None
        if state is not None and tool == "write" and state.policy != "off":
            from ozagia.intent.ledger import partial_view
            partial = partial_view(memory, state, input)
        edit_rel = (_edit_rel(memory, tool, input)
                    if tool in ("edit", "write") else None)
        # The ledger records a file as created only when it did not exist
        # before this call; an edit of an existing file is not a creation.
        created_rel = None
        if edit_rel is not None and not preflight:
            from ozagia.intent.common import root_of
            try:
                if not (root_of(memory) / edit_rel).exists():
                    created_rel = edit_rel
            except OSError:
                created_rel = None
        warned: list[str] = []
        if owners is not None and edit_rel:
            try:
                warned = owners.warnings(memory.journal, edit_rel, harness,
                                         session)
            except Exception:  # noqa: BLE001 - a warning is advisory
                warned = []
        from ozagia.core.owners import acting
        with acting(memory, harness, session):
            res = handler(memory, input)
        if created_rel is not None and res.get("intent") in ("write", "edit_write"):
            try:
                from ozagia.core import files as F
                from ozagia.intent.common import root_of
                reason = input.get("_reason") if isinstance(input, dict) else None
                if not isinstance(reason, str) or not reason.strip():
                    reason = "unknown"
                item = F.record_file(root_of(memory), created_rel,
                                     session=session, harness=harness,
                                     tool=tool, reason=reason)
            except Exception:  # noqa: BLE001 - the file is written
                ledger_meta["file_ledger_errors"] = 1
            else:
                if item is not None:
                    ledger_meta["files_recorded"] = 1
        output = res["output"]
        if partial:
            output += "\n" + partial
        if warned:
            output += "".join("\n" + w for w in warned)
            meta["ownership_warnings"] = len(warned)
        if edit_rel:
            meta["edited_files"] = [edit_rel]
        if res.get("bytes_saved"):
            meta["bytes_saved"] = int(res["bytes_saved"])
        if state is not None and not preflight:
            state.tick()
            if spec is not None:
                output += _ledger_note(state, spec, prior, res, meta)
        body = {"handled": True, "output": output, "is_error": False,
                "permission": permission_request(memory, tool, input)}
        meta.update(intent=res["intent"], keys=list(res.get("keys") or []))
        if isinstance(res.get("meta"), dict):
            meta.update(res["meta"])
    except IntentError as e:
        if state is not None and not preflight:
            state.tick()
        body = {"handled": True, "output": str(e), "is_error": True}
        # A read-only tool's error is shown after the same permission as its
        # answer (the plugin runs the built-in when an answer has none).
        if tool not in MUTATING_TOOLS:
            try:
                body["permission"] = permission_request(memory, tool, input)
            except Exception:  # noqa: BLE001 - no request: the built-in runs
                body["permission"] = None
        meta.update(intent=e.intent or f"{tool}_error", keys=e.keys)
        meta.update(e.meta)
    except Unhandled as e:
        if state is not None and not preflight:
            state.tick(then_after=True)
        body = {"handled": False, "reason": str(e)}
        meta["intent"] = f"{tool}_passthrough"
    except Exception as e:  # noqa: BLE001 - the plugin falls back
        if state is not None and not preflight:
            state.tick(then_after=True)
        body = {"handled": False, "reason": f"{type(e).__name__}: {e}"}
        meta.update(intent=f"{tool}_exception", error=True)
    meta["ms"] = _ms(t0)
    meta.update(ledger_meta)
    body["meta"] = meta
    return body


def _edit_rel(memory: Any, tool: str, inp: dict[str, Any]) -> str | None:
    """The root-relative file an edit or write names, or None."""
    from ozagia.intent.common import resolve, root_of
    path = inp.get("path", inp.get("filePath"))
    try:
        if tool == "edit" and isinstance(inp.get("key"), str) and not path:
            from ozagia.edit.key_edit import key_file
            path = key_file(inp["key"].strip())
        if not isinstance(path, str) or not path:
            return None
        return resolve(root_of(memory), path).rel or None
    except Exception:  # noqa: BLE001 - the handler reports a bad path
        return None


# A built-in edit of a file the graph does not index counts as written by
# the call when the file changed this recently (`_builtin_edits`).
RECENT_WRITE_S = 60.0


def _named_files(memory: Any, tool: str, inp: dict[str, Any]) -> list[str]:
    """Root-relative files a built-in edit, write or patch names."""
    import os
    from ozagia.intent.common import resolve, root_of
    root = root_of(memory)
    base = inp.get("workdir")
    base = base if isinstance(base, str) and os.path.isdir(base) else None
    raw: list[str] = []
    if tool in ("edit", "write"):
        path = inp.get("path", inp.get("filePath"))
        if isinstance(path, str) and path:
            raw.append(path)
    elif tool == "patch":
        from ozagia.intent.patch import parse_patch, patch_text
        text = patch_text(inp)
        if text:
            try:
                parsed = parse_patch(text)
            except Exception:  # noqa: BLE001 - nothing to name
                parsed = None
            for op in (parsed.ops if parsed else []):
                raw.append(op.path)
                if op.move_to:
                    raw.append(op.move_to)
    out: list[str] = []
    from pathlib import Path
    for p in raw:
        try:
            rel = resolve(root, p, base=Path(base) if base else None).rel
        except Exception:  # noqa: BLE001 - outside the root: not ours
            continue
        if rel and rel not in out:
            out.append(rel)
    return out


def _builtin_edits(memory: Any, tool: str, inp: dict[str, Any],
                   pending: list[str]) -> list[str]:
    """The files a built-in edit, write or patch changed: those it names
    that the graph sees as changed, or (for files the graph does not
    index) that were written in the last RECENT_WRITE_S seconds."""
    from ozagia.core.keys import file_key
    from ozagia.intent.common import root_of
    root = root_of(memory)
    changed = set(pending)
    out = []
    for rel in _named_files(memory, tool, inp):
        if rel in changed:
            out.append(rel)
            continue
        if memory.node_by_key(file_key(rel)) is not None:
            continue
        try:
            mtime = (root / rel).stat().st_mtime
        except OSError:
            continue
        if time.time() - mtime <= RECENT_WRITE_S:
            out.append(rel)
    return out


def _recent_writes(memory: Any, pending: list[str]) -> list[str]:
    """The files of `pending` that exist and were written in the last
    RECENT_WRITE_S seconds."""
    from ozagia.intent.common import root_of
    root = root_of(memory)
    now = time.time()
    out = []
    for rel in pending:
        try:
            mtime = (root / rel).stat().st_mtime
        except OSError:
            continue
        if now - mtime <= RECENT_WRITE_S:
            out.append(rel)
    return out


def _path_lines(memory: Any, tool: str, inp: dict[str, Any], output: Any,
                ) -> list[str]:
    """Suggestion lines for a built-in read or edit of a missing path, and
    for the "No such file or directory" paths of a shell output."""
    import os
    from ozagia.intent.common import is_node_key, root_of
    from ozagia.intent.paths import missing_paths, suggest
    root = root_of(memory)
    lines: list[str] = []
    if tool in ("read", "edit"):
        path = inp.get("path", inp.get("filePath"))
        if isinstance(path, str) and path and not is_node_key(path):
            from ozagia.core.structured import parse_key
            if parse_key(path) is None:
                wd = inp.get("workdir")
                base = wd if isinstance(wd, str) and os.path.isdir(wd) else None
                line = suggest(root, path, base=base, memory=memory)
                if line:
                    lines.append(line)
    elif tool == "shell":
        wd = _cd_dir(inp)
        base = wd if isinstance(wd, str) and os.path.isdir(wd) else None
        for p in missing_paths(output):
            line = suggest(root, p, base=base, memory=memory)
            if line and line not in lines:
                lines.append(line)
    return lines


# Intents whose output is not lines of a file range.
_NOT_A_RANGE = frozenset({"read_dir", "read_structured",
                          "read_structured_outline", "shell_jq"})


def _ledger_note(state: Any, spec: Any, prior: Any, res: dict[str, Any],
                 meta: dict[str, Any]) -> str:
    """Stage a served read in the session's ledger (`meta.ack` is its
    token: it is recorded once the caller acknowledges delivery); the note
    lines to append ("" for none)."""
    from ozagia.intent import ledger as L
    if res.get("intent") in _NOT_A_RANGE:
        return ""
    if "shown" in res:             # an outline: only its head was shown
        if res["shown"] is None:
            return ""
        a, b = res["shown"]
        if (a, b) != (spec.a, spec.b):
            spec.a, spec.b = a, min(b, len(spec.lines))
            prior = state.find(spec)
    overlaps = [] if prior is not None else state.overlaps(spec)
    meta["ack"] = state.stage(spec)
    lines = L.note_lines(spec, prior, overlaps)
    if prior is not None:
        meta["repeat"] = "note"
    return "".join("\n" + line for line in lines)


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
        from ozagia.intent.shell import compound_steps
        return any(s["kind"] == "pytest" for s in compound_steps(cls)), cls
    return cls["class"] not in ("read", "grep", "ls", "find", "files", "jq",
                                "git_read", "git_refs"), cls


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
          harness: str = "opencode", session: str | None = None,
          state: Any = None, delivered: Any = None,
          meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Graph context after a tool ran: the delta of any files it changed,
    and for `git diff`/`git show` the keys its hunks touch. With `state`
    (`ozagia.intent.ledger.SessionState`), a read or shell read the
    built-in ran is recorded in the session's ledger, as far as `output`
    shows the file's current lines (`ledger.delivered_spec`; `delivered`
    is a harness's own `{"start", "lines"}`), with a note when it repeats
    or overlaps an earlier read of the unchanged file. `meta` is the request's own meta
    (`builtin_error: true` from the OpenCode plugin's failed built-in edit
    gates the edit-miss reply)."""
    req_meta = meta if isinstance(meta, dict) else {}
    t0 = time.perf_counter()
    meta = {"intent": "after_none", "keys": []}
    lines: list[str] = []
    try:
        inp = input if isinstance(input, dict) else {}
        if state is not None:
            state.tick_after()
            if tool in ("read", "shell") and state.policy != "off":
                lines.extend(_after_ledger(memory, state, tool, inp, meta,
                                           output, delivered))
        if tool == "webfetch":
            # A failed web fetch of a paper: name the research fetcher. Local
            # only; the hint never fetches (`ozagia.research.fallback`).
            from ozagia.research.fallback import after_lines as fetch_hint
            hint, reason = fetch_hint(inp, output)
            if reason:
                meta["fetch_failure"] = reason
            if hint:
                lines.extend(hint)
                meta["fetch_fallback_hints"] = len(hint)
                meta["intent"] = "after_fetch_failed"
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
        if tool == "edit" and isinstance(output, str):
            # A built-in edit whose old text is not in the file: the nearest
            # region, so the next try needs no re-read.
            try:
                flag = req_meta.get("builtin_error")
                miss = edit_miss_lines(
                    memory, inp, output,
                    builtin_error=flag if isinstance(flag, bool) else None)
            except Exception:  # noqa: BLE001 - an optional addition
                miss = []
            if miss:
                lines.extend(miss)
                meta["region_replies"] = 1
                meta["intent"] = "after_edit_miss"
        try:
            suggested = _path_lines(memory, tool, inp, output)
        except Exception:  # noqa: BLE001 - an optional addition
            suggested = []
        if suggested:
            lines.extend(suggested)
            meta["path_suggestions"] = len(suggested)
            if meta["intent"] == "after_none":
                meta["intent"] = "after_path_missing"
        if changes:
            meta["intent"] = ("after_edit_miss" if meta.get("region_replies")
                              else "after_refresh")
            pending = memory.pending_changes()
            meta["changed_files"] = len(pending)
            if tool == "shell":
                # For the session's working set (`ozagia.core.working_set`):
                # the changed files this command may have written (written
                # in the last RECENT_WRITE_S seconds), not every pending
                # change in the root. Built-in edits, writes and patches
                # name theirs in `edited_files`.
                meta["changed_paths"] = _recent_writes(
                    memory, pending)[:MAX_CHANGED_PATHS]
            if (tool in _WRITING_TOOLS and isinstance(session, str) and session
                    and req_meta.get("builtin_error") is not True):
                edited = _builtin_edits(memory, tool, inp, pending)
                if edited:
                    from ozagia.core.owners import acting
                    try:
                        with acting(memory, harness, session):
                            memory.journal_append(
                                "builtin_edit", f"{tool} {', '.join(edited)}",
                                f"{harness} built-in {tool}", files=edited)
                    except OSError as e:
                        lines.append(tag(f"warning: journal not written: {e}"))
                    meta["edited_files"] = edited
            if len(pending) > MAX_REFRESH_FILES:
                lines.append(tag("many files changed; graph refresh deferred"))
            elif pending:
                shas: dict[str, str] = {}
                if tool == "edit":
                    from ozagia.intent.lint import file_shas
                    shas = file_shas(memory, pending)
                delta = memory.reingest(pending)
                lines.extend(delta_lines(delta))
                meta["keys"].extend(delta_keys(delta))
                if tool in ("edit", "write", "patch"):
                    lines.extend(_after_lint(memory, tool, inp, pending,
                                             delta, meta, shas))
    except Exception as e:  # noqa: BLE001 - the tool already ran
        meta.update(intent="after_exception", error=f"{type(e).__name__}: {e}")
    try:
        command = (input if isinstance(input, dict) else {}).get("command")
        if isinstance(command, str) and isinstance(output, str):
            from ozagia.intent.live_hints import hints
            live = hints(command, output)
            if live:
                lines.extend(live)
                meta["live_hints"] = len(live)
    except Exception:  # noqa: BLE001 - hints are advisory only
        pass
    meta["ms"] = _ms(t0)
    return {"append": "\n".join(lines), "meta": meta}


def _after_lint(memory: Any, tool: str, inp: dict[str, Any],
                pending: list[str], delta: Any, meta: dict[str, Any],
                before_sha: dict[str, str] | None = None) -> list[str]:
    """Lint lines for the lines a built-in edit, write or patch changed
    (`ozagia.intent.lint`); never raises."""
    try:
        from ozagia.intent.lint import after_changes, lint_changes, lint_meta
        changes = after_changes(memory, tool, inp, pending, delta,
                                before_sha=before_sha)
        if not changes:
            return []
        got = lint_changes(memory, changes, inp=inp)
    except Exception:  # noqa: BLE001 - lint is advisory only
        return []
    meta.update(lint_meta(got))
    return list(got["lines"])


def _after_ledger(memory: Any, state: Any, tool: str, inp: dict[str, Any],
                  meta: dict[str, Any], output: Any, delivered: Any
                  ) -> list[str]:
    from ozagia.intent import ledger as L
    spec = L.read_spec(memory, tool, inp, served=False,
                       no_cd=state.harness in L.HOOK_HARNESSES)
    if spec is None:
        return []
    spec = L.delivered_spec(spec, output, delivered)
    if spec is None:            # the output does not show the file's lines
        meta["intent"] = "after_read_unconfirmed"
        return []
    prior = state.find(spec)
    overlaps = [] if prior is not None else state.overlaps(spec)
    state.record(spec)
    meta["intent"] = "after_read"
    if prior is not None:
        meta["repeat"] = "note"
    return L.note_lines(spec, prior, overlaps)


def check_read(memory: Any, tool: str, input: Any, *, state: Any = None
               ) -> dict[str, Any]:
    """Whether a read the harness is about to run itself repeats an
    unchanged range this session read already (a hook's PreToolUse):
    `{"repeat": bool, "policy": ..., "stub": "<text>" | None, "meta"}`.
    `stub` is set only under the `stub` policy; the hook then denies the
    call with it. Nothing is recorded; the call's `after` does that."""
    t0 = time.perf_counter()
    meta: dict[str, Any] = {"intent": "check_none", "keys": []}
    policy = state.policy if state is not None else "off"
    out: dict[str, Any] = {"repeat": False, "policy": policy, "stub": None}
    if state is None or policy == "off" or tool not in ("read", "shell"):
        meta["ms"] = _ms(t0)
        return {**out, "meta": meta}
    from ozagia.intent import ledger as L
    spec = L.read_spec(memory, tool, input, served=False,
                       no_cd=state.harness in L.HOOK_HARNESSES)
    prior = state.find(spec) if spec is not None else None
    if prior is None:
        if spec is not None:
            # The built-in runs and its `after` comes next.
            state.tick(then_after=True)
        meta.update(intent="check_read", ms=_ms(t0))
        return {**out, "meta": meta}
    out["repeat"] = True
    meta["intent"] = "check_repeat"
    if policy == "stub":
        try:
            stub, keys = L.stub_text(memory, spec, prior)
        except Exception:  # noqa: BLE001 - let the read run
            meta["ms"] = _ms(t0)
            return {**out, "meta": meta}
        state.tick()
        out["stub"] = stub
        meta.update(intent="check_stub", keys=keys, repeat="stub",
                    bytes_saved=L.stub_bytes_saved(spec, stub))
    else:
        state.tick(then_after=True)
    meta["ms"] = _ms(t0)
    return {**out, "meta": meta}


__all__ = ["HANDLERS", "MAX_REFRESH_FILES", "MUTATING_TOOLS", "Preflight",
           "after", "check_read", "classify", "handle", "permission_request",
           "handle_edit", "handle_glob", "handle_grep", "handle_patch_check",
           "handle_read",
           "handle_shell", "handle_write", "touched_keys"]
