"""Tests for the durable evidence store (WP2, M2 evidence spine)."""

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
from ozagia.core import evidence_store as ES
from ozagia.core import goals as G
from ozagia.core import task_preflight as TP
from ozagia.core import working_set as W
from ozagia.core.fingerprint import tree_fingerprint
from ozagia.core.memory import build_index

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t",
       "-c", "commit.gpgsign=false"]
TEST_KEY = "test:tests/test_core.py::test_run"
CANARY = "sk-CANARY-0123456789abcdef"


def _git(root: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(root), *args], check=True,
                   capture_output=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    _write(root / "tests" / "test_core.py",
           "from pkg.core import run\n\n\ndef test_run():\n"
           "    assert run(1) == 3\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
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
            "ts": 1_700_000_000.0,
            "selectors": ["tests/test_core.py"],
            "failed": [TEST_KEY], "errors": [], "complete": True,
            "filtered": False, "stops": False, "source": "captured",
            "tree": tree_fingerprint(root),
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
    ]) + "\n")
    build_index(root, state_path(root, "graph.json"), include_docs="none",
                include_git=False, include_scripts=False)
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


def _observe(root: Path, **kwargs):
    kwargs.setdefault("sources", _sources())
    kwargs.setdefault("run_ids", ("run-1",))
    kwargs.setdefault("now_ms", 1_700_000_000_000)
    return ES.record_observation(root, "g1", **kwargs)


def _fact(value: object | None = None, *, freshness: str = "current"):
    return TP.fact("preflight.fake", value or {"ok": True}, source="tests",
                   freshness=freshness)


def _observation(readiness: bool | str = True, *,
                 freshness: str = "current", data: dict | None = None,
                 reason: str | None = None):
    return TP.Observation(readiness, (_fact(freshness=freshness),),
                          data or {}, reason=reason)


