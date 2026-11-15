from __future__ import annotations

import csv
import io
import json

import pytest

from ozagia.research import appraisal
from ozagia.research import argument as argument_mod
from ozagia.research import contradiction
from ozagia.research import extraction
from ozagia.research import paper_review as pr
from ozagia.research import structured_review as sr
from ozagia.research import theory
from ozagia.research.graph import ResearchGraph

BIBLIOGRAPHY = """@article{smith2024,
  title = {A Graph Paper},
  author = {Smith, Alice},
  year = {2024},
  journal = {Journal of Graph Studies}
}
"""

PAPER = r"""\section{Introduction}
We study bounded-degree graphs \cite{smith2024}.

\begin{theorem}\label{thm:main}
For every bounded-degree graph \cite{smith2024}, the method converges.
\end{theorem}
\begin{proof}
By the potential argument in the paper.
\end{proof}
"""


def _graph(tmp_path, *, investigation_id="paper-review"):
    source_root = tmp_path / "sources"
    source_root.mkdir(exist_ok=True)
    (source_root / "paper.tex").write_text(PAPER, encoding="utf-8")
    (source_root / "refs.bib").write_text(BIBLIOGRAPHY, encoding="utf-8")
    return ResearchGraph.create(
        "Review a paper",
        root=tmp_path,
        investigation_id=investigation_id,
        roots=[
            {
                "root_id": "papers",
                "path": str(source_root),
                "repository": "fixture",
                "files": {"paper.tex": "selected", "refs.bib": "bibliography"},
            }
        ],
    )


def _start(tmp_path, **overrides):
    graph = _graph(tmp_path, investigation_id=overrides.pop("investigation_id", "paper-review"))
    arguments = {
        "root_id": "papers",
        "path": "paper.tex",
        "question": "Does the method converge for bounded-degree graphs?",
        "roles": ["methods", "statistics"],
        "models": ["fake-model-a", "fake-model-b"],
        "bibliography_path": "refs.bib",
        "min_reviewers": 2,
    }
    arguments.update(overrides)
    return graph, pr.start_paper_review(graph, **arguments)


def _finding(claim_id, *, wording, issue_type, severity="major", **extra):
    item = {
        "wording": wording,
        "severity": severity,
        "issue_type": issue_type,
    }
    if claim_id is not None:
        item["claim_id"] = claim_id
    item.update(extra)
    return item


def _run(graph, plan, reviewer, findings, *, recommendation="revise", confidence=0.7):
    return sr.record_review_run(
        graph,
        plan_id=plan["id"],
        reviewer_id=reviewer,
        role="reviewer",
        model=f"fake-{reviewer}",
        packet_sha256="p" * 64,
        prompt_sha256="q" * 64,
        context_sha256="c" * 64,
        provider="fake",
        output={
            "summary": f"Summary recorded by {reviewer}.",
            "findings": findings,
            "recommendation": recommendation,
            "confidence": confidence,
        },
    )


def _record_argument(graph, claim_id, mode, *, reviewer):
    return argument_mod.record_argument(
        graph,
        claim_id=claim_id,
        mode=mode,
        thesis=f"The {mode} thesis for the convergence claim.",
        premises=[
            {
                "wording": "The potential argument bounds every step.",
                "inference": "deductive",
                "claim_ids": [claim_id],
            },
            {
                "wording": "The bounded-degree condition is used at each step.",
                "inference": "assumption",
            },
        ],
        assumptions=["The graph degree bound is uniform."],
        objections=[
            {
                "wording": "The proof does not cover disconnected graphs.",
                "response": "The potential argument applies component-wise.",
                "status": "answered",
            },
            {
                "wording": "The empirical baseline is missing.",
                "status": "unanswered",
            },
        ],
        falsifiers=["A bounded-degree graph where the potential increases."],
        confidence=0.6,
        reviewer=reviewer,
    )


