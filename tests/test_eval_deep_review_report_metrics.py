import json
import shutil
from pathlib import Path

import pytest

from ozagia.eval import deep_review_report
from ozagia.eval.deep_review_report import DEFAULT_FIXTURE, evaluate


@pytest.fixture(autouse=True)
def _set_temp_checkout_resource_policy(monkeypatch):
    """Persist a test policy into evaluator checkouts before they are committed."""
    copytree = shutil.copytree

    def copytree_with_policy(source, destination, *args, **kwargs):
        result = copytree(source, destination, *args, **kwargs)
        policy_dir = Path(destination) / "ozagia"
        policy_dir.mkdir(parents=True, exist_ok=True)
        (policy_dir / "resources.toml").write_text(
            "enabled = false\n", encoding="utf-8")
        return result

    monkeypatch.setattr(shutil, "copytree", copytree_with_policy)


def _stable(result):
    """Drop host-dependent timing/memory fields before determinism checks."""
    stable = json.loads(json.dumps(result))
    stable.pop("duration", None)
    stable.pop("peak_memory", None)
    stable.pop("report_bytes", None)
    for report in ("first_report", "second_report"):
        stable[report].pop("duration_s", None)
        stable[report].pop("python_peak_bytes", None)
        stable[report].pop("report_bytes", None)
    return stable


def test_report_evaluator_scores_reviewed_labels_and_reports_measurements():
    result = evaluate()
    assert result["labels"]["cases"] == 4
    assert result["labels"]["report_findings"] == 4
    metrics = result["metrics"]["first"]
    assert metrics["expected"] == 8
    assert metrics["predicted"] == 8
    assert metrics["true_positive"] == 8
    assert metrics["false_positive"] == 0
    assert metrics["false_negative"] == 0
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["false_positive_rate"] is None
    assert metrics["false_discovery_rate"] == 0.0
    assert metrics["report_noise_ratio"] == 0.0
    assert metrics["complete_subjects"] == [
        "data-structures", "failure-modes", "performance", "scalability",
        "tests-and-coverage",
    ]
    assert metrics["subjects"]["performance"]["precision"] == 1.0
    assert metrics["subjects"]["tests-and-coverage"]["predicted"] == 4
    assert len(metrics["subjects"]) == 11
    assert metrics["subjects"]["architecture"]["precision"] is None
    assert any("no reviewed labels" in note
               for note in metrics["subjects"]["architecture"]["notes"])

    assert result["duration"]["total_s"] >= 0
    assert result["duration"]["single_host_sample"] is True
    assert result["peak_memory"]["python_tracemalloc_bytes"]["first_report"] > 0
    assert result["peak_memory"]["python_tracemalloc_bytes"]["second_report"] > 0
    assert result["peak_memory"]["process_rss"]["available"] in {True, False}
    assert result["report_bytes"]["first"]["bytes"] > 0
    assert result["report_bytes"]["first"]["files"] > 0
    assert result["report_bytes"]["total_bytes"] > 0
    assert result["counts"]["first"]["findings"] == 8
    assert result["counts"]["first"]["by_subject"]["tests-and-coverage"] == 4
    assert result["comparison"]["stable_ids"] is True


def test_report_evaluator_repeat_is_deterministic_apart_from_measurements():
    first = evaluate()
    second = evaluate()
    assert _stable(first) == _stable(second)


def test_report_evaluator_scores_empty_findings_without_inventing_metrics(monkeypatch):
    def build(_root, _out, **_kwargs):
        return {"manifest": {"complete": True, "status": "complete",
                             "subjects": {}, "comparison": {}},
                "findings": []}

    monkeypatch.setattr(deep_review_report, "build_deep_review", build)
    result = evaluate()
    metrics = result["metrics"]["first"]
    assert result["status"] == "partial"
    assert metrics["expected"] == 0
    assert metrics["excluded_expected"] == 8
    assert len(metrics["unavailable_subjects"]) == 5
    assert metrics["predicted"] == 0
    assert metrics["true_positive"] == 0
    assert metrics["false_negative"] == 0
    assert metrics["precision"] is None
    assert metrics["false_positive_rate"] is None
    assert metrics["report_noise_ratio"] is None
    assert metrics["recall"] is None
    assert result["first_report"]["report_bytes"] is None


def test_report_evaluator_rejects_missing_and_malformed_labels(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        evaluate(tmp_path)
    (tmp_path / "labels.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid deep-review labels JSON"):
        evaluate(tmp_path)


def test_report_evaluator_uses_explicit_labels_path(tmp_path):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({
        "schema_version": 2,
        "cases": [],
        "report_findings": [],
        "complete_subjects": [],
    }), encoding="utf-8")
    result = evaluate(DEFAULT_FIXTURE, labels=labels)
    assert result["labels"]["file"] == str(labels)
    assert result["metrics"]["first"]["expected"] == 0
    assert result["metrics"]["first"]["predicted"] == 8
    assert result["metrics"]["first"]["precision"] is None
    assert result["metrics"]["first"]["recall"] is None
    # All findings are unlabeled when no subject is marked complete.
    assert result["metrics"]["first"]["report_noise_ratio"] == 1.0
