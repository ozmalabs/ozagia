import json
import pytest

from ozagia.core.deep_review import range_review


def _graph():
    return {
        "nodes": [
            {"id": 1, "type": "File", "attrs": {"path": "a.py"}},
            {"id": 2, "type": "File", "attrs": {"path": "b.py"}},
            {"id": 3, "type": "File", "attrs": {"path": "c.py"}},
            {"id": 4, "type": "Symbol", "attrs": {"file": "a.py", "name": "run"}},
            {"id": 5, "type": "Symbol", "attrs": {"file": "b.py", "name": "caller"}},
            {"id": 6, "type": "Symbol", "attrs": {"file": "c.py", "name": "outer"}},
            {"id": 7, "type": "File", "attrs": {"path": "tests/test_a.py"}},
        ],
        "edges": [
            {"src": 3, "type": "imports", "tgt": 1},
            {"src": 1, "type": "imports", "tgt": 2},
            {"src": 5, "type": "calls", "tgt": 4},
            {"src": 6, "type": "calls", "tgt": 5},
            {"src": 7, "type": "tests_module", "tgt": 1},
        ],
    }


def test_range_adapter_normalizes_findings_and_marks_graph_staleness(monkeypatch):
    calls = []

    def review(root, range_spec, *, timeout):
        calls.append((root, range_spec, timeout))
        return {"packet": {
            "findings": [{"id": "CR-001", "type": "security_sensitive",
                          "severity": "blocking", "confidence": "medium",
                          "file": "src/auth.py", "line": 17,
                          "message": "Potential credential issue.",
                          "evidence": ["raw source must not pass through"],
                          "graph_keys": ["node-1"]}],
            "graph": {"freshness": "stale", "usable": False},
            "range": {"base": "abc", "head": "def"},
            "diff": {"files": 1},
            "not_assessed": [{"area": "tests", "reason": "not run"}],
            "omissions": ["large file omitted"],
        }}

    monkeypatch.setattr(range_review, "_bounded_review", review)
    result = range_review.collect_range("/repo", "abc..def", budget_s=2)
    finding = result["findings"][0]
    assert calls == [("/repo", "abc..def", 2)]
    assert finding["section"] == "security"
    assert finding["range_subject"] == "security"
    assert finding["severity"] == "major"
    assert finding["keys"] == ["file:src/auth.py"]
    assert "evidence" not in finding
    assert result["graph"]["freshness"] == "stale"
    assert result["sections"]["correctness"]["checked"] == ["git_range_review"]
    assert result["sections"]["security"]["checked"] == ["git_range_review"]
    assert len(result["sections"]["correctness"]["not_checked"]) == 3
    assert any(item.get("area") == "graph" and "stale" in item["reason"]
               for item in result["sections"]["correctness"]["not_checked"])
    assert result["blast_radius"]["status"] == "omitted"
    assert result["blast_radius"]["graph_freshness"] == "stale"


def test_range_findings_map_to_the_closest_subject(monkeypatch):
    monkeypatch.setattr(range_review, "_bounded_review", lambda *a, **k: {
        "packet": {
            "findings": [
                {"type": "performance_risk", "file": "src/hot.py", "message": "hot"},
                {"type": "missing_test", "file": "src/a.py", "message": "test"},
                {"type": "unhandled_error", "file": "src/a.py", "message": "error"},
                {"type": "unknown", "category": "security", "file": "src/a.py",
                 "message": "auth check"},
                {"type": "unknown", "file": "src/a.py", "message": "plain"},
            ],
            "graph": {"freshness": "absent", "available": False},
        }})
    result = range_review.collect_range("/repo", "base..head")
    by_type = {}
    for finding in result["findings"]:
        by_type.setdefault(finding["type"], []).append(finding["section"])
    assert by_type["performance_risk"] == ["performance"]
    assert by_type["missing_test"] == ["tests"]
    assert by_type["unhandled_error"] == ["failure-modes"]
    assert by_type["unknown"] == ["security", "correctness"]
    assert set(result["sections"]) == {
        "correctness", "performance", "tests", "failure-modes", "security"}
    assert result["blast_radius"]["graph_freshness"] == "absent"