def test_start_ingests_paper_and_targets_the_source(tmp_path):
    graph, result = _start(tmp_path)

    source = result["source"]
    plan = result["plan"]
    packet = result["packet"]
    assert source["type"] == "Source"
    assert result["ingest"]["ingested"] is True
    assert plan["target_id"] == source["id"]
    assert plan["kind"] == "peer_review"
    assert plan["roles"] == ["methods", "statistics"]
    assert plan["models"] == ["fake-model-a", "fake-model-b"]
    assert plan["min_reviewers"] == 2
    assert packet["source_id"] == source["id"]
    assert packet["plan_id"] == plan["id"]
    assert packet["claim_ids"]
    assert packet["claims"][0]["source_locator"]["path"] == "paper.tex"
    assert packet["reference_ids"]
    assert packet["checks"]["references"]["counts"]["references"] == 1
    assert packet["prompt"]
    assert "claims_evidence_alignment" in packet["output_schema"]
    assert packet["packet_sha256"]
    assert len(packet["packet_sha256"]) == 64
    assert packet["truth_assessed"] is False
    assert result["truth_assessed"] is False

    again = pr.start_paper_review(
        graph,
        root_id="papers",
        path="paper.tex",
        question="Does the method converge?",
        roles=["methods"],
        models=["fake-model-a", "fake-model-b"],
        bibliography_path="refs.bib",
    )
    assert again["ingest"]["duplicate"] is True
    assert again["source"]["id"] == source["id"]
    assert len([
        node for node in graph.state()["nodes"]
        if node.get("type") == "Source"
        and node.get("locator", {}).get("root_id") == "papers"
        and node.get("locator", {}).get("path") == "paper.tex"
    ]) == 1
    assert len([
        node for node in graph.state()["nodes"]
        if node.get("type") == "ReviewPlan"
    ]) == 2


def test_start_rejects_unsupported_paper_suffix(tmp_path):
    graph = _graph(tmp_path)
    with pytest.raises(pr.PaperReviewError, match=r"\.pdf"):
        pr.start_paper_review(
            graph,
            root_id="papers",
            path="paper.docx",
            question="Is this reviewable?",
            roles=["methods"],
            models=["fake-model-a"],
            min_reviewers=1,
        )


def test_packet_collects_claims_evidence_checks_and_is_deterministic(tmp_path):
    graph, result = _start(tmp_path)
    source = result["source"]
    claim = result["packet"]["claims"][0]

    evidence = graph.record(
        "Evidence",
        evidence_type="observation",
        wording="The measured error decreases at every step.",
        status="candidate",
        truth_assessed=False,
    )
    graph.link(evidence["id"], "supports", claim["id"])
    assumption = graph.record(
        "Assumption",
        wording="The degree bound is uniform.",
        status="assumed",
        truth_assessed=False,
    )
    graph.link(claim["id"], "assumes", assumption["id"])
    extraction.record_extraction(
        graph,
        claim_id=claim["id"],
        fields={
            "study_design": "theorem",
            "population": "bounded-degree graphs",
            "outcomes": ["convergence"],
            "effect_measure": "rate",
            "effect_size": 0.5,
        },
        extractor="fixture",
        source_locator={"path": "paper.tex", "start_line": 1},
    )
    appraisal.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="custom",
        domains=[
            {
                "name": "selection",
                "judgement": "low",
                "support": "The theorem has no sampling step.",
            }
        ],
        overall="low",
        reviewer="reviewer-a",
    )
    theory.record_theory(
        graph,
        name="Potential-function account",
        statement="Convergence follows from a monotone potential.",
        scope="bounded-degree graphs",
        predictions=["The error decreases every step."],
    )

    packet = pr.build_paper_review_packet(
        graph.state(), source_id=source["id"], plan_id=result["plan"]["id"]
    )
    repeated = pr.build_paper_review_packet(
        graph.state(), source_id=source["id"], plan_id=result["plan"]["id"]
    )

    assert packet["packet_sha256"] == repeated["packet_sha256"]
    assert packet["claims_truncated"] is False
    assert packet["claims"][0]["evidence"][0]["node_id"] == evidence["id"]
    assert packet["claims"][0]["assumptions"][0]["node_id"] == assumption["id"]
    assert "smith2024" in packet["claims"][0]["citation_keys"]
    assert packet["extraction_table"]["count"] == 1
    assert packet["extraction_table"]["rows"][0]["target"] == claim["id"]
    assert packet["appraisal_table"]["count"] == 1
    assert packet["appraisal_table"]["rows"][0]["target_id"] == claim["id"]
    assert packet["contradiction_scan"]["truth_assessed"] is False
    assert packet["alert_scan"]["truth_assessed"] is False
    assert packet["theory_ledger"]["counts"]["theories"] == 1
    assert packet["compatibility_matrix"]["counts"]["theories"] == 1
    assert packet["checks"]["extraction"]["count"] == 1
    assert packet["checks"]["appraisal"]["count"] == 1
    assert packet["checks"]["theory"]["count"] == 1
    assert packet["truth_assessed"] is False

    limited = pr.build_paper_review_packet(
        graph.state(), source_id=source["id"], max_claims=0
    )
    assert limited["claim_ids"] == []
    assert limited["claims_truncated"] is True


