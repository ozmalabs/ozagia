import json

import pytest

from ozagia.core.deep_review import orchestrator
from ozagia.core.deep_review.prioritization import prioritize_findings


def graph():
    return {
        "nodes": [
            {"id": 1, "type": "Symbol", "attrs": {"file": "app.py", "name": "target", "qualname": "Service.target"}},
            {"id": 2, "type": "Symbol", "attrs": {"file": "a.py", "name": "caller_a", "qualname": "caller_a"}},
            {"id": 3, "type": "Symbol", "attrs": {"file": "b.py", "name": "caller_b", "qualname": "caller_b"}},
            {"id": 4, "type": "Symbol", "attrs": {"file": "c.py", "name": "caller_c", "qualname": "caller_c"}},
        ],
        "edges": [{"src": 2, "tgt": 1, "type": "calls"},
                  {"src": 3, "tgt": 2, "type": "calls"},
                  {"src": 4, "tgt": 3, "type": "calls"}],
    }


def finding(severity="minor"):
    return {"id": "f1", "severity": severity,
            "locations": [{"path": "app.py", "symbol": "Service.target"}]}


def test_only_fresh_graph_affects_blast_radius_and_score_is_stable():
    result = prioritize_findings([finding()], graph(), freshness="fresh")[0]
    assert result["priority"]["score"] == 43
    assert result["priority"]["blast_radius"]["reachable_callers"] == 3
    assert result["priority"]["blast_radius"]["caller_files"] == ["a.py", "b.py", "c.py"]
    assert result["priority"]["omission"] is None


@pytest.mark.parametrize("freshness", ["stale", "unknown", "missing"])
def test_unfresh_graph_is_explicitly_omitted(freshness):
    result = prioritize_findings([finding()], graph(), freshness=freshness)[0]
    assert result["priority"]["blast_radius"]["reachable_callers"] == 0
    assert freshness in result["priority"]["omission"]


def test_exact_location_required_and_depth_is_bounded():
    no_symbol = {"id": "f", "severity": "major", "locations": [{"path": "app.py"}]}
    result = prioritize_findings([no_symbol, finding("major")], graph(), freshness="fresh")
    assert "exact path and symbol" in result[1]["priority"]["omission"]
    assert result[0]["priority"]["blast_radius"]["reachable_callers"] == 3
    assert result[0]["priority"]["blast_radius"]["depth_limit"] == 3


def test_oversize_graph_and_excessive_bounds_are_not_used():
    oversized = graph()
    result = prioritize_findings([finding()], oversized, freshness="fresh", max_nodes=2)
    assert "exceeds node or edge bound" in result[0]["priority"]["omission"]
    with pytest.raises(ValueError):
        prioritize_findings([], graph(), freshness="fresh", max_depth=4)


def test_orchestrator_graph_read_requires_freshness_and_stable_file(tmp_path, monkeypatch):
    from ozagia.core import repo_review

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph()))
    monkeypatch.setattr(repo_review, "_graph_file", lambda root, path: graph_path)
    monkeypatch.setattr(orchestrator, "_revision", lambda root: "a" * 40)
    monkeypatch.setattr(repo_review, "_load_graph", lambda root, path, head, **kwargs:
                        ({"freshness": "fresh", "usable": True, "nodes": 4,
                          "edges": 3, "reason": "verified"}, object(), object()))
    snapshot, info = orchestrator._priority_graph_worker(tmp_path)
    assert snapshot["nodes"]
    assert info["freshness"] == "fresh"

    def mutate_during_verification(root, path, head, **kwargs):
        graph_path.write_text("{} " + " " * 40)
        return ({"freshness": "fresh", "usable": True}, object(), object())

    monkeypatch.setattr(repo_review, "_load_graph", mutate_during_verification)
    snapshot, info = orchestrator._priority_graph_worker(tmp_path)
    assert snapshot is None
    assert "changed during freshness verification" in info["reason"]


def test_priority_graph_verification_rejects_symlink_snapshot(tmp_path, monkeypatch):
    from ozagia.core import repo_review

    state = tmp_path / ".ozagia"
    state.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(graph()), encoding="utf-8")
    graph_path = state / "graph.json"
    graph_path.symlink_to(outside)
    monkeypatch.setattr(repo_review, "_graph_file", lambda root, path: graph_path)
    monkeypatch.setattr(orchestrator, "_revision", lambda root: "a" * 40)

    snapshot, info = orchestrator._priority_graph_worker(tmp_path)

    assert snapshot is None
    assert info["freshness"] == "error"
    assert "graph evidence unavailable" in info["reason"]


def test_safe_graph_freshness_does_not_import_project_adapter(tmp_path, monkeypatch):
    import subprocess

    from ozagia import adapters
    from ozagia.core.memory import DevMemory

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "seed.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "seed.json"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm",
                    "initial"], check=True)
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["project_adapter:register"]\n',
        encoding="utf-8")
    graph_path = tmp_path / "graph.json"
    snapshot = {
        "version": 2,
        "nodes": [{"id": 1, "type": "Repo", "attrs": {
            "root": str(root),
            "head": subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True).stdout.strip(),
            "tree_digest": "safe-digest"}}],
        "edges": [], "next_id": 2, "build": {},
        "adapter": {"files": [], "stamps": {}, "watch_stamps": {}},
    }
    graph_path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(
        "ozagia.core.memory._tree_digest",
        lambda *args, **kwargs: ("safe-digest", None))

    def forbidden_import(*args, **kwargs):
        raise AssertionError("project adapter was imported")

    monkeypatch.setattr(adapters, "_import_ref", forbidden_import)
    memory = DevMemory(dump=graph_path, root=root, snapshot_data=snapshot)
    result = memory.staleness(allow_project_adapters=False)

    assert result["freshness"] == "unknown"
    assert any("adapter watch inputs were not loaded" in error
               for error in result["verification_errors"])


def test_priority_graph_verification_is_killable_and_uses_remaining_timeout(
        tmp_path, monkeypatch):
    import subprocess

    observed = {}

    def timed_out(argv, **kwargs):
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(orchestrator.subprocess, "run", timed_out)
    graph_snapshot, info = orchestrator._verified_priority_graph(
        tmp_path, timeout_s=0.37)
    assert graph_snapshot is None
    assert info["freshness"] == "unknown"
    assert "remaining review budget" in info["reason"]
    assert observed["timeout"] == pytest.approx(0.37)
    assert observed["stdin"] is subprocess.DEVNULL


def test_report_exposes_priority_and_symbol_omission(tmp_path, monkeypatch):
    packet = {"findings": [{"section": "security", "type": "candidate",
                            "severity": "major", "keys": ["file:app.py"]}],
              "sections": {"security": {"checked": ["offline scan"]}},
              "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *args, **kwargs: packet)
    monkeypatch.setattr(orchestrator, "_verified_priority_graph",
                        lambda root, **kwargs: (graph(), {"freshness": "fresh", "reason": "test"}))
    out = tmp_path / "review"
    orchestrator.build_deep_review(tmp_path, out, profile="security")
    saved = json.loads((out / "findings.json").read_text())
    assert saved[0]["priority"]["score"] == 70
    assert "exact path and symbol" in saved[0]["priority"]["omission"]
    index = (out / "findings-index.md").read_text()
    assert "Priority graph: fresh" in index
    assert "exact path and symbol" in index
