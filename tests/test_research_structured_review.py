from __future__ import annotations

import hashlib

import pytest

from ozagia.research import references as refs
from ozagia.research import structured_review as sr
from ozagia.research.graph import ResearchGraph


def _base_graph(tmp_path):
    graph = ResearchGraph.create(
        "Structured review",
        root=tmp_path,
        investigation_id="structured-review",
    )
    reference = refs.record_reference(graph, {
        "title": "A Graph Paper",
        "citation_key": "smith2024",
        "identifiers": {"doi": "10.1234/abc.1"},
    })["reference"]
    claim = graph.record(
        "Claim",
        wording="The graph method converges under bounded degree.",
        claim_type="empirical_observation",
        status="unresolved",
        scope="bounded-degree graphs",
        author="author",
        limitations="none recorded",
        citations=["smith2024"],
        source_locator={"path": "paper.tex", "start_line": 4, "end_line": 6},
    )
    graph.link(claim["id"], "cites", reference["id"])
    return graph, claim, reference


def _plan(graph, claim, **overrides):
    arguments = {
        "question": "Does the graph method converge?",
        "roles": ["methods", "statistics"],
        "models": ["model-a", "model-b"],
        "criteria": ["peer-reviewed", "bounded-degree graphs"],
        "exclusion_criteria": ["unbounded degree"],
        "target_id": claim["id"],
    }
    arguments.update(overrides)
    return sr.create_review_plan(graph, **arguments)["plan"]


def _finding(claim_id, *, wording, issue_type="missing_baseline", severity="major", **extra):
    finding = {
        "wording": wording,
        "severity": severity,
        "issue_type": issue_type,
    }
    if claim_id is not None:
        finding["claim_id"] = claim_id
    finding.update(extra)
    return finding


def _run(graph, plan, reviewer, findings, **overrides):
    arguments = {
        "plan_id": plan["id"],
        "reviewer_id": reviewer,
        "role": "reviewer",
        "model": f"fake-{reviewer}",
        "packet_sha256": "p" * 64,
        "prompt_sha256": "q" * 64,
        "context_sha256": "c" * 64,
        "provider": "fake",
        "output": {
            "summary": f"Review by {reviewer}.",
            "findings": findings,
            "recommendation": "revise",
            "confidence": 0.8,
        },
    }
    arguments.update(overrides)
    return sr.record_review_run(graph, **arguments)


def _review_fixture(tmp_path):
    graph, claim, reference = _base_graph(tmp_path)
    plan = _plan(graph, claim)
    common = _finding(
        claim["id"],
        wording="Missing baseline comparison.",
        issue_type="missing_baseline",
        severity="major",
    )
    scope = _finding(
        claim["id"],
        wording="The scope of the claim is ambiguous.",
        issue_type="scope_ambiguity",
        severity="minor",
    )
    citation = _finding(
        None,
        wording="The citation does not support the claim.",
        issue_type="citation_support",
        severity="blocking",
        reference_id=reference["id"],
        citation_keys=["smith2024"],
    )
    run1 = _run(graph, plan, "reviewer-a", [common, scope])["run"]
    run2 = _run(graph, plan, "reviewer-b", [common])["run"]
    run3 = _run(graph, plan, "reviewer-c", [common, citation])["run"]
    return graph, plan, claim, reference, [run1, run2, run3]


