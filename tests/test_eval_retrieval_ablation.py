"""Offline whole-repository/diff and graph-reranking ablation fixture."""
from __future__ import annotations

import json
from pathlib import Path

from ozagia.eval.retrieval_ablation import evaluate


def test_fixture_compares_candidate_scope_and_graph_order(tmp_path: Path) -> None:
    for name, content in {
        "alpha.py": "def retry_budget():\n    return 4\n",
        "beta.py": "def retry_helper():\n    return retry_budget()\n",
        "unrelated.py": "def unrelated():\n    return 0\n",
    }.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{
        "id": "retry", "task": "Find the retry budget calculation",
        "expected_files": ["alpha.py"],
        "diff_files": ["alpha.py", "beta.py"],
    }]}), encoding="utf-8")

    report = evaluate(tmp_path, tasks, budgets=[500],
                      cache_dir=tmp_path / "cache")
    assert report["task_count"] == 1
    assert report["source_file_count"] == 3
    assert len(report["runs"]) == 4
    rows = {(r["scope"], r["graph"]): r for r in report["runs"]}
    assert rows[("diff", "aware")]["candidate_count"] == 2
    assert rows[("whole_repo", "aware")]["candidate_count"] == 3
    assert rows[("diff", "aware")]["recall"] == 1.0
    assert rows[("diff", "less")]["recall"] == 1.0
    assert {r["graph"] for r in report["aggregates"]} == {"aware", "less"}
    assert "not detected git diffs" in report["limitations"][0]


def test_diff_paths_must_be_explicit_and_present(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("pass\n", encoding="utf-8")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{
        "id": "retry", "task": "Find retry behavior",
        "expected_files": ["alpha.py"], "diff_files": ["missing.py"],
    }]}), encoding="utf-8")
    try:
        evaluate(tmp_path, tasks, budgets=[100], cache_dir=tmp_path / "cache")
    except ValueError as exc:
        assert "missing.py" in str(exc)
    else:
        raise AssertionError("missing diff path was accepted")
