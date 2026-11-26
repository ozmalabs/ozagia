import json
import shutil
from pathlib import Path

import pytest

from ozagia.eval.deep_review_corpus import CorpusError, load_manifest
from ozagia.eval.deep_review_corpus_score import (
    evaluate_gates,
    load_run_record,
    score_corpus,
)

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "deep_review_corpus_dry_run"
CORPUS = FIXTURE / "corpus.json"
LABELS = FIXTURE / "labels"
RECORD = FIXTURE / "record.json"


def _score(*, checkout_root=True):
    manifest = load_manifest(CORPUS)
    record = load_run_record(RECORD)
    return score_corpus(
        manifest, LABELS, record,
        checkout_root=FIXTURE if checkout_root else None)


def _raw_record():
    return json.loads(RECORD.read_text(encoding="utf-8"))


def _write_record(tmp_path, payload):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_dry_run_scores_realistic_numbers_without_a_quality_claim():
    result = _score()
    assert result["status"] == "synthetic_regression"
    assert result["quality_claim"] == "none"
    assert result["gate_status"] == "not_applicable_synthetic"
    assert result["pooling"]["synthetic_fixtures_included"] is True
    assert result["pooling"]["separate_from_realistic"] is True
    assert result["reviewers"]["provenance"] == "synthetic_test_fixture"

    assert result["overall"]["expected"] == 5
    assert result["overall"]["predicted"] == 6
    assert result["overall"]["true_positive"] == 4
    assert result["overall"]["false_positive"] == 1
    assert result["overall"]["false_negative"] == 1
    assert result["overall"]["unlabeled"] == 1
    assert result["overall"]["precision"] == pytest.approx(0.75)
    assert result["overall"]["recall"] == pytest.approx(0.8)
    assert result["overall"]["report_noise_ratio"] == pytest.approx(1 / 3)

    assert result["counts"]["precision_denominator"] == 4
    assert result["counts"]["recall_denominator"] == 5
    assert result["counts"]["noise_denominator"] == 6
    assert result["counts"]["labeled_predictions"] == 4
    assert result["counts"]["unlabeled_predictions"] == 2
    assert result["counts"]["negative_reviewed_labels"] == 1


def test_partial_scope_predictions_stay_unlabeled_not_false_positives():
    result = _score()
    assert result["coverage"]["complete_label_subjects"] == 1
    assert result["coverage"]["partial_or_unknown_label_subjects"] == 1
    assert result["coverage"]["complete_repository_count"] == 0
    assert result["coverage"]["source_digest_status"] == "verified"
    assert result["labeled_vs_unlabeled"]["labeled_predictions"] == 4
    assert result["labeled_vs_unlabeled"]["unlabeled_predictions"] == 2
    assert result["overall"]["false_positive"] == 1

    subjects = {row["subject"]: row for row in result["per_subject"]}
    data_structures = subjects["data-structures"]
    assert data_structures["complete"] is False
    assert data_structures["precision"] is None
    assert data_structures["true_positive"] == 1
    performance = subjects["performance"]
    assert performance["expected"] == 1
    assert performance["predicted"] == 2
    assert performance["true_positive"] == 1
    assert performance["false_positive"] == 0
    assert performance["precision"] == 1.0
    assert performance["report_noise_ratio"] == 0.5
    assert subjects["scalability"]["precision"] == 0.0
    assert subjects["tests-and-coverage"]["complete"] is False


def test_complete_label_scope_without_scored_run_is_reported_as_missing(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(FIXTURE, copied)
    record_path = copied / "record.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    raw["runs"] = [run for run in raw["runs"]
                   if run["subject_key"] != "range-complete"]
    record_path.write_text(json.dumps(raw), encoding="utf-8")

    result = score_corpus(
        load_manifest(copied / "corpus.json"), copied / "labels",
        load_run_record(record_path), checkout_root=copied)

    missing = result["coverage"]["missing_runs_for_complete_labels"]
    assert missing == [{
        "repository_id": "fixture-service",
        "subject_key": "range-complete",
        "run_id": None,
        "reason": "no scored run for this label scope",
    }]
    assert any(row["missing_run"] for row in result["per_subject_key"])


def test_high_impact_and_repository_views_are_reported_separately():
    result = _score()
    assert result["high_impact"]["expected"] == 2
    assert result["high_impact"]["predicted"] == 2
    assert result["high_impact"]["true_positive"] == 2
    assert result["high_impact"]["false_positive"] == 0
    assert result["high_impact"]["precision"] == 1.0
    assert result["high_impact"]["unsupported_blocking"] == 0
    assert result["high_impact"]["blocking_evidence_ok"] is True

    repository = result["per_repository"][0]
    assert repository["repository_id"] == "fixture-service"
    assert repository["expected"] == 5
    assert repository["predicted"] == 6
    assert repository["true_positive"] == 4
    assert repository["false_positive"] == 1
    assert repository["precision"] == pytest.approx(0.75)
    assert repository["recall"] == pytest.approx(0.8)
    assert len(result["per_subject_key"]) == 2