def test_create_review_plan_and_search_run(tmp_path):
    graph, claim, _reference = _base_graph(tmp_path)
    result = sr.create_review_plan(
        graph,
        question="Does the graph method converge?",
        kind="rapid",
        roles=["methods", "statistics"],
        models=["model-a", "model-b"],
        criteria=["peer-reviewed"],
        sources=[{"kind": "database", "name": "fixture"}],
        reviewers=["reviewer-a", "reviewer-b"],
        rounds=["independent", "rebuttal"],
        blinding=True,
        min_reviewers=2,
        adjudication="human",
        target_id=claim["id"],
    )
    plan = result["plan"]
    assert plan["type"] == "ReviewPlan"
    assert plan["status"] == "open"
    assert plan["truth_assessed"] is False
    assert plan["kind"] == "rapid"
    assert plan["roles"] == ["methods", "statistics"]
    assert plan["models"] == ["model-a", "model-b"]
    assert plan["rounds"] == ["independent", "rebuttal"]
    assert plan["min_reviewers"] == 2
    assert plan["target_id"] == claim["id"]
    assert result["target"]["id"] == claim["id"]
    assert any(
        edge["type"] == "addresses"
        and edge["src"] == plan["id"]
        and edge["tgt"] == claim["id"]
        for edge in graph.state()["edges"]
    )

    search = sr.record_search_run(
        graph,
        plan["id"],
        query="graph convergence bounded degree",
        provider="fixture-index",
        filters={"year": "2020-2026"},
        result_count=12,
        deduped_count=9,
        export_sha256="a" * 64,
        notes="first pass",
    )
    run = search["search_run"]
    assert run["type"] == "SearchRun"
    assert run["query"] == "graph convergence bounded degree"
    assert run["provider"] == "fixture-index"
    assert run["result_count"] == 12
    assert run["deduped_count"] == 9
    assert run["executed_at"]
    assert run["truth_assessed"] is False
    assert any(
        edge["type"] == "searches"
        and edge["src"] == run["id"]
        and edge["tgt"] == plan["id"]
        for edge in graph.state()["edges"]
    )

    with pytest.raises(sr.ReviewError):
        sr.create_review_plan(
            graph,
            question="Invalid",
            kind="unknown",
            roles=["r"],
            models=["m"],
            min_reviewers=1,
        )
    with pytest.raises(sr.ReviewError):
        sr.create_review_plan(
            graph,
            question="Invalid",
            roles=[],
            models=["m"],
            min_reviewers=1,
        )
    with pytest.raises(sr.ReviewError):
        sr.create_review_plan(
            graph,
            question="Invalid",
            roles=["r"],
            models=["m"],
            min_reviewers=2,
        )
    with pytest.raises(sr.ReviewError):
        sr.create_review_plan(
            graph,
            question="Invalid",
            roles=["r"],
            models=["m"],
            min_reviewers=1,
            target_id="claim-9999",
        )
    with pytest.raises(sr.ReviewError):
        sr.record_search_run(graph, "reviewplan-9999", query="q", provider="p")


def test_review_packet_is_deterministic_and_read_only(tmp_path):
    graph, claim, reference = _base_graph(tmp_path)
    plan = _plan(graph, claim)
    support = graph.record(
        "Evidence",
        wording="A prior benchmark supports the convergence claim.",
        source_locator={"path": "prior.md", "start_line": 3},
    )
    graph.link(support["id"], "supports", claim["id"])
    opposing = graph.record(
        "Evidence",
        wording="A counterexample opposes the claim.",
        source_locator={"path": "counter.md"},
    )
    graph.link(opposing["id"], "opposes", claim["id"])
    assumption = graph.record(
        "Assumption", wording="Bounded degree is representative."
    )
    graph.link(claim["id"], "assumes", assumption["id"])
    derived = graph.record(
        "Evidence",
        wording="A derived observation.",
        source_locator={"path": "derived.md"},
    )
    graph.link(derived["id"], "derived_from", claim["id"])
    graph.amend(
        claim["id"],
        raw_excerpt="Prior work reported gains \\cite{smith2024}.",
    )

    body = "The graph method converges under bounded degree. See [@smith2024]."
    before = graph.state()
    packet = sr.build_review_packet(
        graph.state(), plan_id=plan["id"], manuscript_body=body
    )
    second = sr.build_review_packet(
        graph.state(), plan_id=plan["id"], manuscript_body=body
    )
    after = graph.state()

    assert packet == second
    assert before == after
    assert packet["packet_sha256"] == second["packet_sha256"]
    assert len(packet["packet_sha256"]) == 64
    assert packet["target_id"] == claim["id"]
    assert packet["target"]["id"] == claim["id"]
    assert packet["plan"]["id"] == plan["id"]
    assert packet["context"]["supporting"][0]["node"]["id"] == support["id"]
    assert packet["context"]["opposing"][0]["node"]["id"] == opposing["id"]
    assert packet["context"]["assuming"][0]["node"]["id"] == assumption["id"]
    assert packet["context"]["derived"][0]["node"]["id"] == derived["id"]
    assert any(
        item["node"]["id"] == reference["id"]
        for item in packet["context"]["citation"]
    )
    assert any(
        locator["node_id"] == claim["id"]
        and locator["locator"]["path"] == "paper.tex"
        for locator in packet["source_locators"]
    )
    assert packet["checks"]["references"]["truth_assessed"] is False
    assert packet["checks"]["manuscript"]["ok"] is True
    assert packet["citation_contexts"][0]["citation_keys"] == ["smith2024"]
    assert packet["citation_contexts"][0]["quote"].startswith("Prior work reported gains")
    assert claim["wording"] in packet["gathered_text"]
    assert "Structured review packet." in packet["prompt"]
    assert packet["truth_assessed"] is False

    without_body = sr.build_review_packet(graph.state(), claim_id=claim["id"])
    assert "manuscript" not in without_body["checks"]
    assert without_body["target_id"] == claim["id"]
    assert without_body["plan"] is None

    with pytest.raises(sr.ReviewError):
        sr.build_review_packet(graph.state())

    output = {
        "summary": "One issue and one supporting record.",
        "findings": [{
            "wording": "The claim needs an explicit baseline.",
            "severity": "major",
            "issue_type": "missing_baseline",
            "claim_id": claim["id"],
            "evidence_ids": [support["id"]],
            "citation_keys": ["smith2024"],
            "quote": claim["wording"],
            "confidence": 0.8,
        }],
    }
    validation = sr.validate_review_output(
        output, state=graph.state(), packet=packet
    )
    assert validation["valid"] is True, validation["errors"]
    assert validation["findings"][0]["claim_id"] == claim["id"]
    assert validation["findings"][0]["severity"] == "major"


