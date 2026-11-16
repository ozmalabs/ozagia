"""Tests for optional Ocelot clustering of code findings."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ozagia import ocelot_code_cli as CLI
from ozagia.core import ocelot_code as OC


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
