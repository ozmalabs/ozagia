"""Focused tests for opt-in family-aware federation ranking (read-only fixtures)."""
from __future__ import annotations

import json
from pathlib import Path

from ozagia.core import federation, repo_relations

FAMILY_QUERY = "cache eviction policy warm tier budget clock"
FAMILY_TITLE = "cache eviction policy warm tier notes"
DISTRACTOR_TITLE = "cache eviction policy warm tier budget notes"


def _member(root: Path, name: str,
            docs: list[tuple[str, str]] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    nodes: list[dict[str, object]] = [{
        "id": 0, "type": "Repo", "key": "repo:",
        "attrs": {"name": name, "root": str(root), "head": "deadbeef"},
    }]
    for index, (key, title) in enumerate(docs or [], start=1):
        nodes.append({"id": index, "type": "Doc", "key": key,
                      "attrs": {"title": title, "path": "docs/notes.md"}})
    (state / "graph.json").write_text(json.dumps({
        "version": 2, "nodes": nodes, "edges": [],
    }), encoding="utf-8")


def _manifest(root: Path, members: list[dict[str, str]],
              families: list[dict[str, object]] | None = None) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"version": 1, "members": members, "links": []}
    if families is not None:
        data["families"] = families
    (state / "federation.json").write_text(json.dumps(data), encoding="utf-8")


def _family_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    anchor = tmp_path / "workspace" / "anchor"
    family = tmp_path / "workspace" / "family-member"
    distractor = tmp_path / "workspace" / "distractor"
    _member(anchor, "anchor")
    _member(family, "family-member", [("doc:family-note", FAMILY_TITLE)])
    _member(distractor, "distractor", [("doc:distractor-note", DISTRACTOR_TITLE)])
    _manifest(
        anchor,
        [{"path": str(family), "id": "family-member"},
         {"path": str(distractor), "id": "distractor"}],
        [{"id": "shared-stack", "label": "Shared stack",
          "members": ["anchor", "family-member"],
          "provenance": "test fixture"}],
    )
    return anchor, family, distractor


def test_default_family_boost_is_off_and_matches_lexical_baseline(tmp_path: Path) -> None:
    anchor, _family, _distractor = _family_workspace(tmp_path)

    baseline = federation.search(anchor, FAMILY_QUERY, limit=20)
    explicit_off = federation.search(
        anchor, FAMILY_QUERY, limit=20, family_boost=False)

    assert baseline["family_boost"] is False
    assert baseline["families"]["state"] == "disabled"
    assert explicit_off["results"] == baseline["results"]
    assert [hit["source"]["id"] for hit in baseline["results"]] == [
        "distractor", "family-member"]
    assert [hit["score"] for hit in baseline["results"]] == [6 / 7, 5 / 7]
    assert all(hit["family_match"] is False for hit in baseline["results"])
    assert all(hit["families"] == [] for hit in baseline["results"])


def test_enabled_boost_re_ranks_family_member_over_higher_lexical_hit(
        tmp_path: Path) -> None:
    anchor, _family, _distractor = _family_workspace(tmp_path)

    boosted = federation.search(
        anchor, FAMILY_QUERY, limit=20, family_boost=True)

    assert boosted["family_boost"] is True
    assert boosted["families"]["state"] == "ok"
    assert boosted["families"]["by_member"] == {
        "anchor": ["shared-stack"],
        "distractor": [],
        "family-member": ["shared-stack"],
    }
    assert boosted["n"] == 2
    assert [hit["source"]["id"] for hit in boosted["results"]] == [
        "family-member", "distractor"]
    family_hit, distractor_hit = boosted["results"]
    assert family_hit["family_match"] is True
    assert family_hit["families"] == ["shared-stack"]
    assert distractor_hit["family_match"] is False
    assert distractor_hit["families"] == []
    # The lexical score is preserved; the family arm only changes rank_score.
    assert family_hit["score"] < distractor_hit["score"]
    assert family_hit["rank_score"] > distractor_hit["rank_score"]
    assert family_hit["rank_score"] == 5 / 7 + federation.FAMILY_RANK_BOOST


def test_no_family_declarations_leave_ranking_unchanged(tmp_path: Path) -> None:
    anchor, family, distractor = _family_workspace(tmp_path)
    _manifest(anchor, [{"path": str(family), "id": "family-member"},
                       {"path": str(distractor), "id": "distractor"}])

    baseline = federation.search(anchor, FAMILY_QUERY, limit=20)
    boosted = federation.search(
        anchor, FAMILY_QUERY, limit=20, family_boost=True)

    assert boosted["results"] == baseline["results"]
    assert boosted["families"]["state"] == "absent"
    assert boosted["families"]["errors"] == []
    assert all(hit["family_match"] is False for hit in boosted["results"])
    assert all(hit["families"] == [] for hit in boosted["results"])


