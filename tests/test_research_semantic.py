from __future__ import annotations

from contextlib import closing
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ozagia.research.semantic import (
    SemanticError,
    build_semantic_index,
    hybrid_search,
    semantic_search,
)


@pytest.fixture(autouse=True)
def _no_embed_model(monkeypatch):
    monkeypatch.delenv("OZAGIA_EMBED_MODEL", raising=False)


def _write_repo(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "repo"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text(
        "# Alpha\n\n"
        "Alpha algorithms align arbitrary arrays.\n\n"
        "Beta balancing belongs beside bounded buffers.\n",
        encoding="utf-8",
    )
    (docs / "b.md").write_text(
        "# Gamma\n\n"
        "Gamma graphs guarantee gradual growth.\n\n"
        "Delta dynamics describe distributed drift.\n",
        encoding="utf-8",
    )
    state = {
        "roots": [
            {
                "root_id": "notes",
                "path": str(root),
                "files": ["docs/a.md", "docs/b.md"],
                "repository": "notes",
            }
        ]
    }
    return root, state


def test_hash_bow_index_and_query_are_deterministic(tmp_path):
    _, state = _write_repo(tmp_path)
    one = tmp_path / "one.sqlite"
    two = tmp_path / "two.sqlite"

    first = build_semantic_index(state, one)
    second = build_semantic_index(state, two)

    assert first["engine"] == "hash-bow"
    assert first["model"] == "hash-bow"
    assert first["dim"] == 512
    assert first["passages"] == 2
    assert first["fingerprint"] == second["fingerprint"]
    assert first["passages"] == second["passages"]

    with closing(sqlite3.connect(one)) as connection:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(semantic_passages)")
        ]
        meta = dict(connection.execute("SELECT key, value FROM semantic_meta"))
        dim, vector = connection.execute(
            "SELECT dim, vector FROM semantic_passages LIMIT 1"
        ).fetchone()
    for column in (
        "passage_id",
        "root_id",
        "path",
        "heading",
        "text",
        "start_line",
        "end_line",
        "sha256",
        "revision",
        "model",
        "dim",
        "vector",
    ):
        assert column in columns
    assert meta["engine"] == "hash-bow"
    assert meta["model"] == "hash-bow"
    assert meta["fingerprint"] == first["fingerprint"]
    assert dim == 512
    assert len(vector) == 512 * 4

    query_one = semantic_search(state, "alpha algorithms", limit=3, db_path=one)
    query_two = semantic_search(state, "alpha algorithms", limit=3, db_path=one)

    assert query_one == query_two
    assert query_one["engine"] == "hash-bow"
    assert query_one["model"] == "hash-bow"
    assert query_one["truth_assessed"] is False
    assert query_one["results"]
    top = query_one["results"][0]
    assert top["score"] > 0
    assert top["citation"]["path"] == "docs/a.md"
    assert "alpha" in top["excerpt"].casefold()


def test_content_fingerprint_changes_when_source_changes(tmp_path):
    root, state = _write_repo(tmp_path)
    database = tmp_path / "semantic.sqlite"

    before = build_semantic_index(state, database)
    (root / "docs" / "a.md").write_text("# Alpha\n\nRewritten content.\n", encoding="utf-8")
    after = build_semantic_index(state, database)

    assert before["fingerprint"] != after["fingerprint"]


def test_injected_embedder_builds_and_queries(tmp_path):
    _, state = _write_repo(tmp_path)
    seen: list[list[str]] = []

    def embed(texts: list[str]) -> list[list[float]]:
        seen.append(list(texts))
        vectors = []
        for text in texts:
            folded = text.casefold()
            vectors.append(
                [
                    float(folded.count("alpha")),
                    float(folded.count("gamma")),
                    float(folded.count("beta")),
                ]
            )
        return vectors

    database = tmp_path / "injected.sqlite"
    built = build_semantic_index(
        state, database, embedder=embed, model="test-embedder"
    )

    assert built["engine"] == "injected"
    assert built["model"] == "test-embedder"
    assert built["dim"] == 3

    result = semantic_search(
        state,
        "gamma",
        db_path=database,
        embedder=embed,
        model="test-embedder",
    )

    assert result["engine"] == "injected"
    assert result["model"] == "test-embedder"
    assert result["results"]
    assert result["results"][0]["citation"]["path"] == "docs/b.md"
    assert seen[-1] == ["gamma"]

    with pytest.raises(SemanticError, match="injected embedder"):
        semantic_search(state, "gamma", db_path=database)


def test_hybrid_fusion_deduplicates_and_keeps_citations(tmp_path):
    _, state = _write_repo(tmp_path)
    database = tmp_path / "hybrid.sqlite"
    build_semantic_index(state, database)

    result = hybrid_search(state, "alpha algorithms", limit=5, db_path=database)

    assert result["engine"] == "hybrid-rrf"
    assert result["truth_assessed"] is False
    assert result["fusion"] == {"method": "reciprocal-rank", "k": 60, "alpha": 0.5}
    assert result["results"]
    keys = [
        (
            item["citation"]["root_id"],
            item["citation"]["path"],
            item["citation"]["start_line"],
            item["citation"]["end_line"],
        )
        for item in result["results"]
    ]
    assert len(keys) == len(set(keys))
    top = result["results"][0]
    assert top["citation"]["path"] == "docs/a.md"
    assert top["citation"]["sha256"]
    assert "alpha" in top["excerpt"].casefold()
    assert set(top["sources"]) == {"semantic", "fts"}
    assert top["semantic_rank"] == 1
    assert top["fts_rank"] == 1
    assert top["score"] > 0


