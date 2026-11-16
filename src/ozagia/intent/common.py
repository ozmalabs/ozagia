"""Shared helpers of the intent layer: paths, file text, graph lookups and
the `[ozagia] ` line formats."""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ozagia.core.keys import file_key, symbol_key, test_key

TAG = "[ozagia] "
# Files larger than this are left to the built-in (memory.MAX_PARSE_BYTES).
MAX_READ_BYTES = 2 * 1024 * 1024
BINARY_SNIFF_BYTES = 8 * 1024
NODE_PREFIXES = ("sym:", "test:", "doc:", "text:", "file:")
# Keys listed inline on one header line.
HEADER_KEYS = 3
# Keys listed for a graph delta.
DELTA_KEYS = 10
_MD_SUFFIXES = frozenset({".md", ".markdown"})


class IntentError(Exception):
    """A tool error to report to the model (handled, `is_error`)."""

    def __init__(self, message: str, *, keys: list[str] | None = None,
                 intent: str | None = None,
                 meta: dict[str, Any] | None = None):
        super().__init__(message)
        self.keys = list(keys or [])
        self.intent = intent
        # Added to the answer's meta (counters such as `path_suggestions`).
        self.meta = dict(meta or {})


class Unhandled(Exception):
    """Leave the call to the built-in (`handled: false`)."""


class Preflight(Exception):
    """Raised by a handler whose input has `_preflight` set, at the point
    where it would first change something or run a command: the call
    would be carried out (see `ozagia.intent.handle`). `metadata` goes with
    the permission request (an edit's diff). `permission`, when given, is
    the request itself (the handler already knows what it would change),
    so `handle` need not work it out again."""

    def __init__(self, metadata: dict | None = None,
                 permission: dict | None = None) -> None:
        super().__init__("preflight")
        self.metadata = metadata
        self.permission = permission


def preflight_stop(inp: dict, metadata: dict | None = None, *,
                   permission: dict | None = None) -> None:
    """Stop here when `inp` is a preflight (see `Preflight`)."""
    if inp.get("_preflight"):
        raise Preflight(metadata, permission)


def diff_metadata(rel: str, before: str | None, after: str) -> dict:
    """OpenCode's permission metadata for a file change: `{"files": [{file,
    patch, status, additions, deletions}]}` with a unified diff."""
    import difflib
    a = (before or "").splitlines(keepends=True)
    b = after.splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, fromfile=rel, tofile=rel))
    adds = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    dels = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    return {"files": [{"file": rel, "patch": "".join(lines),
                       "status": "added" if before is None else "modified",
                       "additions": adds, "deletions": dels}]}


def tag(text: str) -> str:
    return TAG + text


def root_of(memory: Any) -> Path:
    return Path(memory.raw_repo_root).resolve()


def is_node_key(text: str) -> bool:
    return isinstance(text, str) and text.startswith(NODE_PREFIXES)


@dataclass(frozen=True)
class Target:
    path: Path      # absolute, resolved
    rel: str        # root-relative POSIX path ("" for the root itself)

    @property
    def display(self) -> str:
        return str(self.path)


def resolve(root: Path, path: Any, *, base: Path | None = None) -> Target:
    """`path` (absolute, or relative to `base` or the root) inside the root.

    Raises IntentError for a path outside the root."""
    if not isinstance(path, str) or not path.strip():
        raise IntentError("path is required")
    p = Path(path)
    if not p.is_absolute():
        p = (base or root) / p
    real = Path(os.path.realpath(p))
    try:
        rel = real.relative_to(root).as_posix()
    except ValueError:
        raise IntentError(
            f"{path} is outside the project root {root}; refused") from None
    return Target(real, "" if rel == "." else rel)


def in_state_dir(root: Path, path: Path) -> bool:
    from ozagia.config import state_dir
    sd = Path(os.path.realpath(state_dir(root)))
    return path == sd or sd in path.parents


