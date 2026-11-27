"""Focused tests for the opt-in, relevance-only federated code-search arm."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.core import federation


def _snapshot(root: Path, name: str) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    graph = {
        "version": 2,
        "nodes": [
            {"id": 0, "type": "Repo", "key": "repo:",
             "attrs": {"name": name, "root": str(root), "head": "deadbeef"}},
            {"id": 1, "type": "File", "key": "file:src/app.py",
             "attrs": {"name": "app.py", "path": "src/app.py"}},
        ],
        "edges": [],
    }
    (state / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def _source(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Anchor 'anchor' plus two sibling checkouts with matching source."""
    parent = tmp_path / "workspace"
    anchor, alpha, zeta = parent / "anchor", parent / "alpha", parent / "zeta"
    for root, name in ((anchor, "anchor"), (alpha, "alpha"), (zeta, "zeta")):
        root.mkdir(parents=True)
        _snapshot(root, name)
    _source(anchor, "src/anchor_mod.py", "def unrelated():\n    return None\n")
    _source(alpha, "src/alpha_feature.py",
            "def federated_widget_cache():\n"
            "    return 'federated widget cache'\n")
    _source(zeta, "src/zeta_feature.py",
            "def federated_widget_cache():\n"
            "    return 'federated widget cache'\n")
    return anchor, alpha, zeta


def _tree_state(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (path.relative_to(root).as_posix(), path.stat().st_mtime_ns,
         path.stat().st_size)
        for path in root.rglob("*") if path.is_file())


def test_query_finds_member_source_file(tmp_path: Path) -> None:
    anchor, _alpha, zeta = _workspace(tmp_path)

    result = federation.code_search(anchor, "federated widget cache", limit=10)

    assert result["ok"] is True
    assert result["mode"] == "code"
    hit = next(item for item in result["results"]
               if item["path"] == "src/zeta_feature.py")
    assert hit["qualified_key"] == "zeta::file:src/zeta_feature.py"
    assert hit["key"] == "file:src/zeta_feature.py"
    assert hit["file"] == "src/zeta_feature.py"
    assert hit["source"]["id"] == "zeta"
    assert hit["source"]["root"] == str(zeta.resolve())
    assert hit["per_member_rank"] == 1
    assert hit["normalized_score"] == 1.0
    assert hit["rank_score"] == 1.0
    member = next(item for item in result["members"] if item["id"] == "zeta")
    assert member["source_state"] == "ok"
    assert member["snapshot"]


def test_code_search_is_relevance_ranked_and_deterministic(tmp_path: Path) -> None:
    anchor, _alpha, _zeta = _workspace(tmp_path)

    first = federation.code_search(anchor, "federated widget cache", limit=10)
    second = federation.code_search(anchor, "federated widget cache", limit=10)

    assert "family_boost" not in first
    assert "families" not in first
    for hit in first["results"]:
        assert "family_match" not in hit
        assert "families" not in hit
        assert hit["rank_score"] == hit["normalized_score"]
    assert [hit["qualified_key"] for hit in first["results"]] == [
        hit["qualified_key"] for hit in second["results"]]
    assert first["results"][0]["source"]["id"] == "alpha"
    assert [hit["source"]["id"] for hit in first["results"]] == ["alpha", "zeta"]


def test_code_search_rejects_family_boost_argument(tmp_path: Path) -> None:
    anchor, _alpha, _zeta = _workspace(tmp_path)

    with pytest.raises(TypeError):
        federation.code_search(  # type: ignore[call-arg]
            anchor, "federated widget cache", family_boost=True)


