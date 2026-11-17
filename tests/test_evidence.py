"""Tests for the first-stage read-only evidence collector."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.core import contracts as C
from ozagia.core import evidence as E
from ozagia.core import goals as G
from ozagia.core import working_set as W

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TEST_KEY = "test:tests/test_core.py::test_run"
CANARY = "sk-CANARY-0123456789abcdef"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    _write(path, json.dumps(value))


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root / "README.md", "# fixture\n")
    _write(root / "pkg" / "core.py", "def run(x):\n    return x + 1\n")
    goal = {
        "id": "g1",
        "title": f"fix the failing test {CANARY}",
        "why": f"the prompt-like text {CANARY}",
        "status": "open",
        "targets": [],
        "scope": [],
        "criteria": [{
            "id": "c1", "kind": "test_passes", "args": {"key": TEST_KEY},
            "last": {"status": "fail", "evidence": {},
                     "reason": f"failed with {CANARY}",
                     "at": 1_700_000_000.0},
        }],
        "claimed_by": [], "history": [], "source": None,
        "created_at": 1, "updated_at": 1, "checked_at": 1,
    }
    _write_json(G.goals_path(root),
                {"version": 1, "next_id": 2, "goals": [goal]})
    _write_json(state_path(root, *W.TEST_RECORD), {
        "tests": {}, "last": None,
        "runs": [{
            "ts": 1_700_000_000.0, "selectors": ["tests/test_core.py"],
            "failed": [TEST_KEY], "errors": [], "complete": True,
            "filtered": False, "stops": False, "source": "captured",
        }],
    })
    run_dir = state_path(root, "tasks", "run-1")
    _write_json(run_dir / "manifest.json", {
        "schema": 1, "run_id": "run-1", "status": "failed", "stop": "error",
        "started_at": "2026-11-17T00:00:00Z",
        "finished_at": "2026-11-17T00:00:02Z", "duration_s": 2.5,
        "steps": [], "evidence_count": 1,
        "spec": {"id": "task-1", "version": 1},
        "output": {"name": "result", "digest": "b" * 64},
    })
    _write_json(run_dir / "artifacts.json", {
        "artifacts": [{"path": "reports/out.txt", "sha256": "a" * 64}],
    })
    _write_json(run_dir / "output.json", {"value": CANARY})
    _write(run_dir / "events.jsonl", "\n".join([
        json.dumps({"seq": 1, "type": "evidence", "kind": "note",
                    "label": CANARY}),
        json.dumps({"seq": 2, "type": "log", "message": CANARY}),
        json.dumps({"seq": 3, "type": "run_finished", "status": "failed",
                    "detail": CANARY}),
    ]) + "\n")
    return root.resolve()


def _sources(**overrides) -> E.Sources:
    defaults = {
        "interpreters": lambda root: [{
            "path": sys.executable, "source": "ozagia",
            "evidence": "the interpreter ozagia runs under",
            "project": False, "also": [],
        }],
    }
    defaults.update(overrides)
    return E.Sources(**defaults)


def collect(root: Path, **kwargs):
    kwargs.setdefault("sources", _sources())
    kwargs.setdefault("observed_at", 1_700_000_000_000)
    return E.collect_evidence(root, "g1", **kwargs)


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------


def test_collects_goal_criteria_runs_tests_and_environment(project):
    report = collect(project, run_ids=("run-1", "run-2"))
    assert report["schema"] == {"namespace": "ozagia.evidence",
                               "version": "1.0"}
    assert report["goal"]["state"] == "present"
    assert report["goal"]["status"] == "open"
    assert report["criteria"][0]["result"] == "failed"
    assert report["criteria"][0]["test"]["outcome"] == "failed"
    assert report["criteria"][0]["interpreter"]["state"] == "not_recorded"
    assert report["test_record"]["state"] == "present"
    assert len(report["test_record"]["digest"]) == 64
    assert report["test_keys"][0]["key"] == TEST_KEY
    assert report["test_keys"][0]["outcome"] == "failed"
    assert report["environment"][0]["exists"] is True
    assert report["environment"][0]["trusted"] is True
    run = report["runs"][0]
    assert run["run_id"] == "run-1"
    assert run["state"] == "present"
    assert run["status"] == "failed"
    assert run["duration_ms"] == 2500
    assert len(run["digest"]) == 64
    assert run["artifacts"] == [{"path": "reports/out.txt",
                                 "sha256": "a" * 64}]
    assert len(run["output_digest"]) == 64
    assert run["events"]["types"]["log"] == 1
    assert report["coverage"]["complete"] is False
    assert report["deferred"]


def test_absent_run_is_reported_not_dropped(project):
    report = collect(project, run_ids=("run-1", "missing-run"))
    absent = next(row for row in report["runs"]
                  if row["run_id"] == "missing-run")
    assert absent["state"] == "absent"
    assert absent["freshness"] == "absent"
    assert absent["evidence"] == "inferred"
    assert "no task run" in absent["reason"]
    assert report["coverage"]["runs"]["states"]["absent"] == 1
    assert report["coverage"]["runs"]["states"]["present"] == 1


def test_invalid_run_manifest_is_invalid_not_absent(project):
    run_dir = state_path(project, "tasks", "run-bad")
    _write(run_dir / "manifest.json", "{not json")
    report = collect(project, run_ids=("run-bad",))
    row = report["runs"][0]
    assert row["state"] == "invalid"
    assert row["freshness"] == "unknown"
    assert row["reason"]


def test_missing_goal_store_is_absent(tmp_path):
    report = E.collect_evidence(tmp_path, "g1", sources=_sources(),
                                observed_at=1)
    assert report["goal"]["state"] == "absent"
    assert report["criteria"] == []
    assert report["truncated"]["truncated"] is False


def test_unreadable_goal_store_is_invalid(tmp_path):
    path = G.goals_path(tmp_path)
    _write(path, "{not json")
    report = E.collect_evidence(tmp_path, "g1", sources=_sources(),
                                observed_at=1)
    assert report["goal"]["state"] == "invalid"
    assert report["goal"]["reason"]


# ---------------------------------------------------------------------------
# Redaction and hashes
# ---------------------------------------------------------------------------


def test_secret_canary_never_appears(project):
    report = collect(project, run_ids=("run-1",))
    text = E.evidence_json(report)
    assert CANARY not in text
    assert "[redacted]" in text
    assert report["runs"][0]["labels"][0]["label"] == "[redacted]"
    assert json.loads(text) == report


def test_only_hashes_and_redacted_refs_are_stored(project):
    report = collect(project, run_ids=("run-1",))
    run = report["runs"][0]
    text = json.dumps(run)
    assert CANARY not in text
    assert run["output_digest"] == C.content_hash({"value": CANARY})
    assert len(run["output_digest"]) == 64
    assert "message" not in text
    assert "detail" not in text
    assert "title" not in report["goal"]
    assert "why" not in report["goal"]


def test_hashes_are_canonical_and_stable(project):
    first = collect(project, run_ids=("run-1",))
    second = collect(project, run_ids=("run-1",))
    assert E.evidence_hash(first) == E.evidence_hash(second)
    assert E.evidence_hash(first) == C.content_hash(
        {key: value for key, value in first.items() if key != "hash"})
    assert E.evidence_json(first) == C.canonical_json(first)
    assert first["hash"] == E.evidence_hash(first)


def test_report_is_stable_across_hash_seeds(project):
    script = (
        "import sys\n"
        "from ozagia.core import evidence as E\n"
        "sources = E.Sources(interpreters=lambda root: [{"
        "'path': sys.executable, 'source': 'ozagia', 'evidence': 'fixture',"
        "'project': False}])\n"
        "report = E.collect_evidence(r'%s', 'g1', run_ids=('run-1',),"
        " sources=sources, observed_at=1)\n"
        "print(E.evidence_json(report))\n"
    ) % project
    outputs = []
    for seed in ("1", "2", "424242"):
        env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONHASHSEED": seed}
        result = subprocess.run([sys.executable, "-c", script], cwd=REPO,
                                env=env, capture_output=True, text=True,
                                check=True)
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1


# ---------------------------------------------------------------------------
# Bounds and errors
# ---------------------------------------------------------------------------


def test_bounds_and_truncation(project):
    goal = json.loads(G.goals_path(project).read_text())
    criteria = goal["goals"][0]["criteria"]
    criteria.extend([
        {"id": "c2", "kind": "test_fails", "args": {"key": "test:b.py::t2"},
         "last": {"status": "pass", "at": 1_700_000_000.0}},
        {"id": "c3", "kind": "test_passes", "args": {"key": "test:c.py::t3"},
         "last": {"status": "unknown", "at": 1_700_000_000.0}},
    ])
    _write_json(G.goals_path(project), goal)
    report = collect(
        project, run_ids=("run-1", "run-2", "run-3"), max_criteria=1,
        max_runs=1, max_test_keys=1, max_interpreters=1,
        sources=_sources(interpreters=lambda root: [
            {"path": sys.executable, "source": "a", "project": False},
            {"path": "/nonexistent/python", "source": "b", "project": False},
        ]))
    assert report["truncated"]["truncated"] is True
    assert report["truncated"]["reason"] == "limit"
    assert report["truncated"]["omitted"] >= 4
    assert len(report["criteria"]) == 1
    assert len(report["runs"]) == 1
    assert len(report["test_keys"]) == 1
    assert len(report["environment"]) == 1
    assert report["bounds"]["runs_omitted"] == 2
    assert report["bounds"]["criteria_omitted"] == 2


def test_typed_request_errors(project):
    with pytest.raises(E.EvidenceError):
        E.collect_evidence(project / "missing", "g1")
    with pytest.raises(E.EvidenceError):
        E.collect_evidence(project, "Not A Goal")
    with pytest.raises(E.EvidenceError):
        E.collect_evidence(project, "g1", run_ids=("bad id",))
    with pytest.raises(E.EvidenceError):
        E.collect_evidence(project, "g1", max_runs=0)
    with pytest.raises(E.EvidenceError):
        E.collect_evidence(project, "g1", max_runs=E.MAX_RUNS + 1)
    with pytest.raises(E.EvidenceError):
        E.collect_evidence(project, "g1", observed_at=True)  # type: ignore[arg-type]


def test_collector_does_not_write(project):
    before = {
        path.relative_to(project).as_posix(): (path.stat().st_size,
                                               path.stat().st_mtime_ns)
        for path in project.rglob("*") if path.is_file()
    }
    collect(project, run_ids=("run-1", "run-2"))
    after = {
        path.relative_to(project).as_posix(): (path.stat().st_size,
                                               path.stat().st_mtime_ns)
        for path in project.rglob("*") if path.is_file()
    }
    assert after == before
    assert G.goals_path(project).is_file()
    assert not (project / "tasks").exists()


# ---------------------------------------------------------------------------
# Optional durable-store join (additive; the collector stays read-only)
# ---------------------------------------------------------------------------


def test_store_join_is_opt_in_and_absent_by_default(project):
    from ozagia.core import evidence_store as ES

    base = collect(project, run_ids=("run-1",))
    assert "durable" not in base
    attached = E.attach_store(base, project)
    assert attached["durable"]["state"] == "absent"
    assert attached["durable"]["records"] == []
    assert attached["durable"]["writes"].startswith("separate store")
    assert attached["deferred"] == base["deferred"]
    assert attached["hash"] == E.evidence_hash(attached)
    assert base["hash"] == E.evidence_hash(base)
    assert ES.journal_path(project).is_file() is False


def test_store_join_returns_current_records_and_marks_partial_writes(project):
    from ozagia.core import evidence_store as ES

    base = collect(project, run_ids=("run-1",))
    ES.record_observation(project, "g1", run_ids=("run-1",),
                          sources=_sources(), now_ms=1_700_000_000_000)
    attached = E.attach_store(base, project)
    assert attached["durable"]["state"] == "current"
    assert attached["durable"]["counts"]["total"] == 1
    assert attached["durable"]["records"][0]["current"] is True
    assert CANARY not in E.evidence_json(attached)

    with ES.journal_path(project).open("ab") as handle:
        handle.write(b'{"torn":')
    partial = E.attach_store(base, project)
    assert partial["durable"]["state"] == "partial"
    assert partial["durable"]["counts"]["total"] == 1
    assert partial["durable"]["partial"]
    assert partial["hash"] == E.evidence_hash(partial)