def test_blast_radius_facts_require_a_fresh_graph():
    fresh = range_review.blast_radius_facts(_graph(), ["a.py"], freshness="fresh")
    assert fresh["status"] == "available"
    assert fresh["changed_files"] == ["a.py"]
    assert fresh["changed_symbols"] == 1
    assert fresh["imports"] == [{"file": "a.py", "imports": ["b.py"],
                                 "truncated": False}]
    assert fresh["imported_by"] == [{"file": "a.py", "imported_by": ["c.py"],
                                     "truncated": False}]
    assert fresh["callers"] == {"reachable_symbols": 2,
                               "files": ["b.py", "c.py"],
                               "depth_limit": 3, "truncated": False}
    assert fresh["tests"] == [{"file": "a.py", "tests": ["tests/test_a.py"],
                               "truncated": False}]

    for freshness in ("stale", "absent", "unknown"):
        omitted = range_review.blast_radius_facts(
            _graph(), ["a.py"], freshness=freshness)
        assert omitted["status"] == "omitted"
        assert omitted["graph_freshness"] == freshness
        assert omitted["imports"] == []
        assert omitted["imported_by"] == []
        assert omitted["callers"]["reachable_symbols"] == 0
        assert any(freshness in reason for reason in omitted["omissions"])


def test_fresh_graph_blast_radius_is_preserved_for_a_range_review(monkeypatch):
    blast = range_review.blast_radius_facts(_graph(), ["a.py"], freshness="fresh")
    monkeypatch.setattr(range_review, "_bounded_review", lambda *a, **k: {
        "packet": {"findings": [], "graph": {"freshness": "fresh", "usable": True},
                   "files": [{"path": "a.py"}], "blast_radius": blast}})
    result = range_review.collect_range("/repo", "base..head")
    assert result["blast_radius"]["status"] == "available"
    assert result["blast_radius"]["imported_by"][0]["imported_by"] == ["c.py"]
    assert not any(item.get("area") == "graph"
                   for item in result["sections"]["correctness"]["not_checked"])


def test_packet_blast_radius_reads_a_verified_fresh_graph(monkeypatch, tmp_path):
    from ozagia.core import repo_review

    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(_graph()), encoding="utf-8")
    monkeypatch.setattr(repo_review, "_graph_file",
                        lambda root, path: graph_file)
    monkeypatch.setattr(repo_review, "_load_graph",
                        lambda root, path, head, **kwargs: (
                            {"freshness": "fresh", "usable": True}, None, None))
    packet = {"graph": {"freshness": "fresh", "usable": True},
              "head_commit": "a" * 40, "files": [{"path": "a.py"}]}
    facts = range_review._packet_blast_radius(str(tmp_path), packet)
    assert facts["status"] == "available"
    assert facts["changed_files"] == ["a.py"]
    assert facts["callers"]["reachable_symbols"] == 2

    graph_file.unlink()
    facts = range_review._packet_blast_radius(str(tmp_path), packet)
    assert facts["status"] == "omitted"
    assert facts["graph_freshness"] == "absent"
    assert any("missing" in reason for reason in facts["omissions"])


def test_blast_radius_uses_same_graph_data_that_was_verified(monkeypatch, tmp_path):
    from ozagia.core import repo_review

    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(_graph()), encoding="utf-8")
    monkeypatch.setattr(repo_review, "_graph_file", lambda root, path: graph_file)

    def verify(root, path, head, **kwargs):
        assert kwargs["snapshot_data"] == _graph()
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
        return {"freshness": "fresh", "usable": True}, None, None

    monkeypatch.setattr(repo_review, "_load_graph", verify)
    packet = {"graph": {"freshness": "fresh", "usable": True},
              "head_commit": "a" * 40, "files": [{"path": "a.py"}]}
    facts = range_review._packet_blast_radius(str(tmp_path), packet)
    assert facts["status"] == "available"
    assert facts["callers"]["reachable_symbols"] == 2


def test_security_impact_uses_only_fresh_graph_and_reports_bounded_evidence(
        monkeypatch, tmp_path):
    from ozagia.core import repo_review

    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(_graph()), encoding="utf-8")
    monkeypatch.setattr(repo_review, "_graph_file", lambda root, path: graph_file)
    monkeypatch.setattr(repo_review, "_load_graph", lambda root, path, head, **kwargs: (
        {"freshness": "fresh", "usable": True}, None, None))
    packet = {"graph": {"freshness": "fresh", "usable": True},
              "head_commit": "a" * 40,
              "findings": [{"type": "security_sensitive", "file": "a.py"}]}
    impact = range_review._packet_security_impact(str(tmp_path), packet)
    assert impact["status"] == "available"
    assert impact["changed_files"] == ["a.py"]
    assert {row["kind"] for row in impact["evidence"]} == {
        "imports", "imported_by", "callers", "tests"}
    assert any("runtime reachability" in value for value in impact["unknowns"])

    monkeypatch.setattr(repo_review, "_load_graph", lambda *a: pytest.fail(
        "stale graph must not be read"))
    packet["graph"] = {"freshness": "stale", "usable": False}
    impact = range_review._packet_security_impact(str(tmp_path), packet)
    assert impact["status"] == "omitted"
    assert impact["evidence"] == []
    assert any("stale" in value for value in impact["unknowns"])


