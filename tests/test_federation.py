"""Focused tests for read-only federation over independent graph snapshots."""
from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

from ozagia.core import federation


def _snapshot(root: Path, name: str = "same") -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True)
    graph = {
        "version": 2,
        "nodes": [
            {"id": 0, "type": "Repo", "key": "repo:",
             "attrs": {"name": name, "root": str(root), "head": "deadbeef"}},
            {"id": 1, "type": "File", "key": "file:src/app.py",
             "attrs": {"name": "app.py", "path": "src/app.py", "summary": "widget logic"}},
            {"id": 2, "type": "Symbol", "key": "sym:src/app.py::widget",
             "attrs": {"name": "widget", "file": "src/app.py"}},
        ],
        "edges": [{"src": 1, "type": "defines", "tgt": 2,
                    "attrs": {"confidence": "snapshot"}}],
    }
    (state / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def test_discover_includes_current_and_indexed_immediate_siblings(tmp_path: Path) -> None:
    root = tmp_path / "workspace" / "alpha"
    sibling = root.parent / "beta"
    unindexed = root.parent / "notes"
    root.mkdir(parents=True)
    sibling.mkdir()
    unindexed.mkdir()
    _snapshot(root, "alpha")
    _snapshot(sibling, "beta")

    report = federation.discover(root)

    assert [m["id"] for m in report["members"]] == ["alpha", "beta"]
    assert report["discovery"] == "immediate-siblings"
    assert all(m["root"] in {str(root.resolve()), str(sibling.resolve())}
               for m in report["members"])


def test_search_and_node_qualify_colliding_keys_and_preserve_edges(tmp_path: Path) -> None:
    root = tmp_path / "a"
    other = tmp_path / "b"
    root.mkdir()
    other.mkdir()
    _snapshot(root)
    _snapshot(other)

    results = federation.search(root, "widget", roots=[other])
    assert results["n"] == 4
    assert len({hit["qualified_key"] for hit in results["results"]}) == 4
    file_hits = [h for h in results["results"] if h["key"] == "file:src/app.py"]
    assert len(file_hits) == 2
    selected = federation.node(root, file_hits[0]["qualified_key"], roots=[other])
    assert selected["ok"]
    assert selected["edges"] == [{
        "src": file_hits[0]["qualified_key"], "type": "defines",
        "tgt": file_hits[0]["qualified_key"].split("::", 1)[0] + "::sym:src/app.py::widget",
        "attrs": {"confidence": "snapshot"},
        "source": {"id": file_hits[0]["qualified_key"].split("::", 1)[0],
                   "root": file_hits[0]["source"]["root"]},
    }]


def test_explicit_missing_and_invalid_members_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "root"
    missing = tmp_path / "missing"
    invalid = tmp_path / "invalid"
    for path in (root, missing, invalid):
        path.mkdir()
    _snapshot(root, "root")
    (invalid / ".ozagia").mkdir()
    (invalid / ".ozagia" / "graph.json").write_text("{", encoding="utf-8")

    report = federation.status(root, roots=[missing, invalid])

    assert [m["state"] for m in report["members"]] == ["unknown", "missing", "invalid"]
    assert report["states"]["missing"] == 1
    assert report["states"]["invalid"] == 1
    assert not (missing / ".ozagia").exists()


def test_stale_snapshot_is_reported_when_head_differs(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "stale"
    root.mkdir()
    _snapshot(root, "stale")
    monkeypatch.setattr(
        federation.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="newhead\n"),
    )

    member = federation.status(root, roots=[])["members"][0]

    assert member["state"] == "stale"
    assert "HEAD differs" in member["detail"]


def test_duplicate_checkout_names_get_distinct_aliases(tmp_path: Path) -> None:
    a = tmp_path / "one" / "repo"
    b = tmp_path / "two" / "repo"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _snapshot(a, "repo")
    _snapshot(b, "repo")

    ids = [member["id"] for member in federation.discover(a, roots=[b])["members"]]

    assert len(ids) == len(set(ids)) == 2
    assert all(identifier.startswith("repo@") for identifier in ids)


def test_explicit_links_are_validated_and_shown_on_incident_nodes(tmp_path: Path) -> None:
    root, other = tmp_path / "consumer", tmp_path / "producer"
    root.mkdir()
    other.mkdir()
    _snapshot(root, "consumer")
    _snapshot(other, "producer")
    manifest_path = root / ".ozagia" / "federation.json"
    manifest = {
        "version": 1,
        "links": [
            {"source": "consumer::file:src/app.py",
             "target": "producer::sym:src/app.py::widget",
             "relation": "depends_on", "provenance": "declared in deployment manifest"},
            {"source": "consumer::file:missing.py", "target": "producer::repo:",
             "relation": "depends_on", "provenance": "bad key"},
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = {path: path.read_bytes() for path in
              (manifest_path, root / ".ozagia" / "graph.json",
               other / ".ozagia" / "graph.json")}

    report = federation.discover(root, roots=[other])
    result = federation.node(root, "consumer::file:src/app.py", roots=[other])

    assert report["links"]["state"] == "partial"
    assert len(report["links"]["links"]) == 1
    assert "endpoint not found" in report["links"]["errors"][0]
    assert result["cross_repository_edges"] == report["links"]["links"]
    assert result["cross_repository_edges"][0]["source_manifest"] == str(manifest_path)
    assert result["cross_repository_link_errors"] == report["links"]["errors"]
    assert all(path.read_bytes() == content for path, content in before.items())