def test_invalid_and_partial_family_declarations_are_reported_not_fatal(
        tmp_path: Path) -> None:
    anchor, family, distractor = _family_workspace(tmp_path)
    members = [{"path": str(family), "id": "family-member"},
               {"path": str(distractor), "id": "distractor"}]
    _manifest(anchor, members, [{
        "id": "Bad Id", "members": ["family-member"],
        "provenance": "invalid fixture",
    }])

    invalid = federation.search(
        anchor, FAMILY_QUERY, limit=20, family_boost=True)

    assert invalid["ok"] is True
    assert invalid["families"]["state"] == "invalid"
    assert any("families[0].id" in error
               for error in invalid["families"]["errors"])
    assert [hit["source"]["id"] for hit in invalid["results"]] == [
        "distractor", "family-member"]
    assert all(hit["family_match"] is False for hit in invalid["results"])

    _manifest(anchor, members, [
        {"id": "Bad Id", "members": ["family-member"],
         "provenance": "invalid fixture"},
        {"id": "shared-stack", "members": ["anchor", "family-member"],
         "provenance": "valid fixture"},
    ])
    partial = federation.search(
        anchor, FAMILY_QUERY, limit=20, family_boost=True)

    assert partial["families"]["state"] == "partial"
    assert partial["families"]["by_member"]["family-member"] == ["shared-stack"]
    assert partial["families"]["errors"]
    assert [hit["source"]["id"] for hit in partial["results"]] == [
        "family-member", "distractor"]


def test_family_boost_accepts_the_default_repository_root(
        tmp_path: Path, monkeypatch) -> None:
    anchor, _family, _distractor = _family_workspace(tmp_path)
    monkeypatch.setattr(federation, "repo_root", lambda: anchor)

    result = federation.search(None, FAMILY_QUERY, limit=20, family_boost=True)

    assert result["families"]["state"] == "ok"
    assert [hit["source"]["id"] for hit in result["results"]] == [
        "family-member", "distractor"]


def test_malformed_manifest_is_reported_and_does_not_break_search(
        tmp_path: Path) -> None:
    anchor, _family, _distractor = _family_workspace(tmp_path)
    (anchor / ".ozagia" / "federation.json").write_text(
        "{not valid json", encoding="utf-8")

    result = federation.search(
        anchor, FAMILY_QUERY, limit=20, family_boost=True)
    empty = federation.search(anchor, "!!!", family_boost=True)

    assert result["ok"] is True
    assert result["families"]["state"] == "invalid"
    assert result["families"]["errors"]
    assert [hit["source"]["id"] for hit in result["results"]] == [
        "distractor", "family-member"]
    assert all(hit["family_match"] is False for hit in result["results"])
    assert empty["results"] == []
    assert empty["families"]["state"] == "invalid"


def test_family_ties_are_deterministic_and_expose_family_fields(
        tmp_path: Path) -> None:
    anchor = tmp_path / "workspace" / "anchor"
    family_a = tmp_path / "workspace" / "family-a"
    family_b = tmp_path / "workspace" / "family-b"
    distractor = tmp_path / "workspace" / "alpha"
    _member(anchor, "anchor")
    _member(family_a, "family-a", [("doc:zeta", "widget"), ("doc:alpha", "widget")])
    _member(family_b, "family-b", [("doc:beta", "widget")])
    _member(distractor, "alpha", [("doc:only", "widget")])
    _manifest(
        anchor,
        [{"path": str(family_a), "id": "family-a"},
         {"path": str(family_b), "id": "family-b"},
         {"path": str(distractor), "id": "alpha"}],
        [{"id": "shared-stack", "members": ["anchor", "family-a", "family-b"],
          "provenance": "test fixture"}],
    )

    boosted = federation.search(anchor, "widget", limit=20, family_boost=True)
    repeated = federation.search(anchor, "widget", limit=20, family_boost=True)

    order = [(hit["source"]["id"], hit["key"]) for hit in boosted["results"]]
    assert order == [
        ("family-a", "doc:alpha"),
        ("family-a", "doc:zeta"),
        ("family-b", "doc:beta"),
        ("alpha", "doc:only"),
    ]
    assert [hit["family_match"] for hit in boosted["results"]] == [
        True, True, True, False]
    assert [hit["families"] for hit in boosted["results"]] == [
        ["shared-stack"], ["shared-stack"], ["shared-stack"], []]
    assert all(hit["rank_score"] == 1.0 for hit in boosted["results"])
    assert repeated["results"] == boosted["results"]


def test_family_index_is_a_lightweight_public_lookup(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor"
    member = tmp_path / "member"
    _member(anchor, "anchor")
    _member(member, "member")
    _manifest(anchor, [{"path": str(member), "id": "member"}], [{
        "id": "shared-stack", "label": "Shared stack",
        "members": ["anchor", "member"], "provenance": "test fixture",
    }])

    index = repo_relations.family_index(
        anchor, [{"alias": "anchor"}, {"alias": "member"}])

    assert index["state"] == "ok"
    assert index["by_member"] == {
        "anchor": ["shared-stack"], "member": ["shared-stack"]}
    assert index["families"] == [{
        "id": "shared-stack", "label": "Shared stack",
        "provenance": "test fixture", "members": ["anchor", "member"],
        "unresolved": [],
    }]
    assert index["errors"] == []
    assert index["truncated"] is False
