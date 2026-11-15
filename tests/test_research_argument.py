from __future__ import annotations

import pytest

from ozagia.research import references as refs
from ozagia.research.argument import (
    ArgumentError,
    argument_report,
    build_argument_brief,
    check_argument,
    record_argument,
)
from ozagia.research.graph import ResearchGraph


def _graph(tmp_path, investigation_id: str) -> ResearchGraph:
    return ResearchGraph.create(
        "Argument tests",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _reference(graph, key: str, title: str, **fields):
    return refs.record_reference(graph, {
        "title": title,
        "citation_key": key,
        **fields,
    })["reference"]


def _claim(graph, wording: str = "The method improves review coverage.", **fields):
    return graph.record(
        "Claim",
        wording=wording,
        claim_type=fields.pop("claim_type", "hypothesis"),
        status=fields.pop("status", "unresolved"),
        author=fields.pop("author", "reader"),
        scope=fields.pop("scope", "the tested corpus"),
        **fields,
    )


def test_brief_gathers_relations_contexts_and_gaps(tmp_path):
    graph = _graph(tmp_path, "argument-brief")
    reference = _reference(graph, "smith2024", "A Graph Paper")
    claim = _claim(
        graph,
        citations=["smith2024", "missing2025"],
        raw_excerpt=(
            "Prior work found gains \\cite{smith2024}. A later study disagrees."
        ),
        source_locator={"path": "paper.tex", "start_line": 10, "end_line": 12},
    )
    support = graph.record(
        "Evidence",
        evidence_type="experiment",
        wording="The trial improved coverage.",
        citation_keys=["smith2024"],
        source_locator={"path": "results.md", "start_line": 4},
    )
    graph.link(support["id"], "supports", claim["id"])
    assumption = graph.record(
        "Assumption", wording="The corpus is representative of the field.")
    graph.link(claim["id"], "assumes", assumption["id"])

    first = build_argument_brief(graph.state(), claim_id=claim["id"])
    again = build_argument_brief(graph.state(), claim_id=claim["id"])

    assert first == again
    assert first["mode"] == "steelman"
    assert first["claim"]["id"] == claim["id"]
    assert first["supports"][0]["node_id"] == support["id"]
    assert first["supports"][0]["relation"] == "supports"
    assert first["opposes"] == []
    assert first["assumptions"][0]["node_id"] == assumption["id"]
    assert {item["node_id"] for item in first["evidence"]} == {support["id"]}
    assert first["evidence"][0]["relations"] == ["supports"]
    assert first["references"][0]["reference_id"] == reference["id"]
    assert first["references"][0]["resolved"] is True
    assert any(item["resolved"] is False and item["citation_key"] == "missing2025"
               for item in first["references"])

    parsed = next(
        context for context in first["citation_contexts"]
        if context["syntax"] == "latex"
    )
    assert parsed["citation_keys"] == ["smith2024"]
    assert parsed["quote"].startswith("Prior work found gains")
    assert parsed["text_available"] is True
    assert parsed["reference_ids"] == [reference["id"]]
    stored = next(
        context for context in first["citation_contexts"]
        if context["syntax"] == "stored_key"
    )
    assert stored["citation_keys"] == ["missing2025"]
    assert stored["quote"] == ""
    assert stored["text_available"] is False

    gap_codes = {gap["code"] for gap in first["gaps"]}
    assert "no_opposing_evidence" in gap_codes
    assert "unresolved_citation" in gap_codes
    assert "no_source_locator" not in gap_codes
    assert first["prompt"].startswith("Role: steelman argument builder.")
    assert len(first["packet_sha256"]) == 64
    assert first["truth_assessed"] is False

    opposition = graph.record(
        "Evidence", evidence_type="counterexample",
        wording="A replication found no effect.",
        source_locator={"path": "counter.md", "start_line": 2})
    graph.link(opposition["id"], "opposes", claim["id"])
    derived = graph.record(
        "Evidence", evidence_type="derived_analysis",
        wording="A reanalysis of the same data.")
    graph.link(claim["id"], "derived_from", derived["id"])
    graph.link(claim["id"], "cites", reference["id"])

    later = build_argument_brief(graph.state(), claim_id=claim["id"])
    assert later["opposes"][0]["node_id"] == opposition["id"]
    assert {item["node_id"] for item in later["evidence"]} == {
        support["id"], opposition["id"], derived["id"]}
    derived_summary = next(
        item for item in later["evidence"] if item["node_id"] == derived["id"])
    assert derived_summary["relations"] == ["derived_from"]
    assert "no_opposing_evidence" not in {gap["code"] for gap in later["gaps"]}
    assert later["packet_sha256"] != first["packet_sha256"]


def test_brief_uses_stored_keys_without_text(tmp_path):
    graph = _graph(tmp_path, "argument-stored-keys")
    claim = _claim(graph, citations=["doe2024"])

    brief = build_argument_brief(graph.state(), claim_id=claim["id"])

    assert brief["citation_contexts"] == [{
        "citation_keys": ["doe2024"],
        "quote": "",
        "start_line": None,
        "end_line": None,
        "syntax": "stored_key",
        "relation_candidate": "unclassified",
        "text_available": False,
        "source_field": None,
        "truth_assessed": False,
        "reference_ids": [],
        "unresolved_keys": ["doe2024"],
    }]
    gap_codes = {gap["code"] for gap in brief["gaps"]}
    assert "no_source_locator" in gap_codes
    assert "unresolved_citation" in gap_codes
    assert "no_supporting_evidence" in gap_codes
    assert "no_assumptions" in gap_codes


def test_record_argument_links_evidence_and_report_checks(tmp_path):
    graph = _graph(tmp_path, "argument-record")
    reference = _reference(graph, "smith2024", "A Graph Paper")
    evidence = graph.record(
        "Evidence", evidence_type="experiment",
        wording="The trial improved coverage.",
        source_locator={"path": "results.md", "start_line": 4})
    finding = graph.record(
        "ReviewFinding", severity="minor", issue_type="scope",
        wording="The result may not generalize.")
    other_claim = _claim(graph, wording="Coverage is the relevant outcome.")
    claim = _claim(
        graph,
        citations=["smith2024"],
        source_locator={"path": "paper.tex", "start_line": 10})

    recorded = record_argument(
        graph,
        claim_id=claim["id"],
        mode="steelman",
        thesis="The method improves review coverage in the tested corpus.",
        premises=[
            {
                "wording": "The trial improved coverage.",
                "inference": "deductive",
                "evidence_ids": [evidence["id"]],
                "claim_ids": [other_claim["id"]],
                "citation_keys": ["smith2024"],
                "strength": 0.7,
                "residual": "Only one corpus was tested.",
            },
            {
                "wording": "The outcome measure is the right one.",
                "inference": "assumption",
            },
        ],
        assumptions=["The corpus is representative."],
        objections=[
            {
                "wording": "The effect may be corpus-specific.",
                "evidence_ids": [finding["id"]],
                "citation_keys": ["smith2024"],
                "response": "The scope is explicitly limited to this corpus.",
            },
            {
                "wording": "A replication has not been attempted.",
                "status": "unanswered",
            },
        ],
        falsifiers=["An independent replication finds no coverage gain."],
        confidence=0.7,
        reviewer="reader",
        model_runs=[{"model": "fake-reviewer", "role": "critic"}],
        notes="Candidate steelman.",
    )

    argument = recorded["argument"]
    assert recorded["truth_assessed"] is False
    assert argument["type"] == "Argument"
    assert argument["mode"] == "steelman"
    assert argument["status"] == "candidate"
    assert argument["truth_assessed"] is False
    assert argument["confidence"] == 0.7
    assert argument["reviewer"] == "reader"
    assert len(argument["premises"]) == 2
    assert argument["model_runs"] == [{"model": "fake-reviewer", "role": "critic"}]

    links = recorded["links"]
    assert links[0]["type"] == "argues_for"
    assert links[0]["tgt"] == claim["id"]
    assert {link["type"] for link in links} == {"argues_for", "uses"}
    uses_targets = {link["tgt"] for link in links if link["type"] == "uses"}
    assert uses_targets == {
        evidence["id"], other_claim["id"], reference["id"], finding["id"]}
    outgoing = [
        edge for edge in graph.state()["edges"] if edge["src"] == argument["id"]
    ]
    assert {edge["type"] for edge in outgoing} == {"argues_for", "uses"}
    assert not any(edge["type"] in {"supports", "opposes"} for edge in outgoing)

    checks = check_argument(graph.state(), argument["id"])
    assert checks["ok"] is True
    assert checks["issues"] == []
    assert checks["warnings"] == []
    assert checks["truth_assessed"] is False
    assert checks["counts"]["premises"] == 2
    assert checks["counts"]["non_assumption_premises"] == 1
    assert checks["counts"]["evidenced_premises"] == 1
    assert checks["counts"]["assumptions"] == 1
    assert checks["counts"]["objections"] == 2
    assert checks["counts"]["unanswered_objections"] == 1
    assert checks["counts"]["falsifiers"] == 1
    assert checks["counts"]["linked_ids"] == 4

    report = argument_report(graph.state(), argument["id"])
    assert report["argument"]["id"] == argument["id"]
    assert report["claim"]["id"] == claim["id"]
    assert {node["id"] for node in report["claims"]} == {
        claim["id"], other_claim["id"]}
    assert {node["id"] for node in report["evidence"]} == {evidence["id"]}
    assert {node["id"] for node in report["references"]} == {reference["id"]}
    assert {node["id"] for node in report["findings"]} == {finding["id"]}
    assert report["checks"]["ok"] is True
    assert report["truth_assessed"] is False


def test_record_argument_rejects_invalid_records_without_writing(tmp_path):
    graph = _graph(tmp_path, "argument-invalid")
    _reference(graph, "smith2024", "A Graph Paper")
    evidence = graph.record(
        "Evidence", evidence_type="experiment", wording="A result.")
    claim = _claim(graph)

    common = {
        "claim_id": claim["id"],
        "mode": "steelman",
        "thesis": "A thesis.",
        "falsifiers": ["A counterexample."],
        "confidence": 0.5,
        "reviewer": "reader",
    }

    with pytest.raises(ArgumentError, match="evidence id"):
        record_argument(
            graph,
            **common,
            premises=[{"wording": "Unevidenced.", "inference": "deductive"}],
        )
    with pytest.raises(ArgumentError, match="unknown node id"):
        record_argument(
            graph,
            **common,
            premises=[{
                "wording": "Unknown claim.",
                "inference": "deductive",
                "claim_ids": ["claim-9999"],
            }],
        )
    with pytest.raises(ArgumentError, match="unknown node id"):
        record_argument(
            graph,
            **common,
            premises=[{
                "wording": "Unknown evidence.",
                "inference": "deductive",
                "evidence_ids": ["evidence-9999"],
            }],
        )
    with pytest.raises(ArgumentError, match="unresolved citation key"):
        record_argument(
            graph,
            **common,
            premises=[{
                "wording": "Unknown citation.",
                "inference": "deductive",
                "citation_keys": ["missing2025"],
            }],
        )
    with pytest.raises(ArgumentError, match="unresolved citation key"):
        record_argument(
            graph,
            **common,
            premises=[{
                "wording": "A supported premise.",
                "inference": "deductive",
                "evidence_ids": [evidence["id"]],
            }],
            objections=[{
                "wording": "An objection with a bad key.",
                "citation_keys": ["missing2025"],
            }],
        )
    with pytest.raises(ArgumentError, match="unknown Claim id"):
        record_argument(
            graph,
            **{**common, "claim_id": "claim-9999"},
            premises=[{"wording": "Assumed.", "inference": "assumption"}],
        )
    with pytest.raises(ArgumentError, match="mode"):
        record_argument(
            graph,
            **{**common, "mode": "verdict"},
            premises=[{"wording": "Assumed.", "inference": "assumption"}],
        )
    with pytest.raises(ArgumentError, match="thesis"):
        record_argument(
            graph,
            **{**common, "thesis": "   "},
            premises=[{"wording": "Assumed.", "inference": "assumption"}],
        )
    with pytest.raises(ArgumentError, match="reviewer"):
        record_argument(
            graph,
            **{**common, "reviewer": " "},
            premises=[{"wording": "Assumed.", "inference": "assumption"}],
        )
    with pytest.raises(ArgumentError, match="confidence"):
        record_argument(
            graph,
            **{**common, "confidence": 1.5},
            premises=[{"wording": "Assumed.", "inference": "assumption"}],
        )
    with pytest.raises(ArgumentError, match="premises must be a non-empty list"):
        record_argument(
            graph,
            **common,
            premises=[],
        )
    with pytest.raises(ArgumentError, match="strength"):
        record_argument(
            graph,
            **common,
            premises=[{
                "wording": "A supported premise.",
                "inference": "deductive",
                "evidence_ids": [evidence["id"]],
                "strength": 2,
            }],
        )

    assert not any(
        node["type"] == "Argument" for node in graph.state()["nodes"])


def test_check_argument_flags_gaps_and_support_edges(tmp_path):
    graph = _graph(tmp_path, "argument-check-gaps")
    _reference(graph, "smith2024", "A Graph Paper")
    evidence = graph.record(
        "Evidence", evidence_type="experiment", wording="A result.")
    claim = _claim(graph)
    recorded = record_argument(
        graph,
        claim_id=claim["id"],
        mode="red_team",
        thesis="The claim does not survive replication.",
        premises=[{
            "wording": "The original result was not replicated.",
            "inference": "inductive",
            "evidence_ids": [evidence["id"]],
        }],
        assumptions=[],
        objections=[{"wording": "The replication may be underpowered."}],
        falsifiers=[],
        confidence=0.5,
        reviewer="reader",
    )
    argument = recorded["argument"]

    checks = check_argument(graph.state(), argument["id"])
    assert checks["ok"] is False
    assert "objection_unanswered" in {issue["code"] for issue in checks["issues"]}
    assert "falsifiers_missing" in {item["code"] for item in checks["warnings"]}
    assert checks["counts"]["objections"] == 1
    assert checks["counts"]["unanswered_objections"] == 1
    assert checks["counts"]["falsifiers"] == 0

    bad = graph.record(
        "Argument",
        claim_id=evidence["id"],
        mode="unknown",
        thesis="",
        premises=[{"wording": "No support.", "inference": "deductive"}],
        assumptions=[42],
        objections=[{
            "wording": "Unknown evidence.",
            "evidence_ids": ["evidence-9999"],
            "citation_keys": ["missing2025"],
        }],
        falsifiers=[],
        confidence=2.0,
        reviewer="",
        truth_assessed=False,
        status="candidate",
    )
    bad_checks = check_argument(graph.state(), bad["id"])
    codes = {issue["code"] for issue in bad_checks["issues"]}
    assert {
        "mode_invalid",
        "thesis_missing",
        "reviewer_missing",
        "confidence_invalid",
        "premise_unevidenced",
        "assumption_invalid",
        "objection_unanswered",
        "unknown_id",
        "unresolved_citation",
        "unknown_id",
        "claim_id_type_mismatch",
    } <= codes
    assert bad_checks["counts"]["issues"] == len(bad_checks["issues"])

    state = graph.state()
    state["edges"].append({
        "id": "edge-999999",
        "type": "supports",
        "src": argument["id"],
        "tgt": evidence["id"],
        "created_at": "2026-01-01T00:00:00Z",
    })
    with_support = check_argument(state, argument["id"])
    assert "argument_support_edge" in {
        issue["code"] for issue in with_support["issues"]}
    state["edges"][-1]["type"] = "opposes"
    with_oppose = check_argument(state, argument["id"])
    assert "argument_opposes_edge" in {
        issue["code"] for issue in with_oppose["issues"]}
    assert with_oppose["counts"]["supports_or_opposes_edges"] == 1

    with pytest.raises(ArgumentError, match="unknown Argument id"):
        check_argument(graph.state(), "argument-9999")
    with pytest.raises(ArgumentError, match="unknown Argument id"):
        argument_report(graph.state(), "argument-9999")


@pytest.mark.parametrize("mode", ["steelman", "red_team", "synthesis"])
def test_argument_modes_are_accepted_and_prompts_are_mode_specific(tmp_path, mode):
    graph = _graph(tmp_path, f"argument-mode-{mode.replace('_', '-')}")
    claim = _claim(graph)

    brief = build_argument_brief(graph.state(), claim_id=claim["id"], mode=mode)
    assert brief["mode"] == mode
    assert f"Role: {mode} argument builder." in brief["prompt"]

    recorded = record_argument(
        graph,
        claim_id=claim["id"],
        mode=mode,
        thesis=f"A {mode} thesis.",
        premises=[{
            "wording": "The corpus is representative.",
            "inference": "assumption",
        }],
        assumptions=["The corpus is representative."],
        objections=[{"wording": "The corpus may not be representative.",
                     "status": "unanswered"}],
        falsifiers=["A representative corpus shows no effect."],
        confidence=0.4,
        reviewer="reader",
    )
    assert recorded["argument"]["mode"] == mode
    assert check_argument(graph.state(), recorded["argument"]["id"])["ok"] is True

    other = "red_team" if mode != "red_team" else "synthesis"
    other_brief = build_argument_brief(
        graph.state(), claim_id=claim["id"], mode=other)
    assert other_brief["prompt"] != brief["prompt"]
    assert other_brief["packet_sha256"] != brief["packet_sha256"]


def test_brief_rejects_unknown_claim_and_bad_mode(tmp_path):
    graph = _graph(tmp_path, "argument-brief-invalid")
    evidence = graph.record(
        "Evidence", evidence_type="experiment", wording="A result.")
    claim = _claim(graph)

    with pytest.raises(ArgumentError, match="unknown Claim id"):
        build_argument_brief(graph.state(), claim_id="claim-9999")
    with pytest.raises(ArgumentError, match="unknown Claim id"):
        build_argument_brief(graph.state(), claim_id=evidence["id"])
    with pytest.raises(ArgumentError, match="mode"):
        build_argument_brief(graph.state(), claim_id=claim["id"], mode="verdict")
