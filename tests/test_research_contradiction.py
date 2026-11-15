from __future__ import annotations

import pytest

from ozagia.research.contradiction import (
    ContradictionError,
    check_contradiction,
    contradiction_report,
    find_contradictions,
    record_contradiction,
)
from ozagia.research.graph import ResearchGraph


def _graph(tmp_path, investigation_id: str) -> ResearchGraph:
    return ResearchGraph.create(
        "Contradiction tests",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _claim(graph, wording: str, **fields):
    return graph.record(
        "Claim",
        wording=wording,
        claim_type=fields.pop("claim_type", "hypothesis"),
        status=fields.pop("status", "unresolved"),
        author=fields.pop("author", "reader"),
        scope=fields.pop("scope", "the tested corpus"),
        **fields,
    )


def _extraction(graph, target_id: str, **fields):
    return graph.record(
        "Extraction",
        target_id=target_id,
        extractor="reader",
        status="recorded",
        **fields,
    )


def test_explicit_opposes_edge_becomes_a_deterministic_candidate(tmp_path):
    graph = _graph(tmp_path, "contradiction-opposes")
    claim = _claim(graph, "The effect is positive.")
    evidence = graph.record(
        "Evidence",
        evidence_type="experiment",
        wording="The effect was negative.",
    )
    graph.link(evidence["id"], "opposes", claim["id"])

    first = find_contradictions(graph.state())
    second = find_contradictions(graph.state())

    assert first == second
    assert first["truth_assessed"] is False
    assert first["count"] == 1
    assert first["pairs_examined"] == 1
    assert first["truncated"] is False
    candidate = first["candidates"][0]
    assert candidate["kind"] == "explicit_opposition"
    assert candidate["signal"] == "explicit_opposes"
    assert {candidate["claim_a_id"], candidate["claim_b_id"]} == {claim["id"], evidence["id"]}
    assert candidate["claim_a_id"] < candidate["claim_b_id"]
    assert candidate["evidence_ids"] == []
    assert "opposes" in candidate["rationale"]

    capped = find_contradictions(graph.state(), max_pairs=0)
    assert capped["candidates"] == []
    assert capped["count"] == 0
    assert capped["truncated"] is True


def test_same_normalized_scope_with_incompatible_statuses(tmp_path):
    graph = _graph(tmp_path, "contradiction-status")
    supported = _claim(
        graph,
        "Coverage improved in the trial.",
        status="supported_within_scope",
        scope="  The  Trial Corpus ",
    )
    contradicted = _claim(
        graph,
        "Coverage did not improve in the trial.",
        status="contradicted",
        scope="the trial corpus",
    )
    other_scope = _claim(
        graph,
        "A different population improved.",
        status="contradicted",
        scope="another corpus",
    )

    result = find_contradictions(graph.state())

    assert result["count"] == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "status_incompatibility"
    assert candidate["signal"] == "claim_status"
    assert {candidate["claim_a_id"], candidate["claim_b_id"]} == {
        supported["id"],
        contradicted["id"],
    }
    assert other_scope["id"] not in {
        candidate["claim_a_id"],
        candidate["claim_b_id"],
    }
    assert "trial corpus" in candidate["rationale"]


def test_extractions_with_opposite_signs_are_candidates(tmp_path):
    graph = _graph(tmp_path, "contradiction-extraction-sign")
    positive = _claim(graph, "The risk increases.", scope="one scope")
    negative = _claim(graph, "The risk falls.", scope="another scope")
    positive_extraction = _extraction(
        graph,
        positive["id"],
        outcome="mortality",
        effect_measure="risk_ratio",
        unit="ratio",
        effect_size=0.4,
        ci_low=0.1,
        ci_high=0.7,
    )
    negative_extraction = _extraction(
        graph,
        negative["id"],
        outcome="mortality",
        effect_measure="risk_ratio",
        unit="ratio",
        effect_size=-0.3,
        ci_low=-0.6,
        ci_high=0.2,
    )

    result = find_contradictions(graph.state())

    assert result["count"] == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "extraction_disagreement"
    assert candidate["signal"] == "opposite_effect_sign"
    assert {candidate["claim_a_id"], candidate["claim_b_id"]} == {
        positive["id"],
        negative["id"],
    }
    assert candidate["evidence_ids"] == sorted(
        [positive_extraction["id"], negative_extraction["id"]]
    )
    assert candidate["confidence"] > 0


def test_extractions_with_disjoint_intervals_are_candidates(tmp_path):
    graph = _graph(tmp_path, "contradiction-extraction-ci")
    small = _claim(graph, "The effect is small.", scope="small scope")
    large = _claim(graph, "The effect is large.", scope="large scope")
    small_extraction = _extraction(
        graph,
        small["id"],
        outcome="recovery",
        effect_measure="risk_difference",
        unit="percentage_points",
        effect_size=0.1,
        ci_low=0.05,
        ci_high=0.15,
    )
    large_extraction = _extraction(
        graph,
        large["id"],
        outcome="recovery",
        effect_measure="risk_difference",
        unit="percentage_points",
        effect_size=0.6,
        confidence_interval={"low": 0.5, "high": 0.7},
    )

    result = find_contradictions(graph.state())

    assert result["count"] == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "extraction_disagreement"
    assert candidate["signal"] == "non_overlapping_ci"
    assert candidate["evidence_ids"] == sorted(
        [small_extraction["id"], large_extraction["id"]]
    )
    assert candidate["claim_a_id"] < candidate["claim_b_id"]


def test_non_comparable_extractions_and_missing_fields_are_skipped(tmp_path):
    graph = _graph(tmp_path, "contradiction-extraction-skip")
    first = _claim(graph, "First claim.", scope="first scope")
    second = _claim(graph, "Second claim.", scope="second scope")
    third = _claim(graph, "Third claim.", scope="third scope")
    _extraction(
        graph,
        first["id"],
        outcome="mortality",
        effect_measure="risk_ratio",
        unit="ratio",
        effect_size=-0.4,
        ci_low=-0.6,
        ci_high=-0.2,
    )
    _extraction(
        graph,
        second["id"],
        outcome="mortality",
        effect_measure="risk_ratio",
        unit="odds_ratio",
        effect_size=0.4,
        ci_low=0.2,
        ci_high=0.6,
    )
    _extraction(
        graph,
        third["id"],
        outcome="mortality",
        effect_size=-0.4,
    )

    result = find_contradictions(graph.state())

    assert result["candidates"] == []
    assert result["count"] == 0


def test_record_check_and_report_links(tmp_path):
    graph = _graph(tmp_path, "contradiction-record")
    left = _claim(graph, "The treatment helps.")
    right = _claim(graph, "The treatment harms.")
    evidence = graph.record(
        "Evidence",
        evidence_type="citation_context_candidate",
        wording="A trial reported harm.",
    )

    recorded = record_contradiction(
        graph,
        claim_a_id=left["id"],
        claim_b_id=right["id"],
        kind="explicit_opposition",
        rationale="The two claims make opposite directional predictions.",
        evidence_ids=[evidence["id"]],
        reviewer="reviewer-a",
    )

    contradiction = recorded["contradiction"]
    assert recorded["truth_assessed"] is False
    assert contradiction["type"] == "Contradiction"
    assert contradiction["claim_a_id"] == left["id"]
    assert contradiction["claim_b_id"] == right["id"]
    assert contradiction["kind"] == "explicit_opposition"
    assert contradiction["status"] == "candidate"
    assert contradiction["reviewer"] == "reviewer-a"
    assert contradiction["evidence_ids"] == [evidence["id"]]
    assert contradiction["truth_assessed"] is False
    assert len(recorded["links"]) == 3

    state = graph.state()
    links = [
        edge for edge in state["edges"]
        if edge["type"] == "contradicts" and edge["src"] == contradiction["id"]
    ]
    assert {edge["tgt"] for edge in links} == {left["id"], right["id"], evidence["id"]}

    checked = check_contradiction(graph.state(), contradiction["id"])
    assert checked["ok"] is True
    assert checked["issues"] == []
    assert checked["counts"]["claims"] == 2
    assert checked["counts"]["evidence"] == 1
    assert checked["counts"]["contradicts_edges"] == 3
    assert checked["truth_assessed"] is False

    report = contradiction_report(graph.state())
    assert report["record_count"] == 1
    assert report["records_by_kind"]["explicit_opposition"] == 1
    assert report["records_by_status"]["candidate"] == 1
    assert report["counts"]["records"] == 1
    assert report["truth_assessed"] is False


def test_report_tracks_unresolved_and_resolved_pairs(tmp_path):
    graph = _graph(tmp_path, "contradiction-report-pairs")
    supported = _claim(
        graph,
        "The effect is positive.",
        status="supported_within_scope",
        scope="shared scope",
    )
    contradicted = _claim(
        graph,
        "The effect is negative.",
        status="contradicted",
        scope="shared scope",
    )

    report = contradiction_report(graph.state())
    assert report["candidate_count"] == 1
    assert report["unresolved_pair_count"] == 1
    assert report["unresolved_pairs"][0]["claim_a_id"] == min(
        supported["id"], contradicted["id"]
    )

    record_contradiction(
        graph,
        claim_a_id=supported["id"],
        claim_b_id=contradicted["id"],
        kind="status_incompatibility",
        rationale="The signed-off statuses are incompatible within the scope.",
        reviewer="reviewer-b",
        status="candidate",
    )
    report = contradiction_report(graph.state())
    assert report["record_count"] == 1
    assert report["unresolved_pair_count"] == 1

    record_contradiction(
        graph,
        claim_a_id=supported["id"],
        claim_b_id=contradicted["id"],
        kind="status_incompatibility",
        rationale="A reviewer confirmed the incompatible statuses.",
        reviewer="reviewer-b",
        status="confirmed",
    )
    report = contradiction_report(graph.state())
    assert report["record_count"] == 2
    assert report["records_by_status"]["confirmed"] == 1
    assert report["unresolved_pair_count"] == 0
    assert report["unresolved_pairs"] == []


def test_record_validation_and_check_detects_missing_links(tmp_path):
    graph = _graph(tmp_path, "contradiction-validation")
    left = _claim(graph, "Left claim.")
    right = _claim(graph, "Right claim.")
    reference = graph.record("Reference", title="A reference", citation_key="ref2024")
    evidence = graph.record("Evidence", wording="Supporting evidence.")

    with pytest.raises(ContradictionError, match="must differ"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id=left["id"],
            kind="other",
            rationale="Same record.",
            reviewer="reviewer",
        )
    with pytest.raises(ContradictionError, match="unknown node id"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id="claim-9999",
            kind="other",
            rationale="Unknown claim.",
            reviewer="reviewer",
        )
    with pytest.raises(ContradictionError, match="kind must be"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id=right["id"],
            kind="not_a_kind",
            rationale="Bad kind.",
            reviewer="reviewer",
        )
    with pytest.raises(ContradictionError, match="status must be"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id=right["id"],
            kind="other",
            rationale="Bad status.",
            reviewer="reviewer",
            status="resolved",
        )
    with pytest.raises(ContradictionError, match="reviewer"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id=right["id"],
            kind="other",
            rationale="No reviewer.",
            reviewer="  ",
        )
    with pytest.raises(ContradictionError, match="evidence_ids"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id=right["id"],
            kind="other",
            rationale="Unknown evidence.",
            evidence_ids=["evidence-9999"],
            reviewer="reviewer",
        )
    with pytest.raises(ContradictionError, match="must be one of"):
        record_contradiction(
            graph,
            claim_a_id=left["id"],
            claim_b_id=right["id"],
            kind="other",
            rationale="A Reference cannot be a contradiction endpoint.",
            evidence_ids=[reference["id"]],
            reviewer="reviewer",
        )

    unlinked = graph.record(
        "Contradiction",
        claim_a_id=left["id"],
        claim_b_id=right["id"],
        kind="other",
        rationale="Recorded without links.",
        evidence_ids=[evidence["id"]],
        reviewer="reviewer",
        status="candidate",
        truth_assessed=False,
    )
    checked = check_contradiction(graph.state(), unlinked["id"])
    assert checked["ok"] is False
    assert {issue["code"] for issue in checked["issues"]} == {"contradicts_edge_missing"}