def read_text(path: Path) -> str:
    """The decoded text of a file, or Unhandled for binary, oversized or
    non-UTF-8 files. A UTF-8 BOM is dropped."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise Unhandled(f"cannot stat {path}: {e}") from None
    if size > MAX_READ_BYTES:
        raise Unhandled(f"{path} is larger than {MAX_READ_BYTES} bytes")
    data = path.read_bytes()
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        raise Unhandled(f"{path} is binary")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unhandled(f"{path} is not UTF-8") from None
    return text[1:] if text.startswith("\ufeff") else text


def split_lines(text: str) -> list[str]:
    """Lines split at "\\n" only, without their line ending ("\\r\\n" too)."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


# --------------------------------------------------------------------------- #
# Python spans
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Span:
    start: int          # first line, decorators included
    end: int
    qualname: str
    is_test: bool
    top: bool           # module-level (qualname without a dot)


def py_spans(text: str) -> list[Span]:
    """Every def/class of a Python text in file order; [] if it does not
    parse. Qualnames and the test rule follow `ozagia.check`."""
    from ozagia.check import _text_spans
    spans = _text_spans(text)
    out = [Span(s, e, q, t, "." not in q) for s, e, q, t in spans]
    out.sort(key=lambda s: (s.start, -s.end))
    return out


def span_key(memory: Any, rel: str, span: Span) -> str:
    """The graph key of a span: the graph's own node when it has one."""
    tk, sk = test_key(rel, span.qualname), symbol_key(rel, span.qualname)
    if memory.node_by_key(sk) is not None:
        return sk
    if memory.node_by_key(tk) is not None:
        return tk
    return tk if span.is_test else sk


def parses(text: str) -> bool:
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- #
# Graph lookups
# --------------------------------------------------------------------------- #
def file_nid(memory: Any, rel: str) -> int | None:
    """The File node of an indexed file, or None."""
    nid = memory.node_by_key(file_key(rel))
    if nid is None:
        return None
    attrs = (memory.node(nid) or {}).get("attrs") or {}
    return nid if attrs.get("indexed", 1) else None


def _types(memory: Any, nids: Iterable[int], *names: str) -> list[int]:
    return [x for x in nids if (memory.node(x) or {}).get("type") in names]


def covering_tests(memory: Any, fid: int) -> list[int]:
    """Tests of a file: tests importing or mentioning it, then tests it holds."""
    seen: list[int] = []
    for et in ("tests_module", "mentions_file"):
        for x in memory._neighbours(fid, et, incoming=True):
            if x not in seen:
                seen.append(x)
    for x in memory._neighbours(fid, "has_test"):
        if x not in seen:
            seen.append(x)
    return _types(memory, seen, "Test")


def file_summary(memory: Any, rel: str) -> dict[str, Any] | None:
    fid = file_nid(memory, rel)
    if fid is None:
        return None
    symbols = _types(memory, memory._neighbours(fid, "defines"), "Symbol")
    return {"nid": fid,
            "imports": memory._neighbours(fid, "imports"),
            "imported_by": memory._neighbours(fid, "imports", incoming=True),
            "tests": covering_tests(memory, fid),
            "symbols": symbols}


def file_header(memory: Any, rel: str, info: dict[str, Any]) -> str:
    return tag(f"{file_key(rel)}  imports: {len(info['imports'])}  "
               f"imported by: {len(info['imported_by'])}  "
               f"tests: {len(info['tests'])}")


def role_note(info: dict[str, Any] | None) -> str:
    """`  [ozagia] <n> symbols, <m> tests` for a graph file, else ""."""
    if info is None:
        return ""
    return f"  {TAG}{len(info['symbols'])} symbols, {len(info['tests'])} tests"


def keys_list(keys: list[str], limit: int = HEADER_KEYS) -> str:
    shown = ", ".join(keys[:limit])
    more = f", +{len(keys) - limit} more" if len(keys) > limit else ""
    return f"{shown}{more}"


class SymbolTests:
    """Tests per symbol name of one file, the way `DevMemory.symbol` picks
    them: the file's covering tests whose file text spells the name, else
    all of the file's covering tests. Test files are read once."""

    def __init__(self, memory: Any, rel: str, info: dict[str, Any] | None):
        self.memory = memory
        self.cands: list[tuple[str, str]] = []   # (test key, test file)
        if info is None:
            return
        for tid in memory._incoming_unique(info["nid"],
                                           ("tests_module", "mentions_file")):
            n = memory.node(tid) or {}
            if n.get("type") != "Test":
                continue
            key = memory.key_of(tid)
            if key:
                self.cands.append((key, str(n["attrs"].get("file", ""))))
        self._texts: dict[str, str] = {}

    def _text(self, rel: str) -> str:
        if rel not in self._texts:
            self._texts[rel] = self.memory._read(rel) if rel else ""
        return self._texts[rel]

    def for_name(self, name: str) -> list[str]:
        if not self.cands:
            return []
        narrowed = []
        if len(name) >= 3:
            narrowed = [k for k, f in self.cands if name in self._text(f)]
        return narrowed or [k for k, _ in self.cands]


