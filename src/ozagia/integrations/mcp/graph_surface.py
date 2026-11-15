"""ozagia.integrations.mcp.graph_surface — the `graph` MCP surface.

Eight verbs over the dev-memory graph, every node named by its stable key
(`ozagia.core.keys`), never by an integer id:

    find     fused name + code search                 -> ranked hits
    node     one node: outline, source, or full       -> its fields and text
    expand   BFS along graph edges (and calls)        -> nodes and edges
    impact   transitive callers, dependent files, the tests covering them
    edit     an exact-span edit (`DevMemory.span_edit`), optionally held to
             a node's line range
    create   a new file, or a new definition appended to a Python file/class
    check    pytest on test keys or on the tests covering other keys, with
             `$OZAGIA_RUN_PYTHON` (else `<root>/.venv/bin/python`, else
             the server's interpreter)
    note     a journal entry naming keys

The server selects this surface with `OZAGIA_SURFACE=graph` or `--surface
graph` (see `ozagia.integrations.mcp.server`). `docs/graph_surface.md`
describes the results.

The symbol graph and the file reference graph come from the tag cache. They
are built once per `GraphSurface` (one server process), the first time a verb
needs them, and rebuilt after this surface applies an edit or create. A
result that paid for that build reports `symbol_graph_seconds`.
"""
from __future__ import annotations

import ast
import bisect
import difflib
import os
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from ozagia.config import state_dir
from ozagia.core.keys import key_file
from ozagia.core.search import RRF_K
from ozagia.edit.code_edit import MAX_SOURCE_BYTES, CodeEditError
from ozagia.edit.txn import (
    normalize_newlines,
    read_source,
    sha256_bytes,
    write_source,
)

# Test files `check` runs for the non-test keys it is given.
MAX_CHECK_TEST_FILES = 20
# Lines of source `node` returns.
MAX_SOURCE_LINES = 2000
# Lines of `diff_view` that `edit` and `create` return.
MAX_DIFF_LINES = 200
# The target's interpreter for `check` (see `GraphSurface.run_python`).
RUN_PYTHON_ENV = "OZAGIA_RUN_PYTHON"

_CORE_KINDS = ("file", "sym", "test", "doc", "text")
_CORE_TYPES = frozenset({"Repo", "File", "Symbol", "Test", "Doc", "Commit",
                         "Failure"})
_CALL_EDGES = {"callers": "reverse", "callees": "forward"}
_TEST_EDGES = ("tests_module", "tested_by_file")


class SurfaceError(ValueError):
    """A verb argument names something that does not resolve."""


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


_KEYS = {"type": "array", "items": {"type": "string"}}

GRAPH_TOOLS: list[dict[str, Any]] = [
    {
        "name": "find",
        "description": (
            "Search the repo graph by name and code text. Returns ranked hits "
            "{key, kind, name, file, line, signature, summary, score}. Keys "
            "name nodes in every other verb: file:<path>, sym:<path>::<qualname>, "
            "test:<path>::<qualname>, doc:<path>, text:<path>#L<a>-L<b>."),
        "inputSchema": _schema({
            "query": {"type": "string"},
            "kinds": {**_KEYS, "description": "Key kinds to keep, e.g. [\"sym\",\"test\"]."},
            "limit": {"type": "integer", "default": 20},
        }, ["query"]),
    },
    {
        "name": "node",
        "description": (
            "One node by key. outline: kind, name, file, lines, signature, "
            "summary, edge counts. source: plus its verbatim text (symbol "
            "body, doc, text chunk). full: plus neighbour keys; a file: key "
            "gives the file text only with full."),
        "inputSchema": _schema({
            "key": {"type": "string"},
            "view": {"type": "string", "enum": ["outline", "source", "full"],
                     "default": "outline"},
        }, ["key"]),
    },
    {
        "name": "expand",
        "description": (
            "Breadth-first walk from keys along edge types (default all graph "
            "edges, both directions; \"callers\"/\"callees\" follow the call "
            "graph). Returns {nodes, edges: [[src, type, tgt]], truncated}."),
        "inputSchema": _schema({
            "keys": _KEYS,
            "edges": _KEYS,
            "depth": {"type": "integer", "default": 1},
            "limit": {"type": "integer", "default": 50},
        }, ["keys"]),
    },
    {
        "name": "impact",
        "description": (
            "What a change to these keys can affect: transitive callers, "
            "dependent files, and the tests covering them, each with `why` "
            "(one edge path back to the changed key). Each list is ranked by "
            "depth and cut at limit; counts gives the totals."),
        "inputSchema": _schema({
            "keys": _KEYS,
            "depth": {"type": "integer", "default": 2},
            "limit": {"type": "integer", "default": 50},
        }, ["keys"]),
    },
    {
        "name": "edit",
        "description": (
            "Replace exact text in a file, named by path or by a node key (the "
            "match must lie inside that node). Refused if it breaks the parse. "
            "Preview unless apply=true; returns keys, graph_delta, "
            "broken_references, diff_view, new_sha256."),
        "inputSchema": _schema({
            "key_or_file": {"type": "string"},
            "old_string": {"type": "string",
                           "description": "Unique unless replace_all."},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
            "apply": {"type": "boolean", "default": False},
            "base_sha256": {"type": "string"},
        }, ["key_or_file", "old_string", "new_string"]),
    },
    {
        "name": "create",
        "description": (
            "Create a NEW file (path), or append a definition to a Python "
            "file (parent_key file:/sym:; a class key adds a method). Parse "
            "guarded. Preview unless apply=true; returns new keys and "
            "graph_delta. Use edit for existing text."),
        "inputSchema": _schema({
            "path": {"type": "string"},
            "parent_key": {"type": "string"},
            "content": {"type": "string"},
            "apply": {"type": "boolean", "default": False},
        }, ["content"]),
    },
    {
        "name": "check",
        "description": (
            "Run pytest on tests (test keys or pytest ids) and on the tests "
            "covering other keys. Returns status, counts, per-test {key, "
            "outcome, message, frames} with frames as keys."),
        "inputSchema": _schema({
            "keys": _KEYS,
            "tests": _KEYS,
            "timeout_s": {"type": "number", "default": 300},
        }, []),
    },
    {
        "name": "note",
        "description": "Record a note naming keys in the dev journal.",
        "inputSchema": _schema({
            "text": {"type": "string"},
            "keys": _KEYS,
        }, ["text"]),
    },
]
GRAPH_TOOL_NAMES = tuple(t["name"] for t in GRAPH_TOOLS)


