import json
import os
import shutil
from pathlib import Path

import pytest

from ozagia.eval import deep_review_comparison, deep_review_report
from ozagia.eval.deep_review_comparison import (
    DEFAULT_FIXTURE,
    DEFAULT_RANGE_LABELS,
    evaluate_range,
)


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
    stable.get("range_graph", {}).pop("reason", None)
    return stable


def test_eval_checkout_does_not_copy_or_run_source_git_hooks(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("pre-commit hook fixture requires POSIX")
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    marker = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\nprintf ran > {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook.parent))

    root, _head = deep_review_comparison._init_checkout(source, workspace, "copy")

    assert not marker.exists()
    assert not (root / ".git" / "hooks" / "pre-commit").exists()


def test_report_evaluator_does_not_run_source_git_hooks(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("pre-commit hook fixture requires POSIX")
    from ozagia.core.repo_review import PreflightError

    source = tmp_path / "fixture"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    (source / "labels.json").write_text(
        json.dumps({"cases": [], "report_findings": [],
                    "complete_subjects": []}), encoding="utf-8")
    hook = source / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    marker = tmp_path / "report-hook-ran"
    hook.write_text(f"#!/bin/sh\nprintf ran > {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook.parent))
    monkeypatch.setattr(
        deep_review_report, "build_deep_review",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PreflightError(
            "stop after checkout", {"ok": False, "checks": []})))

    result = deep_review_report.evaluate(source)

    assert result["environment"]["status"] == "preflight_refused"
    assert not marker.exists()


def test_range_comparison_finds_change_set_findings_and_counts_delta():
    result = evaluate_range()
    assert result["environment"]["status"] == "admitted"
    assert result["commits"]["base"] != result["commits"]["head"]
    assert result["changed_files"] == ["src/checkout/gateway.py"]
    change_set = result["change_set"]
    assert change_set["expected"] == 2
    assert change_set["full"]["recall"] == 0.0
    assert change_set["full"]["true_positive"] == 0
    assert change_set["range"]["recall"] == 1.0
    assert change_set["range"]["true_positive"] == 2
    assert change_set["range"]["precision"] == 1.0
    assert change_set["range"]["false_positive_rate"] is None
    assert change_set["range"]["false_discovery_rate"] == 0.0
    assert change_set["range"]["report_noise_ratio"] == 0.0
    assert change_set["full_signals_on_changed_files"]
    assert any(row["type"] == "security_sensitive"
               for row in change_set["range_signals_on_changed_files"])
    assert result["comparison"]["new"] >= 2
    assert result["comparison"]["resolved"] == 0
    assert result["finding_ids"]["added_by_range"] == result["comparison"]["new"]
    assert result["range_graph"]["freshness"] == "missing"
    assert result["reports"]["full"]["findings"] < result["reports"]["range"]["findings"]
    assert result["duration"]["total_s"] >= 0


def test_range_comparison_is_deterministic_across_repeated_runs():
    first = evaluate_range()
    second = evaluate_range()
    assert _stable(first) == _stable(second)


def test_range_comparison_empty_findings_are_not_scored_as_success(monkeypatch):
    def build(_root, _out, **_kwargs):
        return {"manifest": {"complete": True, "status": "complete",
                             "subjects": {}, "comparison": {},
                             "range_review": {"status": "available"}},
                "findings": []}

    monkeypatch.setattr(deep_review_comparison, "build_deep_review", build)
    result = evaluate_range()
    assert result["change_set"]["range"]["predicted"] == 0
    assert result["change_set"]["range"]["true_positive"] == 0
    assert result["change_set"]["range"]["expected"] == 0
    assert result["change_set"]["range"]["recall"] is None
    assert result["change_set"]["range"]["precision"] is None
    assert result["change_set"]["range"]["report_noise_ratio"] is None
    assert result["comparison"] == {"new": 0, "resolved": 0, "unchanged": 0}


def test_range_comparison_marks_resource_refusal_partial(monkeypatch):
    def build(_root, _out, **kwargs):
        if kwargs.get("range_spec"):
            return {"manifest": {
                "complete": False, "status": "partial", "subjects": {},
                "comparison": {}, "range_review": {
                    "status": "unavailable",
                    "error": "range review unavailable: PreflightError: resource admission refused (disk.host_min)",
                }}, "findings": []}
        return {"manifest": {"complete": True, "status": "complete",
                             "subjects": {}, "comparison": {}},
                "findings": []}

    monkeypatch.setattr(deep_review_comparison, "build_deep_review", build)
    result = evaluate_range()

    assert result["status"] == "partial"
    assert result["environment"] == {
        "status": "partial", "preflight": "passed",
        "range_review": "unavailable",
        "reason": "range review unavailable: PreflightError: resource admission refused (disk.host_min)",
    }
    assert result["change_set"]["range"]["recall"] is None
    assert result["change_set"]["range_signals_on_changed_files"] is None
    assert result["comparison"] is None
    assert result["finding_ids"] is None


def test_range_comparison_rejects_missing_or_malformed_range_labels(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        evaluate_range(DEFAULT_FIXTURE, labels=tmp_path / "absent.json")
    # The whole-repo labels deliberately have no range section.
    with pytest.raises(ValueError, match="has no range section"):
        evaluate_range(DEFAULT_FIXTURE,
                       labels=DEFAULT_FIXTURE / "labels.json")
    bad = tmp_path / "labels.json"
    bad.write_text(json.dumps({
        "schema_version": 2,
        "cases": [],
        "range": {"changes": [{"file": "a.py", "append": "x"}],
                  "expected": [{"file": "/abs.py", "type": "x"}]},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="must stay inside"):
        evaluate_range(DEFAULT_FIXTURE, labels=bad)


def test_range_default_labels_live_outside_the_scanned_fixture():
    labels = json.loads(DEFAULT_RANGE_LABELS.read_text(encoding="utf-8"))
    assert labels["range"]["expected"]
    assert DEFAULT_RANGE_LABELS.parent != DEFAULT_FIXTURE
    # The scanned checkout's own labels must not contain the scripted shell text.
    fixture_labels = (DEFAULT_FIXTURE / "labels.json").read_text(encoding="utf-8")
    assert "os.system" not in fixture_labels