def _preflight(root: Path, *, goal=None, graph=None, environment=None,
               monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    fake = TP.Providers(
        goal=goal or TP.goal_observation,
        graph=graph or (lambda ctx: _observation()),
        capabilities=lambda ctx: _observation(),
        environment=environment or (
            lambda ctx: TP.environment_observation(
                ctx, which=lambda name: None, include_self=True,
                trust=False)),
        resources=lambda ctx: _observation(),
        tests=lambda ctx: _observation(
            data={"executed": False, "plan_only": True}),
    )
    return TP.build_preflight(
        root, "g1", providers=fake, timeout_ms=10_000, clock=lambda: 0,
        observed_at=1_700_000_000_000)


# ---------------------------------------------------------------------------
# Observation joins
# ---------------------------------------------------------------------------


def test_observation_joins_goal_criterion_test_run_and_environment(project):
    result = _observe(project)
    assert result["state"] == "appended"
    assert result["duplicate"] is False
    record = result["record"]
    assert record["schema"] == ES.STORE_NAMESPACE
    assert record["record_version"] == ES.RECORD_VERSION == 1
    assert record["kind"] == "observation"
    assert record["evidence"] == "observed"
    assert record["freshness"] == "current"
    assert record["goal"] == {"id": "goal:g1", "status": "open",
                              "state": "present"}
    assert record["criteria"][0]["id"] == "c1"
    assert record["criteria"][0]["result"] == "failed"
    assert record["criteria"][0]["test_key"] == TEST_KEY
    assert record["tests"][0]["key"] == TEST_KEY
    assert record["tests"][0]["outcome"] == "failed"
    assert record["tests"][0]["run"]["complete"] is True
    assert record["runs"][0]["run_id"] == "run-1"
    assert record["runs"][0]["state"] == "present"
    assert record["environments"][0]["path"] == sys.executable
    assert record["environments"][0]["exists"] is True
    assert record["environments"][0]["trusted"] is True
    assert record["verification"]["state"] == "unverified"
    assert "criterion_c1_result" in record["verification"]["reasons"]
    assert "test_run_complete" not in json.dumps(record)
    assert len(record["refs"]["report_hash"]) == 64
    assert len(record["refs"]["test_record_digest"]) == 64
    assert len(record["record_hash"]) == 64

    view = ES.read_store(project)
    assert view["state"] == "current"
    assert view["counts"]["total"] == 1
    assert view["records"][0]["current"] is True
    assert ES.latest_record(project, kind="observation")["record_id"] == \
        record["record_id"]

    raw = ES.journal_path(project).read_text(encoding="utf-8")
    assert CANARY not in raw
    assert "title" not in raw
    assert "why" not in raw
    assert "message" not in raw
    assert "failed with" not in raw


def test_observation_is_read_only_for_existing_stores(project):
    before = {
        path.relative_to(project).as_posix(): path.stat().st_mtime_ns
        for path in project.rglob("*")
        if path.is_file() and ".ozagia/evidence" not in path.as_posix()
    }
    _observe(project)
    after = {
        path.relative_to(project).as_posix(): path.stat().st_mtime_ns
        for path in project.rglob("*")
        if path.is_file() and ".ozagia/evidence" not in path.as_posix()
    }
    assert after == before
    assert ES.store_dir(project).is_dir()


def test_secret_canary_is_redacted_at_the_boundary(project):
    _observe(project)
    declared = ES.record_declaration(
        project, source="model", statement=f"the tests pass now {CANARY}",
        goal_id="g1", now_ms=1_700_000_000_001)
    raw = ES.journal_path(project).read_text(encoding="utf-8")
    assert CANARY not in raw
    assert "[redacted]" in raw
    assert declared["record"]["declaration"]["statement"] == \
        "the tests pass now [redacted]"


# ---------------------------------------------------------------------------
# Failing test -> environment selection -> goal assessment
# ---------------------------------------------------------------------------


def test_failing_test_traces_through_environment_to_assessment(
        project, monkeypatch):
    observed = _observe(project)
    report = _preflight(project, monkeypatch=monkeypatch)
    assert report["assessment"]["status"] == "failed"
    assert report["dimensions"]["goal"]["readiness"] is False
    assert report["dimensions"]["environment"]["readiness"] is True
    assessed = ES.record_assessment(project, report,
                                    now_ms=1_700_000_000_001)
    assert assessed["state"] == "appended"
    assessment = assessed["record"]
    assert assessment["kind"] == "assessment"
    assert assessment["assessment"]["status"] == "failed"
    assert assessment["assessment"]["green"] is False
    assert assessment["assessment"]["dimensions"]["goal"]["readiness"] is False
    assert assessment["assessment"]["dimensions"]["environment"]["readiness"] \
        is True
    assert assessment["assessment"]["environment"]["selected"] == sys.executable
    assert assessment["assessment"]["criteria"]["counts"] == {"failed": 1}
    assert assessment["evidence"] == "observed"
    assert assessment["verification"]["state"] == "unverified"

    # The durable chain keeps the failing criterion, test outcome and selected
    # interpreter together; the assessment is not green.
    latest_observation = ES.latest_record(project, kind="observation")
    assert latest_observation["record_id"] == observed["record"]["record_id"]
    assert latest_observation["criteria"][0]["result"] == "failed"
    assert latest_observation["tests"][0]["outcome"] == "failed"
    assert latest_observation["environments"][0]["path"] == sys.executable
    latest_assessment = ES.latest_record(project, kind="assessment")
    assert latest_assessment["current"] is True
    assert latest_assessment["assessment"]["green"] is False
    assert latest_assessment["assessment"]["environment"]["selected"] == \
        sys.executable


def test_stale_graph_and_missing_interpreter_never_become_green(
        project, monkeypatch):
    _observe(project)
    graph_stale = lambda ctx: _observation("unknown", freshness="stale")  # noqa: E731
    no_interpreter = lambda ctx: TP.environment_observation(  # noqa: E731
        ctx, which=lambda name: None, include_self=False, trust=False)

    missing = _preflight(project, graph=graph_stale,
                         environment=no_interpreter, monkeypatch=monkeypatch)
    assert missing["assessment"]["status"] == "failed"
    assert missing["dimensions"]["graph"]["readiness"] == "unknown"
    assert missing["dimensions"]["environment"]["readiness"] is False
    stored = ES.record_assessment(project, missing,
                                  now_ms=1_700_000_000_001)["record"]
    assert stored["assessment"]["green"] is False
    assert stored["assessment"]["dimensions"]["graph"]["readiness"] == "unknown"
    assert stored["assessment"]["dimensions"]["environment"]["readiness"] is \
        False
    assert stored["evidence"] == "observed"
    assert ES.latest_record(project, kind="assessment")["current"] is True
    assert ES.latest_record(project, kind="assessment")["assessment"][
        "green"] is False

    # A caller cannot relabel that report green: the store validates every
    # dimension before accepting the status.
    tampered = dict(missing)
    tampered["assessment"] = {**missing["assessment"],
                              "status": "green", "green": True,
                              "reasons": []}
    tampered.pop("hash", None)
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.record_assessment(project, tampered)
    assert error.value.code == "invalid_state"


def test_stale_graph_only_stays_unknown_and_not_current(project, monkeypatch):
    graph_stale = lambda ctx: _observation("unknown", freshness="stale")  # noqa: E731
    report = _preflight(project, goal=lambda ctx: _observation(True),
                        graph=graph_stale, monkeypatch=monkeypatch)
    assert report["assessment"]["status"] == "unknown"
    stored = ES.record_assessment(project, report,
                                  now_ms=1_700_000_000_001)["record"]
    assert stored["freshness"] == "unknown"
    view = ES.read_store(project)
    assert view["records"][0]["current"] is False
    assert ES.latest_record(project, kind="assessment") is None


# ---------------------------------------------------------------------------
# Declarations and idempotency
# ---------------------------------------------------------------------------


def test_declaration_is_never_current_evidence(project):
    result = ES.record_declaration(
        project, source="model", statement="I ran the tests and they pass",
        goal_id="g1", test_key=TEST_KEY, now_ms=1_700_000_000_000)
    record = result["record"]
    assert record["kind"] == "declaration"
    assert record["evidence"] == "declared"
    assert record["freshness"] == "unknown"
    assert record["verification"]["state"] == "unverified"
    assert ES.latest_record(project, current_only=True) is None
    view = ES.read_store(project)
    assert view["records"][0]["current"] is False
    assert view["counts"]["current"] == 0


def test_repeated_write_is_idempotent_and_conflicts_are_refused(project):
    first = _observe(project)
    second = _observe(project)
    assert second["state"] == "duplicate"
    assert second["duplicate"] is True
    assert second["record"]["record_id"] == first["record"]["record_id"]
    assert ES.read_store(project)["counts"]["total"] == 1

    explicit = ES.record_declaration(
        project, source="model", statement="first claim", goal_id="g1",
        idempotency_key="claim-1", now_ms=1_700_000_000_002)
    duplicate = ES.record_declaration(
        project, source="model", statement="first claim", goal_id="g1",
        idempotency_key="claim-1", now_ms=1_700_000_000_003)
    assert duplicate["state"] == "duplicate"
    assert duplicate["record"]["record_id"] == explicit["record"]["record_id"]
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.record_declaration(
            project, source="model", statement="different claim",
            goal_id="g1", idempotency_key="claim-1")
    assert error.value.code == "conflict"
    assert ES.read_store(project)["counts"]["total"] == 2


# ---------------------------------------------------------------------------
# Crash, partial records and fail-closed reads
# ---------------------------------------------------------------------------


def test_torn_tail_is_partial_not_current_and_is_recovered(project):
    first = _observe(project)
    path = ES.journal_path(project)
    with path.open("ab") as handle:
        handle.write(b'{"schema": "ozagia.evidence.store", "torn":')
    view = ES.read_store(project)
    assert view["state"] == "partial"
    assert view["counts"]["total"] == 1
    assert view["partial"]
    assert view["records"][0]["record_id"] == first["record"]["record_id"]
    assert view["records"][0]["current"] is True
    assert ES.latest_record(project)["record_id"] == first["record"]["record_id"]

    second = ES.record_declaration(
        project, source="model", statement="after the crash", goal_id="g1",
        now_ms=1_700_000_000_001)
    assert second["state"] == "appended"
    assert second["recovered"] is True
    raw = path.read_text(encoding="utf-8")
    assert '"torn"' not in raw
    assert raw.endswith("\n")
    assert ES.read_store(project)["state"] == "current"


def test_explicit_partial_record_is_not_served_as_current(project):
    observed = _observe(project)
    partial = ES.record_partial(
        project, reason="the run was interrupted before the check finished",
        goal_id="g1", run_id="run-1", now_ms=1_700_000_000_001)
    assert partial["record"]["complete"] is False
    assert partial["record"]["freshness"] == "unknown"
    view = ES.read_store(project, include_partial=True)
    assert view["state"] == "current"
    assert view["counts"]["total"] == 1
    assert view["counts"]["partial"] == 1
    partial_rows = [item["record"] for item in view["partial"]
                    if isinstance(item.get("record"), dict)]
    assert len(partial_rows) == 1
    assert partial_rows[0]["complete"] is False
    assert partial_rows[0]["current"] is False
    # The newer partial record makes the older observation not current: the
    # latest outcome for that subject is unknown, not the older snapshot.
    old = next(item for item in view["records"]
               if item["record_id"] == observed["record"]["record_id"])
    assert old["current"] is False
    assert ES.latest_record(project, kind="observation") is None


def test_malformed_interior_line_fails_closed(project, monkeypatch):
    _observe(project)
    path = ES.journal_path(project)
    with path.open("ab") as handle:
        handle.write(b'{"not": "a record"}\n')
    view = ES.read_store(project)
    assert view["state"] == "invalid"
    assert view["records"] == []
    assert view["counts"]["current"] == 0
    assert view["reasons"]
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.record_declaration(project, source="model", statement="next")
    assert error.value.code == "invalid_state"


def test_unsupported_record_version_fails_closed(project):
    _observe(project)
    path = ES.journal_path(project)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    row["record_version"] = 2
    row["record_hash"] = C.content_hash(
        {key: value for key, value in row.items() if key != "record_hash"})
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    view = ES.read_store(project)
    assert view["state"] == "unsupported"
    assert view["records"] == []
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.record_declaration(project, source="model", statement="next")
    assert error.value.code == "invalid_state"


def test_oversized_record_and_store_are_rejected(project, monkeypatch):
    monkeypatch.setattr(ES, "MAX_RECORD_BYTES", 300)
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.record_declaration(project, source="model", statement="x")
    assert error.value.code == "budget_exceeded"
    monkeypatch.undo()

    path = ES.journal_path(project)
    _write(path, "x" * 200)
    monkeypatch.setattr(ES, "MAX_STORE_BYTES", 100)
    view = ES.read_store(project)
    assert view["state"] == "invalid"
    assert view["records"] == []


def test_malformed_input_fields_are_rejected(project):
    base = {
        "kind": "declaration", "source": "model",
        "declaration": {"statement": "claim"},
    }
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.append_record(project, {**base, "raw_log": "secret"})
    assert error.value.code == "unknown_field"
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.append_record(project, {**base, "record_version": 2})
    assert error.value.code == "unsupported_version"
    with pytest.raises(ES.EvidenceStoreError):
        ES.append_record(project, {**base, "refs": {"report_hash": "nope"}})
    runs = [{"run_id": f"run-{index}", "state": "unknown",
             "status": None, "digest": None, "output_digest": None}
            for index in range(ES.MAX_RUNS + 1)]
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.append_record(project, {**base, "runs": runs})
    assert error.value.code == "budget_exceeded"
    with pytest.raises(ES.EvidenceStoreError):
        ES.read_store(project, limit=0)
    with pytest.raises(ES.EvidenceStoreError):
        ES.read_store(project, limit=ES.MAX_RECORDS + 1)


# ---------------------------------------------------------------------------
# Retention and concurrency
# ---------------------------------------------------------------------------


def test_retention_evicts_oldest_and_keeps_the_new_record(project):
    for index in range(5):
        result = ES.record_declaration(
            project, source="model", statement=f"claim {index}",
            goal_id="g1", now_ms=1_700_000_000_000 + index,
            max_records=3, max_age_ms=0)
        if index >= 3:
            assert result["retention"] is not None
            assert result["retention"]["evicted"] == 1
    view = ES.read_store(project)
    assert view["counts"]["total"] == 3
    statements = [record["declaration"]["statement"]
                  for record in view["records"]]
    assert statements == ["claim 4", "claim 3", "claim 2"]
    assert ES.latest_record(project, current_only=False)[
        "declaration"]["statement"] == "claim 4"

    compacted = ES.compact_store(project, max_records=1, max_age_ms=0,
                                 now_ms=1_700_000_000_010)
    assert compacted["before"] == 3
    assert compacted["after"] == 1
    assert compacted["evicted"] == 2
    assert ES.read_store(project)["counts"]["total"] == 1


def test_retention_age_bound_evicts(project):
    ES.record_declaration(project, source="model", statement="old",
                          now_ms=1_000, max_age_ms=0)
    ES.record_declaration(project, source="model", statement="new",
                          now_ms=1_000 + ES.MAX_AGE_MS,
                          max_age_ms=ES.MAX_AGE_MS)
    compacted = ES.compact_store(
        project, max_records=ES.MAX_RECORDS, max_age_ms=ES.MAX_AGE_MS,
        now_ms=1_000 + ES.MAX_AGE_MS + 1)
    assert compacted["after"] == 1
    assert compacted["evicted"] == 1
    assert ES.latest_record(project, current_only=False)[
        "declaration"]["statement"] == "new"


def test_concurrent_writers_serialise(project):
    code = (
        "import sys\n"
        "from ozagia.core import evidence_store as ES\n"
        "root, label = sys.argv[1], sys.argv[2]\n"
        "for i in range(3):\n"
        "    ES.record_declaration(root, source='tests',\n"
        "        statement=f'{label}-{i}', now_ms=1000 + i)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(project),
                          f"writer{n}"], env=env)
        for n in range(4)
    ]
    assert all(process.wait(timeout=120) == 0 for process in processes)
    view = ES.read_store(project)
    assert view["state"] == "current"
    assert view["counts"]["total"] == 12
    statements = {record["declaration"]["statement"]
                  for record in view["records"]}
    assert len(statements) == 12


