"""Tests for explicit-edge critical paths and deterministic what-if schedules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core import critical_path as CP

REPO = Path(__file__).resolve().parents[1]


def node(node_id: str, duration_ms: int | None = None, **kwargs: object):
    return CP.DagNode(node_id, duration_ms, **kwargs)


def make_dag(nodes, edges=()):
    return CP.Dag.from_pairs(nodes, edges)


def model_estimate(**kwargs):
    base = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "queue_wait_ms": 0,
        "startup_ms": 0,
    }
    base.update(kwargs)
    return CP.ModelCallEstimate(**base)


# ---------------------------------------------------------------------------
# Longest path, parallel work and explicit edges
# ---------------------------------------------------------------------------


def test_branching_workload_picks_the_correct_critical_path():
    dag = make_dag([
        node("root", 100, outcome="completed", resource_class="tool"),
        node("a", 500, outcome="completed", resource_class="model"),
        node("b", 300, outcome="completed", resource_class="model"),
        node("join", 50, outcome="completed", resource_class="check"),
    ], [
        ("root", "a"), ("root", "b"), ("a", "join"), ("b", "join"),
    ])
    report = CP.critical_path(dag)
    assert report["path"] == ["root", "a", "join"]
    assert report["duration_ms"] == 650
    assert report["work_ms"] == 950
    assert report["overlap_ms"] == 300
    assert report["status"] == "complete"
    assert report["path_is_critical"] is True
    assert report["complete"] is True


def test_parallel_spans_are_never_summed():
    dag = make_dag([
        node("root", 100),
        node("a", 1000),
        node("b", 1000),
        node("join", 100),
    ], [
        ("root", "a"), ("root", "b"), ("a", "join"), ("b", "join"),
    ])
    report = CP.critical_path(dag)
    assert report["duration_ms"] == 1200
    assert report["duration_ms"] != 2200
    assert report["work_ms"] == 2200
    assert report["overlap_ms"] == 1000


def test_multiple_parents_wait_for_the_longest_parent():
    dag = make_dag([
        node("a", 500),
        node("b", 700),
        node("join", 50),
    ], [("a", "join"), ("b", "join")])
    report = CP.critical_path(dag)
    assert report["path"] == ["b", "join"]
    assert report["duration_ms"] == 750
    assert report["work_ms"] == 1250


def test_edges_are_explicit_and_never_created_from_order():
    parallel = make_dag([node("a", 500), node("b", 500)])
    report = CP.critical_path(parallel, include_schedule=True)
    assert report["nodes"] == 2
    assert report["edges"] == 0
    assert report["duration_ms"] == 500
    assert report["path"] == ["a"]
    assert report["starts"] == {"a": 0, "b": 0}
    assert report["work_ms"] == 1000
    assert report["overlap_ms"] == 500

    serial = make_dag([node("a", 500), node("b", 100)], [("a", "b")])
    assert CP.critical_path(serial)["duration_ms"] == 600
    assert CP.critical_path(serial)["path"] == ["a", "b"]


def test_empty_dag_is_not_a_completed_run():
    report = CP.critical_path(CP.Dag([]))
    assert report["duration_ms"] == 0
    assert report["status"] == "complete"
    assert report["run_status"] == "unknown"
    assert report["complete"] is False


# ---------------------------------------------------------------------------
# Unknown durations and dependencies
# ---------------------------------------------------------------------------


def test_unknown_duration_is_unknown_not_zero():
    dag = make_dag([node("a", 1000), node("b")])
    report = CP.critical_path(dag)
    assert report["duration_ms"] == "unknown"
    assert report["duration_ms"] != 1000
    assert report["lower_bound_ms"] == 1000
    assert report["status"] == "incomplete"
    assert report["path_is_critical"] is False
    assert report["unknown_nodes"] == ["b"]
    assert report["work_is_lower_bound"] is True


def test_unknown_dependencies_are_not_roots():
    dag = CP.Dag([node("a", 50, parents_unknown=True)])
    report = CP.critical_path(dag)
    assert report["duration_ms"] == "unknown"
    assert report["lower_bound_ms"] == 50
    assert report["unknown_dependency_nodes"] == ["a"]
    assert report["status"] == "incomplete"


# ---------------------------------------------------------------------------
# Cycles, cancellation and failure
# ---------------------------------------------------------------------------


def test_cycles_are_typed_and_deterministic():
    dag = make_dag([
        node("a", 10), node("b", 10), node("c", 10),
    ], [("a", "b"), ("b", "c"), ("c", "a")])
    with pytest.raises(CP.CycleError) as info:
        CP.critical_path(dag)
    error = info.value
    assert error.code == "critical_path.cycle"
    assert error.cycles == (("a", "b", "c"),)
    assert error.components == (("a", "b", "c"),)
    report = error.report()
    assert report["status"] == "cycle"
    assert report["run_status"] == "invalid"
    assert report["cycles"] == [["a", "b", "c"]]
    assert CP.critical_path(dag, on_cycle="report")["status"] == "cycle"


def test_self_cycle_and_disconnected_cycles_are_reported():
    with pytest.raises(CP.CycleError) as self_loop:
        CP.critical_path(make_dag([node("a", 1)], [("a", "a")]))
    assert self_loop.value.cycles == (("a",),)

    dag = make_dag([
        node("a", 1), node("b", 1), node("c", 1), node("d", 1),
    ], [("a", "b"), ("b", "a"), ("c", "d"), ("d", "c")])
    with pytest.raises(CP.CycleError) as info:
        CP.critical_path(dag)
    assert info.value.cycles == (("a", "b"), ("c", "d"))


def test_cancelled_and_failed_runs_are_never_completed():
    cancelled = make_dag([
        node("a", 100, outcome="cancelled"),
        node("b", 50, outcome="completed"),
    ], [("a", "b")])
    report = CP.critical_path(cancelled)
    assert report["duration_ms"] == 150
    assert report["status"] == "complete"
    assert report["run_status"] == "cancelled"
    assert report["complete"] is False
    assert report["terminal_nodes"] == ["a"]
    assert report["blocked_descendants"] == ["b"]
    assert {item["kind"] for item in report["anomalies"]} == {
        "terminal_with_descendants", "terminal_on_path"}

    failed = CP.critical_path(CP.Dag([
        node("a", 10, outcome="failed"),
        node("b", 10, outcome="completed"),
    ]))
    assert failed["run_status"] == "failed"
    assert failed["complete"] is False

    skipped = CP.critical_path(CP.Dag([
        node("a", 10, outcome="skipped"),
    ]))
    assert skipped["run_status"] == "skipped"


def test_include_schedule_reports_earliest_start_and_finish():
    dag = make_dag([
        node("root", 100),
        node("a", 50),
        node("b", 30),
    ], [("root", "a"), ("root", "b")])
    report = CP.critical_path(dag, include_schedule=True)
    assert report["starts"] == {"root": 0, "a": 100, "b": 100}
    assert report["finishes"] == {"root": 100, "a": 150, "b": 130}


# ---------------------------------------------------------------------------
# What-if schedules
# ---------------------------------------------------------------------------


def model_dag():
    return make_dag([
        node("prep", 500, outcome="completed", resource_class="cpu",
             kind="graph_prepare"),
        node("call_a", None, outcome="pending", resource_class="model",
             kind="model_call"),
        node("call_b", None, outcome="pending", resource_class="model",
             kind="model_call"),
        node("join", 100, outcome="pending", resource_class="check",
             kind="check"),
    ], [
        ("prep", "call_a"), ("prep", "call_b"),
        ("call_a", "join"), ("call_b", "join"),
    ])


def fast_profile():
    return CP.ScheduleProfile.fast(
        prefill_rate=1000,
        generation_rate=100,
        queue_wait_ms=0,
        startup_ms=0,
        max_concurrency=8,
        resource_concurrency={"model": 8},
    )


def limited_profile():
    return CP.ScheduleProfile.resource_limited(
        prefill_rate=1000,
        generation_rate=100,
        queue_wait_ms=0,
        startup_ms=0,
        max_concurrency=1,
        resource_concurrency={"model": 1},
    )


def test_fast_and_resource_limited_profiles_against_known_schedule():
    dag = model_dag()
    estimates = {
        "call_a": model_estimate(),
        "call_b": model_estimate(),
    }
    fast = CP.what_if(dag, fast_profile(), estimates=estimates)
    limited = CP.what_if(dag, limited_profile(), estimates=estimates)

    assert fast["status"] == "complete"
    assert fast["makespan_ms"] == 2600
    assert fast["dependency_lower_bound_ms"] == 2600
    assert fast["critical_path"] == ["prep", "call_a", "join"]
    assert fast["confidence"] == "mixed"

    assert limited["status"] == "complete"
    assert limited["makespan_ms"] == 4600
    assert limited["dependency_lower_bound_ms"] == 2600
    assert limited["lower_bound_ms"] == 4600
    assert limited["resource_wait_total_ms"] == 2000
    assert limited["bottlenecks"] == [{
        "id": "call_b",
        "resource_class": "model",
        "resource_wait_ms": 2000,
        "duration_ms": 2000,
    }]
    assert limited["critical_path"] == ["prep", "call_b", "join"]

    comparison = CP.compare_profiles(
        dag, fast=fast_profile(), resource_limited=limited_profile(),
        estimates=estimates)
    assert comparison["status"] == "complete"
    assert comparison["summary"]["fast_makespan_ms"] == 2600
    assert comparison["summary"]["resource_limited_makespan_ms"] == 4600
    assert comparison["summary"]["resource_limited_delta_ms"] == 2000
    assert comparison["summary"]["critical_path_changed"] is True


def test_what_if_uses_exact_token_rate_arithmetic():
    dag = CP.Dag([node("call", None, kind="model_call",
                       resource_class="model")])
    estimate = CP.ModelCallEstimate(
        input_tokens=1000,
        cached_input_tokens=400,
        output_tokens=30,
        prefill_rate=600,
        generation_rate=60,
        queue_wait_ms=250,
        startup_ms=100,
    )
    profile = CP.ScheduleProfile(name="known", max_concurrency=1)
    report = CP.what_if(dag, profile, estimates={"call": estimate})
    assert report["makespan_ms"] == 1850
    assert report["per_stage"][0]["duration_ms"] == 1850

    fractional = CP.Dag([node("call", None, kind="model_call",
                              resource_class="model")])
    estimate = CP.ModelCallEstimate(
        input_tokens=10,
        output_tokens=0,
        prefill_rate=Fraction(3, 2),
        generation_rate=100,
        queue_wait_ms=0,
        startup_ms=0,
    )
    report = CP.what_if(fractional, profile, estimates={"call": estimate})
    assert report["makespan_ms"] == 6667


def test_missing_queue_evidence_is_unknown_or_a_labelled_lower_bound():
    dag = CP.Dag([node("call", None, kind="model_call",
                       resource_class="model")])
    estimate = CP.ModelCallEstimate(
        input_tokens=1000, output_tokens=100, queue_wait_ms=None)
    profile = CP.ScheduleProfile(
        name="no_queue", prefill_rate=1000, generation_rate=100,
        queue_wait_ms=None, startup_ms=0, max_concurrency=1)

    strict = CP.what_if(dag, profile, estimates={"call": estimate})
    assert strict["status"] == "incomplete"
    assert strict["makespan_ms"] == "unknown"
    assert strict["lower_bound_ms"] == 0
    assert strict["complete"] is False
    assert strict["unresolved"] == [{
        "id": "call", "reason": "missing queue_wait_ms evidence"}]

    labelled = CP.what_if(
        dag, profile, estimates={"call": estimate},
        unknown_policy="lower_bound")
    assert labelled["status"] == "lower_bound"
    assert labelled["makespan_ms"] == 2000
    assert labelled["lower_bound"] is True
    assert labelled["complete"] is False
    assert any("queue_wait_ms=0" in item
               for item in labelled["assumptions"])


@pytest.mark.parametrize(
    "estimate, profile, reason",
    [
        (CP.ModelCallEstimate(input_tokens=None, output_tokens=1,
                              queue_wait_ms=0),
         CP.ScheduleProfile(name="p", prefill_rate=1000,
                            generation_rate=100, queue_wait_ms=0,
                            startup_ms=0),
         "missing input_tokens evidence"),
        (CP.ModelCallEstimate(input_tokens=1, output_tokens=None,
                              queue_wait_ms=0),
         CP.ScheduleProfile(name="p", prefill_rate=1000,
                            generation_rate=100, queue_wait_ms=0,
                            startup_ms=0),
         "missing output_tokens evidence"),
        (CP.ModelCallEstimate(input_tokens=1, output_tokens=1,
                              queue_wait_ms=0),
         CP.ScheduleProfile(name="p", generation_rate=100,
                            queue_wait_ms=0, startup_ms=0),
         "missing prefill rate evidence"),
        (CP.ModelCallEstimate(input_tokens=1, output_tokens=1,
                              queue_wait_ms=0),
         CP.ScheduleProfile(name="p", prefill_rate=1000,
                            queue_wait_ms=0, startup_ms=0),
         "missing generation rate evidence"),
    ],
)
def test_missing_token_and_rate_evidence_is_unknown(estimate, profile, reason):
    dag = CP.Dag([node("call", None, kind="model_call",
                       resource_class="model")])
    for policy in ("unknown", "lower_bound"):
        report = CP.what_if(
            dag, profile, estimates={"call": estimate},
            unknown_policy=policy)
        assert report["status"] == "incomplete"
        assert report["makespan_ms"] == "unknown"
        assert report["unresolved"][0]["reason"] == reason


def test_missing_duration_blocks_descendants_without_becoming_zero():
    dag = make_dag([
        node("prep", 500),
        node("call", None, kind="model_call", resource_class="model"),
        node("join", 100, outcome="pending"),
    ], [("prep", "call"), ("call", "join")])
    report = CP.what_if(dag, fast_profile())
    assert report["status"] == "incomplete"
    assert report["makespan_ms"] == "unknown"
    assert report["lower_bound_ms"] == 500
    assert report["unknown_nodes"] == ["call", "join"]
    assert [stage["id"] for stage in report["per_stage"]] == ["prep"]
    assert report["unresolved"] == [
        {"id": "call", "reason": "missing duration and no model-call estimate"},
        {"id": "join", "reason": "blocked by unresolved parent call"},
    ]


def test_dependency_lower_bound_survives_unknown_model_duration():
    dag = make_dag([
        node("prep", 500),
        node("call", None, kind="model_call", resource_class="model"),
    ], [("prep", "call")])
    report = CP.what_if(dag, fast_profile())
    assert report["makespan_ms"] == "unknown"
    assert report["lower_bound_ms"] == 500
    assert report["dependency_lower_bound_ms"] == 500


def test_report_schemas_are_valid_versioned_records():
    for schema in (CP.CRITICAL_PATH_SCHEMA, CP.WHAT_IF_SCHEMA,
                   CP.COMPARISON_SCHEMA):
        normalized = C.validate_schema(schema)
        assert normalized["namespace"] == schema["namespace"]
        assert normalized["version"] == "1.0"
    assert CP.ScheduleProfile(name="p", resource_concurrency=None).to_dict()[
        "resource_concurrency"] == {}


def test_what_if_bounds_and_unknown_estimate_nodes():
    dag = make_dag([node("a", 10), node("b", 10)])
    with pytest.raises(CP.CriticalPathBoundsError):
        CP.what_if(dag, fast_profile(), max_nodes=1)
    with pytest.raises(CP.UnknownNodeError):
        CP.what_if(dag, fast_profile(), estimates={"nope": model_estimate()})
    with pytest.raises(CP.CriticalPathError):
        CP.what_if(dag, fast_profile(), unknown_policy="guess")
    with pytest.raises(CP.CriticalPathError):
        CP.ModelCallEstimate(prefill_rate=1.5)
    with pytest.raises(CP.CriticalPathError):
        CP.ScheduleProfile(name="p", max_concurrency=0)


# ---------------------------------------------------------------------------
# Malformed DAGs and profiles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CP.Dag([node("a"), node("a")]),
        lambda: CP.Dag([node("a")], [CP.DagEdge("a", "b")]),
        lambda: CP.Dag([node("a")], [CP.DagEdge("a", "a"),
                                     CP.DagEdge("a", "a")]),
        lambda: node("A"),
        lambda: node("a", -1),
        lambda: node("a", True),
        lambda: node("a", outcome="maybe"),
        lambda: node("a", kind="nope"),
        lambda: node("a", resource_class="gpu"),
        lambda: node("a", parents_unknown="yes"),
        lambda: CP.ModelCallEstimate(input_tokens=1, cached_input_tokens=2),
        lambda: CP.ModelCallEstimate(queue_wait_ms=-1),
        lambda: CP.ModelCallEstimate(prefill_rate=0),
        lambda: CP.ScheduleProfile(name="Bad Name"),
        lambda: CP.ScheduleProfile(name="p", resource_concurrency={"gpu": 1}),
    ],
)
def test_malformed_dag_values_fail_explicitly(factory):
    with pytest.raises((CP.CriticalPathError, ValueError)):
        factory()


def test_what_if_cycle_can_be_reported_instead_of_raised():
    dag = make_dag([node("a", 1), node("b", 1)],
                   [("a", "b"), ("b", "a")])
    with pytest.raises(CP.CycleError):
        CP.what_if(dag, fast_profile())
    report = CP.what_if(dag, fast_profile(), on_cycle="report")
    assert report["status"] == "cycle"
    assert report["complete"] is False
    assert report["cycles"] == [["a", "b"]]


def test_report_json_and_hash_are_canonical():
    dag = make_dag([node("a", 10), node("b", 20)], [("a", "b")])
    report = CP.critical_path(dag, include_schedule=True)
    text = CP.report_json(report)
    assert json.loads(text) == report
    assert CP.report_hash(report) == C.content_hash(report)
    assert len(CP.report_hash(report)) == 64


# ---------------------------------------------------------------------------
# Cross-process determinism
# ---------------------------------------------------------------------------


def test_schedules_are_identical_across_hash_seeds():
    script = (
        "from ozagia.core.critical_path import ("
        "Dag, DagNode, ModelCallEstimate, ScheduleProfile, "
        "compare_profiles, report_hash, report_json);"
        "dag=Dag.from_pairs(["
        "DagNode('prep',500,outcome='completed',resource_class='cpu'),"
        "DagNode('a',None,outcome='pending',resource_class='model',"
        "kind='model_call'),"
        "DagNode('b',None,outcome='pending',resource_class='model',"
        "kind='model_call'),"
        "DagNode('join',100,outcome='pending',resource_class='check')],"
        "[('prep','a'),('prep','b'),('a','join'),('b','join')]);"
        "est={x:ModelCallEstimate(input_tokens=1000,output_tokens=100,"
        "queue_wait_ms=0) for x in ('a','b')};"
        "fast=ScheduleProfile.fast(prefill_rate=1000,generation_rate=100,"
        "queue_wait_ms=0,startup_ms=0,max_concurrency=8,"
        "resource_concurrency={'model':8});"
        "limited=ScheduleProfile.resource_limited(prefill_rate=1000,"
        "generation_rate=100,queue_wait_ms=0,startup_ms=0,"
        "max_concurrency=1,resource_concurrency={'model':1});"
        "report=compare_profiles(dag,fast=fast,resource_limited=limited,"
        "estimates=est);"
        "print(report_hash(report));print(report_json(report))"
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
    assert len(outputs[0].splitlines()[0]) == 64
