"""Optional local vector sidecar for research passages.

The default engine is a deterministic hashed bag-of-words projection.  It is a
lexical hashing baseline, not a semantic model, and is always labelled
``hash-bow``.  A local ``sentence-transformers`` model can be named explicitly
or with ``OZAGIA_EMBED_MODEL``; callers may also inject their own embedder.
Vectors live in a small SQLite sidecar, so there is no vector database and no
external service.  Retrieval never claims that a passage supports a claim.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
from typing import Any

from ozagia.research.graph import ResearchError
from ozagia.research.search import search as fts_search
from ozagia.research.sources import build_index

SCHEMA_VERSION = 1
HASH_BOW = "hash-bow"
SENTENCE_TRANSFORMERS = "sentence-transformers"
INJECTED = "injected"
HYBRID_ENGINE = "hybrid-rrf"
RRF_K = 60
EXCERPT_CHARS = 4000

Embedder = Callable[[list[str]], list[list[float]]]
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


class SemanticError(ResearchError):
    """The optional vector index is missing, invalid, or cannot embed text."""


def content_fingerprint(index: Mapping[str, Any]) -> str:
    """Hash the selected roots and file digests, independently of embeddings."""
    parts = [
        f"{root.get('root_id')}\0{path}\0{digest}"
        for root in index.get("roots", [])
        for path, digest in sorted((root.get("files") or {}).items())
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _validate_dim(dim: int) -> None:
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise SemanticError("dim must be a positive integer")


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
        raise SemanticError("limit must be between 0 and 100")


def _validate_alpha(alpha: float) -> None:
    if (
        isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not math.isfinite(alpha)
        or not 0.0 <= alpha <= 1.0
    ):
        raise SemanticError("alpha must be a number between 0 and 1")


def _query_text(query: str) -> str:
    if not isinstance(query, str):
        raise SemanticError("query must be a string")
    return query


def _resolve_model_name(model: str | None, *, fallback: str) -> str:
    """Resolve an explicit model, then the environment opt-in, then the fallback."""
    if model is None:
        configured = os.environ.get("OZAGIA_EMBED_MODEL", "").strip()
        return configured or fallback
    if not isinstance(model, str) or not model.strip():
        raise SemanticError("model must be a non-empty name")
    return model.strip()


def hash_bow_embedder(dim: int = 512) -> Embedder:
    """Return a deterministic hashed bag-of-words embedder.

    Tokens are hashed into ``dim`` buckets, term frequencies are made
    sublinear, and each vector is L2-normalized.  This is a lexical baseline,
    not a learned semantic model.
    """
    _validate_dim(dim)

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            counts: dict[int, int] = {}
            for token in _TOKEN_RE.findall(text):
                digest = hashlib.blake2b(
                    token.casefold().encode("utf-8"), digest_size=8
                ).digest()
                bucket = int.from_bytes(digest, "big") % dim
                counts[bucket] = counts.get(bucket, 0) + 1
            vector = [0.0] * dim
            for bucket, count in counts.items():
                vector[bucket] = 1.0 + math.log(count)
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors

    return embed


def _sentence_transformer_embedder(model_name: str) -> Embedder:
    """Load a local sentence-transformers model without downloading anything."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise SemanticError(
            "model %r requires the optional sentence-transformers package; "
            "install it locally, inject embedder=..., or use model='hash-bow'"
            % model_name
        ) from exc
    try:
        encoder = SentenceTransformer(model_name, local_files_only=True)
    except TypeError as exc:
        raise SemanticError(
            "the installed sentence-transformers cannot load %r offline "
            "(local_files_only is unsupported); upgrade it, inject embedder=..., "
            "or use model='hash-bow'" % model_name
        ) from exc
    except Exception as exc:
        raise SemanticError(
            "cannot load local sentence-transformers model %r offline: %s; "
            "download it first, inject embedder=..., or use model='hash-bow'"
            % (model_name, exc)
        ) from exc

    def embed(texts: list[str]) -> list[list[float]]:
        try:
            encoded = encoder.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except TypeError:
            # Older or minimal model wrappers may not accept these keyword args.
            try:
                encoded = encoder.encode(texts)
            except Exception as exc:
                raise SemanticError(
                    "sentence-transformers model %r failed to embed text: %s"
                    % (model_name, exc)
                ) from exc
        except Exception as exc:
            raise SemanticError(
                "sentence-transformers model %r failed to embed text: %s"
                % (model_name, exc)
            ) from exc
        try:
            return [[float(value) for value in vector] for vector in encoded]
        except (TypeError, ValueError) as exc:
            raise SemanticError(
                "sentence-transformers model %r returned malformed vectors" % model_name
            ) from exc

    return embed


