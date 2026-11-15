"""`edit` and `write` through the edit transaction, reporting the graph delta.

edit success:

    Edited <abs path> (<k> replacement[s])
    [ozagia] in: <enclosing keys>
    [ozagia] delta: <a> added, <r> removed, <c> changed, edges +<x> -<y>: <keys>
    [ozagia] broken: <kind> <key> <file>:<line> <name> — <problem>

edit errors (handled, `is_error`):

    oldString not found in content
    [ozagia] nearest: L<n> <key>: <line text>

    Found multiple matches for oldString. Provide more surrounding lines in
    oldString to identify the correct match.
    [ozagia] match: L<n> <key>: <line text>

    Edit refused: the result does not parse (<message> at line <l>)
    [ozagia] in: <key>

An empty `oldString` writes `newString` as the whole file (OpenCode's create
or overwrite). write:

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
    one_line,
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
    target = resolve(root_of(memory), path)
    if in_state_dir(root_of(memory), target.path):
        raise IntentError(f"{path} is in ozagia's state directory; refused")
    if in_git_dir(target.rel):
        raise IntentError(f"{path} is inside .git; refused")
    return target


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


def handle_edit(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    from ozagia.edit.code_edit import CodeEditError
    path = inp.get("path", inp.get("filePath"))
    old, new = inp.get("oldString"), inp.get("newString")
    if not isinstance(old, str) or not isinstance(new, str):
        raise IntentError("oldString and newString must be strings")
    if old == "":
        res = handle_write(memory, {"path": path, "content": new})
        res["intent"] = "edit_write"
        return res
    target = _target(memory, path)
    if not target.path.is_file():
        raise IntentError(f"File {target.display} not found")
    if old == new:
        raise IntentError(IDENTICAL)
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
    keys = list(dict.fromkeys([*res["keys"], *delta_keys(delta),
                               *(r.get("key") for r in
                                 res.get("broken_references") or [])]))
    return {"intent": "edit", "output": "\n".join(lines), "keys": keys}


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
    if existed:
        suffix = PurePosixPath(target.rel).suffix.lower()
        try:
            src = read_source(root, target.rel, suffixes=(suffix,),
                              max_bytes=MAX_SOURCE_BYTES,
                              error_cls=CodeEditError)
            backup = write_source(root, src, content, error_cls=CodeEditError)
        except CodeEditError as e:
            msg = str(e)
            if any(s in msg for s in _UNHANDLED_ERRORS):
                raise Unhandled(msg) from None
            raise IntentError(msg) from None
    else:
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
    lines = ["Wrote file successfully.", tag(head), *delta_lines(delta),
             *(warning_line(w) for w in warnings)]
    keys = list(dict.fromkeys([fk, *delta_keys(delta)]))
    return {"intent": "write", "output": "\n".join(lines), "keys": keys}


__all__ = ["IDENTICAL", "MULTIPLE", "NOT_FOUND", "broken_line", "handle_edit",
           "handle_write"]