def test_validate_review_output_reports_invalid_schema_ids_citations_and_quotes(tmp_path):
    graph, claim, _reference = _base_graph(tmp_path)
    plan = _plan(graph, claim)
    packet = sr.build_review_packet(graph.state(), plan_id=plan["id"])
    bad = {
        "summary": 7,
        "findings": [
            {
                "wording": "An invalid finding.",
                "severity": "critical",
                "issue_type": "",
                "claim_id": "claim-9999",
                "evidence_ids": ["evidence-9999"],
                "citation_keys": ["missing2020"],
                "quote": "This sentence is not present in the packet.",
                "confidence": 1.5,
            },
            "not-an-object",
        ],
        "recommendation": 4,
        "confidence": "high",
    }
    result = sr.validate_review_output(bad, state=graph.state(), packet=packet)
    assert result["valid"] is False
    assert result["findings"][0]["claim_id"] == "claim-9999"
    joined = " ".join(result["errors"])
    for fragment in (
        "summary must be a string",
        "severity",
        "issue_type",
        "claim-9999",
        "evidence-9999",
        "missing2020",
        "not present in the review packet",
        "findings[1] must be an object",
        "recommendation must be a string",
    ):
        assert fragment in joined, joined

    no_findings = sr.validate_review_output(
        {"summary": "No issues found.", "findings": []}
    )
    assert no_findings["valid"] is True
    assert "findings is empty" in " ".join(no_findings["warnings"])


