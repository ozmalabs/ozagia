import json
import shutil
from pathlib import Path

import pytest

from ozagia.eval import deep_review_comparison
from ozagia.eval.deep_review_comparison import evaluate_graph


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
    stable = json.loads(json.dumps(result))
    for key in ("duration", "peak_memory"):
        stable.pop(key, None)
    for report in stable.get("reports", {}).values():
        report.pop("duration_s", None)
        report.pop("python_peak_bytes", None)
        report.pop("report_bytes", None)
    for arm in stable.get("priority_evidence", {}).values():
        arm.pop("reason", None)
    return stable


def test_graph_comparison_measures_priority_evidence_and_added_noise():
    result = evaluate_graph()
    assert result["environment"]["status"] == "admitted"
    assert result["commits"]["same"] is True
    reports = result["reports"]
    assert reports["graph_less"]["findings"] == 8
    assert reports["graph_aware"]["findings"] == 16
    quality = result["quality"]
    assert quality["graph_less"]["precision"] == 1.0
    assert quality["graph_less"]["recall"] == 1.0
    assert quality["graph_less"]["report_noise_ratio"] == 0.0
    assert quality["graph_aware"]["recall"] == 1.0
    assert quality["graph_aware"]["report_noise_ratio"] > 0
    assert quality["graph_aware"]["unlabeled"] > 0
    assert quality["graph_less"]["precision_all_predictions"] == 1.0
    assert quality["graph_aware"]["precision_all_predictions"] < 1.0
    priority = result["priority_evidence"]
    assert priority["graph_less"]["freshness"] == "error"
    assert priority["graph_less"]["findings_with_direct_symbols"] == 0
    assert priority["graph_aware"]["freshness"] == "fresh"
    assert priority["graph_aware"]["nodes"] > 0
    assert priority["graph_aware"]["edges"] > 0
    assert priority["graph_aware"]["findings_with_direct_symbols"] == 4
    assert priority["graph_aware"]["findings_without_graph_omission"] == 4
    assert result["finding_ids"]["added_by_graph"] == 8
    assert result["finding_ids"]["removed_by_graph"] == 0
    assert result["duration"]["total_s"] >= 0


def test_graph_comparison_repeat_is_deterministic_apart_from_measurements():
    first = evaluate_graph()
    second = evaluate_graph()
    assert _stable(first) == _stable(second)


def test_graph_comparison_handles_empty_findings(monkeypatch):
    def build(_root, _out, **kwargs):
        freshness = "fresh" if kwargs.get("index") else "error"
        return {"manifest": {"complete": True, "status": "complete",
                             "subjects": {},
                             "prioritization_graph": {"freshness": freshness}},
                "findings": []}

    monkeypatch.setattr(deep_review_comparison, "build_deep_review", build)
    result = evaluate_graph()
    assert result["quality"]["graph_less"]["predicted"] == 0
    assert result["quality"]["graph_aware"]["predicted"] == 0
    assert result["quality"]["graph_less"]["precision"] is None
    assert result["quality"]["graph_aware"]["precision"] is None
    assert result["quality"]["graph_less"]["report_noise_ratio"] is None
    assert result["priority_evidence"]["graph_aware"]["freshness"] == "fresh"


def test_graph_comparison_rejects_missing_or_malformed_labels(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        evaluate_graph(labels=tmp_path / "absent.json")
    (tmp_path / "labels.json").write_text(json.dumps({
        "schema_version": 2,
        "cases": [{"id": "x", "file": "a.py", "expected": "not-a-list"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="expected must be a list"):
        evaluate_graph(labels=tmp_path / "labels.json")