def test_range_adapter_includes_redacted_security_impact(monkeypatch):
    monkeypatch.setattr(range_review, "_bounded_review", lambda *a, **k: {
        "packet": {"findings": [{"type": "security_sensitive", "file": "a.py"}],
                   "graph": {"freshness": "stale", "usable": False},
                   "files": [{"path": "a.py"}]}})
    result = range_review.collect_range("/repo", "base..head")
    impact = result["security_impact"]
    assert impact["status"] == "omitted"
    assert impact["changed_files"] == ["a.py"]
    assert impact["evidence"] == []
    assert impact["unknowns"]


def test_range_adapter_rejects_empty_input_without_review(monkeypatch):
    import pytest

    monkeypatch.setattr(range_review, "_bounded_review",
                        lambda *a, **k: pytest.fail("review invoked"))
    with pytest.raises(ValueError, match="BASE..HEAD"):
        range_review.collect_range("/repo", " ")


def test_range_adapter_zero_budget_is_unknown_without_starting_review(monkeypatch):
    import pytest

    monkeypatch.setattr(range_review, "_bounded_review",
                        lambda *a, **k: pytest.fail("review invoked"))
    result = range_review.collect_range("/repo", "abc..def", budget_s=0)
    assert result["findings"] == []
    assert result["sections"]["correctness"]["checked"] == []
    assert result["graph"]["freshness"] == "unknown"
    assert result["blast_radius"]["status"] == "omitted"


def test_range_graph_keys_are_not_rendered_as_file_locations(monkeypatch):
    monkeypatch.setattr(range_review, "_bounded_review", lambda *a, **k: {
        "packet": {"findings": [{"type": "unknown", "graph_keys": ["node-9"],
                                  "message": "Candidate"}],
                   "graph": {"freshness": "fresh"}}})
    result = range_review.collect_range("/repo", "base..head")
    finding = result["findings"][0]
    assert finding["keys"] == []
    assert finding["scope"].endswith("no repository file location.")
    assert "node-9" in finding["rule_key"]


def test_range_adapter_composes_existing_test_plan_without_execution(
        monkeypatch, tmp_path):
    source = tmp_path / "src" / "auth.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(range_review, "_bounded_review", lambda *a, **k: {
        "packet": {"findings": [], "graph": {"freshness": "stale"},
                   "range": {"base": "base", "head": "head"},
                   "test_coverage": {
                       "graph": {"freshness": "stale"},
                       "subjects": [{"file": "src/auth.py",
                                     "state": "graph_stale"}],
                       "executed": False}}})
    result = range_review.collect_range(str(tmp_path), "base..head")
    coverage = result["test_coverage"]
    assert coverage["graph"]["freshness"] == "stale"
    assert coverage["subjects"][0]["state"] == "graph_stale"
    assert coverage["executed"] is False
    assert result["range"] == {"base": "base", "head": "head"}
    assert any("line coverage was not assessed" in item["reason"]
               for item in result["sections"]["correctness"]["not_checked"]
               if isinstance(item, dict) and "reason" in item)


def test_range_adapter_timeout_is_partial_coverage(monkeypatch):
    from ozagia.core import code_review
    monkeypatch.setattr(range_review, "_bounded_review",
        lambda *a, **k: (_ for _ in ()).throw(
            code_review.CodeReviewError("range review time budget exhausted")))
    result = range_review.collect_range("/repo", "base..head", budget_s=1)
    assert result["findings"] == []
    assert result["sections"]["correctness"]["checked"] == []
    assert result["sections"]["correctness"]["not_checked"] == [
        {"reason": "range review time budget exhausted"}]


def test_deep_review_cli_accepts_explicit_range():
    from ozagia.cli import build_parser

    args = build_parser().parse_args([
        "deep-review", "--range", "base..head", "--root", "/repo"])
    assert args.range_spec == "base..head"