def test_record_review_run_links_provenance_and_findings(tmp_path):
    graph, claim, reference = _base_graph(tmp_path)
    plan = _plan(graph, claim)
    evidence = graph.record("Evidence", wording="A supporting benchmark.")
    graph.link(evidence["id"], "supports", claim["id"])
    packet = sr.build_review_packet(graph.state(), plan_id=plan["id"])
    output = {
        "summary": "One major issue.",
        "findings": [{
            "wording": "The claim needs a baseline comparison.",
            "severity": "major",
            "issue_type": "missing_baseline",
            "claim_id": claim["id"],
            "evidence_ids": [evidence["id"]],
            "citation_keys": ["smith2024"],
            "quote": claim["wording"],
            "recommendation": "Add a baseline.",
            "confidence": 0.75,
        }],
        "recommendation": "revise",
        "confidence": 0.7,
    }
    recorded = sr.record_review_run(
        graph,
        plan_id=plan["id"],
        reviewer_id="reviewer-a",
        role="methods",
        model="model-a",
        packet_sha256=packet["packet_sha256"],
        output=output,
        provider="fake-provider",
        round="independent",
        prompt_sha256="q" * 64,
        context_sha256="c" * 64,
        params={"temperature": 0},
        tokens={"input": 10, "output": 20},
        cost=0.001,
        target_id=claim["id"],
    )
    run = recorded["run"]
    finding = recorded["findings"][0]
    assert run["type"] == "ReviewRun"
    assert run["reviewer_id"] == "reviewer-a"
    assert run["role"] == "methods"
    assert run["model"] == "model-a"
    assert run["provider"] == "fake-provider"
    assert run["packet_sha256"] == packet["packet_sha256"]
    assert run["output_sha256"] == hashlib.sha256(
        sr._canonical(output).encode("utf-8")
    ).hexdigest()
    assert run["params"] == {"temperature": 0}
    assert run["tokens"] == {"input": 10, "output": 20}
    assert run["warnings"] == []
    assert run["truth_assessed"] is False
    assert finding["type"] == "ReviewFinding"
    assert finding["run_id"] == run["id"]
    assert finding["severity"] == "major"
    assert finding["claim_id"] == claim["id"]
    assert finding["evidence_ids"] == [evidence["id"]]
    assert finding["citation_keys"] == ["smith2024"]

    edges = graph.state()["edges"]
    assert any(
        edge["type"] == "implements"
        and edge["src"] == run["id"]
        and edge["tgt"] == plan["id"]
        for edge in edges
    )
    assert any(
        edge["type"] == "finds"
        and edge["src"] == run["id"]
        and edge["tgt"] == finding["id"]
        for edge in edges
    )
    for target in (claim["id"], evidence["id"]):
        assert any(
            edge["type"] == "about"
            and edge["src"] == finding["id"]
            and edge["tgt"] == target
            for edge in edges
        )
    assert any(
        edge["type"] == "reviews"
        and edge["src"] == run["id"]
        and edge["tgt"] == claim["id"]
        for edge in edges
    )

    before = len(graph.state()["nodes"])
    with pytest.raises(sr.ReviewError):
        sr.record_review_run(
            graph,
            plan_id=plan["id"],
            reviewer_id="reviewer-b",
            role="reviewer",
            model="model-b",
            packet_sha256="p" * 64,
            output={
                "summary": "Invalid.",
                "findings": [{
                    "wording": "Bad severity.",
                    "severity": "critical",
                    "issue_type": "schema",
                }],
            },
        )
    assert len(graph.state()["nodes"]) == before

    empty = sr.record_review_run(
        graph,
        plan_id=plan["id"],
        reviewer_id="reviewer-c",
        role="reviewer",
        model="model-c",
        packet_sha256="p" * 64,
        output={"summary": "No material issues.", "findings": []},
    )
    assert "findings is empty" in " ".join(empty["run"]["warnings"])
    assert empty["validation"]["valid"] is True


def test_agreement_reports_kappa_jaccard_consensus_and_dissent(tmp_path):
    graph, plan, _claim, _reference, runs = _review_fixture(tmp_path)
    agreement = sr.review_agreement(graph.state(), plan["id"])
    assert sr.review_agreement(graph.state(), plan["id"]) == agreement

    assert agreement["plan_id"] == plan["id"]
    assert agreement["run_ids"] == [run["id"] for run in runs]
    assert len(agreement["runs"]) == 3
    assert agreement["reviewers"] == ["reviewer-a", "reviewer-b", "reviewer-c"]
    assert [len(agreement["findings_by_run"][run["id"]]) for run in runs] == [2, 1, 2]
    assert agreement["counts"] == {
        "runs": 3,
        "reviewers": 3,
        "findings": 5,
        "issues": 3,
        "consensus": 1,
        "dissent": 2,
        "single_reviewer": 2,
    }
    assert agreement["fleiss_kappa"] == pytest.approx(0.1)
    assert agreement["kappa"] == agreement["fleiss_kappa"]
    assert agreement["mean_pairwise_jaccard"] == pytest.approx(0.444444, abs=1e-6)
    assert agreement["mean_jaccard"] == agreement["mean_pairwise_jaccard"]
    assert agreement["severity_agreement"] == 1.0
    assert agreement["truth_assessed"] is False

    consensus = agreement["consensus"]
    dissent = agreement["dissent"]
    assert [row["wording"] for row in consensus] == ["Missing baseline comparison."]
    assert {row["wording"] for row in dissent} == {
        "The scope of the claim is ambiguous.",
        "The citation does not support the claim.",
    }
    assert all(row["reviewer_count"] >= 2 for row in consensus)
    assert all(row["reviewer_count"] == 1 for row in dissent)

    matrix = agreement["issue_matrix"]
    assert matrix["runs"] == agreement["run_ids"]
    assert len(matrix["rows"]) == 3
    common = next(row for row in matrix["rows"] if row["issue_type"] == "missing_baseline")
    assert common["presence"] == {
        runs[0]["id"]: True,
        runs[1]["id"]: True,
        runs[2]["id"]: True,
    }
    assert common["key"][0] == "Claim"
    assert common["key"][3] == "missing baseline comparison."
    assert agreement["warnings"] == []


