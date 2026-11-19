import shutil
from pathlib import Path

import pytest

from ozagia.eval import deep_review_comparison
from ozagia.eval.deep_review_comparison import (
    evaluate_model_pass,
    score_model_candidates,
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


def test_model_pass_comparison_scores_candidates_noise_duplicates_and_usage():
    result = evaluate_model_pass()
    assert result["environment"]["status"] == "admitted"
    assert result["live_provider_calls"] == 0
    assert result["network"] is False
    assert result["deterministic_repeat"] is True
    baseline = result["baseline"]
    assert baseline["model_pass"] is False
    assert baseline["model_runs"] == 0
    assert baseline["deterministic_score"]["precision"] == 1.0
    assert baseline["deterministic_score"]["recall"] == 1.0
    assert baseline["deterministic_score"]["report_noise_ratio"] == 0.0

    with_model = result["with_model_pass"]
    assert with_model["model_pass"] is True
    assert with_model["status"] == "complete"
    assert with_model["model_runs"] == 2
    assert with_model["deterministic_unchanged"] is True
    assert with_model["truth_assessed"] is False
    metrics = with_model["candidate_metrics"]
    assert metrics["accepted_candidates"] == 6
    assert metrics["unique_candidates"] == 3
    assert metrics["supported_candidates"] == 2
    assert metrics["added_noise_candidates"] == 1
    assert metrics["candidate_precision"] == pytest.approx(2 / 3)
    assert metrics["candidate_recall"] == pytest.approx(2 / 8)
    assert metrics["added_noise_rate"] == pytest.approx(1 / 3)
    assert metrics["duplicate_candidates"] == 3
    assert metrics["duplicate_rate"] == pytest.approx(0.5)
    assert with_model["usage"]["available"] is True
    assert with_model["usage"]["prompt_tokens"] == 200
    assert with_model["usage"]["completion_tokens"] == 40
    assert with_model["usage"]["total_tokens"] == 240
    assert result["delta"]["deterministic_findings"] == 0
    assert result["delta"]["noise_candidates_added"] == 1
    assert result["delta"]["tokens_added"] == 240


def test_model_candidate_scoring_maps_citations_to_reviewed_labels():
    findings = [{"id": "f1", "type": "rule", "subject": "subject",
                 "locations": [{"path": "a.py"}]}]
    expected = [{"path": "a.py", "type": "rule", "subject": "subject"}]
    candidates = [{"title": "Check this", "evidence": [{"citation_ids": ["f1"]}]}]
    score = score_model_candidates(candidates, findings, expected)
    assert score["candidates"] == 1
    assert score["supported_candidates"] == 1
    assert score["candidate_precision"] == 1.0
    assert score["candidate_recall"] == 1.0
    assert score["added_noise_candidates"] == 0
    assert score["duplicate_rate"] == 0.0


def test_model_candidate_scoring_handles_empty_inputs_with_null_rates():
    score = score_model_candidates([], [], [])
    assert score["candidates"] == 0
    assert score["unique_candidates"] == 0
    assert score["candidate_precision"] is None
    assert score["candidate_recall"] is None
    assert score["added_noise_rate"] is None
    assert score["duplicate_rate"] is None
    assert score["reason"] == "no model candidates were produced"


def test_model_candidate_precision_is_unknown_without_evaluable_labels():
    score = score_model_candidates(
        [{"title": "candidate", "evidence": []}], [], [])
    assert score["candidates"] == 1
    assert score["candidate_precision"] is None
    assert score["candidate_recall"] is None


def test_model_candidate_scoring_accounts_for_pass_deduplication():
    candidates = [
        {"title": "First", "evidence": [{"citation_ids": ["f1"]}]},
        {"title": "Second", "evidence": [{"citation_ids": []}]},
    ]
    findings = [{"id": "f1", "type": "rule", "subject": "subject",
                 "locations": [{"path": "a.py"}]}]
    expected = [{"path": "a.py", "type": "rule", "subject": "subject"}]
    score = score_model_candidates(candidates, findings, expected,
                                   accepted=4, deduplicated=2)
    assert score["accepted_candidates"] == 4
    assert score["unique_candidates"] == 2
    assert score["duplicate_candidates"] == 2
    assert score["duplicate_rate"] == 0.5
    assert score["candidate_precision"] == 0.5


def test_model_pass_comparison_handles_empty_deterministic_findings(monkeypatch):
    def build(_root, _out, **_kwargs):
        return {"manifest": {"complete": True, "status": "complete",
                             "subjects": {}, "comparison": {}},
                "findings": []}

    monkeypatch.setattr(deep_review_comparison, "build_deep_review", build)
    result = evaluate_model_pass()
    assert result["baseline"]["deterministic_findings"] == 0
    assert result["baseline"]["deterministic_score"]["recall"] is None
    assert result["baseline"]["deterministic_score"]["excluded_expected"] == 8
    assert result["baseline"]["deterministic_score"]["precision"] is None
    with_model = result["with_model_pass"]
    assert with_model["deterministic_findings"] == 0
    assert with_model["candidate_metrics"]["unique_candidates"] >= 1
    assert with_model["candidate_metrics"]["candidate_recall"] is None
    assert with_model["candidate_metrics"]["candidate_precision"] is None
    assert result["deterministic_repeat"] is True


def test_model_pass_comparison_rejects_missing_or_malformed_labels(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        evaluate_model_pass(labels=tmp_path / "absent.json")
    (tmp_path / "labels.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid deep-review labels JSON"):
        evaluate_model_pass(labels=tmp_path / "labels.json")
