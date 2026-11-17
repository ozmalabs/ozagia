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


def test_discover_does_not_follow_symlink_sibling(tmp_path: Path) -> None:
    root = tmp_path / "workspace" / "alpha"
    outside = tmp_path / "outside" / "linked"
    root.mkdir(parents=True)
    outside.mkdir(parents=True)
    _snapshot(root, "alpha")
    _snapshot(outside, "linked")
    (root.parent / "linked").symlink_to(outside, target_is_directory=True)

    report = federation.discover(root)

    assert [m["root"] for m in report["members"]] == [str(root.resolve())]


def test_manifest_members_are_combined_with_indexed_siblings(tmp_path: Path) -> None:
    root = tmp_path / "workspace" / "anchor"
    sibling = root.parent / "indexed-sibling"
    linked = tmp_path / "elsewhere" / "linked"
    missing = tmp_path / "elsewhere" / "future"
    for path in (root, sibling, linked):
        path.mkdir(parents=True)
    _snapshot(root, "anchor")
    _snapshot(sibling, "sibling")
    _snapshot(linked, "linked")
    manifest = root / ".ozagia" / "federation.json"
    manifest.write_text(json.dumps({
        "version": 1,
        "members": ["../../elsewhere/linked", str(missing)],
        "links": [],
    }), encoding="utf-8")

    report = federation.status(root)

    assert [member["name"] for member in report["members"]] == [
        "anchor", "sibling", "linked", "future",
    ]
    assert report["members"][-1]["state"] == "missing"
    assert report["discovery"] == "immediate-siblings"


def test_explicit_roots_override_manifest_members_and_siblings(tmp_path: Path) -> None:
    root, sibling, linked, selected = [tmp_path / name for name in
                                      ("root", "sibling", "linked", "selected")]
    for path in (root, sibling, linked, selected):
        path.mkdir()
        _snapshot(path, path.name)
    (root / ".ozagia" / "federation.json").write_text(json.dumps({
        "version": 1, "members": [str(linked)], "links": [],
    }), encoding="utf-8")

    report = federation.discover(root, roots=[selected])

    assert [member["name"] for member in report["members"]] == ["root", "selected"]


def test_malformed_manifest_members_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _snapshot(root, "root")
    file_member = root / "not-a-directory"
    file_member.write_text("not a checkout", encoding="utf-8")
    (root / ".ozagia" / "federation.json").write_text(json.dumps({
        "version": 1, "members": ["", 3, str(file_member)], "links": [],
    }), encoding="utf-8")

    report = federation.discover(root)

    assert report["links"]["member_errors"] == [
        "members[0] must be a non-empty directory string",
        "members[1] must be a non-empty directory string",
        f"members[2] is not a directory: {file_member}",
    ]


def test_absolute_state_dir_applies_only_to_anchor(tmp_path: Path, monkeypatch) -> None:
    root, sibling = tmp_path / "root", tmp_path / "sibling"
    state = tmp_path / "external-state"
    root.mkdir()
    sibling.mkdir()
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    state.mkdir()
    assert not (root / ".ozagia" / "graph.json").exists()
    _snapshot(sibling, "sibling")
    # The anchor's graph lives at its configured absolute state directory.
    graph = {
        "version": 2,
        "nodes": [{"id": 0, "type": "Repo", "key": "repo:",
                   "attrs": {"name": "root", "root": str(root), "head": "deadbeef"}}],
        "edges": [],
    }
    (state / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    report = federation.discover(root)

    assert [(m["id"], m["snapshot"]) for m in report["members"]] == [
        ("root", str(state / "graph.json")),
        ("sibling", str(sibling / ".ozagia" / "graph.json")),
    ]


def test_explicit_nonexistent_root_is_reported(tmp_path: Path) -> None:
    root, missing = tmp_path / "root", tmp_path / "not-created"
    root.mkdir()
    _snapshot(root, "root")

    report = federation.status(root, roots=[missing])

    assert report["members"][1]["root"] == str(missing.resolve())
    assert report["members"][1]["state"] == "missing"


def test_search_and_node_qualify_colliding_keys_and_preserve_edges(tmp_path: Path) -> None:
    root = tmp_path / "a"
    other = tmp_path / "b"
    root.mkdir()
    other.mkdir()
    _snapshot(root)
    _snapshot(other)
    graph_path = root / ".ozagia" / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({"id": 3, "type": "Opaque", "key": "opaque:key_only_token",
                           "attrs": {"name": "unrelated"}})
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

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
    key_result = federation.search(root, "key_only_token", roots=[other])
    assert [hit["key"] for hit in key_result["results"]] == ["opaque:key_only_token"]


def test_real_build_index_snapshot_is_discoverable_and_searchable(
        tmp_path: Path, monkeypatch) -> None:
    from ozagia.core.memory import build_index

    root = tmp_path / "built-repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "widget.py").write_text(
        "def calculate_widget(value):\n    return value\n", encoding="utf-8")
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.setenv("OZAGIA_ADAPTERS", "")
    build_index(root, root / ".ozagia" / "graph.json", include_docs="none",
                include_git=False, include_scripts=False)

    report = federation.discover(root, roots=[])
    hits = federation.search(root, "calculate_widget", roots=[])

    assert report["members"][0]["state"] == "unknown"
    assert any(hit["key"] == "sym:pkg/widget.py::calculate_widget"
               for hit in hits["results"])


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
