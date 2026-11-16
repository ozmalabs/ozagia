"""ozagia.edit.key_edit — replace the whole text of one node, named by its key.

`apply_key_edit(root, key, new_text, base_sha256=...)` finds the node's span
in the current file, checks the base hash, and replaces the span's text
through the same pipeline as `span_edit.apply_span_edit` (parse guard,
broken references, `ozagia.edit.txn` write with a backup, reingest with a
`DevMemory`). Preview by default.

Keys and the span each names:

    sym:<rel>::<qualname>     the def/class (decorators included), whole
    test:<rel>::<qualname>    lines; Python by its own parse, other languages
                              by the graph node's line range (needs memory)
    doc:<rel>#<slug>          a Markdown or TeX section, subsections
                              included: the key, slug and line range a read
                              or outline shows (`ozagia.core.sections`);
                              the path alias `a/b` is taken too, and the
                              result names the section by its slug key
    doc:<rel>, file:<rel>     the whole file
    text:<rel>#L<a>-L<b>      lines a..b
    json:<rel>#<pointer>      the JSON value at the pointer (RFC 6901), only
                              the value's own text

Base hash. `base_sha256` must be the sha256 of the node's current text
(UTF-8, `node_sha256`). The file's sha256 (the bytes on disk) is accepted
only when the node is the whole file (`file:<rel>`, `doc:<rel>`, or any key
whose span is the whole text). The rule: a base must prove the caller saw
the text it replaces. A key edit cannot tell where a file sha came from (a
full read, or a region reply that showed a few lines of a longer node), so
for a node that is part of a file only its own hash is that proof. The node
hash also survives edits elsewhere in the file. Region replies, edit-miss
replies and stale refusals give the node's full line range and its
`node_sha256`; a successful key edit gives `new_node_sha256` for the next
edit of the same node. A missing, stale or file-only base is refused with
`{ok: False, stale: True}` and the node's current text, line range,
`file_sha256` and `node_sha256`, so the caller can retry without another
read.

Python symbols come from the file's own parse; a qualname with more than
one def (a property getter and setter, `@overload` stubs, `if`/`else`
definitions) is refused as ambiguous with each def's line range (address
one by its `text:` key). Other languages' symbols take the graph node's
line range; when the file changed since it was ingested
(`memory.pending_changes()`) it is reingested first, so the range is the
current one.

Lines are counted at CRLF, lone CR and LF (`txn.LINE_BREAK_RE`), as the
patch checker and the region replies count them; a `doc:` section's lines
are counted at "\\n" as reads count them (`ozagia.core.sections`). The two
agree except in a file with lone-CR line ends.

A line-range node replaced by text without a final newline gets one when
the old text had one (the next line is not joined onto it). Newlines are
converted to the file's style.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

from ozagia.core.keys import parse_key
from ozagia.edit.code_edit import CodeEditError
from ozagia.edit.txn import SourceFile, line_starts, normalize_newlines

_TEXT_KEY_RE = re.compile(r"^text:(?P<rel>.+)#L(?P<a>\d+)-L(?P<b>\d+)$")
KEY_KINDS = ("sym", "test", "doc", "file", "text", "json")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_span(text: str, a: int, b: int) -> tuple[int, int]:
    """Offsets of lines a..b (1-based, inclusive), the last line's newline
    included."""
    starts = line_starts(text)
    total = len(starts) - (1 if starts[-1] == len(text) else 0)
    if a < 1 or b < a or a > max(total, 1):
        raise CodeEditError(f"lines {a}-{b} are not in the file ({total} lines)")
    b = min(b, total)
    end = starts[b] if b < len(starts) else len(text)
    return starts[a - 1], end


# --------------------------------------------------------------------------- #
# Document sections
# --------------------------------------------------------------------------- #
def find_section(rel: str, text: str, fragment: str) -> Any:
    """The `ozagia.core.sections.DocSection` a `doc:` key's fragment names
    (a slug, or a path alias `a/b`), or CodeEditError listing the section
    keys."""
    from ozagia.core.sections import (
        SectionError,
        doc_sections,
        resolve_section,
        section_key,
    )
    sections = doc_sections(rel, text)
    try:
        return resolve_section(sections, fragment)
    except SectionError as e:
        listed = e.matches or tuple(sections)
        keys = ", ".join(f"{section_key(rel, s.slug)} (L{s.start}-L{s.end})"
                         for s in listed[:20]) or "(none)"
        what = "matches" if e.matches else "sections"
        raise CodeEditError(f"{e}; {what}: {keys}") from None


# --------------------------------------------------------------------------- #
# Key -> span
# --------------------------------------------------------------------------- #
def key_file(key: str) -> str:
    """The repository file a node key belongs to."""
    if not isinstance(key, str) or ":" not in key:
        raise CodeEditError(f"not a node key: {key!r}")
    kind, _, rest = key.partition(":")
    if kind not in KEY_KINDS:
        raise CodeEditError(f"key edits take {', '.join(k + ':' for k in KEY_KINDS)} "
                            f"keys, not {key!r}")
    if kind in ("sym", "test"):
        rel = rest.split("::", 1)[0]
    elif kind in ("doc", "text", "json"):
        rel = rest.split("#", 1)[0]
    else:
        rel = rest
    if not rel:
        raise CodeEditError(f"key {key!r} names no file")
    return rel


def node_span(src: Any, key: str, *, memory: Any = None,
              refresh: bool = True) -> dict[str, Any]:
    """`{start, end, start_line, end_line, lines: bool, graph: bool}` of the
    node `key` in `src` (a SourceFile, or anything with `rel` and `text`;
    offsets into `src.text`). `lines` says whether the span is whole lines,
    `graph` whether the line range came from the graph. A graph-sourced
    span of a file changed since ingest is reingested first (`refresh`), or
    refused (`refresh=False`). Raises CodeEditError for a key with no node
    or a qualname with several defs."""
    text = src.text
    kind = key.partition(":")[0]
    starts = line_starts(text)

    def line_of(off: int) -> int:
        import bisect
        return bisect.bisect_right(starts, off)

    if kind in ("file",) or (kind == "doc" and "#" not in key):
        return {"start": 0, "end": len(text), "start_line": 1,
                "end_line": max(1, line_of(max(len(text) - 1, 0))), "lines": True}
    if kind == "doc":
        from ozagia.core.sections import has_sections, section_offsets
        if not has_sections(src.rel):
            raise CodeEditError(f"{key}: sections are for Markdown and TeX files")
        sec = find_section(src.rel, text, key.split("#", 1)[1])
        s, e = section_offsets(text, sec)
        return {"start": s, "end": e, "start_line": sec.start,
                "end_line": sec.end, "lines": True, "slug": sec.slug}
    if kind == "text":
        m = _TEXT_KEY_RE.match(key)
        if not m:
            raise CodeEditError(f"not a text key: {key!r}")
        a, b = int(m.group("a")), int(m.group("b"))
        s, e = _line_span(text, a, b)
        return {"start": s, "end": e, "start_line": a,
                "end_line": line_of(max(e - 1, s)), "lines": True}
    if kind == "json":
        return _json_span(src, key, line_of)
    # sym: / test:
    _, rel, qual = parse_key(key)
    if PurePosixPath(src.rel).suffix.lower() == ".py":
        from ozagia.check import _text_spans
        spans = [sp for sp in _text_spans(text) if sp[2] == qual]
        if not spans:
            raise CodeEditError(f"no node {key} in {src.rel}")
        # A test key names a test span; a sym key prefers a non-test span.
        want_test = kind == "test"
        spans = ([sp for sp in spans if bool(sp[3]) == want_test] or spans)
        spans.sort(key=lambda sp: sp[0])
        if len(spans) > 1:
            where = ", ".join(f"L{sp[0]}-L{sp[1]}" for sp in spans)
            raise CodeEditError(
                f"{key} is ambiguous: {len(spans)} defs of {qual} in {src.rel} "
                f"({where}); edit one by its text:{src.rel}#L<a>-L<b> key")
        a, b = spans[0][0], spans[0][1]
    else:
        if memory is not None and src.rel in memory.pending_changes():
            # The graph's line range is from before the file changed.
            if not refresh:
                raise CodeEditError(f"{src.rel} changed since the graph took it "
                                    f"in; the line range of {key} is stale")
            memory.reingest([src.rel])
        nid = memory.node_by_key(key) if memory is not None else None
        if nid is None:
            raise CodeEditError(f"no node {key}"
                                + ("" if memory is not None else
                                   " (non-Python symbols need the graph)"))
        attrs = (memory.node(nid) or {}).get("attrs") or {}
        a = int(attrs.get("start_line") or 0)
        b = int(attrs.get("end_line") or a)
        if a < 1:
            raise CodeEditError(f"node {key} has no line range")
        s, e = _line_span(text, a, b)
        return {"start": s, "end": e, "start_line": a,
                "end_line": line_of(max(e - 1, s)), "lines": True,
                "graph": True}
    s, e = _line_span(text, a, b)
    return {"start": s, "end": e, "start_line": a,
            "end_line": line_of(max(e - 1, s)), "lines": True}


def accepted_bases(src: Any, span: dict[str, Any]) -> tuple[str, ...]:
    """The base hashes a key edit of `span` takes: the node's sha256, and
    the file's when the node is the whole file (see the module
    docstring)."""
    node_sha = sha256_text(src.text[span["start"]:span["end"]])
    whole = (span["start"] == 0 and span["end"] == len(src.text)
             and not span.get("graph"))
    return (node_sha, src.sha256) if whole else (node_sha,)


def _json_span(src: SourceFile, key: str, line_of: Any) -> dict[str, Any]:
    from ozagia.edit.json_edit import _Parser
    pointer = key.split("#", 1)[1] if "#" in key else ""
    try:
        node = _Parser(src.text).parse()
    except CodeEditError as e:
        raise CodeEditError(f"{src.rel} does not parse as JSON: {e}") from None
    if pointer and not pointer.startswith("/"):
        raise CodeEditError(f"JSON pointer must start with '/': {pointer!r}")
    for raw in pointer.split("/")[1:] if pointer else []:
        tok = raw.replace("~1", "/").replace("~0", "~")
        if node.kind == "object":
            hit = [m["value"] for m in node.members if str(m["key"]) == tok]
            if not hit:
                raise CodeEditError(f"no member {tok!r} at {key}")
            node = hit[-1]
        elif node.kind == "array":
            if not tok.isdigit() or int(tok) >= len(node.items):
                raise CodeEditError(f"no index {tok!r} at {key}")
            node = node.items[int(tok)]
        else:
            raise CodeEditError(f"cannot descend into a {node.kind} at {key}")
    return {"start": node.start, "end": node.end,
            "start_line": line_of(node.start),
            "end_line": line_of(max(node.end - 1, node.start)), "lines": False}


# --------------------------------------------------------------------------- #
# The edit
# --------------------------------------------------------------------------- #
def apply_key_edit(root: Any, key: str, new_text: str, *,
                   base_sha256: str | None = None, write: bool = False,
                   memory: Any = None) -> dict[str, Any]:
    """Replace the text of node `key` by `new_text` (see the module
    docstring). Returns `span_edit`'s result shape with `format: "key"`,
    `key`, `start_line`/`end_line` (of the old node) and `node_sha256` (of
    its old text), or `{ok: False, stale: True, ...}`."""
    from ozagia.edit.span_edit import _Keyer, _parse_py, _read, apply_spans
    if not isinstance(new_text, str):
        raise CodeEditError("new_text must be a string")
    key = str(key or "").strip()
    rel = key_file(key)
    src = _read(root, rel)
    span = node_span(src, key, memory=memory)
    if span.get("slug"):
        from ozagia.core.sections import section_key
        key = section_key(src.rel, span["slug"])     # an alias's own key
    old = src.text[span["start"]:span["end"]]
    node_sha = sha256_text(old)
    view = {"key": key, "file": src.rel, "start_line": span["start_line"],
            "end_line": span["end_line"], "file_sha256": src.sha256,
            "node_sha256": node_sha}
    base = str(base_sha256 or "").strip()
    if base not in accepted_bases(src, span):
        why = ("base_sha256 is required" if not base else
               f"base_sha256 {base} is the file's sha256; a key edit of part "
               f"of a file takes the node's sha256" if base == src.sha256 else
               f"stale base: {base} is neither the node's sha256 nor the "
               f"file's")
        return {"ok": False, "applied": False, "stale": True, **view,
                "error": (f"{why}; the node's current text is in node_text "
                          f"(retry with base_sha256 {node_sha})"),
                "node_text": old}
    replacement = normalize_newlines(new_text, src.newline)
    if (span["lines"] and replacement and old.endswith(("\n", "\r"))
            and not replacement.endswith(("\n", "\r"))):
        replacement += src.newline
    if replacement == old:
        raise CodeEditError("new_text is the node's current text; nothing to do")
    s, e = span["start"], span["end"]
    old_tree = (_parse_py(src.text)[2]
                if PurePosixPath(src.rel).suffix.lower() == ".py" else None)
    keyer = _Keyer(root, src.rel, src.text, py_tree=old_tree)
    match = {"start": s, "end": e, "start_line": span["start_line"],
             "end_line": span["end_line"],
             **keyer.key(s, max(e, s + 1), span["start_line"], span["end_line"])}
    match["key"] = key
    res = apply_spans(root, src, [match], replacement, keys=[key],
                      write=write, memory=memory, fmt="key", old_tree=old_tree)
    res.update({"key": key, "start_line": span["start_line"],
                "end_line": span["end_line"], "node_sha256": node_sha,
                "file_sha256": src.sha256, "new_node_text": replacement,
                "new_node_sha256": sha256_text(replacement)})
    return res


__all__ = ["accepted_bases", "apply_key_edit", "find_section", "key_file", "node_span",
           "sha256_text"]
