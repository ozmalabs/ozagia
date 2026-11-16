"""`edit` and `write` through the edit transaction, reporting the graph delta.

edit success:

    Edited <abs path> (<k> replacement[s])
    [ozagia] in: <enclosing keys>
    [ozagia] delta: <a> added, <r> removed, <c> changed, edges +<x> -<y>: <keys>
    [ozagia] broken: <kind> <key> <file>:<line> <name> — <problem>
    [ozagia] lint <rule> L<n> <key>: <message>

The lint lines are the configured linter's findings on the changed lines
(`ozagia.intent.lint`); an edit, key edit and write all end with them.
edit errors (handled, `is_error`):

    oldString not found in content
    [ozagia] nearest: L<n> <key>: <line text>

    Found multiple matches for oldString. Provide more surrounding lines in
    oldString to identify the correct match.
    [ozagia] match: L<n> <key>: <line text>

    Edit refused: the result does not parse (<message> at line <l>)
    [ozagia] in: <key>

An empty `oldString` writes `newString` as the whole file (OpenCode's create
or overwrite). With `key` (and `newString`, `base_sha256`) instead of
`oldString`, the node's whole text is replaced (`handle_key_edit`,
`ozagia.edit.key_edit`); a missing or stale base is a tool error with the
node's current text and both hashes. write:

    Wrote file successfully.
    [ozagia] created|replaced file:<rel>[  backup: <path>]
    [ozagia] delta: ...
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ozagia.core.keys import file_key
from ozagia.intent.common import (
    IntentError,
    Unhandled,
    delta_keys,
    delta_lines,
    in_state_dir,
    diff_metadata,
    one_line,
    preflight_stop,
    resolve,
    root_of,
    split_lines,
    tag,
)

NOT_FOUND = "oldString not found in content"
MULTIPLE = ("Found multiple matches for oldString. Provide more surrounding "
            "lines in oldString to identify the correct match.")
IDENTICAL = "No changes to apply: oldString and newString are identical."
# CodeEditError messages that mean "not a file the edit layer writes": the
# built-in gets the call.
_UNHANDLED_ERRORS = ("is binary", "not valid UTF-8", "exceeds", "declares encoding",
                     "lone CR")


def _target(memory: Any, path: Any):
    from ozagia.edit.txn import in_git_dir
    from ozagia.intent.paths import with_suggestions
    try:
        target = resolve(root_of(memory), path)
    except IntentError as e:
        raise with_suggestions(memory, path, e) from None
    if in_state_dir(root_of(memory), target.path):
        raise IntentError(f"{path} is in ozagia's state directory; refused")
    if in_git_dir(target.rel):
        raise IntentError(f"{path} is inside .git; refused")
    return target


def _not_found(memory: Any, path: Any, target: Any) -> IntentError:
    """`File <path> not found`, with the suggestion line when a close path
    exists (`ozagia.intent.paths`)."""
    from ozagia.intent.paths import candidates, suggestion_line
    message = f"File {target.display} not found"
    if target.path.exists():
        return IntentError(message)
    line = suggestion_line(str(path), candidates(root_of(memory), path,
                                                 memory=memory))
    if not line:
        return IntentError(message)
    return IntentError(f"{message}\n{line}", intent="edit_missing",
                       meta={"path_suggestions": 1})


def warning_line(error: str) -> str:
    """The line that reports a failure after a file was written."""
    return tag(f"warning: {one_line(error)}")


def broken_line(ref: dict[str, Any]) -> str:
    if ref.get("kind") == "arity":
        why = f"{ref.get('problem', '')}; signature {ref.get('signature', '')}"
    elif ref.get("kind") == "missing_name":
        why = "name is no longer defined"
    else:
        why = ref.get("problem") or ref.get("signature") or ""
    return tag(f"broken: {ref.get('kind')} {ref.get('key')} "
               f"{ref.get('file')}:{ref.get('line')} {ref.get('name')} — "
               f"{one_line(why)}")


def _before(memory: Any, target: Any) -> str | None:
    """The file's text before an edit, when a linter will look at it."""
    try:
        from ozagia.intent.lint import applies
        if not applies(memory, target.rel):
            return None
        return target.path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - no lint then
        return None