def test_digest_checks_are_verified_with_a_checkout_and_incomplete_without_one():
    verified = _score()
    assert verified["digest_checks"]["status"] == "verified"
    assert verified["digest_checks"]["manifest_pin"] == "verified"
    assert verified["digest_checks"]["labels_pin"] == "verified"
    assert verified["digest_checks"]["report_digest"] == "verified"
    assert verified["digest_checks"]["source_files"]["status"] == "verified"

    unchecked = _score(checkout_root=False)
    assert unchecked["digest_checks"]["source_files"]["status"] == "not_checked"
    assert unchecked["digest_checks"]["status"] == "incomplete"


def test_repeatability_and_scoring_are_deterministic():
    first = _score()
    second = _score()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["repeatability"]["status"] == "verified"
    assert first["repeatability"]["repeats"][0]["stable"] is True


def test_tampered_report_digest_is_rejected(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(FIXTURE, copied)
    report = copied / "reports" / "complete" / "findings.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["findings"][0]["severity"] = "note"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CorpusError) as excinfo:
        manifest = load_manifest(copied / "corpus.json")
        record = load_run_record(copied / "record.json")
        score_corpus(manifest, copied / "labels", record,
                     checkout_root=copied)
    assert excinfo.value.reason == "report_digest_mismatch"


def test_record_pin_mismatches_are_rejected(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(FIXTURE, copied)
    raw = json.loads((copied / "record.json").read_text(encoding="utf-8"))
    raw["manifest_digest"] = "0" * 64
    (copied / "record.json").write_text(json.dumps(raw), encoding="utf-8")
    record = load_run_record(copied / "record.json")
    with pytest.raises(CorpusError, match="manifest digest") as excinfo:
        score_corpus(load_manifest(copied / "corpus.json"), copied / "labels", record)
    assert excinfo.value.reason == "manifest_digest_mismatch"

    raw = json.loads((copied / "record.json").read_text(encoding="utf-8"))
    raw["manifest_digest"] = None
    raw["labels_digest"] = "0" * 64
    (copied / "record.json").write_text(json.dumps(raw), encoding="utf-8")
    record = load_run_record(copied / "record.json")
    with pytest.raises(CorpusError, match="labels digest") as excinfo:
        score_corpus(load_manifest(copied / "corpus.json"), copied / "labels", record)
    assert excinfo.value.reason == "labels_digest_mismatch"

    raw = json.loads((copied / "record.json").read_text(encoding="utf-8"))
    raw["labels_digest"] = None
    raw["corpus_id"] = "synthetic:other-corpus"
    (copied / "record.json").write_text(json.dumps(raw), encoding="utf-8")
    record = load_run_record(copied / "record.json")
    with pytest.raises(CorpusError) as excinfo:
        score_corpus(load_manifest(copied / "corpus.json"), copied / "labels", record)
    assert excinfo.value.reason == "corpus_mismatch"


def test_require_realistic_refuses_synthetic_evidence():
    record = load_run_record(RECORD)
    with pytest.raises(CorpusError) as excinfo:
        score_corpus(load_manifest(CORPUS), LABELS, record, require_realistic=True)
    assert excinfo.value.reason == "pooling_refused"


def test_realistic_label_loading_rejects_synthetic_provenance():
    from ozagia.eval.deep_review_corpus_labels import validate_labels_dir
    summary = validate_labels_dir(LABELS, load_manifest(CORPUS), require_human=True)
    assert summary["status"] == "invalid"
    assert any("requires human provenance" in error for error in summary["errors"])


def test_run_record_rejects_duplicate_ids_and_unsafe_identity(tmp_path):
    raw = _raw_record()
    raw["runs"].append(json.loads(json.dumps(raw["runs"][0])))
    with pytest.raises(CorpusError, match="duplicate run_id"):
        load_run_record(_write_record(tmp_path, raw))

    raw = _raw_record()
    raw["runs"][2]["repeat_of"] = "missing"
    with pytest.raises(CorpusError, match="repeat_of references missing run"):
        load_run_record(_write_record(tmp_path, raw))

    raw = _raw_record()
    raw["corpus_id"] = "synthetic:ozagia-deep-review-realistic-v1"
    raw["evidence_class"] = "realistic_corpus"
    with pytest.raises(CorpusError, match="synthetic/fixture corpus identity"):
        load_run_record(_write_record(tmp_path, raw))

    raw = _raw_record()
    raw["corpus_id"] = "synthetic:other"
    raw["evidence_class"] = "synthetic_regression"
    raw["scored_condition"] = "not-a-condition"
    with pytest.raises(CorpusError, match="scored_condition"):
        load_run_record(_write_record(tmp_path, raw))


def test_duplicate_finding_ids_are_deduped_or_rejected(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(FIXTURE, copied)
    record_path = copied / "record.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    for run in raw["runs"]:
        run.pop("report_sha256", None)
    record_path.write_text(json.dumps(raw), encoding="utf-8")
    report = copied / "reports" / "complete" / "findings.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["findings"].append(json.loads(json.dumps(payload["findings"][0])))
    report.write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_manifest(copied / "corpus.json")
    record = load_run_record(record_path)
    result = score_corpus(manifest, copied / "labels", record,
                          checkout_root=copied)
    assert result["overall"]["predicted"] == 6

    payload["findings"][-1]["type"] = "different_rule"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CorpusError) as excinfo:
        score_corpus(manifest, copied / "labels", load_run_record(record_path),)
    assert excinfo.value.reason == "duplicate_finding_id"


def _gate_metrics(**updates):
    metrics = {
        "evidence_class": "realistic_corpus",
        "provenance": "human",
        "overall": {
            "precision": 0.85,
            "recall": 0.75,
            "report_noise_ratio": 0.10,
            "report_noise_ratio_complete_scope": 0.10,
            "true_positive": 17,
            "false_positive": 3,
            "expected": 20,
            "predicted": 20,
        },
        "coverage": {"complete_repository_count": 8,
                     "complete_label_subjects": 10},
        "high_impact": {
            "precision": 0.95,
            "unsupported_blocking": 0,
            "blocking_evidence_ok": True,
            "true_positive": 19,
            "false_positive": 1,
            "predicted": 20,
        },
        "subject_stability": [{"subject_key": "repo/range", "predicted": 20,
                               "precision": 0.70}],
        "repeatability": {"status": "verified"},
        "digest_checks": {"status": "verified"},
        "operational": {"status": "measured", "within_budget_rate": 1.0,
                        "runs": 10, "writes_outside_allowed_paths": []},
        "human_utility": {"status": "measured", "actionability_rate": 0.85,
                          "sample_size": 100},
    }
    metrics.update(updates)
    return metrics


def test_gate_computation_passes_only_when_every_proposed_gate_is_met():
    result = evaluate_gates(_gate_metrics())
    assert result["status"] == "gate_met"
    assert result["quality_claim"] == "gate_met_not_universal_correctness"
    assert {gate["status"] for gate in result["gates"].values()} == {"pass"}


def test_gate_computation_fails_on_low_precision_and_too_few_repositories():
    low_precision = _gate_metrics(overall={
        "precision": 0.79, "recall": 0.75, "report_noise_ratio": 0.10,
        "report_noise_ratio_complete_scope": 0.10,
        "true_positive": 15, "false_positive": 4, "expected": 20, "predicted": 19})
    result = evaluate_gates(low_precision)
    assert result["status"] == "no_go"
    assert result["gates"]["deterministic_report_quality"]["status"] == "fail"

    too_few = _gate_metrics(coverage={"complete_repository_count": 7})
    result = evaluate_gates(too_few)
    assert result["gates"]["deterministic_report_quality"]["status"] == "fail"


def test_gate_computation_fails_on_weak_subject_noise_and_blocking_evidence():
    weak = _gate_metrics(subject_stability=[
        {"subject_key": "repo/range", "predicted": 20, "precision": 0.64}])
    assert evaluate_gates(weak)["gates"]["subject_stability"]["status"] == "fail"

    noisy = _gate_metrics(overall={
        "precision": 0.85, "recall": 0.75, "report_noise_ratio": 0.21,
        "report_noise_ratio_complete_scope": 0.21,
        "true_positive": 17, "false_positive": 3, "expected": 20, "predicted": 21})
    assert evaluate_gates(noisy)["gates"]["noise"]["status"] == "fail"

    unsupported = _gate_metrics(high_impact={
        "precision": 0.95, "unsupported_blocking": 1,
        "blocking_evidence_ok": True})
    assert evaluate_gates(unsupported)["gates"]["high_impact_findings"]["status"] == "fail"


def test_gate_computation_marks_missing_measurements_insufficient():
    no_repeat = _gate_metrics(repeatability={"status": "not_tested"})
    result = evaluate_gates(no_repeat)
    assert result["status"] == "insufficient_data"
    assert result["quality_claim"] == "none"
    assert result["gates"]["repeatability_and_coverage"]["status"] == "insufficient_data"

    no_human = _gate_metrics(human_utility={"status": "not_measured"})
    result = evaluate_gates(no_human)
    assert result["status"] == "insufficient_data"
    assert result["gates"]["human_utility"]["status"] == "not_measured"

    synthetic = evaluate_gates(_gate_metrics(evidence_class="synthetic_regression"))
    assert synthetic["status"] == "not_applicable_synthetic"
    assert synthetic["quality_claim"] == "none"


def test_gate_computation_requires_human_provenance():
    result = evaluate_gates(_gate_metrics(provenance="unknown"))
    assert result["status"] == "insufficient_data"
    assert result["quality_claim"] == "none"
    assert all(gate["status"] == "insufficient_data"
               for gate in result["gates"].values())
