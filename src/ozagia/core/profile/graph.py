"""Join profile frames to the repository graph.

The join is deliberately syntactic and read-only.  A frame is mapped to the
innermost definition whose line range contains it.  When a frame has no line,
an exact qualname or a unique name can still map it.  Everything else is
reported as external, ambiguous or unresolved; no frame is silently assigned
to a symbol.

Callers, callees and impact come from :mod:`ozagia.core.symbol_graph`.  Its
reference resolution is syntactic and currently resolves Python references;
non-Python definitions are still mapped by file and line.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import bounded_int, module_for

MAX_IMPACT_NODES = 5_000
MAX_IMPACT_FILES = 20


class GraphIndex:
    """A tag-cache and symbol-graph view used to enrich profile frames."""

    def __init__(self, root: Path, *, tags: Mapping[str, Any] | None = None,
                 source: str = "injected", error: str | None = None,
                 tag_stats: Mapping[str, Any] | None = None) -> None:
        self.root = Path(root).resolve()
        self.source = source
        self.error = error
        self.tag_stats = dict(tag_stats or {})
        self.tags = dict(tags or {})
        self.defs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._graph: dict[str, Any] | None = None
        self._graph_error: str | None = None
        self._forward: dict[str, set[str]] | None = None
        self._reverse: dict[str, set[str]] | None = None
        self._stats_memo: dict[str, dict[str, Any]] = {}
        self._build_defs()

    @classmethod
    def open(cls, root: str | Path, *, tags: Mapping[str, Any] | None = None,
             refresh: bool = True) -> "GraphIndex":
        root = Path(root).resolve()
        if tags is not None:
            return cls(root, tags=tags, source="injected")
        try:
            from ozagia.config import state_path
            from ozagia.core.repo_map import _load_cache, update_tag_cache
            cache = state_path(root, "tags.json")
            if not refresh:
                # A non-refresh read must not rewrite an existing tag cache.
                # _load_cache validates the cache version and returns the same
                # document the update path would reuse. A missing/empty cache
                # is built once under the state dir so a first profile still
                # gets a graph join.
                loaded = _load_cache(Path(cache))
                if loaded.get("files"):
                    return cls(root, tags=loaded, source="tag_cache")
            loaded, stats = update_tag_cache(root, cache_path=cache)
            return cls(root, tags=loaded, source="tag_cache",
                       tag_stats=stats)
        except Exception as exc:  # a profile report still works without a graph
            return cls(root, source="unavailable",
                       error=f"tag cache unavailable: {type(exc).__name__}: {exc}")

    def _build_defs(self) -> None:
        files = self.tags.get("files")
        if not isinstance(files, Mapping):
            return
        for rel, entry in files.items():
            if not isinstance(rel, str) or not isinstance(entry, Mapping):
                continue
            for tag in entry.get("tags") or []:
                if not isinstance(tag, Mapping) or tag.get("kind") != "def":
                    continue
                name = tag.get("name")
                if not isinstance(name, str) or not name:
                    continue
                qualname = tag.get("qualname") or name
                if not isinstance(qualname, str) or not qualname:
                    qualname = name
                line = bounded_int(tag.get("line"), name="definition line",
                                   low=1, default=1)
                end = bounded_int(tag.get("end_line"), name="definition end",
                                  low=line or 1, default=line or 1)
                self.defs[rel].append({
                    "file": rel,
                    "name": name,
                    "qualname": qualname,
                    "line": line or 1,
                    "end_line": end or line or 1,
                    "detail": str(tag.get("detail") or ""),
                    "module": module_for(rel),
                })
        for entries in self.defs.values():
            entries.sort(key=lambda item: (item["line"], -item["end_line"],
                                           item["qualname"]))

    @property
    def graph(self) -> dict[str, Any] | None:
        if self._graph is not None or self._graph_error is not None:
            return self._graph
        if not self.tags:
            self._graph_error = "no tag cache"
            return None
        try:
            from ozagia.core.symbol_graph import build_symbol_graph
            self._graph = build_symbol_graph(self.tags)
        except Exception as exc:  # mapping by line still works
            self._graph_error = f"{type(exc).__name__}: {exc}"
        return self._graph

    def _adjacency(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        if self._forward is not None and self._reverse is not None:
            return self._forward, self._reverse
        forward: dict[str, set[str]] = defaultdict(set)
        reverse: dict[str, set[str]] = defaultdict(set)
        graph = self.graph
        if graph is not None:
            for edge in graph.get("edges") or []:
                if not isinstance(edge, Mapping):
                    continue
                src, tgt = edge.get("src"), edge.get("tgt")
                if isinstance(src, str) and isinstance(tgt, str):
                    forward[src].add(tgt)
                    reverse[tgt].add(src)
        self._forward, self._reverse = forward, reverse
        return forward, reverse

    def _definition(self, file: str, line: int | None) -> dict[str, Any] | None:
        entries = self.defs.get(file)
        if not entries:
            return None
        if line is None:
            return None
        candidates = [item for item in entries
                      if item["line"] <= line <= item["end_line"]]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item["line"], item["end_line"],
                                          item["qualname"]))
        return candidates[0]

    def _by_name(self, name: str, *,
                 file: str | None = None) -> tuple[dict[str, Any] | None, int, str]:
        graph = self.graph
        if graph is None:
            return None, 0, "symbol graph unavailable"
        try:
            from ozagia.core.symbol_graph import resolve_symbol
            matches = resolve_symbol(graph, name)
        except Exception:
            return None, 0, "symbol resolution failed"
        if not matches:
            return None, 0, "no matching definition"
        if file:
            same_file = [sid for sid in matches
                         if (graph.get("nodes") or {}).get(sid, {}).get("file") == file]
            if same_file:
                matches = same_file
        if len(matches) > 1:
            return None, len(matches), "ambiguous name"
        node = (graph.get("nodes") or {}).get(matches[0])
        if not isinstance(node, Mapping):
            return None, 0, "definition is not in the symbol graph"
        definition = dict(node)
        definition.setdefault("name", name.rsplit(".", 1)[-1])
        definition.setdefault("qualname", name)
        definition.setdefault("module", module_for(str(definition.get("file"))))
        return definition, 1, ""

    def resolve(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        """Map one frame to a graph definition with an explicit status."""
        if not self.tags:
            return {"status": "unavailable", "reason": self.error or "no tag cache"}
        file = frame.get("file")
        name = frame.get("name")
        line = frame.get("line")
        if frame.get("external"):
            return {"status": "external",
                    "reason": "frame path is outside the repository"}
        definition = None
        candidates = 0
        reason = ""
        if isinstance(file, str):
            definition = self._definition(file, line if isinstance(line, int) else None)
        if definition is None and isinstance(name, str) and name:
            definition, candidates, reason = self._by_name(
                name, file=file if isinstance(file, str) else None)
            if (definition is not None and isinstance(file, str)
                    and definition.get("file") != file):
                candidates = max(candidates, 1)
                definition = None
                reason = "name matches only a definition in another file"
        if definition is None:
            if candidates > 1:
                return {"status": "ambiguous", "candidates": candidates,
                        "reason": reason or "ambiguous name"}
            return {"status": "unresolved",
                    "reason": reason or ("no definition covers the frame"
                                         if file else "frame has no file")}
        rel = definition.get("file")
        qualname = definition.get("qualname") or definition.get("name") or name
        if not isinstance(rel, str):
            return {"status": "unresolved", "reason": "definition has no file"}
        symbol = f"{rel}::{qualname}"
        return {
            "status": "mapped",
            "symbol": symbol,
            "qualname": qualname,
            "name": definition.get("name"),
            "file": rel,
            "line": definition.get("line"),
            "end_line": definition.get("end_line"),
            "detail": definition.get("detail") or definition.get("kind_detail"),
            "resolution": "line" if isinstance(file, str) and candidates == 0 else "name",
        }

    def symbol_stats(self, symbol: str, *, depth: int = 2) -> dict[str, Any]:
        """Direct callers/callees and a bounded transitive caller impact."""
        cached = self._stats_memo.get(symbol)
        if cached is not None:
            return cached
        forward, reverse = self._adjacency()
        direct_callers = sorted(reverse.get(symbol, ()))
        direct_callees = sorted(forward.get(symbol, ()))
        seen = {symbol}
        frontier = [symbol]
        files: set[str] = set()
        symbols = 0
        for _ in range(max(0, depth)):
            nxt: list[str] = []
            for node in frontier:
                for caller in reverse.get(node, ()):
                    if caller in seen:
                        continue
                    seen.add(caller)
                    nxt.append(caller)
                    symbols += 1
                    if "::" in caller:
                        files.add(caller.split("::", 1)[0])
                    if len(seen) >= MAX_IMPACT_NODES:
                        break
                if len(seen) >= MAX_IMPACT_NODES:
                    break
            frontier = nxt
            if len(seen) >= MAX_IMPACT_NODES:
                break
        result = {
            "callers": len(direct_callers),
            "callees": len(direct_callees),
            "impact_symbols": symbols,
            "impact_files": sorted(files)[:MAX_IMPACT_FILES],
            "impact_truncated": len(seen) >= MAX_IMPACT_NODES,
            "depth": depth,
        }
        if len(self._stats_memo) < 10_000:
            self._stats_memo[symbol] = result
        return result

    def impact_files(self, symbols: Iterable[str], *, depth: int = 2) -> list[str]:
        files: set[str] = set()
        for symbol in symbols:
            stats = self.symbol_stats(symbol, depth=depth)
            files.update(stats.get("impact_files") or [])
        return sorted(files)

    def summary(self) -> dict[str, Any]:
        graph = self.graph
        nodes = len((graph or {}).get("nodes") or {})
        edges = len((graph or {}).get("edges") or [])
        return {
            "source": self.source,
            "root": str(self.root),
            "root_hash": self.tags.get("root_hash"),
            "updated_at": self.tags.get("updated_at"),
            "files": len(self.tags.get("files") or {}) if self.tags else 0,
            "definitions": sum(len(items) for items in self.defs.values()),
            "symbols": nodes,
            "symbol_edges": edges,
            "error": self.error or self._graph_error,
            "syntax_only": True,
            "note": "caller/impact edges resolve Python references syntactically; "
                    "non-Python frames are mapped by file and line",
        }


__all__ = ["GraphIndex"]
