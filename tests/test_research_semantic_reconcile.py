from __future__ import annotations

import pytest

from ozagia.research import semantic_reconcile as sr


def _fake_embedder(texts):
    vectors = {
        "duplicate claim span": [1.0, 0.0],
        "claim is duplicated": [0.95, 0.05],
        "missing evidence": [0.0, 1.0],
        "missing evidence for the claim": [0.05, 0.95],
    }
    return [vectors[text] for text in texts]


def _row(key, wording, run_id, reviewer, finding_id, severity="major"):
    return {
        "key": [ "Claim", "claim-0001", "issue", key],
        "target_type": "Claim",
        "target_id": "claim-0001",
        "issue_type": "issue",
        "wording": wording,
        "normalized_wording": wording,
        "finding_ids": [finding_id],
        "run_ids": [run_id],
        "reviewers": [reviewer],
        "run_count": 1,
        "reviewer_count": 1,
        "severity": severity,
        "severities": {run_id: severity},
        "presence": {},
        "reviewer_presence": {},
        "findings": [{"id": finding_id, "wording": wording}],
    }


def test_cluster_texts_and_recluster_agreement():
    texts = [
        "duplicate claim span",
        "claim is duplicated",
        "missing evidence",
        "missing evidence for the claim",
    ]
    clusters = sr.cluster_texts(texts, _fake_embedder, threshold=0.8)
    assert sorted(clusters) == [[0, 1], [2, 3]]

    rows = [
        _row("a", texts[0], "run-1", "reviewer-a", "finding-1"),
        _row("b", texts[1], "run-2", "reviewer-b", "finding-2"),
        _row("c", texts[2], "run-1", "reviewer-a", "finding-3", "minor"),
        _row("d", texts[3], "run-3", "reviewer-a", "finding-4", "minor"),
    ]
    agreement = {
        "plan_id": "plan-1",
        "min_reviewers": 2,
        "runs": [{"id": "run-1"}, {"id": "run-2"}, {"id": "run-3"}],
        "reviewers": ["reviewer-a", "reviewer-b", "reviewer-c"],
        "findings": [finding for row in rows for finding in row["findings"]],
        "issues": rows,
        "counts": {"runs": 3, "reviewers": 3, "findings": 4, "issues": 4,
                   "consensus": 0, "dissent": 4, "single_reviewer": 4},
    }
    result = sr.recluster_agreement(agreement, embedder=_fake_embedder, threshold=0.8)
    assert result["counts"]["issues"] == 2
    assert result["counts"]["consensus"] == 1
    assert result["counts"]["dissent"] == 1
    assert result["clustering"]["method"] == "embedding"
    assert result["clustering"]["threshold"] == 0.8
    assert result["clustering"]["exact_clusters"] == 4
    assert len(result["consensus"]) == 1
    assert len(result["dissent"]) == 1
    assert all(row["clustering"] == "embedding" for row in result["issues"])


def test_cluster_threshold_validation():
    with pytest.raises(sr.SemanticReconcileError):
        sr.cluster_texts(["x"], _fake_embedder, threshold=1.5)
