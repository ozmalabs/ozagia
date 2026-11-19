import json
from pathlib import Path

import pytest

from ozagia.eval.deep_review_labels import (
    expected_findings,
    label_metadata,
    load_labels,
    score_findings,
    score_report_findings,
)

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "deep_review_service"


def _write(tmp_path, payload):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _finding(finding_id, path, ftype, subject):
    return {"id": finding_id, "type": ftype, "subject": subject,
            "locations": [{"path": path}]}


def test_fixture_labels_are_valid_and_extract_reviewed_findings():
    manifest = load_labels(FIXTURE / "labels.json")
    expected = expected_findings(manifest, include_report=True)
    assert len(expected) == 8
    assert {row["type"] for row in expected} == {
        "repeated_nested_iteration", "mutable_default_argument",
        "silently_suppressed_exception", "materialized_range",
        "untested_module",
    }
    metadata = label_metadata(manifest, FIXTURE / "labels.json")
    assert metadata["cases"] == 4
    assert metadata["report_findings"] == 4
    assert metadata["range_findings"] == 0
    assert "tests-and-coverage" in metadata["complete_subjects"]


def test_complete_subject_scores_precision_recall_fpr_and_noise():
    expected = [
        {"path": "a.py", "type": "rule_a", "subject": "subject-a"},
        {"path": "a.py", "type": "rule_b", "subject": "subject-a"},
        {"path": "b.py", "type": "rule_c", "subject": "subject-b"},
    ]
    findings = [
        _finding("1", "a.py", "rule_a", "subject-a"),
        _finding("2", "a.py", "rule_extra", "subject-a"),
        _finding("3", "b.py", "rule_c", "subject-b"),
    ]
    score = score_findings(findings, expected,
                           complete_subjects={"subject-a", "subject-b"})
    assert score["expected"] == 3
    assert score["predicted"] == 3
    assert score["true_positive"] == 2
    assert score["false_positive"] == 1
    assert score["false_negative"] == 1
    assert score["precision"] == pytest.approx(2 / 3)
    assert score["recall"] == pytest.approx(2 / 3)
    assert score["false_positive_rate"] == pytest.approx(1 / 3)
    assert score["report_noise_ratio"] == pytest.approx(1 / 3)
    assert score["subjects"]["subject-a"]["false_positive"] == 1


def test_incomplete_subject_predictions_are_noise_not_false_positives():
    expected = [{"path": "a.py", "type": "rule_a", "subject": "subject-a"}]
    findings = [
        _finding("1", "a.py", "rule_a", "subject-a"),
        _finding("2", "other.py", "rule_x", "subject-u"),
    ]
    score = score_findings(findings, expected, complete_subjects={"subject-a"})
    assert score["true_positive"] == 1
    assert score["false_positive"] == 0
    assert score["unlabeled"] == 1
    assert score["report_noise_ratio"] == 0.5
    row = score["subjects"]["subject-u"]
    assert row["precision"] is None
    assert row["false_positive_rate"] is None
    assert row["report_noise_ratio"] == 1.0
    assert any("no reviewed labels" in note for note in row["notes"])


def test_empty_findings_leave_undefined_metrics_null():
    expected = [
        {"path": "a.py", "type": "rule_a", "subject": "subject-a"},
        {"path": "a.py", "type": "rule_b", "subject": "subject-a"},
    ]
    score = score_findings([], expected, complete_subjects={"subject-a"})
    assert score["predicted"] == 0
    assert score["true_positive"] == 0
    assert score["false_negative"] == 2
    assert score["precision"] is None
    assert score["false_positive_rate"] is None
    assert score["report_noise_ratio"] is None
    assert score["recall"] == 0.0


def test_locationless_findings_are_counted_but_not_matched():
    expected = [{"path": "a.py", "type": "rule_a", "subject": "subject-a"}]
    findings = [{"id": "1", "type": "rule_a", "subject": "subject-a",
                 "locations": []}]
    score = score_findings(findings, expected, complete_subjects={"subject-a"})
    assert score["predicted"] == 0
    assert score["locationless"] == 1
    assert score["true_positive"] == 0
    assert score["false_negative"] == 1


def test_scoring_is_deterministic_for_repeated_runs():
    expected = [{"path": "a.py", "type": "rule_a", "subject": "subject-a"}]
    findings = [_finding("1", "a.py", "rule_a", "subject-a"),
                _finding("2", "a.py", "rule_extra", "subject-a")]
    first = score_findings(findings, expected, complete_subjects={"subject-a"})
    second = score_findings(findings, expected, complete_subjects={"subject-a"})
    assert first == second


def test_report_scoring_excludes_unavailable_labeled_subjects():
    expected = [
        {"path": "a.py", "type": "rule_a", "subject": "subject-a"},
        {"path": "b.py", "type": "rule_b", "subject": "subject-b",
         "required_checks": ["static scan"]},
        {"path": "c.py", "type": "rule_c", "subject": "subject-c",
         "required_checks": ["static scan"]},
    ]
    findings = [_finding("1", "b.py", "rule_b", "subject-b"),
                _finding("2", "extra.py", "rule_x", "subject-a")]
    report = {"subjects": {
        "subject-a": {"status": "unknown", "checks": [],
                      "omitted": ["no collector"], "skipped": [], "unknown": []},
        "subject-b": {"status": "partial", "checks": ["static scan"],
                      "omitted": [{"reason": "graph is missing; graph-dependent checks omitted"}],
                      "skipped": [], "unknown": []},
        "subject-c": {"status": "partial", "checks": ["static scan"],
                      "omitted": ["budget exhausted"], "skipped": [], "unknown": []},
    }}

    score = score_report_findings(
        findings, expected, complete_subjects={"subject-a", "subject-b", "subject-c"},
        report_manifest={**report, "prioritization_graph": {"freshness": "missing"}})

    assert score["unavailable_subjects"] == ["subject-a", "subject-c"]
    assert score["excluded_expected"] == 2
    assert score["expected"] == 1
    assert score["true_positive"] == 1
    assert score["false_negative"] == 0
    assert score["false_positive"] == 0
    assert score["subjects"]["subject-a"]["precision"] is None


