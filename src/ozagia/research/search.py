"""Local BM25 retrieval over selected research passages.

SQLite FTS5 is part of the standard library's sqlite3 build on normal Python
installations. When it is unavailable, the existing lexical index is used and
the result says so.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from ozagia.research.sources import build_index, open_locator, retrieve

SCHEMA_VERSION = 1


class SearchError(RuntimeError):
    """The local search index could not be built."""


def _fingerprint(index: Mapping[str, Any]) -> str:
    parts = [
        f"{root.get('root_id')}\0{path}\0{digest}"
        for root in index.get("roots", [])
        for path, digest in sorted((root.get("files") or {}).items())
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _has_fts5(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS __ozagia_fts_probe USING fts5(value)")
        connection.execute("DROP TABLE IF EXISTS __ozagia_fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def build_fts_index(state: Mapping[str, Any], path: str | Path) -> dict[str, Any]:
    """Rebuild the FTS5 index from the currently selected source roots."""
    index = build_index(state.get("roots", []))
    connection = _connect(path)
    try:
        if not _has_fts5(connection):
            return {"available": False, "passages": len(index.get("passages", []))}
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5("
            "passage_id UNINDEXED, root_id UNINDEXED, path UNINDEXED, "
            "heading, text, start_line UNINDEXED, end_line UNINDEXED, "
            "sha256 UNINDEXED, revision UNINDEXED, "
            "tokenize='unicode61 remove_diacritics 2')")
        connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("DELETE FROM passages")
        rows = [
            (
                passage.get("id"),
                passage.get("root_id"),
                passage.get("path"),
                passage.get("heading"),
                passage.get("text"),
                passage.get("start_line"),
                passage.get("end_line"),
                passage.get("sha256"),
                next((root.get("revision") for root in index.get("roots", [])
                      if root.get("root_id") == passage.get("root_id")), None),
            )
            for passage in index.get("passages", [])
        ]
        connection.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('fingerprint', ?)",
            (_fingerprint(index),))
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
            (str(SCHEMA_VERSION),))
        connection.commit()
        return {
            "available": True,
            "passages": len(rows),
            "fingerprint": _fingerprint(index),
        }
    finally:
        connection.close()


def _query_terms(query: str) -> list[str]:
    return [
        term for term in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", query)
        if len(term) > 1
    ]


def search(
    state: Mapping[str, Any],
    query: str,
    *,
    limit: int = 8,
    root_id: str | None = None,
    path: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Search the selected corpus with BM25, falling back to lexical ranking."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 100:
        raise SearchError("limit must be between 0 and 100")
    terms = _query_terms(query)
    if not terms:
        return {
            "query": query,
            "results": [],
            "engine": "none",
            "reason": "query contains no searchable terms",
            "truth_assessed": False,
        }
    index = build_index(state.get("roots", []))
    if db_path is not None:
        try:
            built = build_fts_index(state, db_path)
            if built.get("available"):
                connection = _connect(db_path)
                try:
                    fts_query = " OR ".join(f'"{term}"' for term in terms)
                    clauses = ["passages MATCH ?"]
                    parameters: list[Any] = [fts_query]
                    if root_id is not None:
                        clauses.append("root_id = ?")
                        parameters.append(root_id)
                    if path is not None:
                        clauses.append("path = ?")
                        parameters.append(path)
                    parameters.append(limit)
                    rows = connection.execute(
                        "SELECT passage_id, root_id, path, heading, text, start_line, "
                        "end_line, sha256, revision, bm25(passages) AS rank, "
                        "snippet(passages, 4, '[', ']', '…', 24) "
                        f"FROM passages WHERE {' AND '.join(clauses)} "
                        "ORDER BY rank LIMIT ?",
                        parameters,
                    ).fetchall()
                finally:
                    connection.close()
                results = []
                for row in rows:
                    locator = {
                        "root_id": row[1],
                        "path": row[2],
                        "sha256": row[7],
                        "start_line": row[5],
                        "end_line": row[6],
                        "revision": row[8],
                    }
                    results.append({
                        "score": round(-float(row[9]), 6),
                        "citation": locator,
                        "heading": row[3],
                        "excerpt": row[10],
                        "freshness": open_locator(index, locator)["status"],
                        "support_edges": [],
                    })
                return {
                    "query": query,
                    "results": results,
                    "engine": "sqlite-fts5",
                    "reason": "BM25 matches" if results else "no selected passage matched query terms",
                    "truth_assessed": False,
                }
        except (sqlite3.Error, OSError):
            pass
    fallback = retrieve(index, query, limit=limit)
    fallback["engine"] = "lexical"
    fallback["truth_assessed"] = False
    return fallback
