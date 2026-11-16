"""Graph view model, apertures, HTML export and the view CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import view_cli, view_html
from ozagia.core import apertures as A
from ozagia.core import view as V


def _dev_graph() -> dict:
    """A small dev-memory snapshot: files, symbols, tests, docs and history."""
    return {
        "version": 2,
        "next_id": 9,
        "nodes": [
            {"id": 0, "type": "Repo", "key": "repo:", "attrs": {
                "name": "demo", "root": "/tmp/demo", "head": "abc123",
                "generated_at": "2026-11-16T10:00:00"}},
            {"id": 1, "type": "File", "key": "file:src/app.py", "attrs": {
                "name": "src/app.py", "path": "src/app.py", "language": "py"}},
            {"id": 2, "type": "File", "key": "file:tests/test_app.py", "attrs": {
                "name": "tests/test_app.py", "path": "tests/test_app.py",
                "language": "py"}},
            {"id": 3, "type": "Symbol", "key": "sym:src/app.py::build", "attrs": {
                "name": "build", "file": "src/app.py", "kind": "function"}},
            {"id": 4, "type": "Test", "key": "test:tests/test_app.py::test_build",
             "attrs": {"name": "test_build", "file": "tests/test_app.py"}},
            {"id": 5, "type": "Failure", "key": "failure:build", "attrs": {
                "name": "build", "file": "src/app.py", "class": "ValueError",
                "message": "boom", "last_green_commit": "deadbeef"}},
            {"id": 6, "type": "Doc", "key": "doc:docs/view.md", "attrs": {
                "name": "docs/view.md", "path": "docs/view.md", "title": "View"}},
            {"id": 7, "type": "Commit", "key": "commit:abc123", "attrs": {
                "name": "abc123", "hash": "abc123", "date": "2026-11-15",
                "subject": "Add view"}},
            {"id": 8, "type": "Mystery", "key": "mystery:one", "attrs": {
                "name": "not in the aperture"}},
        ],
        "edges": [
            {"src": 0, "type": "contains", "tgt": 1, "attrs": {}},
            {"src": 0, "type": "contains", "tgt": 2, "attrs": {}},
            {"src": 1, "type": "defines", "tgt": 3, "attrs": {}},
            {"src": 1, "type": "imports", "tgt": 2, "attrs": {}},
            {"src": 2, "type": "imports", "tgt": 1, "attrs": {}},
            {"src": 1, "type": "has_test", "tgt": 4, "attrs": {}},
            {"src": 4, "type": "tests_module", "tgt": 1, "attrs": {}},
            {"src": 4, "type": "mentions_file", "tgt": 1, "attrs": {}},
            {"src": 5, "type": "refers_to", "tgt": 4, "attrs": {}},
            {"src": 5, "type": "in_file", "tgt": 2, "attrs": {}},
            {"src": 7, "type": "changes", "tgt": 1, "attrs": {}},
            {"src": 1, "type": "mystery_edge", "tgt": 8, "attrs": {}},
        ],
    }


def _research_graph() -> dict:
    """A research graph bundle with string ids and top-level fields."""
    nodes = [
        {"id": "question-0001", "type": "Question",
         "created_at": "2026-11-01T00:00:00Z", "wording": "Does X cause Y?"},
        {"id": "frame-0001", "type": "Frame",
         "created_at": "2026-11-01T01:00:00Z", "name": "Exploratory frame"},
        {"id": "claim-0001", "type": "Claim",
         "created_at": "2026-11-02T00:00:00Z", "wording": "X causes Y",
         "status": "open", "scope": "lab"},
        {"id": "claim-0002", "type": "Claim",
         "created_at": "2026-11-02T01:00:00Z", "wording": "X does not cause Y"},
        {"id": "source-0001", "type": "Source",
         "created_at": "2026-11-03T00:00:00Z", "locator": "doi:10.1/x",
         "title": "A study"},
        {"id": "holding-0001", "type": "Holding",
         "created_at": "2026-11-03T01:00:00Z", "source_id": "source-0001"},
        {"id": "evidence-0001", "type": "Evidence",
         "created_at": "2026-11-04T00:00:00Z", "wording": "Trial result"},
        {"id": "observation-0001", "type": "Observation",
         "created_at": "2026-11-04T01:00:00Z", "wording": "Observed effect"},
        {"id": "run-0001", "type": "Run",
         "created_at": "2026-11-04T02:00:00Z", "name": "Run 1"},
        {"id": "reference-0001", "type": "Reference",
         "created_at": "2026-11-05T00:00:00Z", "title": "A study",
         "identifiers": {"doi": "10.1/x"}},
        {"id": "reviewplan-0001", "type": "ReviewPlan",
         "created_at": "2026-11-06T00:00:00Z", "question": "Is the claim sound?"},
        {"id": "reviewrun-0001", "type": "ReviewRun",
         "created_at": "2026-11-06T01:00:00Z", "model": "reviewer-a",
         "role": "methods"},
        {"id": "reviewfinding-0001", "type": "ReviewFinding",
         "created_at": "2026-11-06T02:00:00Z", "severity": "major",
         "issue_type": "missing_test", "wording": "No test is reported."},
        {"id": "reviewfinding-0002", "type": "ReviewFinding",
         "created_at": "2026-11-06T03:00:00Z", "severity": "minor",
         "issue_type": "wording", "wording": "Clarify the scope."},
        {"id": "reconciliation-0001", "type": "Reconciliation",
         "created_at": "2026-11-06T04:00:00Z", "method": "structural"},
    ]
    return {
        "schema_version": 6,
        "investigation_id": "research-test",
        "events": [],
        "nodes": nodes,
        "edges": [
            {"id": "edge-000001", "type": "scoped_by",
             "src": "claim-0001", "tgt": "frame-0001",
             "created_at": "2026-11-02T00:00:00Z"},
            {"id": "edge-000002", "type": "supports",
             "src": "evidence-0001", "tgt": "claim-0001",
             "provenance": {"source": "review:plan-1"}},
            {"id": "edge-000003", "type": "held_as",
             "src": "source-0001", "tgt": "holding-0001"},
            {"id": "edge-000004", "type": "manifestation_of",
             "src": "source-0001", "tgt": "reference-0001"},
            {"id": "edge-000005", "type": "addresses",
             "src": "reviewplan-0001", "tgt": "claim-0001"},
            {"id": "edge-000006", "type": "reviews",
             "src": "reviewrun-0001", "tgt": "claim-0001"},
            {"id": "edge-000007", "type": "implements",
             "src": "reviewrun-0001", "tgt": "reviewplan-0001"},
            {"id": "edge-000008", "type": "finds",
             "src": "reviewrun-0001", "tgt": "reviewfinding-0001"},
            {"id": "edge-000009", "type": "about",
             "src": "reviewfinding-0001", "tgt": "claim-0001"},
            {"id": "edge-000010", "type": "agrees_with",
             "src": "reviewfinding-0001", "tgt": "reviewfinding-0002"},
            {"id": "edge-000011", "type": "reconciles",
             "src": "reconciliation-0001", "tgt": "reviewrun-0001"},
        ],
    }


def _aperture(**overrides) -> dict:
    data = {
        "id": "test-aperture",
        "title": "Test aperture",
        "description": "A test view.",
        "resolves": {"nodes": ["File", "Symbol", "Test"],
                     "edges": ["imports", "contains"]},
        "grain": "file",
        "render": {"layout": "table", "seed": 3},
    }
    data.update(overrides)
    return A.validate_aperture(data)


def _write_graph(root: Path, graph: dict) -> Path:
    path = root / ".ozagia" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# apertures
# --------------------------------------------------------------------------- #
def test_builtins_are_valid_and_have_layouts() -> None:
    definitions = A.builtin_apertures()
    assert set(definitions) == {
        "architecture", "consensus", "evidence", "impact", "timeline"}
    for definition in definitions.values():
        assert definition["schema"] == A.SCHEMA_VERSION
        assert definition["render"]["layout"] in A.LAYOUTS
        assert definition["limits"]["max_nodes"] > 0
        assert definition["limits"]["max_edges"] > 0
        assert definition["resolves"]["nodes"]


def test_aperture_validation_reports_the_field() -> None:
    with pytest.raises(A.ApertureError, match="id"):
        A.validate_aperture({"title": "No id"})
    with pytest.raises(A.ApertureError, match="schema"):
        A.validate_aperture({"schema": 2, "id": "x", "title": "X"})
    with pytest.raises(A.ApertureError, match="render.layout"):
        _aperture(render={"layout": "spiral"})
    with pytest.raises(A.ApertureError, match="limits.max_nodes"):
        _aperture(limits={"max_nodes": 0})
    with pytest.raises(A.ApertureError, match="collapse.unresolved"):
        _aperture(collapse={"unresolved": "wildcard"})
    with pytest.raises(A.ApertureError, match="unknown key"):
        A.validate_aperture({"id": "x", "title": "X", "extra": True})
    with pytest.raises(A.ApertureError, match="unknown aperture"):
        A.get_aperture("not-there")


def test_repo_apertures_add_and_override(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    directory = root / "ozagia" / "apertures"
    directory.mkdir(parents=True)
    (directory / "custom.toml").write_text(
        'id = "custom"\ntitle = "Custom"\ngrain = "node"\n'
        '[resolves]\nnodes = ["File"]\nedges = []\n'
        '[render]\nlayout = "table"\n', encoding="utf-8")
    (directory / "architecture.toml").write_text(
        'id = "architecture"\ntitle = "Repo architecture"\n'
        '[limits]\nmax_nodes = 3\n', encoding="utf-8")
    custom = A.get_aperture("custom", root=root)
    assert custom["title"] == "Custom"
    assert custom["render"]["layout"] == "table"
    overridden = A.get_aperture("architecture", root=root)
    assert overridden["title"] == "Repo architecture"
    assert overridden["limits"]["max_nodes"] == 3
    assert overridden["grain"] == "file"
    assert overridden["render"]["layout"] == "layered"
    assert "Repo" in overridden["resolves"]["nodes"]
    assert "architecture" in A.available_apertures(root)

    (directory / "broken.toml").write_text("title = [", encoding="utf-8")
    with pytest.raises(A.ApertureError, match="broken.toml"):
        A.repo_apertures(root)


# --------------------------------------------------------------------------- #
# view model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("aperture_id", sorted(A.builtin_apertures()))
@pytest.mark.parametrize("graph", [_dev_graph(), _research_graph()])
def test_every_builtin_projects_both_graph_shapes(aperture_id: str,
                                                  graph: dict) -> None:
    view = V.build_view(graph, aperture_id, root="/tmp/demo")
    assert view["schema"] == V.SCHEMA_VERSION
    assert view["aperture"] == aperture_id
    assert view["layout"]["kind"] in A.LAYOUTS
    assert isinstance(view["layout"]["seed"], int)
    assert view["provenance"]["derived"] is True
    assert set(view["snapshot"]) == {
        "root", "head", "freshness", "generated_at"}
    assert view["omitted"]["nodes"] == 0
    assert view["omitted"]["edges"] == 0
    assert view["omitted"]["reason"] == "none"
    assert len({node["id"] for node in view["nodes"]}) == len(view["nodes"])
    assert len({edge["id"] for edge in view["edges"]}) == len(view["edges"])


def test_unknown_types_are_other_not_dropped() -> None:
    view = V.build_view(_dev_graph(), "architecture")
    node = next(node for node in view["nodes"] if node["id"] == "8")
    assert node["type"] == "other"
    assert node["raw_type"] == "Mystery"
    assert node["group"] == "other"
    assert node["label"] == "not in the aperture"
    edge = next(edge for edge in view["edges"]
                if edge["raw_type"] == "mystery_edge")
    assert edge["type"] == "other"
    assert edge["raw_type"] == "mystery_edge"


def test_empty_graph_has_a_complete_shape() -> None:
    for aperture_id in sorted(A.builtin_apertures()):
        view = V.build_view({"version": 2, "nodes": [], "edges": []},
                            aperture_id, root="/tmp/empty")
        assert view["nodes"] == []
        assert view["edges"] == []
        assert view["groups"] == []
        assert view["legend"]["color"] == {}
        assert view["snapshot"]["root"] == "/tmp/empty"
        assert view["snapshot"]["freshness"] is None
        assert view["omitted"] == {
            "nodes": 0, "edges": 0, "reason": "none", "by": {}}
        assert "No nodes" in view_html.render_html(view)


def test_research_bundle_and_string_ids() -> None:
    graph = _research_graph()
    view = V.build_view(graph, "evidence", root="/repo")
    assert V.graph_kind(graph) == "research"
    assert view["snapshot"]["head"] is None
    assert view["snapshot"]["freshness"] is None
    assert view["provenance"]["investigation"] == "research-test"
    claim = next(node for node in view["nodes"] if node["id"] == "claim-0001")
    assert claim["label"] == "X causes Y"
    assert claim["attrs"]["status"] == "open"
    reference = next(node for node in view["nodes"]
                     if node["id"] == "reference-0001")
    assert reference["label"] == "A study"
    assert reference["attrs"]["identifiers"] == {"doi": "10.1/x"}
    edge = next(edge for edge in view["edges"] if edge["id"] == "edge-000002")
    assert edge["type"] == "supports"
    assert edge["attrs"]["provenance"] == {"source": "review:plan-1"}

    wrapped = V.build_view({"bundle": graph}, "evidence", root="/repo")
    assert V.render_json(wrapped) == V.render_json(view)


def test_snapshot_metadata_is_reported_not_invented() -> None:
    graph = _dev_graph()
    view = V.build_view(graph, "architecture")
    assert view["snapshot"]["root"] == "/tmp/demo"
    assert view["snapshot"]["head"] == "abc123"
    assert view["snapshot"]["freshness"] is None
    assert view["provenance"]["sources"] == []

    graph["nodes"][0]["attrs"]["freshness"] = "stale"
    graph["provenance"] = {"sources": ["review:plan-1"]}
    view = V.build_view(graph, "architecture", source=".ozagia/graph.json")
    assert view["snapshot"]["freshness"] == "stale"
    assert view["provenance"]["sources"] == [
        ".ozagia/graph.json", "review:plan-1"]


def test_keys_and_provenance_are_preserved() -> None:
    view = V.build_view(_dev_graph(), "architecture")
    node = next(node for node in view["nodes"] if node["id"] == "1")
    assert node["key"] == "file:src/app.py"
    research = V.build_view(_research_graph(), "consensus")
    claim = next(node for node in research["nodes"]
                 if node["id"] == "claim-0001")
    assert claim["key"] == "claim-0001"
    assert claim["attrs"]["created_at"] == "2026-11-02T00:00:00Z"


def test_deterministic_json_and_input_order() -> None:
    first = V.build_view(_dev_graph(), "architecture")
    second = V.build_view(_dev_graph(), "architecture")
    assert V.render_json(first) == V.render_json(second)

    reordered = _dev_graph()
    reordered["nodes"] = list(reversed(reordered["nodes"]))
    reordered["edges"] = list(reversed(reordered["edges"]))
    assert V.render_json(V.build_view(reordered, "architecture")) == \
        V.render_json(first)

    assert view_html.render_html(first) == view_html.render_html(second)


def test_scope_root_and_depth() -> None:
    graph = {
        "version": 2,
        "nodes": [{"id": index, "type": "File",
                   "attrs": {"name": f"f{index}.py", "path": f"f{index}.py"}}
                  for index in range(4)],
        "edges": [{"src": 0, "type": "imports", "tgt": 1},
                  {"src": 1, "type": "imports", "tgt": 2},
                  {"src": 2, "type": "imports", "tgt": 3}],
    }
    aperture = _aperture(scope={"root": "1", "depth": 1})
    view = V.build_view(graph, aperture)
    assert {node["id"] for node in view["nodes"]} == {"0", "1", "2"}
    assert {edge["src"] for edge in view["edges"]} == {"0", "1"}
    assert view["omitted"]["nodes"] == 1
    assert view["omitted"]["edges"] == 1
    assert view["omitted"]["reason"] == "scope filter"
    assert view["omitted"]["by"] == {"scope_nodes": 1, "scope_edges": 1}

    at_root = V.build_view(graph, aperture, depth=0)
    assert [node["id"] for node in at_root["nodes"]] == ["1"]
    assert at_root["edges"] == []
    assert at_root["omitted"]["nodes"] == 3

    with pytest.raises(V.ViewError, match="scope root"):
        V.build_view(graph, aperture, scope_root="99")
    with pytest.raises(V.ViewError, match="depth"):
        V.build_view(graph, aperture, depth=-1)


def test_caps_report_omitted_counts_and_reasons() -> None:
    graph = {
        "version": 2,
        "nodes": [{"id": index, "type": "File",
                   "attrs": {"name": f"f{index}.py"}} for index in range(4)],
        "edges": [{"src": 0, "type": "imports", "tgt": 1},
                  {"src": 0, "type": "imports", "tgt": 2},
                  {"src": 1, "type": "imports", "tgt": 2},
                  {"src": 2, "type": "imports", "tgt": 3}],
    }
    aperture = _aperture(limits={"max_nodes": 3, "max_edges": 1,
                                 "truncate": "id"})
    view = V.build_view(graph, aperture)
    assert [node["id"] for node in view["nodes"]] == ["0", "1", "2"]
    assert len(view["edges"]) == 1
    assert view["omitted"]["nodes"] == 1
    assert view["omitted"]["edges"] == 3
    assert view["omitted"]["reason"] == "node cap; edge cap"
    assert view["omitted"]["by"] == {
        "node_cap": 1, "edge_endpoints": 1, "edge_cap": 2}

    degree_cap = _aperture(limits={"max_nodes": 1, "max_edges": 10,
                                   "truncate": "degree"})
    kept = V.build_view(graph, degree_cap)
    assert [node["id"] for node in kept["nodes"]] == ["2"]


def test_dangling_and_duplicate_edges() -> None:
    graph = {
        "version": 2,
        "nodes": [{"id": 0, "type": "File", "attrs": {"name": "a.py"}},
                  {"id": 1, "type": "File", "attrs": {"name": "b.py"}}],
        "edges": [{"src": 0, "type": "imports", "tgt": 1},
                  {"src": 0, "type": "imports", "tgt": 1},
                  {"src": 0, "type": "imports", "tgt": 9}],
    }
    view = V.build_view(graph, _aperture())
    assert len(view["edges"]) == 2
    assert len({edge["id"] for edge in view["edges"]}) == 2
    assert view["omitted"]["edges"] == 1
    assert view["omitted"]["by"] == {"dangling_edges": 1}
    assert "dangling edges" in view["omitted"]["reason"]

    graph["edges"] = list(reversed(graph["edges"]))
    assert V.render_json(V.build_view(graph, _aperture())) == \
        V.render_json(view)


def test_collapse_rules_and_labels() -> None:
    graph = {
        "version": 2,
        "nodes": [
            {"id": 1, "type": "File", "attrs": {"path": "a.py"}},
            {"id": 2, "type": "Symbol", "attrs": {"file": "a.py"}},
            {"id": 3, "type": "Test", "attrs": {"file": "b.py"}},
            {"id": 4, "type": "Symbol", "attrs": {}},
        ],
        "edges": [],
    }
    aperture = _aperture(collapse={
        "groups": {"Test": "tests"},
        "by_attr": {"Symbol": "file"},
        "labels": {"tests": "Tests", "a.py": "File a.py"},
    })
    view = V.build_view(graph, aperture)
    groups = {group["id"]: group for group in view["groups"]}
    assert groups["a.py"]["label"] == "File a.py"
    assert groups["a.py"]["collapsed"] == 1
    assert groups["tests"]["label"] == "Tests"
    assert groups["tests"]["collapsed"] == 1
    assert groups["file"]["collapsed"] == 0
    assert groups["other"]["collapsed"] == 1
    assert view["legend"]["color"].keys() == groups.keys()


def test_missing_attributes_still_have_labels() -> None:
    graph = {"version": 2, "nodes": [{"id": 7, "type": "File", "attrs": {}}],
             "edges": []}
    view = V.build_view(graph, "architecture")
    assert view["nodes"][0]["label"] == "File 7"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layout", A.LAYOUTS)
def test_html_renders_every_layout(layout: str) -> None:
    graph = _dev_graph()
    aperture = _aperture(
        id=f"layout-{layout}", title=f"Layout {layout}",
        resolves={"nodes": ["File", "Symbol", "Test", "Commit"],
                  "edges": ["imports", "defines", "changes"]},
        render={"layout": layout, "seed": 5})
    view = V.build_view(graph, aperture)
    document = view_html.render_html(view)
    assert f'data-layout="{layout}"' in document
    assert 'id="view-data"' in document
    assert "Legend" in document
    assert "Provenance and snapshot" in document
    assert "Omitted" in document
    assert "Node inspector" in document
    assert "<script src=" not in document
    assert "<link " not in document
    assert "http://" not in document and "https://" not in document
    if layout in {"force", "layered", "timeline", "matrix"}:
        assert "<svg" in document
    else:
        assert "<table" in document


def test_html_escapes_labels_and_embedded_json() -> None:
    graph = {
        "version": 2,
        "nodes": [{
            "id": 1, "type": "File",
            "attrs": {
                "name": '<script>alert("x")</script> & <b>',
                "evil": "</script><script>alert(1)</script>",
            },
        }],
        "edges": [],
    }
    view = V.build_view(graph, "architecture")
    document = view_html.render_html(view)
    assert "<script>alert" not in document
    assert "</script><script>alert(1)" not in document
    assert "&lt;script&gt;alert" in document
    assert "&amp;" in document
    assert "&lt;b&gt;" in document
    escaped_title = view_html.render_html(view, title="<b>Title</b>")
    assert "<b>Title</b>" not in escaped_title
    assert "&lt;b&gt;Title&lt;/b&gt;" in escaped_title


def test_html_shows_omitted_warning() -> None:
    aperture = _aperture(limits={"max_nodes": 1, "max_edges": 10,
                                 "truncate": "id"})
    document = view_html.render_html(V.build_view(_dev_graph(), aperture))
    assert 'class="warning"' in document
    assert "node cap" in document
    assert "omitted" in document.lower()


def test_html_timeline_uses_dates() -> None:
    view = V.build_view(_dev_graph(), "timeline")
    document = view_html.render_html(view)
    assert "2026-11-15" in document
    assert "<svg" in document


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_json_html_and_list(tmp_path: Path, capsys,
                                monkeypatch) -> None:
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    root = tmp_path / "repo"
    _write_graph(root, _dev_graph())

    assert view_cli.main(["--aperture", "architecture", "--format", "json",
                          "--root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["aperture"] == "architecture"
    assert payload["nodes"]

    output = tmp_path / "view.html"
    assert view_cli.main(["--aperture", "architecture", "--format", "html",
                          "--root", str(root), "--out", str(output)]) == 0
    document = output.read_text(encoding="utf-8")
    assert document.startswith("<!DOCTYPE html>")
    assert 'id="view-data"' in document

    assert view_cli.main(["--list-apertures", "--format", "json",
                          "--root", str(root)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in listing} == set(A.builtin_apertures())


def test_cli_missing_graph_and_unknown_aperture(tmp_path: Path, capsys,
                                                monkeypatch) -> None:
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    code = view_cli.main(["--root", str(tmp_path / "empty")])
    assert code == 2
    assert "no graph at" in capsys.readouterr().err

    root = tmp_path / "repo"
    _write_graph(root, _dev_graph())
    code = view_cli.main(["--aperture", "not-there", "--root", str(root)])
    assert code == 2
    assert "unknown aperture" in capsys.readouterr().err


def test_cli_accepts_explicit_research_graph(tmp_path: Path, capsys,
                                             monkeypatch) -> None:
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    graph_path = tmp_path / "research.json"
    graph_path.write_text(json.dumps(_research_graph()), encoding="utf-8")
    code = view_cli.main(["--aperture", "evidence", "--format", "json",
                          "--graph", str(graph_path), "--root", str(tmp_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"]["investigation"] == "research-test"
    assert any(node["id"] == "claim-0001" for node in payload["nodes"])


def test_cli_module_entrypoint(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_graph(root, _dev_graph())
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("OZAGIA_STATE_DIR", None)
    result = subprocess.run(
        [sys.executable, "-m", "ozagia.view_cli", "--aperture",
         "architecture", "--format", "json", "--root", str(root)],
        capture_output=True, text=True, env=env, cwd=str(root), timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["aperture"] == "architecture"
    assert payload["nodes"]