def _build_embedder(
    embedder: Embedder | None, model: str | None, dim: int
) -> tuple[Embedder, str, str]:
    if embedder is not None:
        if not callable(embedder):
            raise SemanticError("embedder must be callable")
        if model is None:
            label = INJECTED
        elif not isinstance(model, str) or not model.strip():
            raise SemanticError("model must be a non-empty name")
        else:
            label = model.strip()
        if label == HASH_BOW:
            label = INJECTED
        return embedder, INJECTED, label
    name = _resolve_model_name(model, fallback=HASH_BOW)
    if name == HASH_BOW:
        return hash_bow_embedder(dim), HASH_BOW, HASH_BOW
    return _sentence_transformer_embedder(name), SENTENCE_TRANSFORMERS, name


def _call_embedder(embedder: Embedder, texts: list[str]) -> Any:
    try:
        return embedder(texts)
    except SemanticError:
        raise
    except Exception as exc:
        raise SemanticError(f"embedder failed: {exc}") from exc


def _as_vectors(
    raw: Any, count: int, *, dim: int | None = None
) -> tuple[list[list[float]], int]:
    """Validate embedder output and infer or verify its dimension."""
    try:
        rows = list(raw)
    except TypeError as exc:
        raise SemanticError("embedder must return a list of vectors") from exc
    if len(rows) != count:
        raise SemanticError(
            f"embedder returned {len(rows)} vectors for {count} texts"
        )
    if not rows:
        if dim is None or dim <= 0:
            raise SemanticError("cannot determine embedding dimension for an empty corpus")
        return [], dim
    actual = dim
    vectors: list[list[float]] = []
    for position, row in enumerate(rows):
        if isinstance(row, (str, bytes)):
            raise SemanticError(f"embedder vector {position} is not a numeric sequence")
        try:
            vector = [float(value) for value in row]
        except (TypeError, ValueError) as exc:
            raise SemanticError(
                f"embedder vector {position} is not a numeric sequence"
            ) from exc
        if actual is None:
            actual = len(vector)
        if not vector or len(vector) != actual:
            raise SemanticError(
                "embedder returned inconsistent vector dimensions "
                f"(expected {actual}, got {len(vector)} at position {position})"
            )
        if any(not math.isfinite(value) for value in vector):
            raise SemanticError(
                f"embedder vector {position} contains a non-finite value"
            )
        vectors.append(vector)
    if actual is None or actual <= 0:
        raise SemanticError("embedder returned zero-dimensional vectors")
    return vectors, actual