def test_packet_uses_path_and_reference_locator_matching(tmp_path):
    graph = _graph(tmp_path, investigation_id="paper-review-path")
    source = graph.record(
        "Source",
        name="paper.tex",
        paper_import=True,
        locator={"root_id": "papers", "path": "paper.tex", "sha256": "a" * 64},
    )
    by_path = graph.record(
        "Claim",
        wording="Path-linked claim.",
        claim_type="inference",
        status="unresolved",
        source_locator={"path": "paper.tex", "start_line": 1},
    )
    by_reference = graph.record(
        "Claim",
        wording="Reference-linked claim.",
        claim_type="inference",
        status="unresolved",
        source_locator={"reference_id": source["id"], "start_line": 2},
    )
    unrelated = graph.record(
        "Claim",
        wording="Unrelated claim.",
        claim_type="inference",
        status="unresolved",
        source_locator={"path": "other.tex", "start_line": 1},
    )
    packet = pr.build_paper_review_packet(graph.state(), source_id=source["id"])
    assert packet["claim_ids"] == [by_path["id"], by_reference["id"]]
    assert unrelated["id"] not in packet["claim_ids"]


def test_report_aggregates_runs_arguments_and_renders_markdown(tmp_path):
    graph, result = _start(tmp_path)
    plan = result["plan"]
    claim_id = result["packet"]["claim_ids"][0]

    common = _finding(
        claim_id,
        wording="The theorem has no empirical baseline.",
        issue_type="missing_baseline",
        severity="major",
    )
    strength = _finding(
        claim_id,
        wording="The proof is self-contained and clearly bounded.",
        issue_type="strength",
        severity="note",
    )
    statistics = _finding(
        claim_id,
        wording="No effect size or confidence interval is reported.",
        issue_type="statistics",
        severity="major",
    )
    reproducibility = _finding(
        claim_id,
        wording="The implementation is not available.",
        issue_type="reproducibility",
        severity="minor",
    )
    question = _finding(
        claim_id,
        wording="What is the sampling frame for the empirical comparison?",
        issue_type="question",
        severity="minor",
    )
    dissent_only = _finding(
        claim_id,
        wording="The proof omits disconnected graphs.",
        issue_type="scope",
        severity="major",
    )

    _run(graph, plan, "reviewer-a", [common, strength])
    _run(graph, plan, "reviewer-b", [common, statistics, reproducibility, question])
    _run(graph, plan, "reviewer-c", [common, dissent_only])
    _record_argument(graph, claim_id, "steelman", reviewer="advocate")
    _record_argument(graph, claim_id, "red_team", reviewer="critic")
    _record_argument(graph, claim_id, "synthesis", reviewer="synthesizer")
    reconciliation = sr.record_reconciliation(graph, plan_id=plan["id"])

    report = pr.paper_review_report(graph.state(), plan_id=plan["id"])

    assert report["truth_assessed"] is False
    assert report["plan_id"] == plan["id"]
    assert len(report["runs"]) == 3
    assert len(report["findings"]) == 8
    assert report["agreement"]["counts"]["runs"] == 3
    assert report["reconciliation"]["id"] == reconciliation["reconciliation"]["id"]
    assert report["recommendation"] == "revise"
    assert report["confidence"] == 0.7
    assert report["recommendation_source"] == "run"
    assert len(report["arguments"]) == 3
    assert set(report["arguments_by_claim"][claim_id]) == {
        "steelman",
        "red_team",
        "synthesis",
    }
    steelman = report["arguments_by_claim"][claim_id]["steelman"][0]
    assert steelman["premises"][0]["wording"] == "The potential argument bounds every step."
    assert steelman["assumptions"]
    assert steelman["objections"][0]["response"]
    assert steelman["falsifiers"]
    assert report["claims"][0]["id"] == claim_id
    assert report["claims_evidence"]
    assert report["section_meta"]["strengths"]["empty"] is False
    assert report["section_meta"]["ethics"]["empty"] is True
    assert "No ethics findings recorded." in report["sections"]["ethics"]
    assert report["dissent"]
    assert report["checks"]["arguments"]["ok"] is True

    markdown = pr.review_markdown(report)
    for title in (
        "Summary",
        "Strengths",
        "Weaknesses",
        "Major issues",
        "Minor issues",
        "Questions",
        "Methods",
        "Statistics",
        "Reproducibility",
        "Ethics",
        "Novelty and related work",
        "Claims and evidence",
        "Theory",
        "Contradictions",
        "Alerts",
        "Dissent",
        "Audit",
    ):
        assert f"## {title}" in markdown
    lowered = markdown.lower()
    assert "steelman" in lowered
    assert "red-team" in lowered
    assert "synthesis" in lowered
    assert "premises:" in lowered
    assert "falsifiers:" in lowered
    assert "truth assessed: **no**" in lowered
    assert "No ethics findings recorded." in markdown