def test_cache_is_outside_members_and_members_are_not_written(
        tmp_path: Path, monkeypatch) -> None:
    anchor, alpha, zeta = _workspace(tmp_path)
    outside_state = tmp_path / "external-state"
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(outside_state))
    before = {member: _tree_state(member) for member in (anchor, alpha, zeta)}

    result = federation.code_search(anchor, "federated widget cache", limit=10)
    repeat = federation.code_search(anchor, "federated widget cache", limit=10)

    cache_dir = Path(result["cache"]["dir"])
    assert cache_dir == outside_state / "federation-search"
    assert result["cache"]["fallback"] is False
    assert result["cache"]["files"] == repeat["cache"]["files"]
    for path_text in result["cache"]["files"].values():
        path = Path(path_text)
        assert path.is_file()
        assert path.parent == cache_dir
        for member in (anchor, alpha, zeta):
            assert not path.is_relative_to(member.resolve())
    after = {member: _tree_state(member) for member in (anchor, alpha, zeta)}
    assert after == before
    for member in (alpha, zeta):
        assert not (member / ".ozagia" / "federation-search").exists()
    assert any(
        hit["qualified_key"] == "zeta::file:src/zeta_feature.py"
        for hit in repeat["results"])


def test_cache_refuses_member_directories_and_falls_back(
        tmp_path: Path) -> None:
    anchor, _alpha, zeta = _workspace(tmp_path)

    result = federation.code_search(
        anchor, "federated widget cache", cache_dir=zeta / "cache")

    assert result["cache"]["fallback"] is True
    assert result["cache"]["state"] == "temporary"
    assert "member checkout" in result["cache"]["note"]
    assert Path(result["cache"]["dir"]).is_dir()
    assert not (zeta / "cache").exists()

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    fallback = federation.code_search(
        anchor, "federated widget cache", cache_dir=blocked / "cache")
    assert fallback["cache"]["fallback"] is True
    assert "not writable" in fallback["cache"]["note"]


def test_missing_source_is_reported_not_fatal(tmp_path: Path) -> None:
    anchor, alpha, _zeta = _workspace(tmp_path)
    missing = tmp_path / "absent-member"

    result = federation.code_search(
        anchor, "federated widget cache", roots=[alpha, missing], limit=5)

    assert result["ok"] is True
    assert any(hit["source"]["id"] == "alpha" for hit in result["results"])
    member = next(item for item in result["members"] if item["id"] == "absent-member")
    assert member["source_state"] == "missing"
    assert member["search"]["state"] == "skipped"
    assert member["state"] == "missing"
    assert any("absent-member" in note for note in result["notes"])


def test_snippets_are_opt_in(tmp_path: Path) -> None:
    anchor, _alpha, _zeta = _workspace(tmp_path)

    default = federation.code_search(anchor, "federated widget cache", limit=5)
    with_snippets = federation.code_search(
        anchor, "federated widget cache", limit=5, include_snippet=True)

    assert all("snippet" not in hit and "line" not in hit
               for hit in default["results"])
    hit = next(item for item in with_snippets["results"]
               if item["path"] == "src/zeta_feature.py")
    assert hit["line"] == 1
    assert "federated_widget_cache" in hit["snippet"]


def test_deterministic_ties_use_member_id_then_path(tmp_path: Path) -> None:
    parent = tmp_path / "workspace"
    anchor, aaa, bbb = parent / "anchor", parent / "aaa", parent / "bbb"
    for root, name in ((anchor, "anchor"), (aaa, "aaa"), (bbb, "bbb")):
        root.mkdir(parents=True)
        _snapshot(root, name)
    body = "def shared_tie_breaker():\n    return 'shared tie breaker'\n"
    _source(anchor, "src/anchor_mod.py", "def unrelated():\n    return None\n")
    _source(aaa, "src/same.py", body)
    _source(bbb, "src/same.py", body)

    first = federation.code_search(anchor, "shared tie breaker", limit=10)
    second = federation.code_search(anchor, "shared tie breaker", limit=10)

    order = [(hit["source"]["id"], hit["path"]) for hit in first["results"]]
    assert order == [("aaa", "src/same.py"), ("bbb", "src/same.py")]
    assert order == [(hit["source"]["id"], hit["path"])
                     for hit in second["results"]]