def test_hybrid_keeps_fts_only_citations(tmp_path):
    _, state = _write_repo(tmp_path)
    partial = {"roots": [dict(state["roots"][0], files=["docs/a.md"])]}
    database = tmp_path / "partial.sqlite"
    build_semantic_index(partial, database)

    result = hybrid_search(state, "gamma", limit=5, db_path=database)

    assert result["results"]
    top = result["results"][0]
    assert top["citation"]["path"] == "docs/b.md"
    assert top["citation"]["sha256"]
    assert top["citation"]["start_line"] == 1
    assert top["sources"] == ["fts"]
    assert "fts_rank" in top
    assert "semantic_rank" not in top


def test_hybrid_builds_ephemeral_index_without_db_path(tmp_path):
    _, state = _write_repo(tmp_path)

    result = hybrid_search(state, "gamma graphs", limit=3)

    assert result["ephemeral_index"] is True
    assert result["db_path"] is None
    assert result["engine"] == "hybrid-rrf"
    assert result["truth_assessed"] is False
    assert result["results"]
    assert result["results"][0]["citation"]["path"] == "docs/b.md"


@pytest.mark.parametrize("limit", [-1, 101, True, 1.5, "8"])
def test_invalid_limit_is_rejected(tmp_path, limit):
    with pytest.raises(SemanticError, match="limit"):
        semantic_search({}, "alpha", limit=limit, db_path=tmp_path / "missing.sqlite")
    with pytest.raises(SemanticError, match="limit"):
        hybrid_search({}, "alpha", limit=limit)


@pytest.mark.parametrize("alpha", [-0.1, 1.1, float("nan"), True, "0.5"])
def test_invalid_alpha_is_rejected(tmp_path, alpha):
    with pytest.raises(SemanticError, match="alpha"):
        semantic_search({}, "alpha", alpha=alpha, db_path=tmp_path / "missing.sqlite")
    with pytest.raises(SemanticError, match="alpha"):
        hybrid_search({}, "alpha", alpha=alpha)


def test_semantic_search_requires_an_existing_index(tmp_path):
    _, state = _write_repo(tmp_path)

    with pytest.raises(SemanticError, match="db_path"):
        semantic_search(state, "alpha")
    with pytest.raises(SemanticError, match="not found"):
        semantic_search(state, "alpha", db_path=tmp_path / "missing.sqlite")
    with pytest.raises(SemanticError, match="not found"):
        hybrid_search(state, "alpha", db_path=tmp_path / "missing.sqlite")


def test_missing_sentence_transformers_package_reports_clearly(tmp_path, monkeypatch):
    _, state = _write_repo(tmp_path)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    with pytest.raises(SemanticError, match="sentence-transformers") as error:
        build_semantic_index(
            state, tmp_path / "model.sqlite", model="all-MiniLM-L6-v2"
        )

    assert "hash-bow" in str(error.value)


def test_unavailable_local_model_reports_clearly_without_network(tmp_path, monkeypatch):
    _, state = _write_repo(tmp_path)
    module = types.ModuleType("sentence_transformers")
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeSentenceTransformer:
        def __init__(self, name, **kwargs):
            calls.append((name, kwargs))
            raise OSError("model is not cached")

    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    with pytest.raises(SemanticError, match="all-MiniLM-L6-v2") as error:
        build_semantic_index(
            state, tmp_path / "model.sqlite", model="all-MiniLM-L6-v2"
        )

    assert calls and calls[0][1].get("local_files_only") is True
    assert "offline" in str(error.value)


def test_minimal_sentence_transformer_encoder_is_supported(tmp_path, monkeypatch):
    _, state = _write_repo(tmp_path)
    module = types.ModuleType("sentence_transformers")

    class MinimalEncoder:
        def __init__(self, name, **kwargs):
            self.name = name

        def encode(self, texts):
            return [
                [
                    1.0 if "alpha" in text.casefold() else 0.0,
                    1.0 if "gamma" in text.casefold() else 0.0,
                ]
                for text in texts
            ]

    module.SentenceTransformer = MinimalEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    database = tmp_path / "minimal.sqlite"
    built = build_semantic_index(state, database, model="minimal-local")

    assert built["engine"] == "sentence-transformers"
    assert built["dim"] == 2
    result = semantic_search(state, "alpha", db_path=database)
    assert result["results"]
    assert result["results"][0]["citation"]["path"] == "docs/a.md"


def test_environment_named_local_model_is_used(tmp_path, monkeypatch):
    _, state = _write_repo(tmp_path)
    module = types.ModuleType("sentence_transformers")
    loaded: list[tuple[str, dict[str, Any]]] = []

    class FakeEncoder:
        def __init__(self, name, **kwargs):
            loaded.append((name, kwargs))

        def encode(self, texts, **kwargs):
            return [
                [
                    1.0 if "alpha" in text.casefold() else 0.0,
                    1.0 if "gamma" in text.casefold() else 0.0,
                ]
                for text in texts
            ]

    module.SentenceTransformer = FakeEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setenv("OZAGIA_EMBED_MODEL", "fake-local-mini")

    database = tmp_path / "model.sqlite"
    built = build_semantic_index(state, database, model=None)

    assert built["engine"] == "sentence-transformers"
    assert built["model"] == "fake-local-mini"
    assert built["dim"] == 2
    assert loaded[0][0] == "fake-local-mini"
    assert loaded[0][1].get("local_files_only") is True

    result = semantic_search(state, "alpha", db_path=database)

    assert result["engine"] == "sentence-transformers"
    assert result["model"] == "fake-local-mini"
    assert result["results"]
    assert result["results"][0]["citation"]["path"] == "docs/a.md"
