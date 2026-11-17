"""Tests for the offline prefetch planner and its accounting."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core import prefetch as PF

REPO = Path(__file__).resolve().parents[1]
SCOPE = {"task": "task:demo"}
SCOPE_HASH = C.content_hash(SCOPE)


def candidate(candidate_id: str = "a", *, kind: str = "affected_tests",
              **overrides: object) -> PF.PrefetchCandidate:
    value: dict[str, object] = {
        "id": candidate_id,
        "kind": kind,
        "source": "ozagia.core.goals",
        "source_id": f"source-{candidate_id}",
        "priority": 10,
        "scope_hash": SCOPE_HASH,
        "source_hash": "hash-a",
        "expected_saving_ms": 100,
        "cost_ms": 100,
        "io_bytes": 10,
        "memory_bytes": 10,
        "payload_bytes": 10,
        "authorized": True,
        "trusted": True,
        "read_only": True,
        "freshness": "current",
    }
    value.update(overrides)
    return PF.PrefetchCandidate(**value)  # type: ignore[arg-type]


def plan(candidates, **kwargs):
    return PF.build_prefetch_plan(candidates, scope=SCOPE, **kwargs)


def prepare(ledger, candidate_id: str, **overrides) -> dict:
    value = {"prepared_at_ms": 10, "prepared_ms": 100, "payload_bytes": 10,
             "value_hash": "value-a"}
    value.update(overrides)
    return ledger.record_prepared(candidate_id, **value)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_plan_is_versioned_deterministic_and_priority_ordered():
    first = plan([
        candidate("b", priority=20),
        candidate("a", priority=10),
    ])
    second = plan([
        candidate("a", priority=10),
        candidate("b", priority=20),
    ])
    assert first["schema"] == {"namespace": PF.PLAN_NAMESPACE,
                               "version": PF.PLAN_VERSION}
    assert [row["id"] for row in first["selected"]] == ["a", "b"]
    assert first["hash"] == second["hash"]
    assert PF.plan_json(first) == PF.plan_json(second)
    assert first["wave_count"] == 1
    assert first["estimated_ms"] == 100


def test_priority_ties_are_broken_deterministically_by_id():
    result = plan([candidate("c"), candidate("a"), candidate("b")])
    assert [row["id"] for row in result["selected"]] == ["a", "b", "c"]


@pytest.mark.parametrize("kind", PF.FORBIDDEN_KINDS)
def test_side_effecting_candidate_kinds_are_refused(kind):
    with pytest.raises(PF.PrefetchError) as info:
        candidate("a", kind=kind)
    assert info.value.code == "prefetch.forbidden_kind"


def test_read_only_false_is_refused_even_for_a_read_kind():
    with pytest.raises(PF.PrefetchError) as info:
        candidate("a", read_only=False)
    assert info.value.code == "prefetch.forbidden_kind"


def test_untrusted_unauthorized_stale_expired_and_scope_mismatched_are_refused():
    result = plan([
        candidate("authorized", authorized=False),
        candidate("trusted", trusted=False),
        candidate("stale", freshness="stale"),
        candidate("absent", freshness="absent"),
        candidate("expired", expires_at_ms=5),
        candidate("scope", scope_hash="other-scope"),
    ], now_ms=10)
    assert result["status"] == "empty"
    assert result["selected"] == []
    reasons = {row["id"]: row["reason"] for row in result["refused"]}
    assert reasons == {
        "authorized": "unauthorized",
        "trusted": "untrusted",
        "stale": "stale",
        "absent": "absent",
        "expired": "expired",
        "scope": "scope_mismatch",
    }
    assert result["deferred"] == []


def test_time_io_memory_and_payload_budgets_defer_in_priority_order():
    candidates = [candidate(f"c{index}", priority=index, cost_ms=1000,
                            io_bytes=10, memory_bytes=10, payload_bytes=10)
                  for index in range(5)]
    result = plan(candidates, policy=PF.PrefetchPolicy(
        max_total_ms=2000, max_io_bytes=20, max_memory_bytes=20,
        max_payload_bytes=20))
    assert [row["id"] for row in result["selected"]] == ["c0", "c1"]
    reasons = {row["id"]: row["reason"] for row in result["deferred"]}
    assert reasons == {"c2": "time_budget", "c3": "time_budget",
                       "c4": "time_budget"}


@pytest.mark.parametrize("field,reason", [
    ("max_io_bytes", "io_budget"),
    ("max_memory_bytes", "memory_budget"),
    ("max_payload_bytes", "byte_budget"),
])
def test_each_resource_budget_is_enforced(field, reason):
    result = plan([candidate("a", cost_ms=0, io_bytes=10, memory_bytes=10,
                             payload_bytes=10)],
                  policy=PF.PrefetchPolicy(**{field: 5}))
    assert result["selected"] == []
    assert [row["reason"] for row in result["deferred"]] == [reason]


def test_selection_and_concurrency_caps_are_enforced():
    candidates = [candidate(f"c{index}", priority=index)
                  for index in range(6)]
    result = plan(candidates, policy=PF.PrefetchPolicy(
        max_selected=3, max_concurrency=2, max_total_ms=10_000))
    assert [row["id"] for row in result["selected"]] == ["c0", "c1", "c2"]
    assert len(result["deferred"]) == 3
    assert {row["reason"] for row in result["deferred"]} == {"max_selected"}
    assert [row["wave"] for row in result["selected"]] == [0, 0, 1]


def test_dependencies_place_a_child_after_its_parent_even_if_the_child_sorts_first():
    result = plan([
        candidate("child", priority=10, parents=("parent",)),
        candidate("parent", priority=50),
    ], policy=PF.PrefetchPolicy(max_concurrency=1))
    ordered = [row["id"] for row in result["selected"]]
    assert ordered == ["parent", "child"]
    waves = {row["id"]: row["wave"] for row in result["selected"]}
    assert waves["child"] > waves["parent"]


def test_unknown_parent_and_cycle_are_explicit_errors():
    with pytest.raises(PF.PrefetchError) as missing:
        plan([candidate("a", parents=("missing",))])
    assert missing.value.code == "prefetch.unknown_parent"
    with pytest.raises(PF.PrefetchError) as cycle:
        plan([
            candidate("a", parents=("b",)),
            candidate("b", parents=("a",)),
        ])
    assert cycle.value.code == "prefetch.cycle"


def test_cancelled_plan_selects_nothing_and_records_the_reason():
    result = plan([candidate("a"), candidate("b")], cancelled=True,
                  cancelled_reason="scope changed")
    assert result["status"] == "cancelled"
    assert result["selected"] == []
    assert result["cancelled_reason"] == "scope changed"
    assert {row["reason"] for row in result["refused"]} == {"cancelled"}
    ledger = PF.PrefetchLedger(result, now_ms=0)
    assert ledger.use("a", at_ms=1)["status"] == "cancelled"


def test_plan_rejects_more_candidates_than_the_bound():
    with pytest.raises(PF.PrefetchError) as info:
        plan([candidate(f"c{index}") for index in range(3)],
             policy=PF.PrefetchPolicy(max_candidates=2))
    assert info.value.code == "prefetch.bounds_exceeded"


# ---------------------------------------------------------------------------
# Ledger accounting
# ---------------------------------------------------------------------------


def test_ledger_separates_hits_unused_waste_and_overhead():
    result = plan([candidate("a", expected_saving_ms=100),
                   candidate("b", expected_saving_ms=200)])
    ledger = PF.PrefetchLedger(result, now_ms=0)
    prepare(ledger, "a", prepared_ms=100, payload_bytes=20)
    prepare(ledger, "b", prepared_ms=50, payload_bytes=10)
    hit = ledger.use("a", at_ms=30, current_scope_hash=SCOPE_HASH,
                     source_hash="hash-a", freshness="current")
    assert hit == {"id": "a", "status": "hit", "served": True,
                   "current": True}
    report = ledger.report()
    assert report["prepared"] == 2
    assert report["hits"] == 1
    assert report["unused"] == 1
    assert report["wasted_ms"] == 50
    assert report["wasted_bytes"] == 10
    assert report["overhead_ms"] == 150
    assert report["estimated_saved_ms"] == 100
    assert report["net_estimated_ms"] == -50
    assert report["items"][0]["state"] == "used"
    assert report["items"][1]["state"] == "unused"
    assert PF.ledger_json(report)


def test_a_second_use_is_served_but_counted_once_as_a_hit():
    ledger = PF.PrefetchLedger(plan([candidate("a")]), now_ms=0)
    prepare(ledger, "a", prepared_ms=20)
    assert ledger.use("a", at_ms=1, current_scope_hash=SCOPE_HASH)["status"] == "hit"
    assert ledger.use("a", at_ms=2,
                      current_scope_hash=SCOPE_HASH)["status"] == "already_used"
    report = ledger.report()
    assert report["hits"] == 1
    assert report["served"] == 2


def test_use_without_a_scope_check_is_served_but_not_current():
    ledger = PF.PrefetchLedger(plan([candidate("a")]), now_ms=0)
    prepare(ledger, "a")
    result = ledger.use("a", at_ms=1)
    assert result["served"] is True
    assert result["current"] is False
    assert result["status"] == "hit_unknown"


def test_unknown_freshness_is_served_but_is_never_current():
    ledger = PF.PrefetchLedger(plan([candidate("a", freshness="unknown")]),
                               now_ms=0)
    prepare(ledger, "a")
    result = ledger.use("a", at_ms=1)
    assert result["served"] is True
    assert result["current"] is False
    assert result["status"] == "hit_unknown"


def test_an_unknown_source_hash_is_never_current():
    ledger = PF.PrefetchLedger(
        plan([candidate("a", source_hash="unknown")]), now_ms=0)
    prepare(ledger, "a")
    result = ledger.use("a", at_ms=1, current_scope_hash=SCOPE_HASH,
                        freshness="current")
    assert result["served"] is True
    assert result["current"] is False
    assert result["status"] == "hit_unknown"


def test_stale_scope_changed_cancelled_error_untrusted_expired_and_source_changed_are_never_current():
    result = plan([
        candidate("stale"), candidate("scope"), candidate("cancelled"),
        candidate("error"), candidate("untrusted"), candidate("expired"),
        candidate("source"), candidate("unauthorized"),
    ])
    ledger = PF.PrefetchLedger(result, now_ms=0)
    for candidate_id in ("stale", "scope", "cancelled", "error", "untrusted",
                         "expired", "source", "unauthorized"):
        prepare(ledger, candidate_id, expires_at_ms=10
                if candidate_id == "expired" else None,
                error="boom" if candidate_id == "error" else None)
    checks = {
        "stale": ledger.use("stale", at_ms=20, freshness="stale"),
        "scope": ledger.use("scope", at_ms=20,
                            current_scope_hash="different"),
        "error": ledger.use("error", at_ms=20),
        "untrusted": ledger.use("untrusted", at_ms=20, trusted=False),
        "expired": ledger.use("expired", at_ms=20),
        "source": ledger.use("source", at_ms=20, source_hash="different"),
        "unauthorized": ledger.use("unauthorized", at_ms=20,
                                   authorized=False),
    }
    assert checks["stale"]["status"] == "stale"
    assert checks["scope"]["status"] == "scope_changed"
    assert checks["error"]["status"] == "error"
    assert checks["untrusted"]["status"] == "untrusted"
    assert checks["expired"]["status"] == "expired"
    assert checks["source"]["status"] == "source_changed"
    assert checks["unauthorized"]["status"] == "unauthorized"
    assert all(item["served"] is False and item["current"] is False
               for item in checks.values())
    ledger.cancel("user cancelled")
    cancelled = ledger.use("cancelled", at_ms=20)
    assert cancelled == {"id": "cancelled", "status": "cancelled",
                         "served": False, "current": False}
    report = ledger.report()
    assert report["status"] == "cancelled"
    assert report["hits"] == 0
    assert report["rejected"]["stale"] == 1
    assert report["rejected"]["scope_changed"] == 1


def test_scope_change_cancels_and_discards_prepared_items():
    ledger = PF.PrefetchLedger(plan([candidate("a")]), now_ms=0)
    prepare(ledger, "a")
    new_hash = ledger.changed_scope({"task": "task:other"})
    assert new_hash == C.content_hash({"task": "task:other"})
    assert ledger.cancelled_reason == "scope_changed"
    assert ledger.prepared == {}
    report = ledger.report()
    assert report["status"] == "cancelled"
    assert report["prepared"] == 1
    assert report["discarded"] == 1
    assert report["wasted_ms"] == 100
    assert ledger.use("a", at_ms=1)["status"] == "cancelled"


def test_a_tampered_selected_plan_row_is_revalidated_by_the_ledger():
    result = plan([candidate("a")])
    result["selected"][0]["kind"] = "model_call"
    result["hash"] = C.content_hash(
        {key: value for key, value in result.items() if key != "hash"})
    with pytest.raises(PF.PrefetchError) as info:
        PF.PrefetchLedger(result, now_ms=0)
    assert info.value.code == "prefetch.forbidden_kind"


def test_a_prepare_must_target_a_selected_candidate():
    ledger = PF.PrefetchLedger(plan([candidate("a")]), now_ms=0)
    with pytest.raises(PF.PrefetchError) as info:
        prepare(ledger, "b")
    assert info.value.code == "prefetch.not_selected"


# ---------------------------------------------------------------------------
# Candidate builders
# ---------------------------------------------------------------------------


def test_goal_builder_is_bounded_and_does_not_assume_authorization():
    goal = {
        "targets": ["file:src/a.py"],
        "scope": ["file:src/b.py"],
        "criteria": [
            {"id": f"c{index}", "kind": "test_passes",
             "args": {"key": f"test:tests/test_{index}.py::test_{index}"}}
            for index in range(50)
        ],
    }
    built = PF.candidates_from_goal(goal, scope_hash=SCOPE_HASH,
                                    max_candidates=4)
    assert len(built) == 4
    assert all(item.read_only for item in built)
    assert all(item.authorized is False and item.trusted is False
               for item in built)
    assert {item.kind for item in built} <= set(PF.READ_ONLY_KINDS)
    result = plan(built)
    assert result["selected"] == []
    assert {row["reason"] for row in result["refused"]} == {"unauthorized"}


def test_goal_builder_explicitly_authorized_candidates_are_plannable():
    goal = {"criteria": [{
        "id": "c1", "kind": "test_passes",
        "args": {"key": "test:tests/test_a.py::test_a"},
    }]}
    built = PF.candidates_from_goal(goal, scope_hash=SCOPE_HASH,
                                    authorized=True, trusted=True)
    result = plan(built)
    assert [row["kind"] for row in result["selected"]] == ["affected_tests"]


def test_graph_neighborhood_builder_uses_injected_metadata_only():
    built = PF.candidates_from_graph_neighborhood({
        "video.py": {"source_hash": "sha256:" + "a" * 64,
                     "freshness": "current", "cost_ms": 10,
                     "expected_saving_ms": 50},
    }, scope_hash=SCOPE_HASH, authorized=True, trusted=True)
    assert len(built) == 1
    assert built[0].source_hash == "sha256:" + "a" * 64
    assert built[0].cost_ms == 10
    assert built[0].expected_saving_ms == 50
    assert built[0].id.startswith("graph_neighborhood:")


def test_profile_builder_maps_read_only_span_kinds_only():
    profile = {"data": {"spans": [
        {"id": "a", "kind": "check", "resource_class": "check"},
        {"id": "b", "kind": "graph_prepare", "resource_class": "graph"},
        {"id": "c", "kind": "model_call", "resource_class": "model"},
        {"id": "d", "kind": "human_gate", "resource_class": "human"},
    ]}}
    built = PF.candidates_from_profile(profile, scope_hash=SCOPE_HASH)
    assert {item.source_id for item in built} == {"a", "b", "c"}
    assert all(item.kind in PF.READ_ONLY_KINDS for item in built)
    assert all(item.authorized is False for item in built)


def test_a_ledger_feeds_the_task_report_prefetch_section():
    from ozagia.core import task_profile as TP

    ledger = PF.PrefetchLedger(plan([candidate("a")]), now_ms=0)
    prepare(ledger, "a")
    ledger.use("a", at_ms=1, current_scope_hash=SCOPE_HASH,
               source_hash="hash-a", freshness="current")
    manifest = {
        "run_id": "run1", "status": "ok", "duration_s": 1.0,
        "spec": {"id": "demo", "tools": []}, "steps": [],
    }
    report = TP.build_task_report(TP.TaskRecords(
        run={"state": "complete", "manifest": manifest},
        prefetch=ledger.report()))
    assert report["prefetch"]["state"] == "recorded"
    assert report["prefetch"]["prepared"] == 1
    assert report["prefetch"]["hits"] == 1
    assert report["prefetch"]["unused"] == 0
    assert report["prefetch"]["overhead_ms"] == 100


def test_plan_bytes_are_identical_across_hash_seeds():
    script = (
        "import json;"
        "from ozagia.core import contracts as C, prefetch as P;"
        "s={'task':'task:demo'};h=C.content_hash(s);"
        "c=[P.PrefetchCandidate('b','affected_tests','s','b',20,h,"
        "source_hash='h',cost_ms=10,authorized=True,trusted=True,"
        "freshness='current'),"
        "P.PrefetchCandidate('a','graph_neighborhood','s','a',10,h,"
        "source_hash='h',cost_ms=10,authorized=True,trusted=True,"
        "freshness='current')];"
        "p=P.build_prefetch_plan(c,scope=s);"
        "print(P.plan_json(p))"
    )
    outputs = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO / "src")
        env["PYTHONHASHSEED"] = seed
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO, env=env, check=True,
            capture_output=True, text=True)
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])["schema"]["namespace"] == PF.PLAN_NAMESPACE