def test_empty_report_marks_every_section_explicitly(tmp_path):
    graph, result = _start(tmp_path)
    report = pr.paper_review_report(graph.state(), plan_id=result["plan"]["id"])

    assert report["runs"] == []
    assert report["findings"] == []
    assert report["arguments"] == []
    assert report["reconciliation"] is None
    assert report["recommendation"] is None
    assert report["confidence"] is None
    for name in pr.REVIEW_SECTIONS:
        assert name in report["sections"]
        assert name in report["section_meta"]
        assert name in report["sections"]
        assert report["sections"][name].strip()
    assert report["section_meta"]["summary"]["empty"] is True
    assert report["sections"]["summary"] == "No review run summaries recorded."
    assert report["sections"]["claims_evidence"].strip()
    assert "No review run summaries recorded." in pr.review_markdown(report)
    assert "No dissent recorded." in pr.review_markdown(report)
    assert "No reconciled recommendation or confidence recorded." in pr.review_markdown(report)


def test_bundle_writes_only_expected_files_and_is_deterministic(tmp_path):
    graph, result = _start(tmp_path)
    plan = result["plan"]
    bundle_dir = tmp_path / "bundle"
    first = pr.write_paper_review_bundle(graph, plan_id=plan["id"], output_dir=bundle_dir)

    assert first["truth_assessed"] is False
    assert first["count"] == len(pr.BUNDLE_FILES) == 7
    assert set(first["files"]) == set(pr.BUNDLE_FILES)
    assert set(path.name for path in bundle_dir.iterdir()) == set(pr.BUNDLE_FILES)
    assert first["checks"]["truth_assessed"] is False

    review_md = (bundle_dir / "review.md").read_text(encoding="utf-8")
    review_json = json.loads((bundle_dir / "review.json").read_text(encoding="utf-8"))
    arguments_json = json.loads((bundle_dir / "arguments.json").read_text(encoding="utf-8"))
    checks_json = json.loads((bundle_dir / "checks.json").read_text(encoding="utf-8"))
    assert review_md.startswith("# Paper review report")
    assert review_json["plan_id"] == plan["id"]
    assert review_json["truth_assessed"] is False
    assert arguments_json["truth_assessed"] is False
    assert checks_json["truth_assessed"] is False
    assert (bundle_dir / "references.bib").read_text(encoding="utf-8").startswith("@article")
    claim_rows = list(csv.DictReader(io.StringIO(
        (bundle_dir / "claims.csv").read_text(encoding="utf-8")
    )))
    assert claim_rows
    assert claim_rows[0]["kind"] == "Claim"

    before = {
        path.name: path.read_text(encoding="utf-8")
        for path in bundle_dir.iterdir()
    }
    second = pr.write_paper_review_bundle(
        graph, plan_id=plan["id"], output_dir=bundle_dir
    )
    after = {
        path.name: path.read_text(encoding="utf-8")
        for path in bundle_dir.iterdir()
    }
    assert second["count"] == first["count"]
    assert before == after


def test_bundle_defaults_under_the_research_state_directory(tmp_path):
    graph, result = _start(tmp_path, investigation_id="paper-review-default")
    plan = result["plan"]
    written = pr.write_paper_review_bundle(graph, plan_id=plan["id"])

    expected = graph.path.parent / "exports" / f"review-{plan['id']}"
    assert written["output_dir"] == str(expected)
    assert expected.is_dir()
    assert set(path.name for path in expected.iterdir()) == set(pr.BUNDLE_FILES)


def test_paper_review_module_has_no_model_runtime():
    assert "providers" not in pr.__dict__
    assert "model" not in pr.__dict__
    assert "run_model" not in pr.__dict__
    # Contradiction scanning is deterministic and does not need a model runtime.
    assert contradiction.find_contradictions