def symbol_header(key: str, tests: list[str] | None) -> str:
    line = key
    if tests is not None:
        line += f"  tests: {len(tests)}"
        if tests:
            line += f" ({keys_list(tests)})"
    return tag(line)


# --------------------------------------------------------------------------- #
# Enclosing keys
# --------------------------------------------------------------------------- #
class Keyer:
    """The key enclosing a line of one file: the innermost def/class of a
    Python file, `doc:` of a Markdown doc, the text chunk of an unparsed
    file, else `file:`."""

    def __init__(self, memory: Any, rel: str, text: str | None = None):
        from ozagia.index.text_nodes import is_structured
        self.memory, self.rel = memory, rel
        self.suffix = PurePosixPath(rel).suffix.lower()
        self.text = text
        self.structured = is_structured(rel)
        self._chunks: list[dict[str, Any]] | None = None
        self._spans: list[Span] | None = None

    def _source(self) -> str:
        if self.text is None:
            try:
                self.text = read_text(root_of(self.memory) / self.rel)
            except (Unhandled, OSError):
                self.text = ""
        return self.text

    def key(self, line: int) -> str:
        if self.suffix == ".py":
            if self._spans is None:
                self._spans = py_spans(self._source())
            best = None
            for s in self._spans:
                if s.start <= line <= s.end and (
                        best is None or (s.start, -s.end) > (best.start, -best.end)):
                    best = s
            return (span_key(self.memory, self.rel, best) if best
                    else file_key(self.rel))
        if self.suffix in _MD_SUFFIXES:
            dk = f"doc:{self.rel}"
            return dk if self.memory.node_by_key(dk) is not None else file_key(self.rel)
        if self.structured:
            return file_key(self.rel)
        from ozagia.index.text_nodes import locate_chunk, text_chunks
        if self._chunks is None:
            self._chunks = text_chunks(self.rel, self._source())
        chunk = locate_chunk(self._chunks, line)
        return chunk["key"] if chunk else file_key(self.rel)


# --------------------------------------------------------------------------- #
# Graph deltas
# --------------------------------------------------------------------------- #
def delta_lines(delta: Any) -> list[str]:
    """`[ozagia] ` lines for a GraphDelta or its dict; [] when empty."""
    if delta is None:
        return []
    d = delta.to_dict() if hasattr(delta, "to_dict") else dict(delta)
    if d.get("error"):
        return [tag(f"graph update failed: {d['error']}")]
    added, removed, changed = (list(d.get(k) or [])
                               for k in ("added", "removed", "changed"))
    ea, er = len(d.get("edges_added") or []), len(d.get("edges_removed") or [])
    if not (added or removed or changed or ea or er):
        return []
    keys = ([f"+{k}" for k in added] + [f"-{k}" for k in removed]
            + [f"~{k}" for k in changed])
    line = (f"delta: {len(added)} added, {len(removed)} removed, "
            f"{len(changed)} changed, edges +{ea} -{er}")
    if keys:
        line += ": " + ", ".join(keys[:DELTA_KEYS])
        if len(keys) > DELTA_KEYS:
            line += f", +{len(keys) - DELTA_KEYS} more"
    return [tag(line)]


def delta_keys(delta: Any) -> list[str]:
    if delta is None:
        return []
    d = delta.to_dict() if hasattr(delta, "to_dict") else dict(delta)
    return [*(d.get("added") or []), *(d.get("removed") or []),
            *(d.get("changed") or [])]


_WS_RE = re.compile(r"\s+")


def one_line(text: Any, limit: int = 200) -> str:
    s = _WS_RE.sub(" ", str(text)).strip()
    return s if len(s) <= limit else s[:limit - 3] + "..."