def description_tokens(tools: Iterable[dict[str, Any]] = GRAPH_TOOLS) -> int:
    """Estimated tokens (len/4) of the tool and parameter descriptions."""
    chars = 0
    for tool in tools:
        chars += len(tool["description"])
        for spec in tool["inputSchema"]["properties"].values():
            chars += len(spec.get("description", ""))
    return chars // 4


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """`d` without None values and empty strings."""
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _kind(key: str) -> str:
    return key.partition(":")[0]


class GraphSurface:
    """The eight verbs over one `DevMemory`."""

    def __init__(self, memory: Any) -> None:
        self.mem = memory
        self.root = Path(memory.raw_repo_root).resolve()
        self._names: list[tuple[str, int]] | None = None
        self._names_stamp: tuple | None = None
        self._tags: dict[str, Any] | None = None

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        verbs = {"find": self.find, "node": self.node, "expand": self.expand,
                 "impact": self.impact, "edit": self.edit,
                 "create": self.create, "check": self.check,
                 "note": self.note}

        def wrap(fn: Callable[..., Any]) -> Callable[[dict[str, Any]], Any]:
            def call(args: dict[str, Any]) -> Any:
                try:
                    return fn(**args)
                except (SurfaceError, CodeEditError) as e:
                    return {"ok": False, "error": str(e)}
            return call
        return {name: wrap(fn) for name, fn in verbs.items()}

    # ------------------------------------------------------------------ #
    # paths, keys, node fields
    # ------------------------------------------------------------------ #
    def _rel(self, path: str) -> str:
        """`path` as a repo-relative POSIX path; refuses one outside the root."""
        raw = str(path or "").strip()
        if not raw:
            raise SurfaceError("empty path")
        p = Path(raw)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root) or p == self.root:
            raise SurfaceError(f"path escapes the repo root: {path}")
        return p.relative_to(self.root).as_posix()

    def _is_key(self, s: str) -> bool:
        return (self.mem.node_by_key(s) is not None
                or _kind(s) in _CORE_KINDS and ":" in s)

    def _text_ref(self, key: str) -> dict[str, Any]:
        from ozagia.index.text_nodes import _parse_text_key, is_structured, text_chunks
        parsed = _parse_text_key(key)
        if parsed is None:
            raise SurfaceError(f"not a text key: {key}")
        rel = self._rel(parsed[0])
        p = self.root / rel
        if is_structured(rel) or not p.is_file():
            raise SurfaceError(f"no text chunk {key}")
        text = self.mem._read(rel)
        for chunk in text_chunks(rel, text):
            if chunk["key"] == key:
                return {"key": key, "kind": "text", "nid": None, "name": key,
                        "file": rel, "start": chunk["start_line"],
                        "end": chunk["end_line"], "text": chunk["text"]}
        raise SurfaceError(f"no text chunk {key}; the file has changed, "
                           f"find the chunk again with node(file:{rel})")

    def _text_file_ref(self, key: str) -> dict[str, Any]:
        """A `file:` key of a text file the graph does not index: its chunks
        are its nodes (`text:` keys)."""
        from ozagia.index.text_nodes import is_structured, text_chunks
        rel = self._rel(key.partition(":")[2])
        if is_structured(rel) or not (self.root / rel).is_file():
            raise SurfaceError(f"no node {key}")
        text = self.mem._read(rel)
        if not text:
            raise SurfaceError(f"no node {key} (empty or not text)")
        chunks = text_chunks(rel, text)
        return {"key": key, "kind": "file", "nid": None, "name": rel,
                "file": rel, "start": 1, "end": chunks[-1]["end_line"],
                "text": text, "chunks": [c["key"] for c in chunks]}

    def _ref(self, key: str) -> dict[str, Any]:
        """The node named by `key`: {key, kind, nid, name, file, start, end}."""
        key = str(key or "").strip()
        if _kind(key) == "text":
            return self._text_ref(key)
        nid = self.mem.node_by_key(key)
        if nid is None:
            if _kind(key) == "file":
                return self._text_file_ref(key)
            raise SurfaceError(f"no node {key}")
        rel = self._file_of(nid)
        if rel is not None:
            self._rel(rel)
        return {"key": key, "kind": _kind(key), "nid": nid,
                **self._fields(nid)}

    def _file_of(self, nid: int) -> str | None:
        n = self.mem.node(nid) or {}
        if n.get("type") == "Doc":
            return (n.get("attrs") or {}).get("path")
        return self.mem._file_for(nid)

    def _fields(self, nid: int) -> dict[str, Any]:
        n = self.mem.node(nid) or {}
        attrs = n.get("attrs") or {}
        typ = n.get("type")
        if typ in ("Symbol", "Test"):
            name = attrs.get("qualname") or attrs.get("name")
        elif typ == "Doc":
            name = attrs.get("title") or attrs.get("path")
        else:
            name = self.mem.label(nid)
        start = attrs.get("start_line")
        end = attrs.get("end_line")
        rel = self._file_of(nid)
        if start and attrs.get("decorators") and rel:
            start = self._decorated_start(rel, int(start))
        return {"name": name, "file": rel,
                "start": int(start) if start else None,
                "end": int(end) if end else (int(start) if start else None)}

    def _decorated_start(self, rel: str, line: int) -> int:
        """The first line of the decorators above a def at `line`."""
        lines = self.mem._read(rel).splitlines()
        while line > 1 and lines[line - 2:line - 1] and \
                lines[line - 2].lstrip().startswith("@"):
            line -= 1
        return line

    def _key(self, nid: int) -> str | None:
        return self.mem.key_of(nid)

    def _brief(self, nid: int) -> dict[str, Any]:
        f = self._fields(nid)
        key = self._key(nid) or ""
        return _clean({"key": key, "kind": _kind(key), "name": f["name"],
                       "file": f["file"], "line": f["start"]})

    def _signature(self, nid: int) -> str | None:
        n = self.mem.node(nid) or {}
        if n.get("type") not in ("Symbol", "Test"):
            return None
        attrs = n.get("attrs") or {}
        rel = self._file_of(nid) or ""
        start = int(attrs.get("start_line") or 0)
        lines = self.mem._read(rel).splitlines() if rel else []
        if not 0 < start <= len(lines):
            return None
        if not rel.endswith(".py"):
            return lines[start - 1].strip()
        # A Python def/class header runs to the line ending in ":".
        head = []
        for text in lines[start - 1:start - 1 + 10]:
            head.append(text.strip())
            if text.split("#", 1)[0].rstrip().endswith(":"):
                break
        return " ".join(head)

    def _summary(self, nid: int) -> str | None:
        attrs = (self.mem.node(nid) or {}).get("attrs") or {}
        for field in ("doc", "description", "title"):
            v = attrs.get(field)
            if isinstance(v, str) and v.strip():
                return v.strip().splitlines()[0]
        return None

    def _edge_counts(self, nid: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.mem._out.get(nid, []):
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        for e in self.mem._in.get(nid, []):
            t = "<" + e["type"]
            counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items()))

    # ------------------------------------------------------------------ #
    # tag cache: symbol graph and file reference graph
    # ------------------------------------------------------------------ #
    def _tag_graphs(self) -> tuple[dict[str, Any], float | None]:
        """(state, seconds spent building it in this call or None)."""
        if self._tags is not None:
            return self._tags, None
        from ozagia.core.repo_map import _prepared, update_tag_cache
        from ozagia.core.symbol_graph import _adjacency, build_symbol_graph
        t0 = time.perf_counter()
        cache, _stats = update_tag_cache(self.root,
                                         cache_path=self.mem._tag_cache())
        graph = build_symbol_graph(cache)
        forward, reverse = _adjacency(graph)
        prep = _prepared(cache)
        self._tags = {"graph": graph, "forward": forward, "reverse": reverse,
                      "file_reverse": prep.reverse,
                      "files": set(cache.get("files") or {})}
        return self._tags, round(time.perf_counter() - t0, 4)

    def _invalidate(self) -> None:
        self._tags = None
        self._names = None

    def _sg_key(self, sid: str) -> str | None:
        """The graph key for a symbol-graph id `<rel>::<qualname>`."""
        rel, _, qual = sid.partition("::")
        if qual and qual != "<module>":
            for prefix in ("sym:", "test:"):
                if self.mem.node_by_key(prefix + sid) is not None:
                    return prefix + sid
        key = "file:" + rel
        return key if self.mem.node_by_key(key) is not None else None

    @staticmethod
    def _sg_id(key: str) -> str | None:
        if _kind(key) in ("sym", "test"):
            return key.partition(":")[2]
        return None

    # ------------------------------------------------------------------ #
    # find
    # ------------------------------------------------------------------ #
    def _name_index(self) -> list[tuple[str, int]]:
        stamp = (id(self.mem.nodes), len(self.mem.nodes), self.mem._next_id)
        if self._names is not None and self._names_stamp == stamp:
            return self._names
        rows: list[tuple[str, int]] = []
        for nid, n in self.mem.nodes.items():
            if n.get("type") in ("Repo", "File", "Commit", "Failure"):
                continue
            if self.mem.key_of(nid) is None:
                continue
            attrs = n.get("attrs") or {}
            names = {attrs.get(f) for f in ("name", "qualname", "title", "id")}
            for name in names:
                if isinstance(name, str) and name.strip():
                    rows.append((name.strip().lower(), nid))
        rows.sort()
        self._names, self._names_stamp = rows, stamp
        return rows

    def _name_hits(self, query: str) -> list[int]:
        """Nodes whose name/qualname/title is the query (case-sensitive,
        then not) or starts with it."""
        q = query.strip()
        ql = q.lower()
        rows = self._name_index()
        i = bisect.bisect_left(rows, (ql, -1))
        best: dict[int, tuple] = {}
        while i < len(rows) and rows[i][0].startswith(ql):
            name, nid = rows[i]
            i += 1
            attrs = (self.mem.node(nid) or {}).get("attrs") or {}
            exact = any(attrs.get(f) == q for f in
                        ("name", "qualname", "title", "id"))
            rank = (0 if exact else 1 if name == ql else 2, len(name),
                    self.mem.key_of(nid) or "")
            if nid not in best or rank < best[nid]:
                best[nid] = rank
        return sorted(best, key=lambda nid: best[nid])

    def _search_hits(self, query: str, limit: int) -> list[tuple[int, int | None]]:
        """code_search as (nid, line) in rank order: per file, its matched
        symbols, then the file."""
        res = self.mem.code_search(query, limit=limit, include_snippet=False)
        out: list[tuple[int, int | None]] = []
        for row in res.get("results") or []:
            fid = self.mem.node_by_key("file:" + str(row.get("path")))
            if fid is None:
                continue
            children = [x for e in self.mem._out.get(fid, [])
                        if e["type"] in ("defines", "has_test")
                        for x in [int(e["tgt"])]
                        if (self.mem.node(x) or {}).get("type") in ("Symbol", "Test")]
            for sym in row.get("symbols") or []:
                name, line = sym.get("name"), sym.get("line")
                same = [x for x in children
                        if (self.mem.node(x) or {}).get("attrs", {}).get("name") == name]
                at = [x for x in same if (self.mem.node(x) or {}).get(
                    "attrs", {}).get("start_line") == line]
                pick = (at or sorted(same, key=lambda x: self._key(x) or ""))
                if pick:
                    out.append((pick[0], None))
            out.append((fid, row.get("line")))
        return out

    def find(self, query: str, kinds: list[str] | None = None,
             limit: int = 20) -> dict[str, Any]:
        q = str(query or "").strip()
        if not q:
            raise SurfaceError("empty query")
        limit = max(1, int(limit))
        wanted = {str(k) for k in kinds} if kinds else None
        scores: dict[int, float] = {}
        lines: dict[int, int] = {}
        exact_key = self.mem.node_by_key(q)
        ranked_lists: list[list[int]] = []
        if exact_key is not None:
            ranked_lists.append([exact_key])
        ranked_lists.append(self._name_hits(q))
        search = self._search_hits(q, limit)
        ranked_lists.append(list(dict.fromkeys(nid for nid, _ in search)))
        for nid, line in search:
            if line and nid not in lines:
                lines[nid] = int(line)
        for ranked in ranked_lists:
            for rank, nid in enumerate(ranked, start=1):
                scores[nid] = scores.get(nid, 0.0) + 1.0 / (RRF_K + rank)
        order = sorted(scores, key=lambda nid: (-scores[nid],
                                                self._key(nid) or ""))
        hits = []
        for nid in order:
            key = self._key(nid)
            if not key or (wanted is not None and _kind(key) not in wanted):
                continue
            f = self._fields(nid)
            hits.append(_clean({
                "key": key, "kind": _kind(key), "name": f["name"],
                "file": f["file"], "line": f["start"] or lines.get(nid),
                "signature": self._signature(nid),
                "summary": self._summary(nid),
                "score": round(scores[nid], 6)}))
            if len(hits) >= limit:
                break
        return {"ok": True, "query": q, "hits": hits}

    # ------------------------------------------------------------------ #
    # node
    # ------------------------------------------------------------------ #
    @staticmethod
    def _capped(lines: list[str]) -> tuple[str, bool]:
        return ("".join(lines[:MAX_SOURCE_LINES]),
                len(lines) > MAX_SOURCE_LINES)

    def node(self, key: str, view: str = "outline") -> dict[str, Any]:
        if view not in ("outline", "source", "full"):
            raise SurfaceError(f"unknown view {view!r}")
        ref = self._ref(key)
        nid = ref["nid"]
        out: dict[str, Any] = {"ok": True, "key": ref["key"],
                               "kind": ref["kind"], "name": ref["name"],
                               "file": ref["file"]}
        if ref["start"]:
            out["lines"] = [ref["start"], ref["end"]]
        if nid is not None:
            out.update(_clean({"signature": self._signature(nid),
                               "summary": self._summary(nid)}))
            out["edges"] = self._edge_counts(nid)
        if ref.get("chunks") is not None:
            out["chunks"] = ref["chunks"]
        if view == "outline":
            return _clean(out)
        text, truncated = self._source(ref, full=view == "full")
        if text is not None:
            out["source"] = text
            out["truncated"] = truncated
        elif ref["kind"] == "file":
            out["source_note"] = "use view=full for a file's text"
        if view == "full" and nid is not None:
            out["neighbours"] = self._neighbour_keys(nid)
        return _clean(out)

    def _source(self, ref: dict[str, Any], *, full: bool
                ) -> tuple[str | None, bool]:
        if ref["kind"] == "text":
            return ref["text"], False
        if ref.get("chunks") is not None:
            if not full:
                return None, False
            return self._capped(ref["text"].splitlines(keepends=True))
        nid = ref["nid"]
        typ = (self.mem.node(nid) or {}).get("type")
        rel = ref["file"]
        if typ in ("Symbol", "Test") and rel and ref["start"]:
            lines = self.mem._read(rel).splitlines(keepends=True)
            return self._capped(lines[ref["start"] - 1:ref["end"]])
        if typ == "Doc" and rel:
            return self._capped(self.mem._read(rel).splitlines(keepends=True))
        if typ == "File":
            if not full or not rel:
                return None, False
            return self._capped(self.mem._read(rel).splitlines(keepends=True))
        if typ in _CORE_TYPES:
            return None, False
        snip = self.mem.snippet(nid)
        return (snip.get("text") or None), False

    def _neighbour_keys(self, nid: int) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self.mem._out.get(nid, []):
            k = self._key(int(e["tgt"]))
            if k:
                out.setdefault(e["type"], []).append(k)
        for e in self.mem._in.get(nid, []):
            k = self._key(int(e["src"]))
            if k:
                out.setdefault("<" + e["type"], []).append(k)
        return {t: sorted(ks)[:12] for t, ks in sorted(out.items())}

    # ------------------------------------------------------------------ #
    # expand
    # ------------------------------------------------------------------ #
    def expand(self, keys: list[str], edges: list[str] | None = None,
               depth: int = 1, limit: int = 50) -> dict[str, Any]:
        depth, limit = max(0, int(depth)), max(1, int(limit))
        starts = [self._ref(k) for k in keys]
        for ref in starts:
            if ref["nid"] is None:
                raise SurfaceError(f"{ref['key']} has no graph edges")
        wanted = [str(e) for e in edges] if edges else None
        calls = [e for e in (wanted or []) if e in _CALL_EDGES]
        graph_types = (None if wanted is None
                       else {e for e in wanted if e not in _CALL_EDGES})
        out: dict[str, Any] = {"ok": True}
        tags = None
        if calls:
            tags, built = self._tag_graphs()
            if built is not None:
                out["symbol_graph_seconds"] = built
        seen: dict[str, int] = {}
        order: list[str] = []
        for ref in starts:
            if ref["key"] not in seen:
                seen[ref["key"]] = ref["nid"]
                order.append(ref["key"])
        edge_list: list[list[str]] = []
        edge_seen: set[tuple[str, str, str]] = set()
        truncated = False
        frontier = sorted(order)
        for _hop in range(depth):
            nxt: list[str] = []
            for cur in frontier:
                if cur == "repo:":
                    continue  # the Repo hub links every file
                for src, typ, tgt, other in self._steps(
                        cur, seen[cur], graph_types, calls, tags):
                    if other not in seen:
                        if len(order) >= limit:
                            truncated = True
                            continue
                        seen[other] = self.mem.node_by_key(other)
                        order.append(other)
                        nxt.append(other)
                    edge = (src, typ, tgt)
                    if edge not in edge_seen:
                        edge_seen.add(edge)
                        edge_list.append(list(edge))
            frontier = sorted(nxt)
            if not frontier:
                break
        out["nodes"] = [self._brief(seen[k]) for k in order]
        out["edges"] = edge_list
        out["truncated"] = truncated
        return out

    def _steps(self, key: str, nid: int, types: set[str] | None,
               calls: list[str], tags: dict[str, Any] | None
               ) -> list[tuple[str, str, str, str]]:
        """(src, type, tgt, other end) for every edge of `key` to walk."""
        steps: list[tuple[str, str, str, str]] = []
        if types is None or types:
            for e in self.mem._out.get(nid, []):
                if types is None or e["type"] in types:
                    other = self._key(int(e["tgt"]))
                    if other:
                        steps.append((key, e["type"], other, other))
            for e in self.mem._in.get(nid, []):
                if types is None or e["type"] in types:
                    other = self._key(int(e["src"]))
                    if other:
                        steps.append((other, e["type"], key, other))
        sid = self._sg_id(key)
        if tags is not None and sid is not None:
            for direction in calls:
                adj = tags[_CALL_EDGES[direction]]
                for target, _res, _conf in adj.get(sid, ()):
                    other = self._sg_key(target)
                    if not other or other == key:
                        continue
                    if direction == "callers":
                        steps.append((other, "calls", key, other))
                    else:
                        steps.append((key, "calls", other, other))
        steps.sort(key=lambda s: (s[1], s[3], s[0]))
        return steps

    # ------------------------------------------------------------------ #
    # impact
    # ------------------------------------------------------------------ #
    def impact(self, keys: list[str], depth: int = 2,
               limit: int = 50) -> dict[str, Any]:
        depth, limit = max(1, int(depth)), max(1, int(limit))
        refs = [self._ref(k) for k in keys]
        tags, built = self._tag_graphs()
        graph, reverse = tags["graph"], tags["reverse"]
        origin_keys = {r["key"] for r in refs}
        # Symbol BFS over reverse call edges: sid -> (depth, why path).
        sym_paths: dict[str, tuple[int, list[str]]] = {}
        frontier: list[tuple[str, list[str]]] = []
        origin_files: dict[str, list[str]] = {}
        for ref in refs:
            rel = ref["file"]
            if rel:
                origin_files.setdefault(rel, [ref["key"]] if ref["kind"] == "file"
                                        else ["file:" + rel, "defines", ref["key"]]
                                        if ref["kind"] in ("sym", "test")
                                        else [ref["key"]])
            sid = self._sg_id(ref["key"])
            if sid is not None and sid in graph["nodes"]:
                frontier.append((sid, [ref["key"]]))
            elif ref["kind"] == "file" and rel:
                for s, node in sorted(graph["nodes"].items()):
                    if node["file"] == rel and node["qualname"] != "<module>":
                        k = self._sg_key(s)
                        if k:
                            frontier.append((s, [k, "defined_in", ref["key"]]))
        visited = {s for s, _ in frontier}
        for hop in range(1, depth + 1):
            nxt: list[tuple[str, list[str]]] = []
            for sid, path in sorted(frontier, key=lambda x: x[0]):
                for caller, _res, _conf in sorted(reverse.get(sid, ())):
                    if caller in visited:
                        continue
                    visited.add(caller)
                    ck = self._sg_key(caller)
                    if not ck:
                        continue
                    p = [ck, "calls", *path]
                    sym_paths[caller] = (hop, p)
                    nxt.append((caller, p))
            frontier = nxt
        symbols, tests = [], {}
        for sid, (d, path) in sym_paths.items():
            k = path[0]
            if k in origin_keys:
                continue
            item = {"key": k, "depth": d, "why": path}
            if _kind(k) == "test":
                tests.setdefault(k, item)
            elif _kind(k) == "sym":
                symbols.append(item)
        # File BFS over the reference graph.
        file_rev = tags["file_reverse"]
        file_paths: dict[str, tuple[int, list[str]]] = {}
        ffront = [(rel, path) for rel, path in sorted(origin_files.items())
                  if rel in tags["files"]]
        fvisited = set(origin_files)
        for hop in range(1, depth + 1):
            nxt_f: list[tuple[str, list[str]]] = []
            for rel, path in ffront:
                for dep in sorted(file_rev.get(rel, ())):
                    if dep in fvisited:
                        continue
                    fvisited.add(dep)
                    fk = "file:" + dep
                    if self.mem.node_by_key(fk) is None:
                        continue
                    p = [fk, "references", *path]
                    file_paths[dep] = (hop, p)
                    nxt_f.append((dep, p))
            ffront = nxt_f
        for item in symbols:
            rel = key_file(item["key"])
            if rel and rel not in file_paths and rel not in origin_files:
                file_paths[rel] = (item["depth"],
                                   ["file:" + rel, "defines", *item["why"]])
        files = [{"key": p[0], "depth": d, "why": p}
                 for rel, (d, p) in file_paths.items()]
        # Tests covering the changed and affected files.
        covered = [(rel, 0, path) for rel, path in sorted(origin_files.items())]
        covered += [(rel, d, p) for rel, (d, p) in sorted(file_paths.items())]
        for rel, d, path in covered:
            fid = self.mem.node_by_key("file:" + rel)
            if fid is None:
                continue
            for e in self.mem._in.get(fid, []):
                if e["type"] not in _TEST_EDGES:
                    continue
                tk = self._key(int(e["src"]))
                if tk and _kind(tk) == "test" and tk not in origin_keys:
                    item = {"key": tk, "depth": d + 1,
                            "why": [tk, e["type"], *path]}
                    if tk not in tests or tests[tk]["depth"] > item["depth"]:
                        tests[tk] = item

        def ranked(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(items, key=lambda x: (x["depth"], x["key"]))[:limit]
        totals = {"symbols": len(symbols), "files": len(files),
                  "tests": len(tests)}
        out = {"ok": True, "keys": [r["key"] for r in refs], "depth": depth,
               "symbols": ranked(symbols), "files": ranked(files),
               "tests": ranked(tests.values()), "counts": totals,
               "truncated": any(n > limit for n in totals.values())}
        if built is not None:
            out["symbol_graph_seconds"] = built
        return out

    # ------------------------------------------------------------------ #
    # edit
    # ------------------------------------------------------------------ #
    def edit(self, key_or_file: str, old_string: str, new_string: str,
             replace_all: bool = False, apply: bool = False,
             base_sha256: str | None = None) -> dict[str, Any]:
        from ozagia.edit.span_edit import locate
        target = str(key_or_file or "").strip()
        ref = None
        if self._is_key(target):
            ref = self._ref(target)
            if not ref["file"]:
                raise SurfaceError(f"{target} names no file")
            rel = self._rel(ref["file"])
        else:
            rel = self._rel(target)
        self._guard_rel(rel, verb="edit")
        if ref is not None and ref["start"]:
            loc = locate(self.root, rel, old_string, replace_all=replace_all)
            if loc.get("ok"):
                lo, hi = ref["start"], ref["end"]
                outside = [m for m in loc["matches"]
                           if m["start_line"] < lo or m["end_line"] > hi]
                if outside:
                    return {"ok": False, "applied": False, "file": rel,
                            "error": (f"old_string matches outside {ref['key']} "
                                      f"(lines {lo}-{hi})"),
                            "range": [lo, hi],
                            "matches": [self._match(m) for m in loc["matches"]]}
        res = self.mem.span_edit(rel, old_string, new_string,
                                 replace_all=replace_all,
                                 base_sha256=base_sha256 or None, apply=apply)
        if res.get("applied"):
            self._invalidate()
        return self._trim_edit(res)

    @staticmethod
    def _match(m: dict[str, Any]) -> dict[str, Any]:
        return {"key": m.get("key"), "lines": [m.get("start_line"),
                                               m.get("end_line")]}

    @staticmethod
    def _diff(text: str) -> tuple[str, bool]:
        lines = text.splitlines(keepends=True)
        return "".join(lines[:MAX_DIFF_LINES]), len(lines) > MAX_DIFF_LINES

    def _trim_edit(self, res: dict[str, Any]) -> dict[str, Any]:
        if not res.get("ok"):
            out = {"ok": False, "applied": False, "file": res.get("file"),
                   "error": res.get("error")}
            for k in ("parse_error", "parse_check", "nearest"):
                if res.get(k) is not None:
                    out[k] = res[k]
            if res.get("matches"):
                out["matches"] = [self._match(m) for m in res["matches"]]
            return _clean(out)
        diff, diff_cut = self._diff(res.get("diff_view") or "")
        out = {"ok": True, "applied": bool(res.get("applied")),
               "file": res.get("file"), "keys": res.get("keys"),
               "parse_check": res.get("parse_check"),
               "broken_references": res.get("broken_references") or [],
               "broken_references_truncated":
                   bool(res.get("broken_references_truncated")),
               "diff_view": diff, "diff_truncated": diff_cut,
               "new_sha256": res.get("new_sha256"),
               "backup": res.get("backup"),
               "graph_delta": res.get("graph_delta"),
               "warning": res.get("warning")}
        return _clean(out)

    # ------------------------------------------------------------------ #
    # create
    # ------------------------------------------------------------------ #
    def create(self, content: str, path: str | None = None,
               parent_key: str | None = None,
               apply: bool = False) -> dict[str, Any]:
        if (path is None) == (parent_key is None):
            raise SurfaceError("create takes exactly one of path or parent_key")
        if path is not None:
            return self._create_file(self._rel(path), content, apply)
        return self._create_in(str(parent_key).strip(), content, apply)

    def _guard_rel(self, rel: str, *, verb: str = "create") -> None:
        """Refuse writes into ozagia's state directory and into `.git` (at
        any depth, also through a symlink)."""
        from ozagia.edit.txn import in_git_dir
        p = (self.root / rel).resolve()
        state = Path(os.path.realpath(state_dir(self.root)))
        try:
            real = p.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            real = rel
        if in_git_dir(rel) or in_git_dir(real):
            raise SurfaceError(f"refusing to {verb} inside .git: {rel}")
        if p == state or p.is_relative_to(state):
            raise SurfaceError(f"refusing to {verb} inside ozagia's state "
                               f"directory: {rel}")

    def _create_file(self, rel: str, content: str, apply: bool) -> dict[str, Any]:
        from ozagia.edit.span_edit import _parse_error
        self._guard_rel(rel)
        p = self.root / rel
        if p.exists() or p.is_symlink():
            raise SurfaceError(f"{rel} exists; use edit to change it")
        status, err = _parse_error(rel, content)
        if status == "error":
            return {"ok": False, "applied": False, "file": rel,
                    "parse_check": "refused", "parse_error": err,
                    "error": f"content does not parse at line {err['line']}: "
                             f"{err['message']}"}
        data = content.encode("utf-8")
        out: dict[str, Any] = {"ok": True, "applied": False, "file": rel,
                               "parse_check": status,
                               "lines": len(content.splitlines()),
                               "new_sha256": sha256_bytes(data)}
        if not apply:
            return out
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.",
                                        suffix=".ozagia_tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp_name, 0o666 & ~umask)
            try:
                os.link(tmp_name, p)  # atomic, and never replaces a file
            except FileExistsError:
                raise SurfaceError(f"{rel} exists; use edit to change it") from None
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        out["applied"] = True
        return self._after_write(out, rel, "create", f"create {rel}")

    def _after_write(self, out: dict[str, Any], rel: str, kind: str,
                     task: str) -> dict[str, Any]:
        self.mem._drop_cached(rel)
        self._invalidate()
        try:
            delta = self.mem.reingest([rel]).to_dict()
        except Exception as e:  # noqa: BLE001 - the write is done; report it
            delta = {"error": f"{type(e).__name__}: {e}"}
        out["keys"] = list(delta.get("added") or [])
        out["graph_delta"] = delta
        self.mem.journal_append(kind, task, f"{len(out['keys'])} new key(s)",
                                files=[rel], keys=out["keys"],
                                new_sha256=out.get("new_sha256"),
                                backup=out.get("backup"))
        return out

    def _create_in(self, parent_key: str, content: str,
                   apply: bool) -> dict[str, Any]:
        if _kind(parent_key) not in ("file", "sym"):
            raise SurfaceError("parent_key must be a file: or sym: key")
        ref = self._ref(parent_key)
        rel = self._rel(ref["file"] or "")
        self._guard_rel(rel)
        if not rel.endswith(".py"):
            raise SurfaceError(f"{rel} is not a Python file")
        src = read_source(self.root, rel, suffixes=(".py",),
                          max_bytes=MAX_SOURCE_BYTES, error_cls=CodeEditError)
        try:
            tree = ast.parse(src.text)
        except SyntaxError as e:
            raise SurfaceError(f"{rel} does not parse (line {e.lineno}); "
                               f"fix it with edit first") from None
        body = textwrap.dedent(content).strip("\n")
        try:
            snippet = ast.parse(body)
        except SyntaxError as e:
            return {"ok": False, "applied": False, "file": rel,
                    "parse_check": "refused",
                    "error": f"content does not parse at line {e.lineno}: {e.msg}"}
        defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        if not snippet.body or not all(isinstance(s, defs) for s in snippet.body):
            raise SurfaceError("content must be one or more def/class statements")
        lines = src.text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += src.newline
        nl = src.newline
        if ref["kind"] == "file":
            at, indent, gap = len(lines), "", 2
        else:
            node = self._find_def(tree, ref)
            if node is None:
                raise SurfaceError(f"{parent_key} not found in {rel}")
            at = node.end_lineno or node.lineno
            if isinstance(node, ast.ClassDef):
                first = node.body[0]
                if first.lineno == node.lineno:
                    indent = " " * (node.col_offset + 4)
                else:
                    text = lines[first.lineno - 1]
                    indent = text[:len(text) - len(text.lstrip())]
                gap = 1
            elif node.col_offset == 0:
                indent, gap = "", 2
            else:
                raise SurfaceError(f"{parent_key} is nested; name its class or "
                                   f"file to add a definition")
        trailing = 0
        for line in reversed(lines[:at]):
            if line.strip():
                break
            trailing += 1
        block = "".join(nl for _ in range(max(0, gap - trailing)))
        block += "".join((indent + ln if ln.strip() else "") + nl
                         for ln in body.split("\n"))
        if at < len(lines) and lines[at].strip():
            block += nl * gap
        new_text = "".join(lines[:at]) + normalize_newlines(block, nl) + \
            "".join(lines[at:])
        try:
            ast.parse(new_text)
        except SyntaxError as e:
            return {"ok": False, "applied": False, "file": rel,
                    "parse_check": "refused",
                    "error": f"the insertion breaks the parse of {rel} at "
                             f"line {e.lineno}: {e.msg}"}
        diff, cut = self._diff("".join(difflib.unified_diff(
            src.text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}")))
        out: dict[str, Any] = {"ok": True, "applied": False, "file": rel,
                               "parent": parent_key, "insert_after_line": at,
                               "parse_check": "ok", "diff_view": diff,
                               "diff_truncated": cut}
        if not apply:
            return out
        out["backup"] = write_source(self.root, src, new_text,
                                     error_cls=CodeEditError)
        out["new_sha256"] = sha256_bytes(
            (self.root / rel).read_bytes())
        out["applied"] = True
        return self._after_write(out, rel, "create", f"create in {parent_key}")

    @staticmethod
    def _find_def(tree: ast.Module, ref: dict[str, Any]) -> ast.AST | None:
        """The def/class with the key's qualname (the graph's line first)."""
        qual = str(ref.get("name") or "")
        found: list[ast.AST] = []

        def walk(body: list[ast.stmt], prefix: str) -> None:
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                    q = f"{prefix}.{n.name}" if prefix else n.name
                    if q == qual:
                        found.append(n)
                    if isinstance(n, ast.ClassDef):
                        walk(n.body, q)
        walk(tree.body, "")
        found.sort(key=lambda n: n.lineno != ref.get("start"))
        return found[0] if found else None

    # ------------------------------------------------------------------ #
    # check
    # ------------------------------------------------------------------ #
    def run_python(self) -> str:
        """The interpreter `check` runs the target's tests with:
        `$OZAGIA_RUN_PYTHON`, else `check.default_python(root)`
        (`<root>/.venv/bin/python` when it exists, else this interpreter)."""
        from ozagia.check import default_python
        return os.environ.get(RUN_PYTHON_ENV) or default_python(self.root)

    def check(self, keys: list[str] | None = None,
              tests: list[str] | None = None,
              timeout_s: float = 300) -> dict[str, Any]:
        from ozagia.check import _is_test_file, resolve_selectors, run_tests
        selectors: list[str] = []
        ignored: list[dict[str, str]] = []

        def add(sel: str) -> None:
            if sel not in selectors:
                selectors.append(sel)

        for t in tests or []:
            t = str(t).strip()
            if _kind(t) in ("test", "file"):
                res = resolve_selectors(self.root, [t])
                for s in res["selectors"]:
                    add(s)
                ignored += res["ignored"]
                continue
            rel = t.split("::", 1)[0]
            try:
                rel = self._rel(rel)
            except SurfaceError as e:
                ignored.append({"key": t, "reason": str(e)})
                continue
            if not (self.root / rel).is_file():
                ignored.append({"key": t, "reason": f"no such file: {rel}"})
                continue
            add(rel + t[len(t.split("::", 1)[0]):])
        test_files: list[str] = []
        for k in keys or []:
            ref = self._ref(str(k))
            if ref["kind"] == "test" or (ref["kind"] == "file" and ref["file"]
                                         and _is_test_file(ref["file"])):
                res = resolve_selectors(self.root, [ref["key"]])
                for s in res["selectors"]:
                    add(s)
                ignored += res["ignored"]
                continue
            found = self._covering_test_files(ref)
            if not found:
                ignored.append({"key": ref["key"],
                                "reason": "no tests cover it in the graph"})
            for rel in found:
                if rel not in test_files:
                    test_files.append(rel)
        test_files.sort()
        truncated = len(test_files) > MAX_CHECK_TEST_FILES
        for rel in test_files[:MAX_CHECK_TEST_FILES]:
            add(rel)
        if not selectors:
            return {"ok": False, "error": "no tests to run", "ignored": ignored}
        r = run_tests(self.root, selectors, python=self.run_python(),
                      timeout_s=float(timeout_s))
        rows = []
        for t in r["tests"]:
            rows.append(_clean({
                "key": t.get("key"), "param": t.get("param"),
                "outcome": t.get("outcome"), "message": t.get("message"),
                "frames": [f["key"] for f in t.get("frames") or []
                           if f.get("key")] or None}))
        out = {"ok": r["ok"], "status": r["status"], "seconds": r["seconds"],
               "counts": r["counts"], "tests": rows,
               "collection_errors": r["collection_errors"],
               "selectors": selectors, "ignored": ignored,
               "test_files_truncated": truncated}
        if r.get("error"):
            out["error"] = r["error"]
        return out

    def _covering_test_files(self, ref: dict[str, Any]) -> list[str]:
        """Test files with a Test node linked to the key's file."""
        rel = ref["file"]
        if not rel:
            return []
        fid = self.mem.node_by_key("file:" + rel)
        if fid is None:
            return []
        out: set[str] = set()
        for e in self.mem._in.get(fid, []):
            if e["type"] not in _TEST_EDGES:
                continue
            src = int(e["src"])
            if (self.mem.node(src) or {}).get("type") != "Test":
                continue
            f = self._file_of(src)
            if f:
                out.add(f)
        return sorted(out)

    # ------------------------------------------------------------------ #
    # note
    # ------------------------------------------------------------------ #
    def note(self, text: str, keys: list[str] | None = None) -> dict[str, Any]:
        if not str(text or "").strip():
            raise SurfaceError("empty note")
        names = [self._ref(str(k))["key"] for k in keys or []]
        entry = self.mem.journal_append("note", "note", str(text),
                                        keys=names or None)
        return {"ok": True, "entry": entry}


__all__ = [
    "GRAPH_TOOLS",
    "GRAPH_TOOL_NAMES",
    "GraphSurface",
    "MAX_CHECK_TEST_FILES",
    "MAX_DIFF_LINES",
    "MAX_SOURCE_LINES",
    "description_tokens",
]