def _lint(memory: Any, target: Any, before: str | None, *, created: bool = False
          ) -> tuple[list[str], dict[str, int]]:
    """Lint lines for the lines the edit changed (`ozagia.intent.lint`);
    never raises."""
    if before is None and not created:
        return [], {}
    try:
        from ozagia.intent.lint import (
            applies,
            changed_lines,
            lint_changes,
            lint_meta,
        )
        if created and not applies(memory, target.rel):
            return [], {}
        after = target.path.read_text(encoding="utf-8", errors="replace")
        got = lint_changes(memory, {target.rel: changed_lines(before, after)})
        return list(got["lines"]), lint_meta(got)
    except Exception:  # noqa: BLE001 - lint never fails the edit
        return [], {}


def handle_edit(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    from ozagia.edit.code_edit import CodeEditError
    if inp.get("key") is not None:
        return handle_key_edit(memory, inp)
    path = inp.get("path", inp.get("filePath"))
    old, new = inp.get("oldString"), inp.get("newString")
    if not isinstance(old, str) or not isinstance(new, str):
        raise IntentError("oldString and newString must be strings")
    if old == "":
        res = handle_write(memory, {"path": path, "content": new,
                                    "_preflight": inp.get("_preflight")})
        res["intent"] = "edit_write"
        return res
    target = _target(memory, path)
    if not target.path.is_file():
        raise _not_found(memory, path, target)
    if old == new:
        raise IntentError(IDENTICAL)
    if inp.get("_preflight"):
        try:
            res = memory.span_edit(target.rel, old, new,
                                   replace_all=bool(inp.get("replaceAll")),
                                   apply=False)
        except CodeEditError as e:
            msg = str(e)
            if any(s in msg for s in _UNHANDLED_ERRORS):
                raise Unhandled(msg) from None
            raise IntentError(msg) from None
        if not res.get("ok"):
            raise _edit_error(target.path, res)
        text = target.path.read_text(encoding="utf-8", errors="replace")
        after = (text.replace(old, new) if inp.get("replaceAll")
                 else text.replace(old, new, 1))
        preflight_stop(inp, diff_metadata(target.rel, text, after))
    before = _before(memory, target)
    try:
        res = memory.span_edit(target.rel, old, new,
                               replace_all=bool(inp.get("replaceAll")),
                               apply=True)
    except CodeEditError as e:
        msg = str(e)
        if any(s in msg for s in _UNHANDLED_ERRORS):
            raise Unhandled(msg) from None
        raise IntentError(msg) from None
    if not res.get("ok"):
        raise _edit_error(target.path, res)
    k = len(res["matches"])
    lines = [f"Edited {target.display} ({k} replacement{'s' if k != 1 else ''})",
             tag("in: " + ", ".join(res["keys"]))]
    delta = res.get("graph_delta")
    lines.extend(delta_lines(delta))
    for ref in res.get("broken_references") or []:
        lines.append(broken_line(ref))
    if res.get("broken_references_truncated"):
        lines.append(tag("broken references truncated"))
    if res.get("parse_check") == "was_broken":
        lines.append(tag("note: the file did not parse before this edit; "
                         "the parse was not checked"))
    if res.get("warning"):
        lines.append(warning_line(str(res["warning"])))
    lint_lines, lint_meta = _lint(memory, target, before)
    lines.extend(lint_lines)
    keys = list(dict.fromkeys([*res["keys"], *delta_keys(delta),
                               *(r.get("key") for r in
                                 res.get("broken_references") or [])]))
    out = {"intent": "edit", "output": "\n".join(lines), "keys": keys}
    if lint_meta:
        out["meta"] = lint_meta
    return out


# Lines of the current node text shown when a key edit is refused as stale.
STALE_NODE_LINES = 400


def _stale_error(res: dict[str, Any]) -> IntentError:
    key = res["key"]
    lines = [f"Edit refused: {res['error']}",
             tag(f"{key} L{res['start_line']}-L{res['end_line']}  file sha256 "
                 f"{res['file_sha256']}  node sha256 {res['node_sha256']}")]
    body = split_lines(res.get("node_text") or "")
    for i, text in enumerate(body[:STALE_NODE_LINES]):
        lines.append(f"{res['start_line'] + i}: {text}")
    if len(body) > STALE_NODE_LINES:
        lines.append(tag(f"... {len(body) - STALE_NODE_LINES} more lines; "
                         f"read {key}"))
    return IntentError("\n".join(lines), keys=[key], intent="edit_key_stale")


def handle_key_edit(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    """`edit` with `key` (and `newString`, `base_sha256`): replace the
    node's whole text (`ozagia.edit.key_edit`). A missing or stale base is
    refused with the node's current text and both hashes."""
    from ozagia.edit.code_edit import CodeEditError
    from ozagia.edit.key_edit import key_file
    key = inp.get("key")
    new = inp.get("newString", inp.get("new_text"))
    if not isinstance(key, str) or not key.strip():
        raise IntentError("key must be a node key")
    if not isinstance(new, str):
        raise IntentError("newString must be a string")
    key = key.strip()
    try:
        rel = key_file(key)
    except CodeEditError as e:
        raise IntentError(str(e)) from None
    target = _target(memory, rel)
    path = inp.get("path", inp.get("filePath"))
    if isinstance(path, str) and path and _target(memory, path).rel != target.rel:
        raise IntentError(f"path {path} is not the file of {key}")
    if not target.path.is_file():
        raise IntentError(f"File {target.display} not found")
    base = inp.get("base_sha256")
    base = base if isinstance(base, str) else None

    def run(apply: bool) -> dict[str, Any]:
        try:
            res = memory.key_edit(key, new, base_sha256=base, apply=apply)
        except CodeEditError as e:
            raise IntentError(str(e), keys=[key]) from None
        if res.get("stale"):
            raise _stale_error(res)
        if not res.get("ok"):
            raise _edit_error(target.path, res)
        return res

    if inp.get("_preflight"):
        from ozagia.intent.common import read_text
        res = run(False)
        text = read_text(target.path)
        m = res["matches"][0]
        after = text[:m["start"]] + res["new_node_text"] + text[m["end"]:]
        preflight_stop(inp, diff_metadata(target.rel, text, after))
    before = _before(memory, target)
    res = run(True)
    lines = [f"Edited {target.display} (node {key}, lines "
             f"{res['start_line']}-{res['end_line']})",
             tag("in: " + ", ".join(res["keys"])),
             tag(f"sha256 {res['new_sha256']} (the file now); node sha256 "
                 f"{res['new_node_sha256']} (base_sha256 for the next key edit "
                 f"of this node)")]
    delta = res.get("graph_delta")
    lines.extend(delta_lines(delta))
    for ref in res.get("broken_references") or []:
        lines.append(broken_line(ref))
    if res.get("broken_references_truncated"):
        lines.append(tag("broken references truncated"))
    if res.get("warning"):
        lines.append(warning_line(str(res["warning"])))
    lint_lines, lint_meta = _lint(memory, target, before)
    lines.extend(lint_lines)
    keys = list(dict.fromkeys([*res["keys"], *delta_keys(delta),
                               *(r.get("key") for r in
                                 res.get("broken_references") or [])]))
    out = {"intent": "edit_key", "output": "\n".join(lines), "keys": keys}
    if lint_meta:
        out["meta"] = lint_meta
    return out


def _edit_error(path: Path, res: dict[str, Any]) -> IntentError:
    if res.get("parse_check") == "refused":
        pe = res.get("parse_error") or {}
        lines = [f"Edit refused: the result does not parse "
                 f"({pe.get('message')} at line {pe.get('line')})",
                 tag(f"in: {res.get('key')}")]
        return IntentError("\n".join(lines), keys=list(res.get("keys") or []))
    if "nearest" in res:
        # OpenCode's built-in edit retries with whitespace- and
        # indentation-tolerant matching; an exact miss goes to it, and `after`
        # re-ingests whatever it wrote.
        raise Unhandled("oldString has no exact match; the built-in edit "
                        "tries its tolerant matchers")
    if res.get("matches"):
        try:
            text = split_lines(path.read_text(encoding="utf-8",
                                              errors="replace"))
        except OSError:
            text = []
        lines = [MULTIPLE]
        for m in res["matches"]:
            ln = m["start_line"]
            body = text[ln - 1] if 0 < ln <= len(text) else ""
            lines.append(tag(f"match: L{ln} {m['key']}: {one_line(body)}"))
        return IntentError("\n".join(lines), keys=list(res.get("keys") or []))
    return IntentError(str(res.get("error") or "edit failed"))


def _create(path: Path, content: str) -> None:
    """Create `path` with `content` atomically; refuse if it appeared."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=f".{path.name}.",
                                    suffix=".ozagia_tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise IntentError(
                f"{path} was created by someone else meanwhile; re-read and "
                f"retry") from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def handle_write(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    from ozagia.edit.code_edit import MAX_SOURCE_BYTES, CodeEditError
    from ozagia.edit.txn import read_source, write_source
    path = inp.get("path", inp.get("filePath"))
    content = inp.get("content")
    if not isinstance(content, str):
        raise IntentError("content must be a string")
    target = _target(memory, path)
    if target.path.is_dir():
        raise IntentError(f"{target.display} is a directory")
    root = root_of(memory)
    backup = None
    existed = target.path.exists()
    before = _before(memory, target) if existed else None
    if existed:
        suffix = PurePosixPath(target.rel).suffix.lower()
        try:
            src = read_source(root, target.rel, suffixes=(suffix,),
                              max_bytes=MAX_SOURCE_BYTES,
                              error_cls=CodeEditError)
            if inp.get("_preflight"):
                before = target.path.read_text(encoding="utf-8", errors="replace")
                preflight_stop(inp, diff_metadata(target.rel, before, content))
            backup = write_source(root, src, content, error_cls=CodeEditError)
        except CodeEditError as e:
            msg = str(e)
            if any(s in msg for s in _UNHANDLED_ERRORS):
                raise Unhandled(msg) from None
            raise IntentError(msg) from None
    else:
        preflight_stop(inp, diff_metadata(target.rel, None, content))
        _create(target.path, content)
    # The file is written: from here on a failure is reported as a warning
    # line, never raised (a raise would make the plugin write it again).
    warnings: list[str] = []
    delta: Any = None
    try:
        memory._drop_cached(target.rel)
        delta = memory.reingest([target.rel])
    except Exception as e:  # noqa: BLE001 - the file is written; report it
        delta = {"error": f"{type(e).__name__}: {e}"}
    try:
        memory.journal_append(
            "write", f"write {target.rel}",
            f"{'replaced' if existed else 'created'} {target.rel}; "
            f"{len(split_lines(content))} lines",
            files=[target.rel], backup=backup)
    except Exception as e:  # noqa: BLE001 - the file is written; report it
        warnings.append(f"journal not written: {type(e).__name__}: {e}")
    fk = file_key(target.rel)
    head = f"{'replaced' if existed else 'created'} {fk}"
    if backup:
        head += f"  backup: {backup}"
    lint_lines, lint_meta = _lint(memory, target, before, created=not existed)
    lines = ["Wrote file successfully.", tag(head), *delta_lines(delta),
             *(warning_line(w) for w in warnings), *lint_lines]
    keys = list(dict.fromkeys([fk, *delta_keys(delta)]))
    out = {"intent": "write", "output": "\n".join(lines), "keys": keys}
    if lint_meta:
        out["meta"] = lint_meta
    return out


__all__ = ["IDENTICAL", "MULTIPLE", "NOT_FOUND", "broken_line", "handle_edit",
           "handle_write"]