def test_concurrent_duplicate_writes_store_one_record(project):
    code = (
        "import sys\n"
        "from ozagia.core import evidence_store as ES\n"
        "ES.record_declaration(sys.argv[1], source='tests',\n"
        "    statement='one event', now_ms=1000)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(project)], env=env)
        for _ in range(4)
    ]
    assert all(process.wait(timeout=120) == 0 for process in processes)
    view = ES.read_store(project)
    assert view["counts"]["total"] == 1
    assert view["records"][0]["declaration"]["statement"] == "one event"


def test_absent_store_is_explicit(tmp_path):
    view = ES.read_store(tmp_path)
    assert view["state"] == "absent"
    assert view["records"] == []
    assert view["counts"]["total"] == 0
    assert ES.latest_record(tmp_path) is None
    assert ES.store_summary(tmp_path)["state"] == "absent"


def test_verified_observation_for_a_met_test_fails_criterion(project):
    goal = json.loads(G.goals_path(project).read_text(encoding="utf-8"))
    criterion = goal["goals"][0]["criteria"][0]
    criterion["kind"] = "test_fails"
    criterion["last"]["status"] = "pass"
    _write_json(G.goals_path(project), goal)
    result = _observe(project)
    record = result["record"]
    assert record["criteria"][0]["result"] == "passed"
    assert record["tests"][0]["outcome"] == "failed"
    assert record["verification"]["state"] == "verified"
    assert record["evidence"] == "verified"
    assert ES.latest_record(project)["evidence"] == "verified"


def test_green_assessment_is_accepted_only_when_all_dimensions_are_true(
        project, monkeypatch):
    report = _preflight(project, goal=lambda ctx: _observation(True),
                        monkeypatch=monkeypatch)
    assert report["assessment"]["status"] == "green"
    record = ES.record_assessment(project, report,
                                  now_ms=1_700_000_000_001)["record"]
    assert record["assessment"]["green"] is True
    assert record["evidence"] == "verified"
    assert record["verification"]["state"] == "verified"
    assert ES.latest_record(project, kind="assessment")["current"] is True


def test_assessment_hash_mismatch_is_refused(project, monkeypatch):
    report = _preflight(project, goal=lambda ctx: _observation(True),
                        monkeypatch=monkeypatch)
    tampered = dict(report)
    tampered["assessment"] = {**report["assessment"], "green": False,
                              "status": "failed", "reasons": []}
    with pytest.raises(ES.EvidenceStoreError) as error:
        ES.record_assessment(project, tampered)
    assert error.value.code == "invalid_value"
