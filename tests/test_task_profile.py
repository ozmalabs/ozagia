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
