"""Bounded, span-preserving semantic edits for HCL source.

This intentionally supports a small edit surface instead of accepting text
patches: a uniquely addressed block and one of its direct attributes. Comments,
formatting, line endings, and all text outside the value span remain unchanged.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ozagia.edit.code_edit import CodeEditError, FORBIDDEN_SCRIPT_KEYS, MAX_SOURCE_BYTES
from ozagia.edit.lossless import LosslessText
from ozagia.edit.txn import (
    check_base,
    encode_text,
    normalize_newlines,
    read_source,
    sha256_bytes,
    write_source,
)

_NEWLINE_NAMES = {"\n": "lf", "\r\n": "crlf", "\r": "cr"}
_BLOCK_LINE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)\b")
_LABELS = re.compile(r'\s*(?:"([^"\\\r\n]*)"\s*)*\Z')
_ATTRIBUTE = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=")
_MAX_VALUE_CHARS = 16_384


@dataclass(frozen=True)
class _Block:
    kind: str
    labels: tuple[str, ...]
    start: int
    open_brace: int
    close_brace: int


@dataclass(frozen=True)
class _Scan:
    clean: str
    comments: tuple[tuple[int, int], ...]
    strings: tuple[tuple[int, int], ...]
    heredocs: tuple[tuple[int, int], ...]


def _heredoc_end(text: str, after_opener: int, name: str, indented: bool) -> int:
    start = text.find("\n", after_opener)
    if start < 0:
        raise CodeEditError("malformed HCL: unterminated heredoc")
    start += 1
    delimiter = re.compile(
        rf"[ \t]*{re.escape(name)}[ \t]*" if indented else rf"{re.escape(name)}[ \t]*"
    )
    while start <= len(text):
        end = text.find("\n", start)
        if end < 0:
            end = len(text)
        if delimiter.fullmatch(text[start:end].rstrip("\r")):
            return min(len(text), end + (end < len(text)))
        if end == len(text):
            break
        start = end + 1
    raise CodeEditError("malformed HCL: unterminated heredoc")


def _scan(text: str) -> _Scan:
    """Mask strings/comments/heredocs while checking lexical delimiter balance."""
    chars = list(text)
    comments: list[tuple[int, int]] = []
    strings: list[tuple[int, int]] = []
    heredocs: list[tuple[int, int]] = []
    stack: list[str] = []
    pairs = {"}": "{", "]": "[", ")": "("}
    i = 0
    while i < len(text):
        if text[i] == '"':
            start = i
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                if text[i] == "\n":
                    raise CodeEditError("malformed HCL: quoted string crosses a line")
                i += 1
            else:
                raise CodeEditError("malformed HCL: unterminated quoted string")
            strings.append((start, i))
            for pos in range(start, i):
                if text[pos] != "\n":
                    chars[pos] = " "
            continue
        if text.startswith("<<", i):
            match = re.match(r"<<(-?)[ \t]*([A-Za-z_][A-Za-z0-9_-]*)", text[i:])
            if match:
                after = i + match.end()
                line_end = text.find("\n", after)
                if line_end < 0:
                    line_end = len(text)
                trailing = text[after:line_end].strip(" \t\r")
                if trailing and not trailing.startswith(("#", "//")):
                    raise CodeEditError("malformed HCL: ambiguous heredoc opener")
                end = _heredoc_end(text, after, match.group(2), bool(match.group(1)))
                heredocs.append((i, end))
                for pos in range(i, end):
                    if text[pos] != "\n":
                        chars[pos] = " "
                i = end
                continue
        if text.startswith("#", i) or text.startswith("//", i):
            start = i
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            comments.append((start, end))
            for pos in range(start, end):
                chars[pos] = " "
            i = end
            continue
        if text.startswith("/*", i):
            start = i
            end = text.find("*/", i + 2)
            if end < 0:
                raise CodeEditError("malformed HCL: unterminated block comment")
            end += 2
            comments.append((start, end))
            for pos in range(start, end):
                if text[pos] != "\n":
                    chars[pos] = " "
            i = end
            continue
        ch = text[i]
        if ch in "{[(":
            stack.append(ch)
        elif ch in "}])":
            if not stack or stack.pop() != pairs[ch]:
                raise CodeEditError("malformed HCL: unbalanced delimiters")
        i += 1
    if stack:
        raise CodeEditError("malformed HCL: unbalanced delimiters")
    return _Scan("".join(chars), tuple(comments), tuple(strings), tuple(heredocs))


def _blocks(text: str, scan: _Scan) -> list[_Block]:
    """Find literal-label block headers at any nesting level."""
    out: list[_Block] = []
    lines = scan.clean.splitlines(keepends=True)
    offset = 0
    for clean_line in lines:
        line_end = offset + len(clean_line)
        match = _BLOCK_LINE.match(clean_line)
        if match:
            brace = scan.clean.find("{", offset + match.end(), line_end)
            if brace >= 0:
                header = text[offset + match.end():brace]
                labels_match = _LABELS.fullmatch(header)
                # A line such as `value = {` is an object expression, not a block.
                if labels_match and "=" not in header:
                    labels = tuple(re.findall(r'"([^"\\\r\n]*)"', header))
                    depth = 1
                    close = brace + 1
                    while close < len(scan.clean) and depth:
                        if scan.clean[close] == "{":
                            depth += 1
                        elif scan.clean[close] == "}":
                            depth -= 1
                        close += 1
                    if depth:
                        raise CodeEditError("malformed HCL: unclosed block")
                    out.append(_Block(match.group(1), labels, offset, brace, close - 1))
        offset = line_end
    return out


def _block_path(op: dict[str, Any]) -> list[dict[str, Any]]:
    value = op.get("blocks", op.get("block"))
    path = value if isinstance(value, list) else [value]
    if not path or any(not isinstance(item, dict) for item in path):
        raise CodeEditError("HCL op needs a block or non-empty blocks path")
    normalized = []
    for item in path:
        kind = item.get("kind")
        labels = item.get("labels", [])
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", kind):
            raise CodeEditError("HCL block kind is invalid")
        if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels):
            raise CodeEditError("HCL block labels must be a list of strings")
        normalized.append({"kind": kind, "labels": tuple(labels)})
    return normalized


def _resolve_block(text: str, scan: _Scan, blocks: list[_Block],
                   path: list[dict[str, Any]]) -> _Block:
    parents: list[_Block] = []
    candidates = blocks
    for part in path:
        found = [
            block for block in candidates
            if block.kind == part["kind"] and block.labels == part["labels"]
        ]
        if len(found) != 1:
            if not found:
                raise CodeEditError("HCL block path does not exist")
            raise CodeEditError("HCL block path is ambiguous; add a parent block path")
        chosen = found[0]
        parents.append(chosen)
        candidates = [
            block for block in blocks
            if chosen.open_brace < block.start < block.close_brace
        ]
    return parents[-1]


def _attributes(text: str, scan: _Scan, block: _Block) -> list[tuple[str, int, int, int]]:
    found: list[tuple[str, int, int, int]] = []
    body_start, body_end = block.open_brace + 1, block.close_brace
    depth = 0
    line_start = body_start
    while line_start < body_end:
        line_end = text.find("\n", line_start, body_end)
        line_end = body_end if line_end < 0 else line_end
        clean_line = scan.clean[line_start:line_end]
        if depth == 0:
            match = _ATTRIBUTE.match(clean_line)
            if match:
                eq = clean_line.find("=", match.start(1) + len(match.group(1)))
                value_start = line_start + eq + 1
                while value_start < line_end and text[value_start] in " \t":
                    value_start += 1
                value_end = _expression_end(text, scan, value_start, body_end)
                found.append((match.group(1), line_start + match.start(1),
                              value_start, value_end))
        # Brackets in strings and comments are already masked.
        for ch in scan.clean[line_start:line_end]:
            if ch in "{[(":
                depth += 1
            elif ch in "}])":
                depth -= 1
        if line_end == body_end:
            break
        line_start = line_end + 1
    return found


def _expression_end(text: str, scan: _Scan, start: int, body_end: int) -> int:
    """Find a simple expression span, including balanced multiline containers."""
    cursor = start
    brace = bracket = paren = 0
    while cursor <= body_end:
        line_end = text.find("\n", cursor, body_end)
        line_end = body_end if line_end < 0 else line_end
        comment_start = next(
            (begin for begin, _end in scan.comments if cursor <= begin < line_end),
            None,
        )
        effective_end = min(line_end, comment_start) if comment_start is not None else line_end
        i = cursor
        while i < effective_end:
            heredoc = next(
                ((begin, end) for begin, end in scan.heredocs if begin <= i < end),
                None,
            )
            if heredoc:
                # The caller rejects a targeted heredoc; retain its full span so
                # it cannot be mistaken for a one-line scalar.
                return heredoc[1]
            ch = scan.clean[i]
            if ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket -= 1
            elif ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
            i += 1
        if brace == bracket == paren == 0:
            value_end = effective_end
            while value_end > start and text[value_end - 1] in " \t\r":
                value_end -= 1
            if line_end < body_end and effective_end == line_end:
                # Bare operators cannot be safely span-bounded without a full
                # HCL expression parser.
                raw_line = text[start:line_end].rstrip()
                if raw_line.endswith(("?", ":", "+", "-", "*", "/", "&&", "||", ",")):
                    next_start = line_end + 1
                    if next_start < body_end and text[next_start:body_end].strip():
                        raise CodeEditError(
                            "HCL attribute expression spans lines ambiguously")
            return value_end
        if line_end == body_end:
            break
        cursor = line_end + 1
    raise CodeEditError("malformed HCL: unterminated attribute expression")


def _validate_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeEditError("set_attribute needs a non-empty value_raw string")
    if len(value) > _MAX_VALUE_CHARS or "\n" in value or "\r" in value:
        raise CodeEditError("HCL attribute value must be a bounded single-line expression")
    scan = _scan(value)
    if scan.comments or scan.heredocs:
        raise CodeEditError("HCL attribute value contains ambiguous comments or heredoc syntax")
    return value.strip()


def _apply_ops(source: str, ops: list[dict[str, Any]], newline: str) -> tuple[str, list[dict]]:
    scan = _scan(source)
    blocks = _blocks(source, scan)
    edits: list[tuple[int, int, str]] = []
    changes: list[dict] = []
    for op in ops:
        if not isinstance(op, dict):
            raise CodeEditError("each HCL op must be an object")
        kind = str(op.get("op") or "").strip()
        if kind not in ("set_attribute", "remove_attribute", "delete_attribute"):
            raise CodeEditError(
                "unsupported HCL op; supported: set_attribute, remove_attribute")
        path = _block_path(op)
        block = _resolve_block(source, scan, blocks, path)
        name = op.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            raise CodeEditError("HCL attribute name is invalid")
        matches = [item for item in _attributes(source, scan, block) if item[0] == name]
        if not matches:
            raise CodeEditError("HCL attribute does not exist")
        if len(matches) != 1:
            raise CodeEditError("HCL attribute is ambiguous")
        _attr, attr_start, start, end = matches[0]
        if any(start < heredoc_end and end > heredoc_start
               for heredoc_start, heredoc_end in scan.heredocs):
            raise CodeEditError("refusing to edit a heredoc attribute")
        if kind == "set_attribute":
            edits.append((start, end, _validate_value(op.get("value_raw"))))
        else:
            line_start = source.rfind("\n", 0, attr_start) + 1
            prefix = source[line_start:attr_start]
            if not prefix.strip(" \t"):
                line_end = source.find("\n", attr_start)
                line_end = len(source) if line_end < 0 else line_end
                comment_on_line = any(
                    start <= comment < line_end for comment, _end in scan.comments
                )
                if comment_on_line:
                    edits.append((line_start, end, prefix))
                else:
                    line_end = len(source) if line_end == len(source) else line_end + 1
                    edits.append((line_start, line_end, ""))
            else:
                edits.append((attr_start, end, ""))
        changes.append({"op": kind, "summary": "updated HCL attribute"})

    ordered = sorted(edits, reverse=True)
    previous_start = len(source) + 1
    for start, end, _replacement in ordered:
        if end > previous_start:
            raise CodeEditError("overlapping HCL edits in one script")
        previous_start = start
    result = source
    for start, end, replacement in ordered:
        result = result[:start] + normalize_newlines(replacement, newline) + result[end:]
    return result, changes


def apply_hcl_edit_script(root: str | Path, script: dict[str, Any], *,
                          write: bool = False) -> dict[str, Any]:
    if not isinstance(script, dict):
        raise CodeEditError("edit script must be an object")
    for key in FORBIDDEN_SCRIPT_KEYS:
        if key in script:
            raise CodeEditError(f"raw {key!r} input is refused; use semantic HCL ops")
    root = Path(root).resolve()
    file = str(script.get("file") or "").strip()
    if not file:
        raise CodeEditError("edit script needs file")
    path = (root / file).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CodeEditError("file escapes the repo root") from exc
    suffixes = (".tf", ".tofu", ".hcl")
    src = read_source(root, file, suffixes=suffixes,
                      max_bytes=MAX_SOURCE_BYTES, error_cls=CodeEditError)
    check_base(src, script.get("base_sha256"), CodeEditError)
    ops = script.get("ops")
    if not isinstance(ops, list) or not ops:
        raise CodeEditError("edit script needs a non-empty ops list")
    new_source, changes = _apply_ops(src.text, ops, src.newline)
    # Re-scan before allowing a write; it catches delimiter damage from an op.
    _scan(new_source)
    new_sha = sha256_bytes(encode_text(src, new_source))
    diff_lines = list(difflib.unified_diff(
        src.text.splitlines(keepends=True), new_source.splitlines(keepends=True),
        fromfile=f"a/{file}", tofile=f"b/{file}",
    ))
    backup = write_source(root, src, new_source, error_cls=CodeEditError) if write else None
    return {
        "ok": True,
        "file": file,
        "applied": bool(write),
        "format": "hcl",
        "base_sha256": src.sha256,
        "new_sha256": new_sha,
        "before": {"lines": len(src.text.splitlines()), "sha256": src.sha256},
        "after": {"lines": len(new_source.splitlines()), "sha256": new_sha},
        "changes": changes,
        # Source diffs and raw op values can contain credentials or canaries.
        "diff_view": "",
        "diff_note": "content omitted; inspect the requested file locally",
        "diff_line_count": len(diff_lines),
        "backup": str(backup) if backup else None,
        "newline": _NEWLINE_NAMES[src.newline],
        "bom": src.bom,
        "lossless": {
            "line_leaves": True,
            "roundtrip": LosslessText.from_lines(src.text).render() == src.text,
        },
        "graph_stale": bool(write),
    }
