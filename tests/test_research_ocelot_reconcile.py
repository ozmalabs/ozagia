from __future__ import annotations

from pathlib import Path

import pytest

from ozagia.research import ocelot_reconcile, semantic_reconcile

OCELOT = Path(__file__).resolve().parents[2] / "ocelot"
OCELOT_PYTHON = OCELOT / ".venv/bin/python"


def _agreement():
    findings = [
        {"id": "f1", "run_id": "run-1", "wording": "The claim is duplicated.",
         "issue_type": "duplicate_claim", "severity": "major",
         "claim_id": "claim-0001"},
        {"id": "f2", "run_id": "run-2", "wording": "This claim overlaps another.",
         "issue_type": "claim_overlap", "severity": "minor",
         "claim_id": "claim-0001"},
        {"id": "f3", "run_id": "run-1", "wording": "No supporting evidence is linked.",
         "issue_type": "evidence_gap", "severity": "major",
         "claim_id": "claim-0001"},
        {"id": "f4", "run_id": "run-2", "wording": "The record contains no evidence.",
         "issue_type": "evidence_scope", "severity": "major",
         "claim_id": "claim-0001"},
    ]
    rows = [
        {
            "key": ["Claim", "claim-0001", finding["issue_type"]],
            "finding_ids": [finding["id"]],
            "run_ids": [finding["run_id"]],
            "reviewers": [finding["run_id"]],
            "target_type": "Claim",
            "target_id": "claim-0001",
            "wording": finding["wording"],
            "issue_type": finding["issue_type"],
            "severity": finding["severity"],
            "severities": {finding["run_id"]: finding["severity"]},
            "findings": [finding],
        }
        for finding in findings
    ]
    return {
        "plan_id": "plan-1",
        "min_reviewers": 2,
        "runs": [{"id": "run-1"}, {"id": "run-2"}],
        "reviewers": ["run-1", "run-2"],
        "findings": findings,
        "issues": rows,
    }


def test_ocelot_issue_family_mapping():
    assert ocelot_reconcile.canonical_issue_family("duplicate_claim") == "duplicate"
    assert ocelot_reconcile.canonical_issue_family("claim_overlap") == "duplicate"
    assert ocelot_reconcile.canonical_issue_family("evidence_scope") == "evidence"
    assert ocelot_reconcile.canonical_issue_family("falsifiability") == "falsifiability"
    assert ocelot_reconcile.canonical_issue_family("reference_metadata") == "provenance"


@pytest.mark.skipif(
    not OCELOT_PYTHON.is_file(), reason="sibling Ocelot checkout is unavailable")
def test_ocelot_structural_clusters_merge_same_shape_only():
    agreement = _agreement()
    result = ocelot_reconcile.ocelot_groups(
        agreement, ocelot_root=OCELOT, python=OCELOT_PYTHON)
    clusters = [set(cluster) for cluster in result["clusters"]]
    assert {"f1", "f2"} in clusters
    assert {"f3", "f4"} in clusters
    assert result["analogies"]
    semantic = semantic_reconcile.recluster_agreement(
        agreement, groups=result["groups"], method="ocelot_analogy",
        model=result["engine"], threshold=None)
    assert semantic["counts"]["issues"] == 2
    assert semantic["counts"]["consensus"] == 2
    assert semantic["counts"]["dissent"] == 0
    assert semantic["clustering"]["method"] == "ocelot_analogy"


@pytest.mark.skipif(not OCELOT.is_dir(), reason="sibling Ocelot checkout is unavailable")
def test_ocelot_inprocess_matches_subprocess_shape_clusters():
    agreement = _agreement()
    result = ocelot_reconcile.ocelot_groups(
        agreement, ocelot_root=OCELOT, mode="inprocess")
    clusters = {frozenset(cluster) for cluster in result["clusters"]}
    assert frozenset({"f1", "f2"}) in clusters
    assert frozenset({"f3", "f4"}) in clusters
    assert result["engine"] == "ocelot:analogy_search"
