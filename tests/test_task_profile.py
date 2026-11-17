"""Tests for the offline task-profile assembly primitives."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core import task_profile as P

REPO = Path(__file__).resolve().parents[1]
SCOPE = {"task": "task:demo", "run": "run:demo"}


def make_span(span_id: str, *, kind: str = "tool_call",
              parents: tuple[str, ...] | None = (),
              **fields: object) -> P.SpanRecord:
    return P.SpanRecord(span_id, kind, parents=parents, **fields)


def make_profile(spans, **kwargs):
    return P.build_profile(spans, scope=SCOPE, **kwargs)


def clone(profile):
    return json.loads(P.profile_json(profile))


# ---------------------------------------------------------------------------
# Schema, canonical form and hashing
# ---------------------------------------------------------------------------


def test_profile_schema_is_versioned_and_validated():
    schema = P.profile_schema()
    assert schema["namespace"] == "ozagia.task_profile"
    assert schema["version"] == "1.0"
    assert schema["sensitivity"] == "internal"
    assert C.validate_schema(schema) == schema
    fingerprint = P.profile_schema_fingerprint()
    assert len(fingerprint) == 64
    assert fingerprint == C.schema_fingerprint(P.PROFILE_SCHEMA)
    assert fingerprint == P.profile_schema_fingerprint()
    changed = dict(P.PROFILE_SCHEMA, constraints={"max_spans": 1})
    assert C.schema_fingerprint(changed) != fingerprint


def test_profile_round_trip_is_canonical_and_hashed():
    profile = make_profile([
        make_span("root", duration_ms=100, clock="injected",
                  outcome="completed"),
        make_span("child", parents=("root",), duration_ms=50,
                  clock="injected", outcome="completed"),
    ])
    text = P.profile_json(profile)
    assert text == C.canonical_json(profile)
    assert json.loads(text) == profile
    assert "\n" not in text
    assert P.profile_hash(profile) == C.instance_hash(profile)
    assert len(P.profile_hash(profile)) == 64


def test_instance_hash_excludes_transport_fields_only():
    first = make_profile([make_span("a")], created_at=1, trace_id="trace-1")
    second = make_profile([make_span("a")], created_at=2, trace_id="trace-2")
    assert P.profile_hash(first) == P.profile_hash(second)
    assert P.profile_json(first) != P.profile_json(second)
    extended = make_profile([make_span("a")])
    extended["x-build"] = "ci-42"
    assert P.profile_hash(extended) != P.profile_hash(first)


# ---------------------------------------------------------------------------
# Records, unknowns and explicit edges
# ---------------------------------------------------------------------------


def test_missing_span_fields_stay_explicitly_unknown():
    profile = make_profile([make_span("a")])
    span = profile["data"]["spans"][0]
    assert span["parents"] == []
    for name in (
        "start_ms", "end_ms", "duration_ms", "attempt", "queue_wait_ms",
        "readiness_wait_ms", "input_tokens", "cached_input_tokens",
        "output_tokens", "prefill_ms", "generation_ms", "cost_micros",
    ):
        assert span[name] == "unknown", name
    for name in ("clock", "outcome", "result", "evidence", "measurement",
                 "resource_class"):
        assert span[name] == "unknown", name
    coverage = profile["data"]["coverage"]
    assert coverage["complete"] is False
    assert coverage["timing_complete"] is False
    assert coverage["unknown_duration"] == 1
    assert coverage["unknown_dependencies"] == 0
    assert coverage["unknown_queue_wait"] == 1
    assert coverage["unknown_readiness_wait"] == 1
    assert coverage["unknown_tokens"] == 0
    assert coverage["unknown_evidence"] == 1
    assert profile["data"]["integrity"]["unknown_durations"] == ["a"]


def test_missing_parents_are_unknown_not_an_empty_root():
    profile = make_profile([
        make_span("a", parents=None, duration_ms=10, clock="injected",
                  outcome="completed"),
    ])
    span = profile["data"]["spans"][0]
    assert span["parents"] == "unknown"
    assert profile["data"]["integrity"]["unknown_dependencies"] == ["a"]
    assert profile["data"]["coverage"]["unknown_dependencies"] == 1
    assert profile["data"]["coverage"]["complete"] is False
    report = P.profile_critical_path(profile)
    assert report["duration_ms"] == "unknown"
    assert report["lower_bound_ms"] == 10
    assert report["status"] == "incomplete"
    assert report["unknown_dependency_nodes"] == ["a"]


def test_parents_become_explicit_edges_and_timestamps_do_not():
    profile = make_profile([
        make_span("a", start_ms=100, end_ms=200, clock="injected",
                  outcome="completed", measurement="measured"),
        make_span("b", parents=("a",), start_ms=50, end_ms=80,
                  clock="injected", outcome="completed"),
        make_span("c", start_ms=60, end_ms=90, clock="injected",
                  outcome="completed"),
    ])
    assert profile["data"]["edges"] == [
        {"from": "a", "to": "b", "kind": "finish_to_start"},
    ]
    spans = [P.SpanRecord.from_mapping(item)
             for item in profile["data"]["spans"]]
    assert P.edges_from_spans(spans) == profile["data"]["edges"]
    assert profile["data"]["integrity"]["time_order_anomalies"] == [{
        "from": "a", "to": "b", "parent_end_ms": 200, "child_start_ms": 50,
    }]
    report = P.profile_critical_path(profile)
    assert report["path"] == ["a", "b"]
    assert report["duration_ms"] == 130
    assert report["work_ms"] == 160
    assert report["overlap_ms"] == 30


def test_a_wall_clock_never_becomes_a_duration():
    profile = make_profile([
        make_span("a", start_ms=10, end_ms=20, clock="system"),
    ])
    span = profile["data"]["spans"][0]
    assert span["duration_ms"] == "unknown"
    assert profile["data"]["coverage"]["unknown_duration"] == 1
    report = P.profile_critical_path(profile)
    assert report["duration_ms"] == "unknown"
    assert report["lower_bound_ms"] == 0


def test_token_cost_and_evidence_fields_are_preserved():
    profile = make_profile([
        make_span(
            "call", kind="model_call", duration_ms=500, clock="injected",
            outcome="completed", result="passed", evidence="observed",
            measurement="measured", attempt=2, resource_class="model",
            queue_wait_ms=50, readiness_wait_ms=10,
            input_tokens=1000, cached_input_tokens=400, output_tokens=100,
            prefill_ms=200, generation_ms=300, cost_micros=12_345,
        ),
    ])
    span = profile["data"]["spans"][0]
    assert span["input_tokens"] == 1000
    assert span["cached_input_tokens"] == 400
    assert span["output_tokens"] == 100
    assert span["cost_micros"] == 12_345
    assert span["evidence"] == "observed"
    assert span["measurement"] == "measured"
    assert span["attempt"] == 2
    coverage = profile["data"]["coverage"]
    assert coverage["unknown_tokens"] == 0
    assert coverage["unknown_cost"] == 0
    assert coverage["unknown_evidence"] == 0
    assert coverage["measured"] == 1


def test_build_is_deterministic_across_input_order():
    spans = [
        make_span("c", parents=("a",), duration_ms=10),
        make_span("a", duration_ms=20),
        make_span("b", parents=("a",), duration_ms=30),
    ]
    first = make_profile(spans)
    second = make_profile([spans[1], spans[2], spans[0]])
    assert P.profile_json(first) == P.profile_json(second)
    assert P.profile_hash(first) == P.profile_hash(second)


# ---------------------------------------------------------------------------
# Bounds and truncation
# ---------------------------------------------------------------------------


def test_span_bound_truncates_a_deterministic_prefix():
    spans = [make_span(f"s{index:02d}", duration_ms=10)
             for index in range(10)]
    profile = make_profile(spans, max_spans=3)
    assert [span["id"] for span in profile["data"]["spans"]] == [
        "s00", "s01", "s02"]
    assert profile["omissions"] == {
        "truncated": True,
        "omitted": 7,
        "next_cursor": "s02",
        "reason": "limit",
    }
    coverage = profile["data"]["coverage"]
    assert coverage["spans_included"] == 3
    assert coverage["spans_total"] == 10
    assert coverage["truncated"] is True
    assert coverage["complete"] is False
    P.validate_profile(profile)


def test_edge_bound_keeps_an_ancestor_closed_prefix():
    spans = [
        make_span("a"),
        make_span("b", parents=("a",)),
        make_span("c", parents=("a",)),
        make_span("d", parents=("b", "c")),
    ]
    profile = make_profile(spans, max_edges=2)
    assert [span["id"] for span in profile["data"]["spans"]] == [
        "a", "b", "c"]
    assert profile["data"]["coverage"]["edges"] == 2
    assert profile["omissions"]["omitted"] == 1
    kept_parents = {parent["from"] for parent in profile["data"]["edges"]}
    kept_ids = {span["id"] for span in profile["data"]["spans"]}
    assert kept_parents <= kept_ids


def test_byte_bound_truncates_and_never_exceeds():
    spans = [make_span(f"s{index:02d}", duration_ms=10)
             for index in range(10)]
    profile = make_profile(spans, max_bytes=2000)
    assert profile["omissions"]["truncated"] is True
    assert profile["omissions"]["reason"] == "bytes"
    assert profile["omissions"]["omitted"] > 0
    assert len(P.profile_json(profile).encode("utf-8")) <= 2000
    P.validate_profile(profile)
    with pytest.raises(P.TaskProfileBoundsError):
        make_profile(spans, max_bytes=100)


def test_zero_span_bound_is_a_valid_empty_profile():
    spans = [make_span("a"), make_span("b")]
    profile = make_profile(spans, max_spans=0)
    assert profile["data"]["spans"] == []
    assert profile["data"]["edges"] == []
    assert profile["omissions"] == {
        "truncated": True,
        "omitted": 2,
        "next_cursor": None,
        "reason": "limit",
    }
    assert profile["data"]["coverage"]["spans_total"] == 2
    assert profile["data"]["coverage"]["complete"] is False
    P.validate_profile(profile)
    report = P.profile_critical_path(profile)
    assert report["duration_ms"] == 0
    assert report["run_status"] == "unknown"
    assert report["complete"] is False


# ---------------------------------------------------------------------------
# Malformed input and tampering
# ---------------------------------------------------------------------------


def test_duplicate_and_missing_parents_fail_explicitly():
    with pytest.raises(P.TaskProfileError) as duplicate:
        make_profile([make_span("a"), make_span("a")])
    assert duplicate.value.code == "task_profile.duplicate_id"

    with pytest.raises(P.TaskProfileError) as missing:
        make_profile([make_span("a", parents=("nope",))])
    assert missing.value.code == "task_profile.unknown_parent"


def test_cycles_fail_with_the_cycle_path():
    with pytest.raises(P.TaskProfileCycleError) as info:
        make_profile([
            make_span("a", parents=("b",), duration_ms=1),
            make_span("b", parents=("a",), duration_ms=1),
        ])
    assert info.value.code == "task_profile.cycle"
    assert info.value.cycles == (("a", "b"),)
    assert info.value.components == (("a", "b"),)


@pytest.mark.parametrize(
    "factory, code",
    [
        (lambda: make_span("A"), "task_profile.invalid_field"),
        (lambda: make_span("a", kind="not-a-kind"),
         "task_profile.invalid_field"),
        (lambda: make_span("a", duration_ms=-1),
         "task_profile.invalid_field"),
        (lambda: make_span("a", start_ms=10, end_ms=5),
         "task_profile.invalid_field"),
        (lambda: make_span("a", start_ms=10, end_ms=20, duration_ms=20,
                           clock="injected"), "task_profile.invalid_field"),
        (lambda: make_span("a", kind="model_call", input_tokens=10,
                           cached_input_tokens=11),
         "task_profile.invalid_field"),
        (lambda: make_span("a", kind="model_call", duration_ms=10,
                           prefill_ms=8, generation_ms=8),
         "task_profile.invalid_field"),
        (lambda: make_span("a", clock="wall"),
         "task_profile.invalid_field"),
        (lambda: make_span("a", measurement="guessed"),
         "task_profile.invalid_field"),
    ],
)
def test_bad_span_fields_are_typed(factory, code):
    with pytest.raises(P.TaskProfileError) as info:
        factory()
    assert info.value.code == code


def test_bad_records_and_bounds_fail_explicitly():
    with pytest.raises(P.TaskProfileError):
        make_profile(["not-a-span"])
    with pytest.raises(P.TaskProfileError):
        P.build_profile([], scope=[])
    with pytest.raises(P.TaskProfileError):
        P.build_profile([], scope=SCOPE, snapshot="")
    with pytest.raises(P.TaskProfileError):
        P.build_profile([], scope=SCOPE, inputs="one-string")
    with pytest.raises(P.TaskProfileBoundsError):
        P.build_profile([], scope=SCOPE, max_spans=-1)
    with pytest.raises(P.TaskProfileBoundsError):
        P.build_profile([], scope=SCOPE, max_bytes=0)
    with pytest.raises(P.TaskProfileBoundsError):
        P.build_profile([], scope=SCOPE, max_bytes=P.MAX_PROFILE_BYTES + 1)


def test_unknown_span_mapping_fields_fail_explicitly():
    with pytest.raises(P.TaskProfileError) as info:
        P.SpanRecord.from_mapping({"id": "a", "kind": "tool_call",
                                   "secret": "nope"})
    assert info.value.code == "task_profile.invalid_field"
    with pytest.raises(P.TaskProfileError):
        P.SpanRecord.from_mapping({"id": "a"})
    with pytest.raises(P.TaskProfileError):
        P.SpanRecord.from_mapping({"id": "a", "kind": "tool_call",
                                   "parents": "root"})


def test_validate_profile_rejects_tampering():
    profile = clone(make_profile([
        make_span("a", duration_ms=10, clock="injected",
                  outcome="completed"),
        make_span("b", parents=("a",), duration_ms=20, clock="injected",
                  outcome="completed"),
    ]))

    tampered = json.loads(json.dumps(profile))
    tampered["data"]["edges"].append(
        {"from": "b", "to": "a", "kind": "finish_to_start"})
    with pytest.raises(P.TaskProfileError) as edges:
        P.validate_profile(tampered)
    assert edges.value.code == "task_profile.inconsistent_edges"

    tampered = json.loads(json.dumps(profile))
    tampered["data"]["coverage"]["spans_included"] += 1
    with pytest.raises(P.TaskProfileError) as coverage:
        P.validate_profile(tampered)
    assert coverage.value.code == "task_profile.inconsistent_data"

    tampered = json.loads(json.dumps(profile))
    tampered["data"]["clock"] = "system"
    with pytest.raises(P.TaskProfileError):
        P.validate_profile(tampered)

    tampered = json.loads(json.dumps(profile))
    tampered["schema"]["namespace"] = "ozagia.something_else"
    with pytest.raises(P.TaskProfileError):
        P.validate_profile(tampered)

    tampered = json.loads(json.dumps(profile))
    tampered["data"]["profile_version"] = "2.0"
    with pytest.raises(P.TaskProfileError):
        P.validate_profile(tampered)

    tampered = json.loads(json.dumps(profile))
    tampered["data"]["unexpected"] = 1
    with pytest.raises(P.TaskProfileError):
        P.validate_profile(tampered)

    tampered = json.loads(json.dumps(profile))
    del tampered["omissions"]
    with pytest.raises(P.TaskProfileError):
        P.validate_profile(tampered)

    tampered = json.loads(json.dumps(profile))
    tampered["data"]["spans"][0]["secret"] = "nope"
    with pytest.raises(P.TaskProfileError):
        P.validate_profile(tampered)


# ---------------------------------------------------------------------------
# Fake clock and cross-process determinism
# ---------------------------------------------------------------------------


def test_fake_clock_is_injected_and_monotonic():
    clock = P.FakeClock(now_ms=0, step_ms=2)
    assert clock.now() == 0
    assert clock.advance(5) == 5
    assert clock.tick() == 7
    assert clock.now() == 7
    with pytest.raises(P.TaskProfileError):
        clock.advance(-1)
    with pytest.raises(P.TaskProfileError):
        P.FakeClock(now_ms=-1)
    with pytest.raises(P.TaskProfileError):
        P.FakeClock(kind="system")


def test_profile_bytes_are_identical_across_hash_seeds():
    script = (
        "import json;"
        "from ozagia.core.task_profile import SpanRecord, build_profile, "
        "profile_json, profile_hash;"
        "spans=["
        "SpanRecord('a','tool_call',parents=(),duration_ms=10,"
        "clock='injected',outcome='completed'),"
        "SpanRecord('b','model_call',parents=('a',),duration_ms=20,"
        "clock='injected',outcome='completed',input_tokens=10,"
        "output_tokens=2)];"
        "p=build_profile(spans,scope={'task':'task:demo','run':'run:demo'});"
        "print(profile_hash(p));print(profile_json(p))"
    )
    outputs = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src")
        env["PYTHONHASHSEED"] = seed
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]
    assert outputs[0].splitlines()[0] == P.profile_hash(
        make_profile([
            make_span("a", duration_ms=10, clock="injected",
                      outcome="completed"),
            make_span("b", kind="model_call", parents=("a",),
                      duration_ms=20, clock="injected",
                      outcome="completed", input_tokens=10,
                      output_tokens=2),
        ]))


# ---------------------------------------------------------------------------
# WP4 adapters
# ---------------------------------------------------------------------------


def run_manifest(**overrides):
    value = {
        "schema": 1,
        "run_id": "run1",
        "status": "ok",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:10+00:00",
        "duration_s": 10.0,
        "spec": {
            "id": "demo", "version": 1, "title": "Demo",
            "tools": ["read", "test"], "requires_graph": True,
        },
        "steps": [
            {"id": "s1", "tool": "read", "status": "ok", "elapsed_s": 1.5},
            {"id": "s2", "tool": "test", "status": "ok", "elapsed_s": 2.5},
        ],
        "artifacts": [
            {"path": "src/a.py", "sha256": "a" * 64, "size": 10},
        ],
        "budget": {
            "used": {"tokens": 100, "cost_usd": 0.001, "wall_s": 10,
                     "memory_mb": 64, "disk_mb": 2, "pids": 4},
        },
    }
    value.update(overrides)
    return value


def run_record(manifest=None):
    manifest = manifest or run_manifest()
    return {"schema": 1, "run_id": manifest["run_id"],
            "state": "complete", "manifest": manifest}


def goal_record(**overrides):
    value = {
        "id": "g1", "version": 1, "status": "met",
        "criteria": [
            {
                "id": "c1", "kind": "test_passes",
                "args": {"key": "test:tests/test_a.py::test_a"},
                "last": {"status": "pass", "at": 1767225600},
            },
        ],
    }
    value.update(overrides)
    return value


def sample_test_record():
    return {
        "runs": [{
            "ts": 1767225600, "selectors": ["tests/test_a.py"],
            "failed": [], "errors": [], "complete": True, "filtered": False,
            "source": "captured",
        }],
        "tests": {},
    }


def test_task_run_adapter_preserves_ids_and_declares_only_step_order():
    spans = P.adapt_task_run(run_record(), parents=())
    assert spans[0].id.startswith("task.task-run1-run-")
    assert spans[0].parents == ()
    assert spans[1].id.startswith("task.task-run1-step-s1-")
    assert spans[2].parents == (spans[1].id,)
    assert spans[1].parents == ()
    assert P.edges_from_spans(spans) == [{
        "from": spans[1].id, "to": spans[2].id,
        "kind": "finish_to_start",
    }]
    assert spans[0].duration_ms == 10_000
    assert spans[0].clock == "system"
    assert spans[1].duration_ms == 1_500
    assert spans[1].clock == "monotonic"


def test_task_run_adapter_keeps_missing_duration_unknown():
    manifest = run_manifest(steps=[
        {"id": "s1", "tool": "read", "status": "ok"},
    ])
    spans = P.adapt_task_run(run_record(manifest))
    step = spans[1]
    assert step.duration_ms is None
    assert step.clock == "unknown"
    assert step.measurement == "unknown"
    profile = P.build_task_profile_from_records(
        P.TaskRecords(run=run_record(manifest)),
        scope={"task": "task:demo", "run": "run:run1"})
    assert profile["data"]["coverage"]["unknown_duration"] == 1


def test_goal_adapter_preserves_criterion_results_without_durations():
    goal = goal_record(criteria=[
        {"id": "c1", "kind": "test_passes",
         "args": {"key": "test:tests/test_a.py::test_a"},
         "last": {"status": "pass", "at": 1767225600}},
        {"id": "c2", "kind": "file_exists", "args": {"key": "file:a.py"}},
    ])
    spans = P.adapt_goal_record(goal)
    assert spans[0].outcome == "completed"
    assert spans[0].result == "passed"
    assert spans[1].result == "passed"
    assert spans[1].evidence == "declared"
    assert spans[2].result == "not_assessed"
    assert spans[2].outcome == "pending"
    assert all(span.duration_ms is None for span in spans)


def test_test_record_adapter_links_a_deciding_run_but_not_runs_to_each_other():
    spans = P.adapt_test_record(sample_test_record(), keys=["test:tests/test_a.py::test_a"])
    run_span = spans[0]
    key_span = spans[1]
    assert run_span.clock == "system"
    assert run_span.duration_ms is None
    assert key_span.parents == (run_span.id,)
    assert key_span.result == "passed"
    unknown = P.adapt_test_record(
        {"runs": [], "tests": {}}, keys=["test:tests/test_b.py::test_b"])
    assert unknown[0].parents is None
    assert unknown[0].result == "unknown"


def test_session_adapter_uses_only_declared_edges_and_wall_clock():
    session = {
        "session_id": "s1", "version": 1,
        "nodes": [
            {"id": "s1", "kind": "session", "type": "Session",
             "ts": "2026-01-01T00:00:00+00:00"},
            {"id": "n2", "kind": "step", "type": "Step",
             "ts": "2026-01-01T00:00:01+00:00"},
            {"id": "n3", "kind": "attempt", "type": "Attempt",
             "ts": "2026-01-01T00:00:02+00:00"},
            {"id": "n4", "kind": "verification", "type": "Verification",
             "passed": False, "ts": "2026-01-01T00:00:03+00:00"},
        ],
        "edges": [
            {"src": "s1", "type": "has_step", "tgt": "n2"},
            {"src": "n2", "type": "attempt", "tgt": "n3"},
            {"src": "n3", "type": "verification", "tgt": "n4"},
        ],
    }
    spans = P.adapt_session_record(session)
    by_id = {span.id: span for span in spans}
    assert spans[0].parents == ()
    assert by_id[spans[1].id].parents == (spans[0].id,)
    assert by_id[spans[2].id].parents == (spans[1].id,)
    assert by_id[spans[3].id].parents == (spans[2].id,)
    assert by_id[spans[3].id].result == "failed"
    assert all(span.clock == "system" for span in spans)
    assert all(span.duration_ms is None for span in spans)


def test_daemon_and_evaluation_adapters_keep_measured_components_parallel():
    daemon = P.adapt_daemon_evidence([
        {"seq": 1, "tool": "read", "handled": True, "ms": 12.5,
         "waited_s": 0.5},
    ])
    assert daemon[0].duration_ms == 12
    assert daemon[0].clock == "monotonic"
    assert daemon[0].queue_wait_ms == 500
    evaluation = {
        "run_id": "e1", "status": "ok", "wall_s": 5.0,
        "tokens_in": 100, "tokens_out": 20, "cost_usd": 0.002,
        "model": "demo/model",
        "timing": {"model": {"s": 2.0}, "tools": {"s": 1.0}},
    }
    spans = P.adapt_evaluation_record(evaluation)
    assert len(spans) == 3
    assert spans[0].duration_ms == 5_000
    assert spans[1].parents == ()
    assert spans[2].parents == ()
    assert spans[1].input_tokens == 100
    assert spans[1].output_tokens == 20
    assert P.edges_from_spans(spans) == []


# ---------------------------------------------------------------------------
# WP4 post-run report
# ---------------------------------------------------------------------------


def task_records(**overrides):
    value = {
        "run": run_record(),
        "goal": goal_record(),
        "test_record": sample_test_record(),
    }
    value.update(overrides)
    return P.TaskRecords(**value)


def test_task_report_answers_criteria_work_and_selections():
    report = P.build_task_report(
        task_records(), criterion_freshness={"c1": "current"},
        snapshot="snapshot:demo")
    assert report["schema"] == {"namespace": "ozagia.task_report",
                                "version": "1.0"}
    assert report["outcome"]["status"] == "passed"
    assert report["outcome"]["criteria_status"] == "passed"
    assert report["outcome"]["criteria"]["items"][0]["test"]["outcome"] == (
        "passed")
    work = report["work"]
    assert work["planned"]["steps"] == 2
    assert work["executed"]["steps"] == 2
    assert work["changed"]["count"] == 1
    assert work["changed"]["files"][0]["path"] == "src/a.py"
    assert report["selections"]["tools"] == ["read", "test"]
    assert report["snapshot"]["id"] == "snapshot:demo"
    assert report["measurements"]["tokens"]["total"] == 100
    assert report["measurements"]["cost"]["micros"] == 1_000
    assert report["execution_critical_path"]["duration_ms"] == 10_000
    assert len(report["execution_critical_path"]["path"]) == 1
    assert report["execution_critical_path"]["path"][0].startswith(
        "task.task-run1-run-")
    assert report["coverage"]["complete"] is True
    assert report["outcome"]["complete"] is True
    assert report["coverage"]["evidence_timing"]["timing_complete"] is False
    assert report["coverage"]["goal"] == "present"
    assert report["not_assessed"]
    P.task_report_json(report)
    P.task_report_hash(report)


def test_task_report_schema_is_versioned_and_the_document_has_no_floats():
    schema = P.task_report_schema()
    assert schema["namespace"] == "ozagia.task_report"
    assert schema["version"] == "1.0"
    assert schema["sensitivity"] == "internal"
    assert len(P.task_report_schema_fingerprint()) == 64
    report = P.build_task_report(
        task_records(), criterion_freshness={"c1": "current"})

    def walk(value):
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(report)
    tampered = dict(report)
    tampered["surprise"] = 1
    with pytest.raises(P.TaskReportError):
        P.validate_task_report(tampered)
    tampered = dict(report)
    tampered["hash"] = "0" * 64
    with pytest.raises(P.TaskReportError):
        P.validate_task_report(tampered)


def test_task_report_is_regenerable_from_the_same_retained_records():
    records = task_records()
    first = P.build_task_report(
        records, criterion_freshness={"c1": "current"}, created_at=1)
    second = P.build_task_report(
        records, criterion_freshness={"c1": "current"}, created_at=2)
    assert P.task_report_json(first) != P.task_report_json(second)
    assert P.task_report_hash(first) == P.task_report_hash(second)
    body = {key: value for key, value in first.items() if key != "created_at"}
    rebuilt = P.validate_task_report({**body, "hash": P.task_report_hash(body)})
    assert P.task_report_hash(rebuilt) == P.task_report_hash(first)


def test_task_report_marks_a_failed_partial_run_and_unknown_criteria():
    manifest = run_manifest(
        status="failed", stop="gate_failed",
        stop_detail="the gate failed",
        steps=[{"id": "s1", "tool": "read", "status": "error",
                "elapsed_s": 1.0, "summary": "failed read"}],
        budget={"used": {}},
    )
    goal = goal_record(status="open", criteria=[
        {"id": "c1", "kind": "file_exists", "args": {"key": "file:a.py"},
         "last": {"status": "unknown", "reason": "not checked"}},
        {"id": "c2", "kind": "file_exists", "args": {"key": "file:b.py"}},
    ])
    report = P.build_task_report(P.TaskRecords(
        run=run_record(manifest), goal=goal))
    assert report["outcome"]["status"] == "failed"
    assert report["outcome"]["criteria_status"] == "unknown"
    assert report["outcome"]["complete"] is False
    assert report["failures"][0]["kind"] == "stop"
    assert report["blocked"] == []
    assert report["work"]["executed"]["failed"] == 1
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["test_record"] == "not_requested"
    assert report["outcome"]["criteria"]["counts"] == {
        "not_assessed": 1, "unknown": 1}


def test_task_report_keeps_an_incomplete_run_incomplete():
    manifest = run_manifest(status="ok")
    report = P.build_task_report(P.TaskRecords(
        run={"schema": 1, "run_id": "run1", "state": "incomplete",
             "manifest": manifest}))
    assert report["outcome"]["run_status"] == "incomplete"
    assert report["outcome"]["status"] == "incomplete"
    assert report["outcome"]["complete"] is False


def test_task_report_keeps_a_partial_run_without_a_manifest_incomplete():
    report = P.build_task_report(P.TaskRecords(run={
        "schema": 1, "run_id": "partial", "state": "incomplete",
        "manifest": None, "errors": ["manifest is missing"],
    }))
    assert report["run"]["run_id"] == "partial"
    assert report["outcome"]["run_status"] == "incomplete"
    assert report["outcome"]["status"] == "incomplete"
    assert report["outcome"]["complete"] is False
    assert report["coverage"]["run"] == "incomplete"
    assert report["work"]["planned"]["steps"] == 0
    assert report["sources"][0]["source_id"] == "partial"


def test_task_report_redacts_a_secret_canary_from_json_and_text():
    canary = "sk-abcdefghijklmnop"
    manifest = run_manifest(
        status="failed", stop="gate_failed",
        stop_detail=f"request used Authorization: Bearer {canary}",
        what_was_not_assessed=[f"leaked {canary}"],
    )
    goal = goal_record(criteria=[
        {"id": "c1", "kind": "file_exists", "args": {"key": "file:a.py"},
         "last": {"status": "fail", "reason": f"token={canary}"}},
        {"id": "c2", "kind": "test_passes",
         "args": {"key": f"test:tests/{canary}.py::test_x"}},
    ])
    report = P.build_task_report(P.TaskRecords(
        run=run_record(manifest), goal=goal))
    text = P.task_report_json(report)
    assert canary not in text
    assert canary not in P.format_task_report(report)
    assert "[redacted]" in text


def test_task_report_trims_to_the_byte_bound_and_marks_it():
    goal = goal_record(criteria=[
        {"id": f"c{index}", "kind": "file_exists",
         "args": {"key": f"file:src/file_{index}.py"},
         "last": {"status": "pass", "at": 1767225600}}
        for index in range(50)
    ])
    report = P.build_task_report(P.TaskRecords(
        run=run_record(), goal=goal, test_record=sample_test_record()),
        criterion_freshness={f"c{index}": "current" for index in range(50)},
        max_bytes=16384)
    assert len(P.task_report_json(report).encode("utf-8")) <= 16384
    assert report["truncated"]["reason"] == "bytes"
    assert report["truncated"]["omitted"] > 0
    assert report["outcome"]["complete"] is False


def test_task_report_drops_absolute_or_escaping_artifact_paths():
    manifest = run_manifest(artifacts=[
        {"path": "/etc/passwd"},
        {"path": "../outside"},
        {"path": "src/a.py", "sha256": "a" * 64},
    ])
    report = P.build_task_report(P.TaskRecords(
        run=run_record(manifest)))
    assert report["work"]["changed"]["count"] == 1
    assert report["work"]["changed"]["files"][0]["path"] == "src/a.py"


def test_task_report_cli_reads_only_the_state_directory(tmp_path: Path,
                                                         capsys):
    from ozagia import tasks_cli

    root = tmp_path / "repo"
    run_dir = root / ".ozagia" / "tasks" / "run1"
    run_dir.mkdir(parents=True)
    manifest = run_manifest()
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    goal_dir = root / ".ozagia" / "goals"
    goal_dir.mkdir(parents=True)
    (goal_dir / "goals.json").write_text(json.dumps({
        "version": 1, "next_id": 2, "goals": [goal_record()],
    }), encoding="utf-8")

    code = tasks_cli.main([
        "report", "run1", "--root", str(root), "--goal", "goal:g1",
        "--criterion-freshness", "c1=current", "--json",
    ])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"]["status"] == "passed"
    assert report["scope"]["goal"] == "goal:g1"
    assert report["run"]["run_id"] == "run1"
    regenerated = P.build_task_report_from_root(
        root, "run1", goal_id="g1", criterion_freshness={"c1": "current"})
    assert regenerated["hash"] == report["hash"]
    profile = P.build_task_profile_from_root(root, "run1", goal_id="g1")
    assert profile["scope"]["run"] == "run:run1"
    P.validate_profile(profile)

    code = tasks_cli.main(["report", "missing", "--root", str(root)])
    assert code == 2
    assert "cannot read task run" in capsys.readouterr().err
