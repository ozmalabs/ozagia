"""`read`: a file, a directory or a node key, verbatim, with graph headers.

Output (OpenCode 2.3's shape):

    Read file <abs path>, lines <a>-<b>[ of <total>]
    [ozagia] file:<rel>  imports: <n>  imported by: <n>  tests: <n>
    [ozagia] sym:<rel>::<name>  tests: <n> (<up to 3 keys>)
    <n>: <the file's line n, verbatim>
    ...

` of <total>` is added when the range does not cover the whole file. The
file header is present for a file in the graph. A symbol header precedes the
first line (decorators included) of each module-level def/class of an
indexed Python file; when the range starts inside one, its header precedes
the first shown line with `  (from line <start>)`. Test functions get
`[ozagia] test:<key>` without a test count. Callers are not shown: the graph
has no cheap caller edges. A Markdown doc gets `[ozagia] doc:<rel>` after the
file header; an unparsed text file gets `[ozagia] text:<key>` at each chunk
start.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ozagia.core.keys import file_key, parse_key
from ozagia.intent.common import (
    IntentError,
    SymbolTests,
    Target,
    Unhandled,
    file_header,
    file_summary,
    is_node_key,
    py_spans,
    read_text,
    resolve,
    role_note,
    root_of,
    span_key,
    split_lines,
    symbol_header,
    tag,
)

DEFAULT_LIMIT = 2000
_TEXT_KEY_RE = re.compile(r"^text:(?P<rel>.+)#L(?P<a>\d+)-L(?P<b>\d+)$")


def _int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise IntentError(f"{name} must be an integer, got {value!r}") from None
    if n < 1:
        raise IntentError(f"{name} must be at least 1, got {n}")
    return n


def _first_line(kind: str, display: str, a: int, b: int, total: int,
                unit: str) -> str:
    if total == 0:
        return f"Read {kind} {display}, 0 {unit}"
    head = f"Read {kind} {display}, {unit} {a}-{b}"
    if a > 1 or b < total:
        head += f" of {total}"
    return head


class _Rendered:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.keys: list[str] = []

    def key(self, k: str) -> None:
        if k not in self.keys:
            self.keys.append(k)


def _headers(memory: Any, rel: str, text: str, a: int, b: int,
             info: dict[str, Any] | None, out: _Rendered
             ) -> dict[int, list[str]]:
    """Header lines by the line number they precede."""
    from ozagia.index.text_nodes import is_structured, text_chunks
    at: dict[int, list[str]] = {}
    if rel.endswith(".py") and info is not None:
        tests = SymbolTests(memory, rel, info)
        for s in py_spans(text):
            if not s.top or s.end < a or s.start > b:
                continue
            key = span_key(memory, rel, s)
            out.key(key)
            line = symbol_header(
                key, None if key.startswith("test:") else
                tests.for_name(s.qualname))
            where = s.start
            if s.start < a:
                line += f"  (from line {s.start})"
                where = a
            at.setdefault(where, []).append(line)
    elif not is_structured(rel):
        for c in text_chunks(rel, text):
            if c["end_line"] < a or c["start_line"] > b:
                continue
            out.key(c["key"])
            where = max(a, c["start_line"])
            at.setdefault(where, []).append(tag(c["key"]))
    return at


def render_file(memory: Any, target: Target, *, offset: int | None,
                limit: int | None, cap: bool = True) -> _Rendered:
    """A file range in read format. `cap=False` reads to the end by default."""
    text = read_text(target.path)
    lines = split_lines(text)
    total = len(lines)
    a = offset or 1
    if total and a > total:
        raise IntentError(
            f"Offset {a} is out of range for this file ({total} lines)")
    n = limit if limit is not None else (DEFAULT_LIMIT if cap else total)
    b = min(total, a + n - 1)
    out = _Rendered()
    rel = target.rel
    info = file_summary(memory, rel)
    out.lines.append(_first_line("file", target.display, a, b, total, "lines"))
    if info is not None:
        out.key(file_key(rel))
        out.lines.append(file_header(memory, rel, info))
        dk = f"doc:{rel}"
        if memory.node_by_key(dk) is not None:
            out.key(dk)
            out.lines.append(tag(dk))
    at = _headers(memory, rel, text, a, b, info, out) if total else {}
    for i in range(a, b + 1):
        out.lines.extend(at.get(i, ()))
        out.lines.append(f"{i}: {lines[i - 1]}")
    return out


def _node_range(memory: Any, key: str, text: str, total: int
                ) -> tuple[int, int, str | None]:
    """(start, end, symbol name) of a sym:/test:/text: key in its file."""
    if key.startswith("text:"):
        m = _TEXT_KEY_RE.match(key)
        if not m:
            raise IntentError(f"not a text key: {key}")
        a, b = int(m.group("a")), int(m.group("b"))
        if a > max(total, 1) or b < a:
            raise IntentError(f"{key}: the file has {total} lines")
        return a, min(b, total), None
    _, rel, qual = parse_key(key)
    for s in py_spans(text):
        if s.qualname == qual and span_key(memory, rel, s) == key:
            return s.start, s.end, qual.rsplit(".", 1)[-1]
    nid = memory.node_by_key(key)
    if nid is not None:
        attrs = (memory.node(nid) or {}).get("attrs") or {}
        a = int(attrs.get("start_line") or 1)
        b = int(attrs.get("end_line") or a)
        if a <= total:
            return a, min(b, total), str(attrs.get("name") or qual)
    import difflib
    known = [span_key(memory, rel, s) for s in py_spans(text)]
    near = difflib.get_close_matches(key, known, n=3, cutoff=0.6)
    hint = f"\n{tag('nearest: ' + ', '.join(near))}" if near else ""
    raise IntentError(f"No node {key}{hint}")


def render_node(memory: Any, key: str, *, offset: int | None,
                limit: int | None) -> _Rendered:
    root = root_of(memory)
    kind = key.split(":", 1)[0]
    try:
        parsed = parse_key(key)
    except ValueError:
        raise IntentError(f"not a node key: {key}") from None
    if kind in ("file", "doc"):
        return render_file(memory, resolve(root, parsed[1]),
                           offset=offset, limit=limit)
    rel = (parsed[1].split("#", 1)[0] if kind == "text" else parsed[1])
    target = resolve(root, rel)
    if not target.path.is_file():
        raise IntentError(f"No node {key}: {rel} does not exist")
    text = read_text(target.path)
    lines = split_lines(text)
    total = len(lines)
    a, b, name = _node_range(memory, key, text, total)
    out = _Rendered()
    out.key(key)
    out.lines.append(_first_line("file", target.display, a, b, total, "lines"))
    if kind == "text":
        out.lines.append(tag(key))
    else:
        tests = None
        if kind == "sym":
            tests = SymbolTests(memory, rel, file_summary(memory, rel)
                                ).for_name(name or "")
        out.lines.append(symbol_header(key, tests))
    for i in range(a, b + 1):
        out.lines.append(f"{i}: {lines[i - 1]}")
    return out


def render_dir(memory: Any, target: Target, *, offset: int | None,
               limit: int | None) -> _Rendered:
    names = sorted(os.listdir(target.path))
    entries = []
    for name in names:
        p = target.path / name
        if p.is_dir():
            entries.append((name + "/", None))
        else:
            rel = f"{target.rel}/{name}" if target.rel else name
            entries.append((name, rel))
    total = len(entries)
    a = offset or 1
    if total and a > total:
        raise IntentError(
            f"Offset {a} is out of range for this directory ({total} entries)")
    b = min(total, a + (limit or DEFAULT_LIMIT) - 1)
    out = _Rendered()
    out.lines.append(_first_line("directory", target.display, a, b, total,
                                 "entries"))
    for name, rel in entries[a - 1:b]:
        note = ""
        if rel is not None:
            info = file_summary(memory, rel)
            note = role_note(info)
            if info is not None:
                out.key(file_key(rel))
        out.lines.append(name + note)
    return out


def handle_read(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    path = inp.get("path", inp.get("filePath"))
    offset = _int(inp.get("offset"), "offset")
    limit = _int(inp.get("limit"), "limit")
    root = root_of(memory)
    if is_node_key(path) and not (root / str(path)).exists():
        out = render_node(memory, path, offset=offset, limit=limit)
        return {"intent": "read_node", "output": "\n".join(out.lines),
                "keys": out.keys}
    target = resolve(root, path)
    if target.path.is_dir():
        out = render_dir(memory, target, offset=offset, limit=limit)
        intent = "read_dir"
    elif target.path.is_file():
        out = render_file(memory, target, offset=offset, limit=limit)
        intent = "read_file"
    else:
        raise Unhandled(f"no such file: {path}")
    return {"intent": intent, "output": "\n".join(out.lines), "keys": out.keys}


def read_range(memory: Any, path: Path, rng: tuple) -> dict[str, Any]:
    """A shell read (`cat`, `head`, `tail`, `sed -n`) as a read."""
    target = resolve(root_of(memory), str(path))
    if not target.path.is_file():
        raise Unhandled(f"not a regular file: {path}")
    text = read_text(target.path)
    total = len(split_lines(text))
    kind = rng[0]
    if kind == "all":
        offset, limit = 1, max(total, 1)
    elif kind == "head":
        offset, limit = 1, rng[1]
    elif kind == "tail":
        offset, limit = max(1, total - rng[1] + 1), rng[1]
    elif kind == "from":
        offset, limit = rng[1], max(1, total - rng[1] + 1)
    else:
        offset = rng[1]
        limit = (total - rng[1] + 1) if rng[2] is None else rng[2] - rng[1] + 1
    if total and offset > total:
        raise Unhandled("range starts after the end of the file")
    out = render_file(memory, target, offset=offset, limit=max(limit, 1),
                      cap=False)
    return {"intent": "shell_read", "output": "\n".join(out.lines),
            "keys": out.keys}


__all__ = ["DEFAULT_LIMIT", "handle_read", "read_range",
           "render_dir", "render_file", "render_node"]