def _vector_blob(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _vector_from_blob(blob: Any, dim: int) -> list[float]:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise SemanticError("semantic index has a malformed vector blob")
    data = bytes(blob)
    expected = 4 * dim
    if len(data) != expected:
        raise SemanticError(
            f"semantic index vector has {len(data)} bytes; expected {expected} for dim={dim}"
        )
    try:
        return list(struct.unpack(f"<{dim}f", data))
    except (struct.error, ValueError) as exc:
        raise SemanticError("semantic index has a malformed vector blob") from exc


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise SemanticError(
            f"cannot compare vectors of different dimensions ({len(left)} and {len(right)})"
        )
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _connect_write(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        raise SemanticError(f"cannot open semantic index for writing: {exc}") from exc
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _connect_read(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SemanticError(
            f"semantic index not found: {path}; call build_semantic_index(...) first"
        )
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise SemanticError(f"cannot open semantic index {path}: {exc}") from exc
    return connection


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM semantic_meta").fetchall()
    except sqlite3.Error as exc:
        raise SemanticError(
            "file is not a semantic index; build it with build_semantic_index(...)"
        ) from exc
    meta = {str(key): str(value) for key, value in rows}
    required = {"schema", "fingerprint", "engine", "model", "dim", "passages"}
    missing = sorted(required - set(meta))
    if missing:
        raise SemanticError(
            "semantic index is missing metadata: " + ", ".join(missing)
        )
    try:
        schema = int(meta["schema"])
        dim = int(meta["dim"])
    except ValueError as exc:
        raise SemanticError("semantic index has malformed schema or dimension") from exc
    if schema != SCHEMA_VERSION:
        raise SemanticError(
            f"unsupported semantic index schema {schema}; expected {SCHEMA_VERSION}"
        )
    if dim <= 0:
        raise SemanticError("semantic index has an invalid dimension")
    return meta


def _revisions(index: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        str(root.get("root_id")): root.get("revision")
        for root in index.get("roots", [])
    }


def build_semantic_index(
    state: Mapping[str, Any],
    path: str | Path,
    *,
    embedder: Embedder | None = None,
    model: str | None = "hash-bow",
    dim: int = 512,
) -> dict[str, Any]:
    """Rebuild the SQLite vector sidecar from the selected source roots.

    ``dim`` sizes the default hash-bow projection.  Injected embedders and
    sentence-transformers models determine their own dimension, which is
    recorded in the sidecar.  Pass ``model=None`` to opt into a model named by
    ``OZAGIA_EMBED_MODEL``; the literal default stays deterministic.
    """
    if not isinstance(state, Mapping):
        raise SemanticError("state must be a mapping containing a roots sequence")
    roots = state.get("roots") or []
    if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)):
        raise SemanticError("state['roots'] must be a sequence of root manifests")
    _validate_dim(dim)
    index = build_index(roots)
    passages = list(index.get("passages", []))
    texts = [str(passage.get("text") or "") for passage in passages]
    embed, engine, model_label = _build_embedder(embedder, model, dim)
    if texts:
        raw = _call_embedder(embed, texts)
        vectors, actual_dim = _as_vectors(
            raw,
            len(texts),
            dim=dim if engine == HASH_BOW else None,
        )
    else:
        vectors, actual_dim = [], dim
    revisions = _revisions(index)
    rows = []
    for passage, vector in zip(passages, vectors):
        rows.append(
            (
                passage.get("id"),
                passage.get("root_id"),
                passage.get("path"),
                passage.get("heading"),
                passage.get("text"),
                int(passage.get("start_line", 1)),
                int(passage.get("end_line", 1)),
                passage.get("sha256"),
                revisions.get(str(passage.get("root_id"))),
                model_label,
                actual_dim,
                _vector_blob(vector),
            )
        )
    fingerprint = content_fingerprint(index)
    path = Path(path).expanduser()
    connection = _connect_write(path)
    try:
        with connection:
            connection.execute("DROP TABLE IF EXISTS semantic_passages")
            connection.execute("DROP TABLE IF EXISTS semantic_meta")
            connection.execute(
                "CREATE TABLE semantic_passages ("
                "passage_id TEXT PRIMARY KEY, root_id TEXT, path TEXT, heading TEXT, "
                "text TEXT, start_line INTEGER, end_line INTEGER, sha256 TEXT, "
                "revision TEXT, model TEXT, dim INTEGER, vector BLOB)"
            )
            connection.execute(
                "CREATE INDEX semantic_passages_path ON semantic_passages(path)"
            )
            connection.execute(
                "CREATE TABLE semantic_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            connection.executemany(
                "INSERT INTO semantic_passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.executemany(
                "INSERT INTO semantic_meta(key, value) VALUES (?, ?)",
                [
                    ("schema", str(SCHEMA_VERSION)),
                    ("fingerprint", fingerprint),
                    ("engine", engine),
                    ("model", model_label),
                    ("dim", str(actual_dim)),
                    ("passages", str(len(rows))),
                ],
            )
    except sqlite3.Error as exc:
        raise SemanticError(f"cannot write semantic index: {exc}") from exc
    finally:
        connection.close()
    result: dict[str, Any] = {
        "available": True,
        "engine": engine,
        "model": model_label,
        "dim": actual_dim,
        "passages": len(rows),
        "fingerprint": fingerprint,
        "schema": SCHEMA_VERSION,
        "db_path": str(path),
        "truth_assessed": False,
    }
    if engine == HASH_BOW:
        result["note"] = (
            "hash-bow is a deterministic lexical hashing baseline, not a semantic model"
        )
    return result


def _query_embedder(
    engine: str,
    model_label: str,
    dim: int,
    embedder: Embedder | None,
    model: str | None,
) -> Embedder:
    if embedder is not None:
        if not callable(embedder):
            raise SemanticError("embedder must be callable")
        if model is not None and str(model) != model_label:
            raise SemanticError(
                f"index was built with model {model_label!r}; use that model or rebuild"
            )
        return embedder
    if engine == HASH_BOW:
        if model is not None and str(model) != HASH_BOW:
            raise SemanticError(
                f"index was built with hash-bow; rebuild with model={model!r} "
                "to query a different vector space"
            )
        return hash_bow_embedder(dim)
    if engine == SENTENCE_TRANSFORMERS:
        if model is not None and str(model) != model_label:
            raise SemanticError(
                f"index was built with model {model_label!r}; rebuild with "
                f"model={model!r} to query a different vector space"
            )
        return _sentence_transformer_embedder(model_label)
    if engine == INJECTED:
        raise SemanticError(
            "this index was built with an injected embedder; pass embedder=... "
            "to query it"
        )
    raise SemanticError(f"unknown semantic index engine: {engine!r}")


def _semantic_payload(
    query: str,
    results: list[dict[str, Any]],
    *,
    engine: str,
    model: str,
    dim: int,
    fingerprint: str,
    alpha: float,
    reason: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query,
        "results": results,
        "engine": engine,
        "model": model,
        "dim": dim,
        "fingerprint": fingerprint,
        "alpha": alpha,
        "reason": reason,
        "truth_assessed": False,
    }
    if engine == HASH_BOW:
        payload["note"] = (
            "hash-bow is a deterministic lexical hashing baseline, not a semantic model"
        )
    return payload


def _citation(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "root_id": row[1],
        "path": row[2],
        "heading": row[3],
        "start_line": row[5],
        "end_line": row[6],
        "sha256": row[7],
        "revision": row[8],
    }


def _semantic_result(score: float, row: Sequence[Any]) -> dict[str, Any]:
    text = str(row[4] or "")
    return {
        "score": round(score, 6),
        "citation": _citation(row),
        "heading": row[3],
        "excerpt": text[:EXCERPT_CHARS],
        "excerpt_truncated": len(text) > EXCERPT_CHARS,
        "passage_id": row[0],
        "support_edges": [],
    }


def semantic_search(
    state: Mapping[str, Any],
    query: str,
    *,
    limit: int = 8,
    path: str | None = None,
    db_path: str | Path | None = None,
    embedder: Embedder | None = None,
    model: str | None = None,
    alpha: float = 0.5,
) -> dict[str, Any]:
    """Rank passages by cosine similarity in the SQLite sidecar.

    ``alpha`` is validated for interface parity with :func:`hybrid_search` but
    does not change this pure-vector ranking.
    """
    _validate_limit(limit)
    _validate_alpha(alpha)
    query = _query_text(query)
    if db_path is None:
        raise SemanticError(
            "semantic_search requires db_path; call build_semantic_index(...) first"
        )
    if path is not None and not isinstance(path, str):
        raise SemanticError("path must be a string")
    connection = _connect_read(Path(db_path).expanduser())
    try:
        meta = _read_meta(connection)
        engine, model_label = meta["engine"], meta["model"]
        dim = int(meta["dim"])
        if not query.strip():
            return _semantic_payload(
                query,
                [],
                engine=engine,
                model=model_label,
                dim=dim,
                fingerprint=meta["fingerprint"],
                alpha=alpha,
                reason="query is empty",
            )
        embed = _query_embedder(engine, model_label, dim, embedder, model)
        raw = _call_embedder(embed, [query])
        query_vectors, query_dim = _as_vectors(raw, 1)
        if query_dim != dim:
            raise SemanticError(
                f"query embedder returned dimension {query_dim} but the index "
                f"was built with {dim}; rebuild the index or use the matching embedder"
            )
        query_vector = query_vectors[0]
        clauses = []
        parameters: list[Any] = []
        if path is not None:
            clauses.append("path = ?")
            parameters.append(path)
        sql = (
            "SELECT passage_id, root_id, path, heading, text, start_line, "
            "end_line, sha256, revision, dim, vector FROM semantic_passages"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        try:
            rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise SemanticError(f"cannot read semantic index: {exc}") from exc
    finally:
        connection.close()
    hits: list[tuple[float, Sequence[Any]]] = []
    for row in rows:
        row_dim = int(row[9])
        if row_dim != dim:
            raise SemanticError(
                f"semantic index row {row[0]!r} has dim={row_dim}; expected {dim}"
            )
        score = _cosine(query_vector, _vector_from_blob(row[10], row_dim))
        if score > 0.0:
            hits.append((score, row))
    hits.sort(key=lambda item: (-item[0], str(item[1][2]), int(item[1][5]), str(item[1][0])))
    results = [_semantic_result(score, row) for score, row in hits[:limit]]
    return _semantic_payload(
        query,
        results,
        engine=engine,
        model=model_label,
        dim=dim,
        fingerprint=meta["fingerprint"],
        alpha=alpha,
        reason=(
            "vector similarity matches"
            if results
            else "no indexed passage had positive vector similarity to the query"
        ),
    )


def _citation_key(citation: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (
        citation.get("root_id"),
        citation.get("path"),
        citation.get("start_line"),
        citation.get("end_line"),
    )


def _merge_hits(
    semantic: Mapping[str, Any],
    fts: Mapping[str, Any],
    *,
    alpha: float,
    limit: int,
) -> list[dict[str, Any]]:
    fused: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
    for source, payload, weight in (
        ("semantic", semantic, alpha),
        ("fts", fts, 1.0 - alpha),
    ):
        for rank, item in enumerate(payload.get("results", []), start=1):
            citation = item.get("citation")
            if not isinstance(citation, Mapping):
                continue
            key = _citation_key(citation)
            entry = fused.get(key)
            if entry is None:
                entry = {
                    "citation": dict(citation),
                    "heading": item.get("heading"),
                    "excerpt": item.get("excerpt", ""),
                    "excerpt_truncated": item.get("excerpt_truncated", False),
                    "passage_id": item.get("passage_id"),
                    "support_edges": list(item.get("support_edges") or []),
                    "freshness": item.get("freshness"),
                    "sources": [],
                    "score": 0.0,
                }
                fused[key] = entry
            else:
                if entry.get("passage_id") is None and item.get("passage_id") is not None:
                    entry["passage_id"] = item["passage_id"]
                if entry.get("heading") is None and item.get("heading") is not None:
                    entry["heading"] = item["heading"]
                if not entry.get("excerpt") and item.get("excerpt"):
                    entry["excerpt"] = item["excerpt"]
                if entry.get("freshness") is None and item.get("freshness") is not None:
                    entry["freshness"] = item["freshness"]
                if not entry.get("support_edges") and item.get("support_edges"):
                    entry["support_edges"] = list(item["support_edges"])
            entry["sources"].append(source)
            entry[f"{source}_rank"] = rank
            entry[f"{source}_score"] = item.get("score")
            entry["score"] += weight / (RRF_K + rank)
    ordered = sorted(
        (entry for entry in fused.values() if entry["score"] > 0.0),
        key=lambda entry: (
            -entry["score"],
            str(entry["citation"].get("path")),
            int(entry["citation"].get("start_line") or 0),
            str(entry.get("passage_id") or ""),
        ),
    )
    for entry in ordered:
        entry["score"] = round(entry["score"], 8)
    return ordered[:limit]


def hybrid_search(
    state: Mapping[str, Any],
    query: str,
    *,
    limit: int = 8,
    db_path: str | Path | None = None,
    embedder: Embedder | None = None,
    model: str | None = None,
    alpha: float = 0.5,
) -> dict[str, Any]:
    """Fuse vector and FTS5 rankings with weighted reciprocal-rank fusion.

    ``alpha`` weights the vector ranking; ``1 - alpha`` weights FTS5.  When
    ``db_path`` is omitted, a temporary sidecar is built and discarded.
    """
    _validate_limit(limit)
    _validate_alpha(alpha)
    query = _query_text(query)
    ephemeral = db_path is None
    if ephemeral:
        with tempfile.TemporaryDirectory(prefix="ozagia-semantic-") as directory:
            temporary = Path(directory) / "semantic.sqlite"
            build_semantic_index(state, temporary, embedder=embedder, model=model)
            return _hybrid_payload(
                state,
                query,
                limit=limit,
                db_path=temporary,
                embedder=embedder,
                model=model,
                alpha=alpha,
                ephemeral=True,
            )
    database = Path(db_path).expanduser()
    if not database.is_file():
        raise SemanticError(
            f"semantic index not found: {database}; call build_semantic_index(...) "
            "or omit db_path for an ephemeral hybrid index"
        )
    return _hybrid_payload(
        state,
        query,
        limit=limit,
        db_path=database,
        embedder=embedder,
        model=model,
        alpha=alpha,
        ephemeral=False,
    )


def _hybrid_payload(
    state: Mapping[str, Any],
    query: str,
    *,
    limit: int,
    db_path: Path,
    embedder: Embedder | None,
    model: str | None,
    alpha: float,
    ephemeral: bool,
) -> dict[str, Any]:
    fts = fts_search(state, query, limit=limit, db_path=db_path)
    semantic = semantic_search(
        state,
        query,
        limit=limit,
        db_path=db_path,
        embedder=embedder,
        model=model,
        alpha=alpha,
    )
    results = _merge_hits(semantic, fts, alpha=alpha, limit=limit)
    return {
        "query": query,
        "results": results,
        "engine": HYBRID_ENGINE,
        "model": semantic["model"],
        "semantic_engine": semantic["engine"],
        "fts_engine": fts.get("engine"),
        "fusion": {"method": "reciprocal-rank", "k": RRF_K, "alpha": alpha},
        "fingerprint": semantic.get("fingerprint"),
        "db_path": None if ephemeral else str(db_path),
        "ephemeral_index": ephemeral,
        "reason": (
            "vector and FTS5 rankings fused"
            if results
            else "no passage matched the query in either ranking"
        ),
        "truth_assessed": False,
    }