def test_graph_dependent_label_is_not_scored_without_fresh_graph():
    expected = [{"path": "a.py", "type": "graph_rule", "subject": "architecture",
                 "required_checks": ["graph_cycles"], "requires_graph": True}]
    report = {"prioritization_graph": {"freshness": "missing"},
              "subjects": {"architecture": {
                  "status": "partial", "checks": ["graph_cycles"],
                  "omitted": [{"check": "graph_cycles",
                               "reason": "graph is missing"}],
                  "skipped": [], "unknown": [],
              }}}

    score = score_report_findings(
        [], expected, complete_subjects={"architecture"}, report_manifest=report)

    assert score["expected"] == 0
    assert score["excluded_expected"] == 1
    assert score["recall"] is None
    assert score["coverage_reasons"]["architecture"] == "label requires a fresh graph"


def test_one_missing_required_check_disables_multi_label_subject_metrics():
    expected = [
        {"path": "a.py", "type": "rule_a", "subject": "subject-a",
         "required_checks": ["check_a"]},
        {"path": "b.py", "type": "rule_b", "subject": "subject-a",
         "required_checks": ["check_b"]},
    ]
    report = {"prioritization_graph": {"freshness": "fresh"},
              "subjects": {"subject-a": {
                  "status": "partial", "checks": ["check_a"],
                  "omitted": [{"check": "check_b", "reason": "not checked"}],
                  "skipped": [], "unknown": [],
              }}}

    score = score_report_findings(
        [_finding("1", "a.py", "rule_a", "subject-a")], expected,
        complete_subjects={"subject-a"}, report_manifest=report)

    assert score["unavailable_subjects"] == ["subject-a"]
    assert score["expected"] == 0
    assert score["predicted"] == 1
    assert score["true_positive"] == 0
    assert score["false_negative"] == 0
    assert score["false_positive"] is None
    assert score["unlabeled"] == 1


def test_labeled_path_omission_disables_recall_even_when_check_ran():
    expected = [{"path": "src/target.py", "type": "rule", "subject": "performance",
                 "required_checks": ["python_ast_static_checks"]}]
    report = {"subjects": {"performance": {
        "status": "partial", "checks": ["python_ast_static_checks"],
        "omitted": [{"path": "src/target.py", "reason": "source exceeds read limit"}],
        "skipped": [], "unknown": [],
    }}}

    score = score_report_findings(
        [], expected, complete_subjects={"performance"}, report_manifest=report)

    assert score["expected"] == 0
    assert score["excluded_expected"] == 1
    assert score["recall"] is None
    assert score["coverage_reasons"]["performance"] == "labeled source path was omitted"


@pytest.mark.parametrize("truncation", [
    {"normalization_findings_omitted": 1},
    {"max_findings": {"truncated": True}},
])
def test_global_finding_truncation_makes_label_coverage_unavailable(truncation):
    expected = [{"path": "a.py", "type": "rule_a", "subject": "subject-a",
                 "required_checks": ["static scan"]}]
    report = {"truncation": truncation,
              "subjects": {"subject-a": {"status": "complete",
                                           "checks": ["static scan"],
                                           "omitted": [], "skipped": [],
                                           "unknown": []}}}

    score = score_report_findings(
        [], expected, complete_subjects={"subject-a"}, report_manifest=report)

    assert score["expected"] == 0
    assert score["excluded_expected"] == 1
    assert score["recall"] is None
    assert score["coverage_reasons"]["subject-a"]


@pytest.mark.parametrize("payload, message", [
    ({"cases": "not-a-list"}, "cases must be a list"),
    ({"cases": [{"id": "x", "file": "/abs.py", "expected": []}]},
     "must stay inside"),
    ({"cases": [{"id": "x", "file": "a.py", "expected": "not-a-list"}]},
     "expected must be a list"),
    ({"cases": [{"id": "x", "file": "a.py", "expected": ["rule"],
                 "required_checks": []}]}, "required_checks must not be empty"),
    ({"cases": [], "report_findings": [{"file": "a.py", "type": "x",
                                         "requires_graph": "yes"}]},
     "requires_graph must be a boolean"),
    ({"cases": [{"id": "x", "file": "a.py", "expected": ["a"]},
                {"id": "x", "file": "b.py", "expected": ["b"]}]},
     "duplicate labels case id"),
    ({"cases": [], "report_findings": [{"file": "a.py", "type": "x"},
                                       {"file": "a.py", "type": "x"}]},
     "duplicate reviewed label"),
    ({"cases": [], "range": {"changes": [{"file": "a.py"}],
                             "expected": [{"file": "a.py", "type": "x"}]}},
     "needs exactly one of append or content"),
    ({"cases": [], "range": {"changes": [{"file": "a.py", "append": "x"}],
                             "expected": []}},
     "expected must contain at least one"),
])
def test_malformed_labels_are_rejected(tmp_path, payload, message):
    path = _write(tmp_path, payload)
    with pytest.raises(ValueError, match=message):
        load_labels(path)


def test_missing_labels_and_invalid_json_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        load_labels(tmp_path / "absent.json")
    path = tmp_path / "labels.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid deep-review labels JSON"):
        load_labels(path)