def test_reconciliation_preserves_dissent_without_support_edges(tmp_path):
    graph, plan, _claim, _reference, runs = _review_fixture(tmp_path)
    result = sr.record_reconciliation(
        graph,
        plan_id=plan["id"],
        method="consensus",
        adjudicator="editor",
        rationale="Keep the minority findings visible.",
    )
    reconciliation = result["reconciliation"]
    assert reconciliation["type"] == "Reconciliation"
    assert reconciliation["status"] == "recorded"
    assert reconciliation["method"] == "consensus"
    assert reconciliation["adjudicator"] == "editor"
    assert reconciliation["rationale"] == "Keep the minority findings visible."
    assert reconciliation["truth_assessed"] is False
    assert reconciliation["run_ids"] == [run["id"] for run in runs]
    assert reconciliation["metrics"]["fleiss_kappa"] == pytest.approx(0.1)
    assert reconciliation["metrics"]["mean_pairwise_jaccard"] == pytest.approx(
        0.444444, abs=1e-6
    )
    assert len(reconciliation["consensus"]) == 1
    assert {row["wording"] for row in reconciliation["dissent"]} == {
        "The scope of the claim is ambiguous.",
        "The citation does not support the claim.",
    }

    edges = graph.state()["edges"]
    reconciled = {
        edge["tgt"] for edge in edges
        if edge["type"] == "reconciles" and edge["src"] == reconciliation["id"]
    }
    assert reconciled == {run["id"] for run in runs}
    assert not any(edge["type"] in {"supports", "opposes"} for edge in edges)

    subset = sr.record_reconciliation(
        graph,
        plan_id=plan["id"],
        method="consensus",
        min_reviewers=2,
        run_ids=[runs[0]["id"], runs[1]["id"]],
    )
    subset_reconciliation = subset["reconciliation"]
    assert subset_reconciliation["run_ids"] == [runs[0]["id"], runs[1]["id"]]
    assert subset_reconciliation["min_reviewers"] == 2
    assert {row["wording"] for row in subset_reconciliation["dissent"]} == {
        "The scope of the claim is ambiguous.",
    }
    subset_edges = {
        edge["tgt"] for edge in graph.state()["edges"]
        if edge["type"] == "reconciles"
        and edge["src"] == subset_reconciliation["id"]
    }
    assert subset_edges == {runs[0]["id"], runs[1]["id"]}
    assert not any(
        edge["type"] in {"supports", "opposes"} for edge in graph.state()["edges"]
    )


def test_validate_review_output_can_drop_a_bad_finding_without_losing_the_run(tmp_path):
    graph, claim, _reference = _base_graph(tmp_path)
    plan = _plan(graph, claim)
    packet = sr.build_review_packet(graph.state(), plan_id=plan["id"])
    output = {
        "summary": "One valid finding and one unsupported quote.",
        "findings": [
            _finding(claim["id"], wording="Missing baseline.", quote=claim["wording"]),
            _finding(claim["id"], wording="Bad quote.",
                     quote="This sentence is not in the packet."),
        ],
    }
    strict = sr.validate_review_output(output, state=graph.state(), packet=packet)
    assert strict["valid"] is False
    partial = sr.validate_review_output(
        output, state=graph.state(), packet=packet, drop_invalid_findings=True)
    assert partial["valid"] is True
    assert len(partial["findings"]) == 1
    assert partial["findings"][0]["wording"] == "Missing baseline."
    assert any("dropped" in warning for warning in partial["warnings"])


def test_review_report_collects_plan_runs_findings_and_latest_reconciliation(tmp_path):
    graph, plan, _claim, _reference, runs = _review_fixture(tmp_path)
    sr.record_reconciliation(graph, plan_id=plan["id"], method="consensus")
    latest = sr.record_reconciliation(
        graph,
        plan_id=plan["id"],
        method="consensus",
        rationale="Second pass.",
    )["reconciliation"]

    report = sr.review_report(graph.state(), plan["id"])
    assert sr.review_report(graph.state(), plan["id"]) == report
    assert report["plan"]["id"] == plan["id"]
    assert [run["id"] for run in report["runs"]] == [run["id"] for run in runs]
    assert len(report["findings"]) == 5
    assert report["reconciliation"]["id"] == latest["id"]
    assert len(report["reconciliations"]) == 2
    assert report["checks"]["references"]["truth_assessed"] is False
    assert report["checks"]["ok"] is True
    assert report["counts"] == {
        "runs": 3,
        "findings": 5,
        "reconciliations": 2,
    }
    assert report["truth_assessed"] is False

    with pytest.raises(sr.ReviewError):
        sr.review_report(graph.state(), "reviewplan-9999")
