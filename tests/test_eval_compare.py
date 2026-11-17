"""Tests for the offline matched-arm eval comparison (WP8/M6, offline slice)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.eval import compare as EC

REPO = Path(__file__).resolve().parents[1]


def record(arm: str, task: str = "task:t1", repeat: int = 1,
           **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": f"{arm}:{task}:{repeat}",
        "arm": arm,
        "task": task,
        "repeat": repeat,
        "acceptance": "sha256:acceptance",
        "revision": "snapshot:revision",
        "model": "local/model",
        "permissions": "sha256:permissions",
        "check": "sha256:check",
        "harness": "fake",
        "prefetch_enabled": False,
        "check_result": "passed",
        "valid": True,
        "self_reported": "passed",
        "timing_ms": {
            "wall": 1000,
            "setup": 100,
            "index_cold": 200,
            "index_repeat": 50,
            "model": 300,
            "tool": 150,
            "other": 50,
            "queue_wait": 20,
            "readiness_wait": 10,
            "prefetch": 30,
            "check": 50,
            "operator": 10,
        },
        "time_to_verified_ms": 1000,
        "counts": {
            "tool_calls": 3,
            "tokens_input": 100,
            "tokens_cached": 20,
            "tokens_output": 50,
            "cost_micros": 12,
            "parse_attempts": 2,
            "parse_failures": 0,
            "fallbacks": 1,
        },
        "coverage": {"current": 8, "stale": 1, "unknown": 1, "absent": 0},
        "critical_path": {"predicted_ms": 900, "observed_ms": 1000},
        "prefetch": {
            "prepared": 2,
            "hits": 1,
            "served": 1,
            "used": 1,
            "unused": 1,
            "discarded": 0,
            "errors": 0,
            "wasted_ms": 40,
            "overhead_ms": 60,
            "estimated_saved_ms": 100,
        },
    }
    value.update(overrides)
    return value


def four_arms(task: str = "task:t1", repeat: int = 1,
              **overrides: object) -> list[dict[str, object]]:
    times = {"plain": 1000, "prompted": 950, "ozagia": 900, "ozagia_map": 850}
    return [record(arm, task=task, repeat=repeat,
                   time_to_verified_ms=times[arm], **overrides)
            for arm in EC.ARM_ORDER]


def walk(value: object):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def test_compatible_comparison_is_versioned_hashed_and_has_headline_measures():
    document = EC.build_comparison(four_arms())
    assert document["schema"] == {"namespace": EC.COMPARISON_NAMESPACE,
                                  "version": EC.COMPARISON_VERSION}
    assert document["status"] == "compatible"
    assert document["compatible"] is True
    assert document["arms"] == list(EC.ARM_ORDER)
    assert document["reasons"] == ["matched_cells"]
    assert document["aggregate"] is not None
    plain = document["aggregate"]["arms"][0]
    assert plain["arm"] == "plain"
    assert plain["runs"] == 1
    assert plain["verified_successes"] == 1
    assert plain["decisive_runs"] == 1
    assert plain["success_rate_per_mille"] == 1000
    assert plain["time_to_verified"] == {
        "total_ms": 1000, "known_runs": 1, "unknown_runs": 0,
        "mean_ms": 1000,
    }
    assert plain["verified_tasks_per_hour_milli"] == 3_600_000
    assert plain["tool_calls"]["total"] == 3
    assert plain["tokens"]["total"]["total"] == 150
    assert plain["cost_micros"]["total"] == 12
    assert plain["coverage"]["current"] == 8
    assert plain["coverage"]["current_per_mille"] == 800
    assert plain["critical_path"]["pairs"] == 1
    assert plain["critical_path"]["delta_ms"] == 100
    assert plain["prefetch"]["hits"]["total"] == 1
    assert plain["prefetch"]["net_estimated_ms"]["total"] == 40
    assert [row["arm"] for row in document["aggregate"]["deltas"]] == [
        "prompted", "ozagia", "ozagia_map"]
    map_delta = document["aggregate"]["deltas"][2]
    assert map_delta["time_to_verified_mean_delta_ms"] == -150
    assert document["verdict"] == {
        "status": "not_evaluated",
        "reason": "thresholds_unresolved",
        "checks": [],
    }
    assert document["thresholds"] is None
    assert document["bounds"]["runs_total"] == 4
    assert document["bounds"]["cells_complete"] == 1
    assert document["hash"] == EC.comparison_hash(document)
    assert EC.validate_comparison(document) == document
    assert EC.comparison_json(document) == EC.C.canonical_json(document)
    assert not any(isinstance(value, float) for value in walk(document))


def test_matched_arms_are_deterministic_across_input_order_and_hash_seeds():
    records = four_arms()
    first = EC.comparison_json(EC.build_comparison(records))
    second = EC.comparison_json(EC.build_comparison(list(reversed(records))))
    assert first == second

    script = (
        "import json,sys;"
        "from ozagia.eval import compare as EC;"
        "records=json.loads(sys.stdin.read());"
        "print(EC.comparison_json(EC.build_comparison(records)))"
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
            input=json.dumps(records),
        )
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]
    assert outputs[0].strip() == first


def test_model_self_report_is_never_acceptance():
    records = [
        record("plain", check_result="unknown",
               self_reported="I am certain this is correct"),
        record("ozagia", check_result="unknown", self_reported="passed"),
    ]
    document = EC.build_comparison(records)
    assert document["status"] == "compatible"
    plain = document["aggregate"]["arms"][0]
    assert plain["verified_successes"] == 0
    assert plain["decisive_runs"] == 0
    assert plain["unknown_check_runs"] == 1
    assert plain["success_rate_per_mille"] == "unknown"
    assert document["runs"][0]["result"]["self_reported"] == "claimed"
    assert document["runs"][1]["result"]["self_reported"] == "passed"

    failed = EC.build_comparison([
        record("plain", check_result="failed", self_reported="passed"),
        record("ozagia", check_result="failed", self_reported="passed"),
    ])
    plain = failed["aggregate"]["arms"][0]
    assert plain["decisive_runs"] == 1
    assert plain["verified_successes"] == 0
    assert plain["success_rate_per_mille"] == 0


def test_missing_metrics_stay_unknown_and_are_never_zero():
    records = [
        record("plain", counts={}, coverage=None, critical_path={},
               prefetch=None, timing_ms={"wall": 1000},
               time_to_verified_ms=1000),
        record("ozagia", counts={}, coverage=None, critical_path={},
               prefetch=None, timing_ms={"wall": 900},
               time_to_verified_ms=900),
    ]
    document = EC.build_comparison(records)
    plain = document["aggregate"]["arms"][0]
    assert plain["tool_calls"]["total"] == "unknown"
    assert plain["tool_calls"]["unknown_runs"] == 1
    assert plain["tokens"]["input"]["total"] == "unknown"
    assert plain["cost_micros"]["total"] == "unknown"
    assert plain["parse"]["failure_per_mille"] == "unknown"
    assert plain["fallbacks"]["run_per_mille"] == "unknown"
    assert plain["coverage"]["complete"] is False
    assert plain["coverage"]["current_per_mille"] == "unknown"
    assert plain["coverage"]["current"] == "unknown"
    assert plain["coverage"]["known_runs"] == 0
    assert plain["critical_path"]["pairs"] == 0
    assert plain["critical_path"]["predicted_ms"] == "unknown"
    assert plain["prefetch"]["hits"]["total"] == "unknown"
    assert plain["prefetch"]["hits"]["unknown_runs"] == 1
    assert plain["time_to_verified"]["total_ms"] == 1000


def test_unknown_compatibility_refuses_to_aggregate():
    records = four_arms()
    records[2]["acceptance"] = None
    document = EC.build_comparison(records)
    assert document["status"] == "incompatible"
    assert document["compatible"] is False
    assert document["aggregate"] is None
    assert "compatibility_unknown" in document["reasons"]
    assert any(issue["dimension"] == "acceptance"
               and issue["status"] == "unknown"
               for issue in document["compatibility"]["issues"])
    assert "aggregate: refused" in EC.format_comparison(document)


def test_mismatched_model_refuses_to_aggregate():
    records = four_arms()
    records[1]["model"] = "local/other-model"
    document = EC.build_comparison(records)
    assert document["status"] == "incompatible"
    assert document["aggregate"] is None
    assert "compatibility_mismatch" in document["reasons"]
    assert any(issue["dimension"] == "model"
               and issue["status"] == "mismatch"
               for issue in document["compatibility"]["issues"])


def test_mixed_model_across_tasks_refuses_to_aggregate():
    records = four_arms(task="task:t1")
    second = four_arms(task="task:t2")
    for item in second:
        item["model"] = "local/other-model"
    document = EC.build_comparison(records + second)
    assert document["status"] == "incompatible"
    assert document["aggregate"] is None
    assert any(issue["scope"] == "set" and issue["dimension"] == "model"
               and issue["status"] == "mismatch"
               for issue in document["compatibility"]["issues"])


def test_duplicate_run_unknown_arm_and_bad_json_are_explicit_errors():
    with pytest.raises(EC.ComparisonError) as info:
        EC.build_comparison([record("plain"), record("plain")])
    assert info.value.code == "compare.duplicate_run"
    with pytest.raises(EC.ComparisonError) as info:
        EC.build_comparison([record("mystery")])
    assert info.value.code == "compare.unknown_arm"
    with pytest.raises(EC.ComparisonError) as info:
        EC.build_comparison("not json")
    assert info.value.code == "compare.invalid_input"


def test_partial_pairing_aggregates_only_complete_cells():
    records = four_arms(task="task:t1")
    records += [record("plain", task="task:t2")]
    document = EC.build_comparison(records)
    assert document["status"] == "partial"
    assert document["compatible"] is True
    assert document["reasons"] == ["unpaired_cells"]
    assert document["cells"] == {
        "total": 2, "complete": 1, "partial": 1, "tasks": 2}
    assert document["aggregate"] is not None
    assert [row["runs"] for row in document["aggregate"]["arms"]] == [1, 1, 1, 1]
    assert len(document["runs"]) == 5


def test_no_shared_cell_is_unpaired_and_not_aggregated():
    document = EC.build_comparison([
        record("plain", task="task:t1"),
        record("ozagia", task="task:t2"),
    ])
    assert document["status"] == "unpaired"
    assert document["aggregate"] is None
    assert document["reasons"] == ["no_cell_has_every_arm"]
    assert document["cells"]["complete"] == 0


def test_missing_required_arm_is_incomplete():
    document = EC.build_comparison(
        [record("plain"), record("ozagia")],
        required_arms=["plain", "prompted", "ozagia"])
    assert document["status"] == "incomplete"
    assert document["aggregate"] is None
    assert document["reasons"] == ["missing_required_arms"]
    assert document["required_arms"] == ["plain", "prompted", "ozagia"]


def test_prefetch_on_off_pair_compares_verified_time_and_reports_waste():
    records = [
        record("plain", repeat=1, time_to_verified_ms=1200),
        record("plain", repeat=2, time_to_verified_ms=1200),
        record("ozagia", repeat=1, prefetch_enabled=False,
               time_to_verified_ms=1400,
               prefetch={
                   "prepared": 0, "hits": 0, "served": 0, "used": 0,
                   "unused": 0, "discarded": 0, "errors": 0,
                   "wasted_ms": 0, "overhead_ms": 0,
                   "estimated_saved_ms": 0,
               }),
        record("ozagia", repeat=2, prefetch_enabled=True,
               time_to_verified_ms=1000,
               prefetch={
                   "prepared": 3, "hits": 2, "served": 2, "used": 1,
                   "unused": 1, "discarded": 1, "errors": 0,
                   "wasted_ms": 120, "overhead_ms": 80,
                   "estimated_saved_ms": 200,
               }),
    ]
    document = EC.build_comparison(records)
    assert document["status"] == "compatible"
    pairing = document["aggregate"]["prefetch_pairing"]
    assert pairing["status"] == "paired"
    assert pairing["pairs"][0]["task"] == "task:t1"
    assert pairing["pairs"][0]["arm"] == "ozagia"
    assert pairing["pairs"][0]["on_mean_ms"] == 1000
    assert pairing["pairs"][0]["off_mean_ms"] == 1400
    assert pairing["pairs"][0]["delta_ms"] == -400
    assert pairing["faster_on"] == 1
    assert pairing["faster_off"] == 0
    assert pairing["total_delta_ms"] == -400

    ozagia = next(row for row in document["aggregate"]["arms"]
                  if row["arm"] == "ozagia")
    assert ozagia["prefetch"]["hits"]["total"] == 2
    assert ozagia["prefetch"]["used"]["total"] == 1
    assert ozagia["prefetch"]["unused"]["total"] == 1
    assert ozagia["prefetch"]["wasted_ms"]["total"] == 120
    assert ozagia["prefetch"]["overhead_ms"]["total"] == 80
    assert ozagia["prefetch"]["estimated_saved_ms"]["total"] == 200
    assert ozagia["prefetch"]["net_estimated_ms"]["total"] == 120
    assert ozagia["prefetch"]["use_per_mille"] == 500


def test_prefetch_is_not_paired_when_only_one_state_is_recorded():
    document = EC.build_comparison([
        record("plain", prefetch_enabled=False),
        record("ozagia", prefetch_enabled=False),
    ])
    pairing = document["aggregate"]["prefetch_pairing"]
    assert pairing["status"] == "not_paired"
    assert pairing["pairs"] == []
    assert pairing["total_delta_ms"] == "unknown"


def test_predicted_versus_observed_critical_path():
    records = [
        record("plain", critical_path={"predicted_ms": 900,
                                       "observed_ms": 1200}),
        record("ozagia", critical_path={"predicted_ms": 800,
                                        "observed_ms": 1000}),
    ]
    document = EC.build_comparison(records)
    plain = document["aggregate"]["arms"][0]
    assert plain["critical_path"] == {
        "predicted_ms": 900,
        "observed_ms": 1200,
        "delta_ms": 300,
        "mean_absolute_error_ms": 300,
        "pairs": 1,
        "unknown_pairs": 0,
    }
    assert document["runs"][0]["critical_path"]["delta_ms"] == 300

    unknown = EC.build_comparison([
        record("plain", critical_path={"predicted_ms": 900}),
        record("ozagia", critical_path={"observed_ms": 1000}),
    ])
    plain = unknown["aggregate"]["arms"][0]
    assert plain["critical_path"]["pairs"] == 0
    assert plain["critical_path"]["predicted_ms"] == "unknown"
    assert plain["critical_path"]["unknown_pairs"] == 1


def test_wall_clock_components_report_known_and_unknown_counts():
    records = [
        record("plain", timing_ms={"wall": 1000, "model": 400,
                                   "check": 50}),
        record("ozagia", timing_ms={"wall": 900, "queue_wait": 10}),
    ]
    document = EC.build_comparison(records)
    plain = document["aggregate"]["arms"][0]
    assert plain["wall_ms"] == {
        "total": 1000, "known_runs": 1, "unknown_runs": 0}
    assert plain["timing_ms"]["model_ms"] == {
        "total": 400, "known_runs": 1, "unknown_runs": 0}
    assert plain["timing_ms"]["queue_wait_ms"] == {
        "total": "unknown", "known_runs": 0, "unknown_runs": 1}
    ozagia = document["aggregate"]["arms"][1]
    assert ozagia["timing_ms"]["queue_wait_ms"] == {
        "total": 10, "known_runs": 1, "unknown_runs": 0}


def test_existing_report_ingestion_uses_report_defaults_and_seconds():
    report = {
        "base_commit": "abc123",
        "driver": "opencode",
        "backend": {"model": "local/qwen"},
        "runs": [
            {"task": "task:t1", "arm": "baseline", "repeat": 1,
             "success": True, "valid": True, "wall_s": 10.0,
             "tokens_in": 100, "tokens_out": 20, "cost_usd": 0.000123,
             "tool_calls": 3,
             "timing": {
                 "setup": {"s": 1.0}, "index": {"cold_s": 2.0},
                 "model": {"s": 4.0}, "tools": {"s": 1.0},
                 "other": {"s": 1.0}, "check": {"s": 1.0},
             }},
            {"task": "task:t1", "arm": "dev_memory", "repeat": 1,
             "success": True, "valid": True, "wall_s": 9.0,
             "tokens_in": 90, "tokens_out": 20, "cost_usd": 0.000111,
             "tool_calls": 2,
             "timing": {
                 "setup": {"s": 1.0}, "index": {"cold_s": 2.0},
                 "model": {"s": 3.0}, "tools": {"s": 1.0},
                 "other": {"s": 1.0}, "check": {"s": 1.0},
             }},
        ],
    }
    document = EC.build_comparison(
        report,
        defaults={"acceptance": "sha256:a", "permissions": "sha256:p",
                  "check": "sha256:c"})
    assert document["status"] == "compatible"
    assert document["arms"] == ["plain", "ozagia"]
    plain = document["aggregate"]["arms"][0]
    assert plain["time_to_verified"]["total_ms"] == 14_000
    assert plain["cost_micros"]["total"] == 123
    assert plain["wall_ms"]["total"] == 10_000
    assert document["runs"][0]["timing_ms"]["wall_ms"] == 10_000
    assert document["runs"][0]["counts"]["cost_micros"] == 123

    unknown = EC.build_comparison(report)
    assert unknown["status"] == "incompatible"
    assert unknown["aggregate"] is None
    assert any(issue["dimension"] == "acceptance"
               for issue in unknown["compatibility"]["issues"])


def test_graph_intent_alias_maps_to_the_map_arm():
    document = EC.build_comparison([
        record("baseline"),
        record("graph_intent", time_to_verified_ms=900),
    ])
    assert document["status"] == "compatible"
    assert document["arms"] == ["plain", "ozagia_map"]
    custom = EC.build_comparison(
        [record("control"), record("map_service")],
        aliases={"control": "plain", "map_service": "ozagia_map"})
    assert custom["arms"] == ["plain", "ozagia_map"]


def test_parse_fallback_and_coverage_rates():
    records = [
        record("plain", counts={
            "parse_attempts": 4, "parse_failures": 1, "fallbacks": 0,
            "tool_calls": 1, "tokens_input": 1, "tokens_output": 1,
            "cost_micros": 1,
        }, coverage={"current": 2, "stale": 2, "unknown": 0, "absent": 0}),
        record("ozagia", counts={
            "parse_attempts": 4, "parse_failures": 2, "fallbacks": 2,
            "tool_calls": 1, "tokens_input": 1, "tokens_output": 1,
            "cost_micros": 1,
        }, coverage={"current": 4, "stale": 0, "unknown": 0, "absent": 0}),
    ]
    document = EC.build_comparison(records)
    plain = document["aggregate"]["arms"][0]
    assert plain["parse"]["failure_per_mille"] == 250
    assert plain["fallbacks"]["run_per_mille"] == 0
    assert plain["coverage"]["current_per_mille"] == 500
    assert plain["coverage"]["stale_per_mille"] == 500
    ozagia = document["aggregate"]["arms"][1]
    assert ozagia["parse"]["failure_per_mille"] == 500
    assert ozagia["fallbacks"]["run_per_mille"] == 1000
    assert ozagia["coverage"]["current_per_mille"] == 1000


def test_thresholds_are_caller_supplied_and_unresolved_by_default():
    document = EC.build_comparison([
        record("plain", time_to_verified_ms=1200),
        record("ozagia", time_to_verified_ms=1000),
    ])
    assert document["thresholds"] is None
    assert document["verdict"]["status"] == "not_evaluated"
    assert document["verdict"]["reason"] == "thresholds_unresolved"

    passed = EC.build_comparison([
        record("plain", time_to_verified_ms=1200),
        record("ozagia", time_to_verified_ms=1000),
    ], thresholds={
        "reference_arm": "plain", "candidate_arm": "ozagia",
        "min_verified_runs": 1, "max_success_drop_per_mille": 0,
        "min_time_to_verified_improvement_per_mille": 100,
    })
    assert passed["verdict"]["status"] == "pass"
    assert passed["verdict"]["reason"] == "thresholds_met"
    assert passed["aggregate"]["paired_time"]["candidate_improvement_per_mille"] == 167

    failed = EC.build_comparison([
        record("plain", time_to_verified_ms=1200),
        record("ozagia", time_to_verified_ms=1000),
    ], thresholds={
        "reference_arm": "plain", "candidate_arm": "ozagia",
        "min_verified_runs": 1, "max_success_drop_per_mille": 0,
        "min_time_to_verified_improvement_per_mille": 500,
    })
    assert failed["verdict"]["status"] == "fail"
    assert failed["verdict"]["reason"] == "threshold_failed"

    with pytest.raises(EC.ComparisonError) as info:
        EC.build_comparison([record("plain"), record("ozagia")],
                            thresholds={"reference_arm": "plain"})
    assert info.value.code == "compare.invalid_thresholds"


def test_bounds_record_limit_is_explicit():
    document = EC.build_comparison(four_arms(), max_records=2)
    assert document["omissions"]["truncated"] is True
    assert document["omissions"]["omitted_runs"] == 2
    assert document["omissions"]["reason"] == "limit"
    assert document["omissions"]["next_cursor"] == "prompted:task:t1:1"
    assert document["bounds"]["runs_total"] == 4
    assert document["bounds"]["runs_included"] == 2
    assert document["arms"] == ["plain", "prompted"]
    assert document["status"] == "compatible"

    with pytest.raises(EC.ComparisonError) as info:
        EC.build_comparison([], max_records=EC.HARD_MAX_RECORDS + 1)
    assert info.value.code == "compare.bounds_exceeded"


def test_byte_trimming_drops_detail_rows_deterministically():
    records = []
    for index in range(40):
        task = f"task:t{index:03d}"
        for arm in EC.ARM_ORDER:
            records.append(record(arm, task=task))
    document = EC.build_comparison(records, max_bytes=32 * 1024)
    assert len(EC.comparison_json(document).encode("utf-8")) <= 32 * 1024
    assert document["omissions"]["truncated"] is True
    assert document["omissions"]["reason"] == "bytes"
    assert document["omissions"]["omitted_details"] > 0
    assert document["aggregate"] is not None
    assert document["bounds"]["runs_total"] == 160
    first_hash = document["hash"]
    second = EC.build_comparison(list(reversed(records)), max_bytes=32 * 1024)
    assert second["hash"] == first_hash
    assert second["omissions"] == document["omissions"]


def test_too_small_byte_budget_is_an_explicit_error():
    with pytest.raises(EC.ComparisonError) as info:
        EC.build_comparison(four_arms(), max_bytes=1024)
    assert info.value.code == "compare.bounds_exceeded"


def test_validate_rejects_a_tampered_hash_or_aggregate():
    document = EC.build_comparison(four_arms())
    tampered = dict(document)
    tampered["hash"] = "0" * 64
    with pytest.raises(EC.ComparisonError) as info:
        EC.validate_comparison(tampered)
    assert info.value.code == "compare.invalid_comparison"

    incompatible = EC.build_comparison([
        record("plain"), record("ozagia", model="local/other")])
    tampered = dict(incompatible)
    tampered["aggregate"] = {"arms": []}
    with pytest.raises(EC.ComparisonError) as info:
        EC.validate_comparison(tampered)
    assert info.value.code == "compare.invalid_comparison"


def test_created_at_is_excluded_from_the_hash():
    records = four_arms()
    first = EC.build_comparison(records, created_at=1)
    second = EC.build_comparison(records, created_at=2)
    assert first["hash"] == second["hash"]
    assert first["hash"] == EC.comparison_hash(first)
    assert EC.comparison_json(first) != EC.comparison_json(second)


def test_comparison_schema_is_versioned_and_fingerprinted():
    schema = EC.comparison_schema()
    assert schema["namespace"] == EC.COMPARISON_NAMESPACE
    assert schema["version"] == EC.COMPARISON_VERSION
    fingerprint = EC.comparison_schema_fingerprint()
    assert EC.comparison_schema_fingerprint() == fingerprint
    assert len(fingerprint) == 64


def test_format_comparison_is_readable_and_bounded():
    document = EC.build_comparison(four_arms())
    text = EC.format_comparison(document, max_lines=30)
    assert text.startswith("eval comparison")
    assert "status: compatible" in text
    assert "plain" in text
    assert "verdict: not_evaluated (thresholds_unresolved)" in text
    assert "hash:" in text
    assert len(text.splitlines()) <= 30
    assert "None" not in text


def test_json_text_input_is_accepted():
    document = EC.build_comparison(json.dumps(four_arms()))
    assert document["status"] == "compatible"
    assert document["arms"] == list(EC.ARM_ORDER)
