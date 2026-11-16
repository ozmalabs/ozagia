"""Tests for optional Ocelot clustering of code findings."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia import ocelot_code_cli as CLI
from ozagia.core import ocelot_code as OC
from ozagia.core.memory import _tree_digest


def _review_finding(ident: str, ftype: str, file: str, *,
                    message: str | None = None,
                    severity: str = "major") -> dict[str, Any]:
    return {
        "id": ident,
        "type": ftype,
        "severity": severity,
        "file": file,
        "line": 1,
        "message": message or ftype,
        "evidence": [f"git diff: {file}"],
        "graph_keys": [],
        "confidence": "high",
        "confidence_note": "test fixture",
    }


def _scan_opportunity(ident: str, category: str, paths: list[str], *,
                      risk: str = "medium") -> dict[str, Any]:
    return {
        "id": ident,
        "category": category,
        "title": f"{category} opportunity",
        "risk": risk,
        "confidence": "medium",
        "proposed_module": paths[0] if paths else "",
        "evidence": [{"kind": "file", "path": path} for path in paths],
        "action": "review it",
        "not_assessed": ["semantic equivalence"],
    }


def _not_installed(_root: Any = None, **_kwargs: Any) -> dict[str, Any]:
    return {
        "state": "not_installed",
        "path": None,
        "version": None,
        "source": None,
        "manifest": None,
        "manifest_path": None,
        "install_root": "/nonexistent/ozagia/ocelot",
        "invalid_installs": [],
        "note": "Ocelot is not installed.",
    }


def _candidate_ocelot() -> Path | None:
    root = Path(__file__).resolve().parents[1]
    for candidate in (root.parent / "ocelot", root.parent.parent / "ocelot"):
        if (candidate / ".venv/bin/python").is_file() and (
                candidate / "seeds" / "analogy_search.json").is_file():
            return candidate
    return None


OCELOT = _candidate_ocelot()


# --------------------------------------------------------------------------- #
# adaptation
# --------------------------------------------------------------------------- #
def test_payload_shape_target_and_family():
    findings = [
        _review_finding("CR-001", "duplicate_logic", "src/x.py"),
        _review_finding("CR-002", "missing_test", "src/x.py"),
    ]
    payload = OC.findings_payload(findings)
    assert payload[0] == {
        "finding_id": "CR-001",
        "run_id": "code_review",
        "target_type": "File",
        "target_id": "src/x.py",
        "family": "duplicate",
        "severity": "major",
        "wording": "duplicate_logic",
    }
    assert payload[1]["family"] == "evidence"


def test_scan_opportunity_targets_a_file_set_and_maps_risk():
    payload = OC.findings_payload([
        _scan_opportunity("dup-1", "duplicate_modules",
                          ["src/b.py", "src/a.py"], risk="high"),
    ])
    assert payload[0]["target_type"] == "FileSet"
    assert payload[0]["target_id"] == "src/a.py|src/b.py"
    assert payload[0]["family"] == "duplicate"
    assert payload[0]["severity"] == "major"
    assert payload[0]["run_id"] == "repo_scan"


def test_explicit_worker_fields_are_preserved():
    payload = OC.findings_payload([{
        "finding_id": "f-1",
        "run_id": "reviewer-a",
        "target_type": "Symbol",
        "target_id": "sym:src/x.py::f",
        "family": "scope",
        "severity": "minor",
        "wording": "chosen wording",
    }])
    assert payload[0] == {
        "finding_id": "f-1",
        "run_id": "reviewer-a",
        "target_type": "Symbol",
        "target_id": "sym:src/x.py::f",
        "family": "scope",
        "severity": "minor",
        "wording": "chosen wording",
    }


def test_duplicate_ids_are_made_unique_in_input_order():
    payload = OC.findings_payload([
        _review_finding("same", "duplicate_logic", "src/x.py"),
        _review_finding("same", "duplicate_logic", "src/y.py"),
    ])
    assert [entry["finding_id"] for entry in payload] == ["same", "same~2"]


def test_findings_without_ids_or_targets_are_not_merged():
    payload = OC.findings_payload([
        {"type": "duplicate_logic", "severity": "major", "message": "one"},
        {"type": "duplicate_logic", "severity": "major", "message": "two"},
    ])
    clusters = OC.exact_clusters(payload)
    assert [cluster["findings"] for cluster in clusters] == [
        ["finding-0001"], ["finding-0002"]]


# --------------------------------------------------------------------------- #
# exact fallback and safety
# --------------------------------------------------------------------------- #
def test_exact_fallback_merges_same_family_and_target_only(monkeypatch):
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    findings = [
        _review_finding("a", "duplicate_logic", "src/x.py"),
        _review_finding("b", "duplicate_logic", "src/x.py"),
        _review_finding("c", "missing_test", "src/x.py"),
        _review_finding("d", "duplicate_logic", "src/y.py"),
    ]
    result = OC.cluster_findings(findings, root="/tmp")
    assert result["state"] == "not_installed"
    assert result["available"] is False
    assert result["engine"] == "exact"
    assert result["truth_assessed"] is False
    groups = {tuple(cluster["findings"]) for cluster in result["clusters"]}
    assert ("a", "b") in groups
    assert ("c",) in groups
    assert ("d",) in groups
    assert result["analogies"] == []
    assert result["ocelot_clusters"] == []


def test_exact_clusters_are_sorted_and_deterministic():
    payload = OC.findings_payload([
        _review_finding("b", "duplicate_logic", "src/z.py"),
        _review_finding("a", "duplicate_logic", "src/z.py"),
        _review_finding("c", "missing_test", "src/a.py"),
    ])
    first = OC.exact_clusters(payload)
    second = OC.exact_clusters(list(reversed(payload)))
    assert first == second
    assert [cluster["findings"] for cluster in first] == [
        ["a", "b"], ["c"]]


def test_missing_explicit_checkout_reports_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    result = OC.cluster_findings(
        [_review_finding("a", "duplicate_logic", "src/x.py")],
        root=tmp_path, ocelot_root=tmp_path / "missing")
    assert result["state"] == "unavailable"
    assert result["available"] is False
    assert "not found" in str(result["error"])
    assert result["clusters"][0]["origin"] == "exact"


def test_resolution_error_is_reported_not_raised(monkeypatch, tmp_path):
    def _broken(_root: Any = None, **_kwargs: Any) -> dict[str, Any]:
        raise OC.ocelot_installer.OcelotError("bad $OZAGIA_OCELOT")

    monkeypatch.setattr(OC.ocelot_installer, "resolve", _broken)
    result = OC.cluster_findings(
        [_review_finding("a", "duplicate_logic", "src/x.py")],
        root=tmp_path)
    assert result["state"] == "error"
    assert result["available"] is False
    assert "bad $OZAGIA_OCELOT" in result["error"]
    assert result["clusters"][0]["findings"] == ["a"]


def test_unexpected_resolution_error_is_reported_not_raised(
        monkeypatch, tmp_path):
    def _broken(_root: Any = None, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("unexpected resolver bug")

    monkeypatch.setattr(OC.ocelot_installer, "resolve", _broken)
    result = OC.cluster_findings(
        [_review_finding("a", "duplicate_logic", "src/x.py")],
        root=tmp_path)
    assert result["state"] == "error"
    assert result["available"] is False
    assert "unexpected resolver bug" in result["error"]
    assert result["clusters"][0]["findings"] == ["a"]


def test_worker_output_is_split_by_family_and_target(monkeypatch, tmp_path):
    checkout = tmp_path / "ocelot"
    checkout.mkdir()

    def _worker(_payload, *, ocelot_root, mode, timeout):
        assert ocelot_root == checkout
        assert mode == "auto"
        assert timeout == 120
        return {
            "clusters": [
                ["a", "b"],
                ["a", "c"],
                ["b", "d"],
                ["unknown", "a"],
            ],
            "analogies": [
                {"source_finding": "a", "target_finding": "b",
                 "order_n": 2},
                {"source_finding": "a", "target_finding": "c",
                 "order_n": 3},
                {"source_finding": "b", "target_finding": "d",
                 "order_n": 3},
                {"source_finding": "a", "target_finding": "unknown",
                 "order_n": 4},
            ],
            "engine": "ocelot:test",
        }, "subprocess"

    monkeypatch.setattr(OC, "_worker_output", _worker)
    findings = [
        _review_finding("a", "duplicate_logic", "src/x.py"),
        _review_finding("b", "duplicate_logic", "src/x.py"),
        _review_finding("c", "missing_test", "src/x.py"),
        _review_finding("d", "duplicate_logic", "src/y.py"),
    ]
    result = OC.cluster_findings(
        findings, root=tmp_path, ocelot_root=checkout)
    assert result["state"] == "ready"
    assert result["available"] is True
    assert result["actual_mode"] == "subprocess"
    assert result["engine"] == "ocelot:test"
    groups = {tuple(cluster["findings"]) for cluster in result["clusters"]}
    assert groups == {("a", "b"), ("c",), ("d",)}
    assert result["ocelot_clusters"] == [["a", "b"]]
    assert [(item["source_finding"], item["target_finding"])
            for item in result["analogies"]] == [("a", "b")]
    assert result["analogies"][0]["family"] == "duplicate"
    assert result["truth_assessed"] is False
    merged = next(cluster for cluster in result["clusters"]
                  if cluster["findings"] == ["a", "b"])
    assert merged["origin"] == "exact+ocelot"
    assert merged["analogy_count"] == 1


def test_worker_failure_reports_error_and_keeps_exact_clusters(
        monkeypatch, tmp_path):
    checkout = tmp_path / "ocelot"
    checkout.mkdir()

    def _broken(*_args, **_kwargs):
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(OC, "_worker_output", _broken)
    result = OC.cluster_findings(
        [_review_finding("a", "duplicate_logic", "src/x.py")],
        root=tmp_path, ocelot_root=checkout)
    assert result["state"] == "error"
    assert result["available"] is False
    assert result["engine"] == "exact"
    assert "worker crashed" in result["error"]
    assert result["clusters"][0]["findings"] == ["a"]


def test_no_findings_does_not_call_ocelot(monkeypatch, tmp_path):
    def _unexpected(*_args, **_kwargs):
        raise AssertionError("Ocelot must not be called without findings")

    monkeypatch.setattr(OC, "_worker_output", _unexpected)
    result = OC.cluster_findings([], root=tmp_path)
    assert result["state"] == "no_findings"
    assert result["available"] is False
    assert result["clusters"] == []
    assert result["finding_count"] == 0


def test_payload_is_bounded_and_reports_omitted_findings(
        monkeypatch, tmp_path):
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    findings = [
        _review_finding(f"f-{index}", "duplicate_logic", f"src/{index}.py")
        for index in range(OC.MAX_FINDINGS + 2)
    ]
    result = OC.cluster_findings(findings, root=tmp_path)
    assert result["finding_count"] == OC.MAX_FINDINGS
    assert result["truncated"] is True
    assert result["omitted_findings"] == 2


def test_invalid_mode_and_timeout_are_caller_errors(tmp_path):
    with pytest.raises(OC.OcelotCodeError):
        OC.cluster_findings([], root=tmp_path, mode="remote")
    with pytest.raises(OC.OcelotCodeError):
        OC.cluster_findings([], root=tmp_path, timeout=0)


def test_cluster_section_catches_an_injected_failure():
    section = OC.cluster_section(
        [_review_finding("a", "duplicate_logic", "src/x.py")],
        clusterer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("bad clusterer")))
    assert section["state"] == "error"
    assert section["available"] is False
    assert section["truth_assessed"] is False
    assert section["clusters"][0]["findings"] == ["a"]
    assert "bad clusterer" in section["error"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _write_findings(tmp_path: Path) -> Path:
    path = tmp_path / "findings.json"
    path.write_text(json.dumps([
        _review_finding("a", "duplicate_logic", "src/x.py"),
        _review_finding("b", "duplicate_logic", "src/x.py"),
        _review_finding("c", "missing_test", "src/x.py"),
    ]), encoding="utf-8")
    return path


def test_cli_cluster_json_and_exit_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    findings = _write_findings(tmp_path)
    code = CLI.main(["cluster", "--root", str(tmp_path),
                     "--findings", str(findings), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["schema"] == OC.SCHEMA
    assert payload["state"] == "not_installed"
    assert payload["truth_assessed"] is False
    assert {tuple(cluster["findings"]) for cluster in payload["clusters"]} == {
        ("a", "b"), ("c",)}


def test_cli_cluster_text_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    findings = _write_findings(tmp_path)
    code = CLI.main(["cluster", "--root", str(tmp_path),
                     "--findings", str(findings)])
    text = capsys.readouterr().out
    assert code == 0
    assert "Ocelot code clustering" in text
    assert "state: not_installed" in text
    assert "truth_assessed: false" in text


def test_cli_cluster_usage_and_input_errors(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    code = CLI.main(["cluster", "--root", str(tmp_path),
                     "--findings", str(missing)])
    assert code == 2
    assert "cannot read findings" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert CLI.main(["cluster", "--root", str(tmp_path),
                     "--findings", str(bad)]) == 2
    assert "not valid JSON" in capsys.readouterr().err

    shape = tmp_path / "shape.json"
    shape.write_text('{"other": []}', encoding="utf-8")
    assert CLI.main(["cluster", "--root", str(tmp_path),
                     "--findings", str(shape)]) == 2
    assert "no findings/opportunities" in capsys.readouterr().err


def test_cli_status_json_uses_the_resolver(monkeypatch, capsys):
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    code = CLI.main(["status", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["state"] == "not_installed"
    assert payload["license"]["spdx"] == "MIT"
    assert payload["reconciliation_optional"] is True


# --------------------------------------------------------------------------- #
# optional sibling checkout
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(OCELOT is None,
                    reason="sibling Ocelot checkout is unavailable")
def test_real_ocelot_clusters_same_shape_findings(tmp_path):
    findings = [
        _review_finding("a", "duplicate_logic", "src/x.py"),
        {**_review_finding("b", "duplicate_logic", "src/x.py"),
         "run_id": "reviewer-b"},
        {**_review_finding("c", "duplicate_logic", "src/y.py"),
         "run_id": "reviewer-b"},
    ]
    result = OC.cluster_findings(
        findings, root=tmp_path, ocelot_root=OCELOT, mode="subprocess")
    assert result["state"] == "ready"
    assert result["available"] is True
    assert result["engine"].startswith("ocelot")
    groups = {tuple(cluster["findings"]) for cluster in result["clusters"]}
    assert ("a", "b") in groups
    assert ("c",) in groups
    assert result["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# change shapes and test-impact clusters
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str, check: bool = True) -> str:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Shape Tester",
        "GIT_AUTHOR_EMAIL": "shape@example.com",
        "GIT_COMMITTER_NAME": "Shape Tester",
        "GIT_COMMITTER_EMAIL": "shape@example.com",
    })
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env=env, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def _write_file(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _write_shape_graph(root: Path, head: str) -> Path:
    graph_path = root / ".ozagia" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    digest, error = _tree_digest(root, dump=graph_path)
    assert error is None and digest
    nodes = [
        {"id": 0, "type": "Repo", "key": "repo:",
         "attrs": {"root": str(root.resolve()), "head": head,
                   "tree_digest": digest}},
        {"id": 1, "type": "File", "key": "file:src/app.py",
         "attrs": {"path": "src/app.py"}},
        {"id": 2, "type": "Symbol", "key": "sym:src/app.py::f",
         "attrs": {"file": "src/app.py", "qualname": "f",
                   "start_line": 1, "end_line": 2}},
        {"id": 3, "type": "Symbol", "key": "sym:src/app.py::g",
         "attrs": {"file": "src/app.py", "qualname": "g",
                   "start_line": 4, "end_line": 5}},
        {"id": 4, "type": "File", "key": "file:src/other.py",
         "attrs": {"path": "src/other.py"}},
        {"id": 5, "type": "Symbol", "key": "sym:src/other.py::h",
         "attrs": {"file": "src/other.py", "qualname": "h",
                   "start_line": 1, "end_line": 2}},
        {"id": 6, "type": "Test", "key": "test:tests/test_app.py::test_f",
         "attrs": {"file": "tests/test_app.py", "qualname": "test_f"}},
        {"id": 7, "type": "File", "key": "file:tests/test_app.py",
         "attrs": {"path": "tests/test_app.py"}},
        {"id": 8, "type": "File", "key": "file:README.md",
         "attrs": {"path": "README.md"}},
    ]
    edges = [
        {"src": 1, "type": "defines", "tgt": 2},
        {"src": 1, "type": "defines", "tgt": 3},
        {"src": 4, "type": "defines", "tgt": 5},
        {"src": 1, "type": "imports", "tgt": 4},
        {"src": 4, "type": "imports", "tgt": 1},
        {"src": 6, "type": "tests_module", "tgt": 1},
        {"src": 1, "type": "has_test", "tgt": 6},
        {"src": 7, "type": "defines", "tgt": 6},
    ]
    graph_path.write_text(json.dumps({
        "version": 2, "nodes": nodes, "edges": edges, "next_id": 9,
        "build": {}, "files": {},
    }), encoding="utf-8")
    return graph_path


@pytest.fixture
def shape_repo(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "repo"
    root.mkdir()
    _write_file(root, ".gitignore", ".ozagia/\n")
    _write_file(root, "src/app.py", "def f():\n    return 1\n")
    _write_file(root, "src/other.py", "def h():\n    return 2\n")
    _write_file(root, "tests/test_app.py",
                "def test_f():\n    assert True\n")
    _write_file(root, "README.md", "# Fixture\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Shape Tester")
    _git(root, "config", "user.email", "shape@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    base = _commit(root, "base")
    _write_file(root, "src/app.py",
                "def f():\n    return 2\n\n\ndef g():\n    return 3\n")
    head = _commit(root, "head")
    graph = _write_shape_graph(root, head)
    return {"root": root, "base": base, "head": head, "graph": graph}


def _minimal_shape(shape_id: str, files: list[str], *,
                   family: str = "source") -> dict[str, Any]:
    kinds = {path: family for path in files}
    return {
        "shape_id": shape_id,
        "changed_files": files,
        "changed_kinds": kinds,
        "family": family,
        "package_dirs": sorted({str(Path(path).parent).replace("\\", "/")
                                for path in files} - {"."}),
        "suffixes": sorted({Path(path).suffix for path in files}),
    }


def test_change_shape_is_deterministic_and_carries_graph_evidence(
        shape_repo) -> None:
    root = shape_repo["root"]
    first = OC.change_shape(root, changed=["src/app.py"])
    second = OC.change_shape(root, changed=["src/app.py"])
    assert first["digest"] == second["digest"]
    assert first["shape_id"] == second["shape_id"]
    assert first["graph"]["usable"] is True
    assert first["family"] == "source"
    assert first["changed_kinds"] == {"src/app.py": "source"}
    assert [row["qualname"] for row in first["symbols"]] == ["f", "g"]
    assert {"defines", "imports", "has_test", "tests_module"} <= set(
        first["edge_types"])
    assert first["package_dirs"] == ["src"]
    assert first["import_dirs"] == ["src"]
    assert first["test_files"] == ["tests/test_app.py"]
    link = first["test_links"][0]
    assert link["source"] == "graph"
    assert link["node_ids"] == ["test:tests/test_app.py::test_f"]
    assert first["truth_assessed"] is False


def test_change_shape_separates_families_and_digests(shape_repo) -> None:
    root = shape_repo["root"]
    source = OC.change_shape(root, changed=["src/app.py"])
    test = OC.change_shape(root, changed=["tests/test_app.py"])
    docs = OC.change_shape(root, changed=["README.md"])
    assert len({source["digest"], test["digest"], docs["digest"]}) == 3
    assert (source["family"], test["family"], docs["family"]) == (
        "source", "test", "docs")
    assert test["test_links"][0]["source"] == "changed_test"
    assert docs["test_files"] == []


def test_change_shape_accepts_a_diff_range_and_attaches_a_test_plan(
        shape_repo) -> None:
    shape = OC.change_shape(
        shape_repo["root"], diff=f"{shape_repo['base']}..{shape_repo['head']}",
        with_test_plan=True)
    assert shape["changed_files"] == ["src/app.py"]
    assert shape["range"]["source"] == "diff"
    assert shape["test_plan"]["available"] is True
    assert shape["test_plan"]["executed"] is False
    assert shape["planned_tests"] == ["tests/test_app.py"]


def test_change_shape_without_graph_uses_filename_test_links(
        tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_file(root, "src/app.py", "x = 1\n")
    _write_file(root, "tests/test_app.py", "def test_x(): pass\n")
    shape = OC.change_shape(root, changed=["src/app.py"])
    assert shape["graph"]["freshness"] == "missing"
    assert shape["symbols"] == []
    assert shape["edge_types"] == []
    assert shape["test_links"][0]["source"] == "filename"
    assert shape["test_files"] == ["tests/test_app.py"]
    assert any("not assessed" in item for item in shape["not_assessed"])


def test_change_shape_rejects_ambiguous_or_bad_ranges(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(OC.OcelotCodeError):
        OC.change_shape(root, diff="main..HEAD", changed=["src/app.py"])
    with pytest.raises(OC.OcelotCodeError):
        OC.change_shape(root, diff="main")
    with pytest.raises(OC.OcelotCodeError):
        OC.change_shape(tmp_path / "missing")


def test_test_impact_clusters_exact_fallback_and_no_completeness_claim(
        monkeypatch, shape_repo) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    source = OC.change_shape(shape_repo["root"], changed=["src/app.py"])
    copy = {**source, "shape_id": "shape-copy"}
    test = OC.change_shape(
        shape_repo["root"], changed=["tests/test_app.py"],)
    result = OC.test_impact_clusters(
        [source, copy, test], ocelot_root=shape_repo["root"] / "missing")
    assert result["state"] == "unavailable"
    assert result["engine"] == "exact"
    assert result["truth_assessed"] is False
    assert result["test_set_complete"] is False
    assert result["tests_executed"] is False
    assert "complete" in result["claim"]
    clusters = {tuple(cluster["shape_ids"]): cluster
                for cluster in result["clusters"]}
    assert (source["shape_id"], "shape-copy") in clusters
    assert (test["shape_id"],) in clusters
    merged = clusters[(source["shape_id"], "shape-copy")]
    assert merged["family"] == "source"
    assert merged["origin"] == "exact"
    assert merged["candidate_tests"] == ["tests/test_app.py"]
    assert merged["test_set_complete"] is False
    assert (test["shape_id"],) not in clusters[(source["shape_id"],
                                               "shape-copy")]["shape_ids"]


def test_test_impact_clusters_overlap_groups_similar_source_shapes(
        monkeypatch) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    left = _minimal_shape("a", ["src/app.py"])
    right = _minimal_shape("b", ["src/other.py"])
    result = OC.test_impact_clusters(
        [left, right], ocelot_root="/nonexistent", min_overlap=0.25)
    assert result["state"] == "unavailable"
    assert [cluster["shape_ids"] for cluster in result["clusters"]] == [["a", "b"]]
    cluster = result["clusters"][0]
    assert cluster["origin"] == "overlap"
    assert cluster["confidence"] == "medium"
    assert "shared package_dirs" in " ".join(cluster["evidence"])


def test_test_impact_clusters_fake_clusterer_filters_cross_family() -> None:
    calls: list[list[str]] = []

    def _clusterer(shapes, *, ocelot_root, mode):
        calls.append([shape["shape_id"] for shape in shapes])
        return {
            "engine": "fake-ocelot",
            "state": "ready",
            "available": True,
            "analogies": [
                {"source_shape": "a", "target_shape": "b", "order_n": 3},
                {"source_shape": "a", "target_shape": "c", "order_n": 2},
                {"source_shape": "a", "target_shape": "missing"},
            ],
        }

    left = _minimal_shape("a", ["src/a.py"])
    right = _minimal_shape("b", ["lib/b.py"])
    test = _minimal_shape("c", ["tests/test_c.py"], family="test")
    result = OC.test_impact_clusters(
        [left, right, test], clusterer=_clusterer)
    assert calls == [["a", "b", "c"]]
    assert result["engine"] == "fake-ocelot"
    assert result["truth_assessed"] is False
    clusters = {tuple(cluster["shape_ids"]) for cluster in result["clusters"]}
    assert ("a", "b") in clusters
    assert ("c",) in clusters
    assert not any("c" in cluster and "a" in cluster for cluster in clusters)
    assert [(item["source_shape"], item["target_shape"])
            for item in result["analogies"]] == [("a", "b")]


def test_test_impact_clusters_failure_is_reported() -> None:
    def _broken(*_args, **_kwargs):
        raise RuntimeError("bad clusterer")

    result = OC.test_impact_clusters(
        [_minimal_shape("a", ["src/a.py"])], clusterer=_broken)
    assert result["state"] == "error"
    assert "bad clusterer" in result["error"]
    assert result["truth_assessed"] is False
    assert result["clusters"][0]["shape_ids"] == ["a"]


def test_test_impact_clusters_empty_input_does_not_resolve(monkeypatch) -> None:
    def _unexpected(*_args, **_kwargs):
        raise AssertionError("Ocelot must not be called without shapes")

    monkeypatch.setattr(OC.ocelot_installer, "resolve", _unexpected)
    result = OC.test_impact_clusters([])
    assert result["state"] == "no_shapes"
    assert result["clusters"] == []
    assert result["shape_count"] == 0


# --------------------------------------------------------------------------- #
# architecture patterns
# --------------------------------------------------------------------------- #
def _opportunity(ident: str, category: str, files: list[str]) -> dict[str, Any]:
    return {
        "id": ident,
        "category": category,
        "title": f"{category} candidate",
        "risk": "medium",
        "proposed_module": files[0],
        "evidence": [{"kind": "file", "path": path} for path in files],
    }


def test_architecture_patterns_groups_opportunities_without_cross_family(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    items = [
        _opportunity("dup-a", "duplicate_modules", ["src/a.py"]),
        _opportunity("dup-b", "duplicate_modules", ["src/b.py"]),
        _opportunity("weak", "weak_boundaries", ["src/c.py"]),
        _opportunity("docs", "duplicate_docs", ["docs/a.md"]),
    ]
    result = OC.architecture_patterns(
        items, root=tmp_path, ocelot_root=tmp_path / "missing")
    assert result["state"] == "unavailable"
    assert result["truth_assessed"] is False
    assert result["candidate"] is True
    assert "not a verdict" in result["claim"]
    families = {cluster["family"]: cluster for cluster in result["patterns"]}
    assert "opportunity:duplicate_modules" in families
    assert families["opportunity:duplicate_modules"]["shape_ids"] == [
        "dup-a", "dup-b"]
    assert families["opportunity:weak_boundaries"]["shape_ids"] == ["weak"]
    assert families["opportunity:duplicate_docs"]["shape_ids"] == ["docs"]
    assert all(cluster["truth_assessed"] is False
               for cluster in result["patterns"])


def test_architecture_patterns_graph_file_shapes(monkeypatch,
                                                 shape_repo) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    result = OC.architecture_patterns(
        None, root=shape_repo["root"], ocelot_root="/nonexistent")
    assert result["graph"]["usable"] is True
    assert result["state"] == "unavailable"
    assert result["pattern_count"] >= 1
    source = next(cluster for cluster in result["patterns"]
                  if "src/app.py" in cluster["files"])
    assert source["family"] == "file:source"
    assert "f" in source["symbols"] or "g" in source["symbols"]
    assert source["truth_assessed"] is False


def test_architecture_patterns_fake_clusterer_and_cross_family_filter() -> None:
    def _clusterer(shapes, *, ocelot_root, mode):
        return {
            "engine": "fake-patterns",
            "analogies": [
                {"source_shape": "file:src/a.py",
                 "target_shape": "file:src/b.py"},
                {"source_shape": "file:src/a.py",
                 "target_shape": "file:tests/test_a.py"},
            ],
        }

    result = OC.architecture_patterns(
        ["src/a.py", "src/b.py", "tests/test_a.py"], clusterer=_clusterer)
    assert result["engine"] == "fake-patterns"
    clusters = {tuple(cluster["shape_ids"]) for cluster in result["patterns"]}
    assert ("file:src/a.py", "file:src/b.py") in clusters
    assert ("file:tests/test_a.py",) in clusters
    assert result["truth_assessed"] is False
    assert all(cluster["candidate"] is True
               for cluster in result["patterns"])


def test_architecture_patterns_report_mapping_and_root_output(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    report = {
        "root": str(tmp_path),
        "opportunities": [
            _opportunity("one", "duplicate_modules", ["src/a.py"]),
            _opportunity("two", "duplicate_modules", ["src/b.py"]),
        ],
    }
    result = OC.architecture_patterns(report, ocelot_root="/nonexistent")
    assert result["shape_count"] == 2
    assert result["patterns"][0]["shape_ids"] == ["one", "two"]


# --------------------------------------------------------------------------- #
# CLI: impact and patterns
# --------------------------------------------------------------------------- #
def test_cli_impact_json(monkeypatch, shape_repo, capsys) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    code = CLI.main([
        "impact", "--root", str(shape_repo["root"]),
        "--diff", f"{shape_repo['base']}..{shape_repo['head']}", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == OC.IMPACT_SCHEMA
    assert payload["state"] == "not_installed"
    assert payload["truth_assessed"] is False
    assert payload["test_set_complete"] is False
    assert payload["clusters"][0]["candidate_tests"] == ["tests/test_app.py"]


def test_cli_impact_text_and_usage_errors(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    root = tmp_path / "repo"
    root.mkdir()
    _write_file(root, "src/app.py", "x = 1\n")
    code = CLI.main(["impact", "--root", str(root),
                     "--changed", "src/app.py"])
    text = capsys.readouterr().out
    assert code == 0
    assert "Ocelot test-impact clusters" in text
    assert "test_set_complete: false" in text
    assert "truth_assessed: false" in text

    assert CLI.main(["impact", "--root", str(root)]) == 2
    assert "needs --diff" in capsys.readouterr().err


def test_cli_patterns_report_and_root(monkeypatch, shape_repo, tmp_path,
                                      capsys) -> None:
    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    report = {
        "root": str(shape_repo["root"]),
        "opportunities": [
            _opportunity("dup-a", "duplicate_modules", ["src/app.py"]),
            _opportunity("dup-b", "duplicate_modules", ["src/other.py"]),
        ],
    }
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert CLI.main(["patterns", "--report", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == OC.PATTERN_SCHEMA
    assert payload["truth_assessed"] is False
    assert payload["candidate"] is True
    assert payload["patterns"][0]["shape_ids"] == ["dup-a", "dup-b"]

    assert CLI.main([
        "patterns", "--root", str(shape_repo["root"]),
        "--graph", str(shape_repo["graph"]), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shape_count"] >= 3
    assert payload["patterns"]
    assert "candidate" in payload["claim"]


def test_cli_patterns_input_errors(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.json"
    assert CLI.main(["patterns", "--report", str(missing)]) == 2
    assert "cannot read report" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert CLI.main(["patterns", "--report", str(bad)]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    shape = tmp_path / "shape.json"
    shape.write_text('{"other": []}', encoding="utf-8")
    assert CLI.main(["patterns", "--report", str(shape)]) == 2
    assert "no opportunities/items/files" in capsys.readouterr().err
