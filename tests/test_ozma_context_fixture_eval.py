from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ozagia.core.memory import build_index
from ozagia.eval.retrieval import evaluate


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evaluation" / "ozma_context" / "fixture"
TASKS = ROOT / "evaluation" / "ozma_context" / "tasks.json"


def test_ozma_context_fixture_is_reproducible_and_offline(tmp_path):
    repo = tmp_path / "fixture"
    shutil.copytree(FIXTURE, repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "eval@example.test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture Eval"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    state = repo / ".ozagia"
    build_index(repo, state / "graph.json", include_docs="none")

    report = evaluate(
        repo,
        tasks_path=TASKS,
        budgets=[100, 300],
        arms=["baseline", "grep", "dev_context", "code_search"],
        cache_path=state / "eval_tags.json",
        memory_path=state / "graph.json",
    )
    assert report["task_count"] == 1
    assert report["budgets"] == [100, 300]
    assert set(report["arms"]) == {"baseline", "grep", "dev_context", "code_search"}
    assert {r["arm"] for r in report["runs"]} == set(report["arms"])
    assert all(r["budget_respected"] for r in report["runs"])
    assert all(r["recall"] <= 1 for r in report["runs"])
    json.dumps(report)
