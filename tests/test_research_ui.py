from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import research_ui_cli
from ozagia.config import state_path
from ozagia.core.research_ui import (
    SCHEMA_VERSION,
    VIEWS,
    ResearchUIError,
    build_research_view,
    load_state,
    render_html,
    render_json,
)
from ozagia.research.graph import SCHEMA_VERSION as GRAPH_SCHEMA_VERSION
from ozagia.research.graph import ResearchGraph


def _reference(graph: ResearchGraph, **fields):
    if "citation_keys" not in fields and fields.get("citation_key"):
        fields["citation_keys"] = [fields["citation_key"]]
    return graph.record("Reference", **fields)


@pytest.fixture()
def research(tmp_path: Path):
    graph = ResearchGraph.create(
        "Does X cause Y?", root=tmp_path,
        investigation_id="research-ui-test", mode="empirical")

    ref1 = _reference(
        graph, title="Retracted paper", authors=["Smith, A."], year="2020",
        venue="Journal", reference_type="article",
        identifiers={"doi": "10.1000/retracted"},
        citation_key="smith2020retracted",
        metadata_status="resolved_crossref",
        retraction={
            "status": "retracted", "provider": "openalex",
            "checked_at": "2026-01-01", "notices": ["Retraction notice"],
        },
        metadata_provenance=[{"provider": "crossref"}],
        source_locators=[{"path": "refs.bib", "start_line": 3}],
    )
    ref2 = _reference(
        graph, title="Concern paper", authors=["Doe, B."], year="2021",
        identifiers={"doi": "10.1000/concern"},
        citation_key="doe2021concern",
        retraction={"status": "concern", "provider": "openalex"},
        open_access={
            "is_oa": True, "status": "gold",
            "url": "https://example.test/open-access.pdf",
        },
        license="cc-by",
    )
    ref3 = _reference(
        graph, title="Duplicate alpha", identifiers={"doi": "10.1000/dup"},
        citation_key="dupa2022")
    ref4 = _reference(
        graph, title="Duplicate beta", identifiers={"doi": "10.1000/dup"},
        citation_key="dupb2022")
    ref5 = _reference(
        graph, title="Preprint version", identifiers={"arxiv": "2001.00001"},
        citation_key="pre2020", reference_type="preprint")
    ref6 = _reference(
        graph, title="Published version", identifiers={"doi": "10.1000/published"},
        citation_key="pub2021")
    graph.link(ref5["id"], "same_as", ref6["id"])
    ref7 = _reference(graph, title="No identifiers", citation_key="noid2024")
    ref8 = graph.record("Reference", title="", raw="", reference_type="misc")
    ref9 = _reference(graph, title="Another unenriched record",
                      citation_key="noid2025")

    digest = "a" * 64
    relative = f"objects/aa/{digest}.pdf"
    cache_file = state_path(tmp_path, "research", "cache", relative)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"%PDF-1.4")
    source = graph.record(
        "Source", name="smith2020retracted.pdf", reference_id=ref1["id"],
        locator={
            "kind": "cached_fetch", "reference_id": ref1["id"],
            "sha256": digest, "cache_relative": relative,
            "url": "https://example.test/paper.pdf", "provider": "unpaywall",
            "media_type": "application/pdf", "license": "cc-by",
            "version": "published", "retrieved_at": "2026-01-01T00:00:00Z",
            "size": 8, "external": False, "unofficial": False,
        })
    graph.link(source["id"], "manifestation_of", ref1["id"])
    missing_digest = "c" * 64
    missing_relative = f"objects/cc/{missing_digest}.pdf"
    missing_source = graph.record(
        "Source", name="missing.pdf", reference_id=ref7["id"],
        locator={
            "kind": "cached_fetch", "reference_id": ref7["id"],
            "sha256": missing_digest, "cache_relative": missing_relative,
            "url": "https://example.test/missing.pdf", "provider": "unpaywall",
            "media_type": "application/pdf", "license": "cc-by",
            "version": "published", "retrieved_at": "2026-01-01T00:00:00Z",
            "size": 100, "external": False, "unofficial": False,
        })
    graph.link(missing_source["id"], "manifestation_of", ref7["id"])
    local = graph.record(
        "Source", name="local.tex", reference_id=ref2["id"],
        locator={
            "kind": "local_file", "root_id": "papers", "path": "paper.tex",
            "sha256": "b" * 64,
        })
    graph.link(local["id"], "manifestation_of", ref2["id"])

    claim1 = graph.record(
        "Claim", wording="X causes Y.", claim_type="empirical_observation",
        status="supported_within_scope", scope="lab", author="A",
        citations=["smith2020retracted", "missing2024"],
        source_locator={"path": "paper.tex", "start_line": 10, "end_line": 12})
    claim2 = graph.record(
        "Claim", wording="X has no effect.", claim_type="hypothesis",
        status="unresolved", scope="lab", author="B")
    evidence = graph.record(
        "Evidence", wording="Observed X and Y.", evidence_type="observation",
        source_locator={"path": "paper.tex", "start_line": 20, "end_line": 22})
    orphan = graph.record(
        "Evidence", wording="Unlinked note.",
        source_locator={"path": "notes.md", "start_line": 5})
    graph.link(claim1["id"], "supports", evidence["id"])
    graph.link(claim1["id"], "cites", ref1["id"])

    def decision(reference, stage, value, reason, reviewer):
        node = graph.record(
            "Decision", screening=True, reference_id=reference["id"],
            stage=stage, decision=value, reason=reason, reviewer=reviewer,
            criteria=["topic"], truth_assessed=False)
        graph.link(node["id"], "screens", reference["id"])
        return node

    d1 = decision(ref1, "title_abstract", "include", "Relevant", "reviewer-a")
    d2 = decision(ref2, "title_abstract", "exclude", "Off topic", "reviewer-b")
    d3 = decision(ref1, "title_abstract", "maybe", "Needs full text",
                  "reviewer-c")
    graph.link(d3["id"], "supersedes", d1["id"])
    d4 = decision(ref1, "full_text", "include", "Methods clear", "reviewer-a")
    d5 = decision(ref2, "full_text", "exclude", "No data", "reviewer-b")

    plan1 = graph.record(
        "ReviewPlan", question="Does X cause Y?", kind="rapid",
        roles=["methodologist", "domain"], models=["m1", "m2"],
        rounds=["r1"], blinding=True, min_reviewers=2,
        adjudication="consensus", status="open", truth_assessed=False)
    graph.link(plan1["id"], "addresses", claim1["id"])
    run1 = graph.record(
        "ReviewRun", plan_id=plan1["id"], reviewer_id="r1",
        role="methodologist", model="m1", round="r1",
        packet_sha256="packet-1", status="completed",
        summary="Method concerns", recommendation="Revise", confidence=0.6,
        finding_count=1, target_ids=[claim1["id"]], truth_assessed=False)
    graph.link(run1["id"], "implements", plan1["id"])
    graph.link(run1["id"], "reviews", claim1["id"])
    f1 = graph.record(
        "ReviewFinding", plan_id=plan1["id"], run_id=run1["id"],
        wording="Missing control.", severity="major", issue_type="method",
        quote="we did X", recommendation="Add control", confidence=0.7,
        truth_assessed=False)
    graph.link(run1["id"], "finds", f1["id"])
    graph.link(f1["id"], "about", claim1["id"])
    run2 = graph.record(
        "ReviewRun", plan_id=plan1["id"], reviewer_id="r2", role="domain",
        model="m2", round="r1", packet_sha256="packet-2", status="completed",
        summary="Sampling concerns", recommendation="Clarify", confidence=0.5,
        finding_count=2, target_ids=[claim1["id"]], truth_assessed=False)
    graph.link(run2["id"], "implements", plan1["id"])
    graph.link(run2["id"], "reviews", claim1["id"])
    f2 = graph.record(
        "ReviewFinding", plan_id=plan1["id"], run_id=run2["id"],
        wording="Different sample.", severity="minor", issue_type="sampling",
        confidence=0.4, truth_assessed=False)
    f3 = graph.record(
        "ReviewFinding", plan_id=plan1["id"], run_id=run2["id"],
        wording="Missing control.", severity="major", issue_type="method",
        confidence=0.6, truth_assessed=False)
    graph.link(run2["id"], "finds", f2["id"])
    graph.link(run2["id"], "finds", f3["id"])
    graph.link(f2["id"], "about", claim1["id"])
    graph.link(f3["id"], "about", claim1["id"])
    reconciliation = graph.record(
        "Reconciliation", plan_id=plan1["id"], method="consensus",
        min_reviewers=2, run_ids=[run1["id"], run2["id"]],
        metrics={"fleiss_kappa": 0.5, "mean_pairwise_jaccard": 0.5},
        clusters=[{"wording": "Missing control."}],
        consensus=[{
            "wording": "Missing control.",
            "finding_ids": [f1["id"], f3["id"]],
            "reviewer_count": 2, "consensus": True, "dissent": False,
        }],
        dissent=[{
            "wording": "Different sample.",
            "finding_ids": [f2["id"]],
            "reviewer_count": 1, "consensus": False, "dissent": True,
        }],
        status="recorded", truth_assessed=False)
    graph.link(reconciliation["id"], "reconciles", run1["id"])
    graph.link(reconciliation["id"], "reconciles", run2["id"])
    plan2 = graph.record(
        "ReviewPlan", question="Second plan", kind="narrative",
        roles=["reviewer"], models=["m3"], rounds=["r1"], blinding=False,
        min_reviewers=1, adjudication="single", status="open")

    graph.link(ref1["id"], "cites_work", ref2["id"], provenance={
        "provider": "openalex", "retrieved_at": "2026-01-01"})
    graph.link(ref5["id"], "cites_work", ref6["id"])

    return {
        "graph": graph,
        "root": tmp_path,
        "ids": {
            "ref1": ref1["id"], "ref2": ref2["id"], "ref3": ref3["id"],
            "ref4": ref4["id"], "ref5": ref5["id"], "ref6": ref6["id"],
            "ref7": ref7["id"], "ref8": ref8["id"], "ref9": ref9["id"],
            "source": source["id"], "local": local["id"],
            "missing_source": missing_source["id"],
            "claim1": claim1["id"], "claim2": claim2["id"],
            "evidence": evidence["id"], "orphan": orphan["id"],
            "d1": d1["id"], "d2": d2["id"], "d3": d3["id"],
            "d4": d4["id"], "d5": d5["id"],
            "plan1": plan1["id"], "plan2": plan2["id"],
            "run1": run1["id"], "run2": run2["id"],
            "f1": f1["id"], "f2": f2["id"], "f3": f3["id"],
            "reconciliation": reconciliation["id"],
        },
    }


def test_library_view_reports_identity_versions_retractions_and_gaps(research):
    model = build_research_view(
        research["graph"].state(), "library", root=research["root"])
    ids = research["ids"]
    assert model["schema"] == SCHEMA_VERSION
    assert model["view"] == "library"
    assert model["investigation_id"] == "research-ui-test"
    assert model["truth_assessed"] is False
    assert model["not_assessed"]
    assert model["provenance"]["derived"] is True
    assert model["provenance"]["graph_schema"] == GRAPH_SCHEMA_VERSION

    rows = {row["reference_id"]: row for row in model["rows"]}
    assert rows[ids["ref1"]]["retraction_status"] == "retracted"
    assert rows[ids["ref2"]]["retraction_status"] == "concern"
    assert rows[ids["ref2"]]["identity"] == "doi:10.1000/concern"
    assert rows[ids["ref5"]]["versions"][0]["reference_id"] == ids["ref6"]
    assert rows[ids["ref6"]]["versions"][0]["reference_id"] == ids["ref5"]
    assert rows[ids["ref8"]]["identity"] is None
    assert "title" in rows[ids["ref8"]]["missing"]
    assert "identity" in rows[ids["ref8"]]["missing"]
    assert rows[ids["ref1"]]["source_ids"] == [ids["source"]]

    duplicates = {
        (item["kind"], item["value"]): item for item in model["duplicates"]}
    duplicate = duplicates[("identity", "doi:10.1000/dup")]
    assert duplicate["reference_ids"] == [ids["ref3"], ids["ref4"]]
    assert ids["ref4"] in rows[ids["ref3"]]["duplicate_of"]
    assert {item["citation_key"] for item in model["unresolved_keys"]} == {
        "missing2024"}
    assert model["summary"]["references"] == 9
    assert model["summary"]["retracted"] == 1
    assert model["summary"]["concern"] == 1
    assert model["summary"]["duplicate_groups"] == 1
    assert model["summary"]["unresolved_keys"] == 1


def test_fetch_view_projects_cached_artifacts_without_network(research):
    model = build_research_view(
        research["graph"].state(), "fetch", root=research["root"])
    ids = research["ids"]
    rows = model["rows"]
    artifact = next(
        row for row in rows
        if row["row_kind"] == "artifact" and row["source_id"] == ids["source"])
    assert artifact["provider"] == "unpaywall"
    assert artifact["license"] == "cc-by"
    assert artifact["sha256"] == "a" * 64
    assert artifact["cache_present"] is True
    assert artifact["status"] == "cached"
    assert Path(artifact["cache_path"]).is_file()

    candidate = next(
        row for row in rows
        if row["row_kind"] == "candidate"
        and row["reference_id"] == ids["ref2"])
    assert candidate["provider"] == "recorded_open_access"
    assert candidate["status"] == "metadata_candidate"
    assert candidate["url"] == "https://example.test/open-access.pdf"

    local = next(
        row for row in rows
        if row["row_kind"] == "source" and row["source_id"] == ids["local"])
    assert local["status"] == "recorded_source"

    missing = next(
        row for row in rows
        if row["row_kind"] == "artifact"
        and row["source_id"] == ids["missing_source"])
    assert missing["status"] == "missing_cache"
    assert missing["cache_present"] is False

    unresolved = next(
        row for row in rows
        if row["row_kind"] == "unresolved_reference"
        and row["reference_id"] == ids["ref9"])
    assert unresolved["status"] == "no_identifier"
    assert model["summary"]["artifacts"] == 2
    assert model["summary"]["cached_artifacts"] == 1
    assert model["summary"]["missing_artifacts"] == 1
    assert model["summary"]["candidates"] == 1
    assert model["summary"]["unresolved_references"] >= 1


def test_screening_view_prisma_counts_and_supersession(research):
    model = build_research_view(
        research["graph"].state(), "screening", root=research["root"])
    ids = research["ids"]
    rows = {row["decision_id"]: row for row in model["rows"]}
    assert rows[ids["d1"]]["latest_for_stage"] is False
    assert rows[ids["d3"]]["latest_for_stage"] is True
    assert rows[ids["d1"]]["superseded_by"] == [ids["d3"]]
    assert rows[ids["d3"]]["supersedes"] == [ids["d1"]]
    assert rows[ids["d4"]]["stage"] == "full_text"
    assert rows[ids["d5"]]["decision"] == "exclude"

    counts = model["summary"]
    assert counts["records_identified"] == 9
    assert counts["title_abstract_screened"] == 2
    assert counts["title_abstract_included"] == 0
    assert counts["title_abstract_excluded"] == 1
    assert counts["title_abstract_maybe"] == 1
    assert counts["full_text_assessed"] == 2
    assert counts["full_text_included"] == 1
    assert counts["full_text_excluded"] == 1
    assert counts["included"] == 1
    assert counts["excluded"] == 1
    assert counts["unresolved"] == 7
    assert model["prisma"]["counts"]["records_identified"] == 9
    assert ids["ref1"] in model["prisma"]["included_references"]
    assert ids["ref2"] in model["prisma"]["excluded_references"]


def test_evidence_view_links_claims_to_locators(research):
    model = build_research_view(
        research["graph"].state(), "evidence", root=research["root"])
    ids = research["ids"]
    rows = model["rows"]
    supports = next(
        row for row in rows
        if row["claim_id"] == ids["claim1"] and row["relation"] == "supports")
    assert supports["evidence_id"] == ids["evidence"]
    assert supports["locator_label"] == "paper.tex:20-22"
    assert ids["ref1"] in supports["reference_ids"]
    assert "smith2020retracted" in supports["citation_keys"]

    missing = next(row for row in rows if row["claim_id"] == ids["claim2"])
    assert missing["relation"] == "none"
    assert missing["status"] == "no_recorded_evidence"

    unlinked = next(
        row for row in rows
        if row["evidence_id"] == ids["orphan"]
        and row["status"] == "unlinked_evidence")
    assert unlinked["claim_id"] is None
    assert unlinked["locator_label"] == "notes.md:5"

    assert model["summary"]["claims"] == 2
    assert model["summary"]["claims_without_evidence"] == 1
    assert model["summary"]["unlinked_evidence"] == 1
    assert model["summary"]["support_edges"] == 1
    assert model["graph"]["nodes"]


def test_consensus_view_preserves_consensus_and_dissent(research):
    model = build_research_view(
        research["graph"].state(), "consensus", root=research["root"])
    assert len(model["consensus"]) == 1
    assert model["consensus"][0]["wording"] == "Missing control."
    assert len(model["dissent"]) == 1
    assert model["dissent"][0]["wording"] == "Different sample."
    assert model["dissent"][0]["dissent"] is True

    kinds = [row["row_kind"] for row in model["rows"]]
    assert kinds.count("plan") == 2
    assert kinds.count("run") == 2
    assert kinds.count("finding") == 3
    assert kinds.count("reconciliation") == 1
    assert model["summary"]["findings"] == 3
    assert model["summary"]["consensus_items"] == 1
    assert model["summary"]["dissent_items"] == 1
    assert model["summary"]["plans_without_reconciliation"] == 1
    assert model["graph"]["nodes"]


def test_citation_view_reports_edges_retractions_and_neighbourhoods(research):
    model = build_research_view(
        research["graph"].state(), "citation", root=research["root"])
    ids = research["ids"]
    edge_types = {row["edge_type"] for row in model["rows"]}
    assert {"cites", "cites_work", "manifestation_of", "same_as"} <= edge_types
    assert model["summary"]["cites_work_edges"] == 2
    assert model["summary"]["retracted"] == 1
    assert model["summary"]["concern"] == 1

    retractions = {
        row["reference_id"]: row for row in model["retractions"]}
    assert retractions[ids["ref1"]]["retraction_status"] == "retracted"
    assert retractions[ids["ref1"]]["notices"] == ["Retraction notice"]
    assert retractions[ids["ref2"]]["retraction_status"] == "concern"

    neighbourhoods = {
        row["reference_id"]: row for row in model["neighbourhoods"]}
    assert ids["ref2"] in neighbourhoods[ids["ref1"]]["outgoing"]
    assert ids["ref1"] in neighbourhoods[ids["ref2"]]["incoming"]
    assert ids["ref6"] in neighbourhoods[ids["ref5"]]["outgoing"]
    assert neighbourhoods[ids["ref7"]]["status"] == "isolated"
    assert model["graph"]["nodes"]


@pytest.mark.parametrize("view", VIEWS)
def test_empty_graph_has_a_complete_shape(tmp_path: Path, view: str):
    graph = ResearchGraph.create(
        "Empty investigation", root=tmp_path,
        investigation_id=f"empty-{view}", mode="exploratory")
    model = build_research_view(graph.state(), view, root=tmp_path)
    assert model["schema"] == SCHEMA_VERSION
    assert model["view"] == view
    assert model["investigation_id"] == f"empty-{view}"
    assert model["rows"] == []
    assert model["summary"]["row_count"] == 0
    assert model["truth_assessed"] is False
    assert model["not_assessed"]
    assert model["omitted"]["rows"] == 0
    if view in {"evidence", "consensus", "citation"}:
        assert model["graph"]["edges"] == []
        # The investigation question is the only recorded node in an empty
        # investigation; the citation aperture scopes it out.
        assert all(node["raw_type"] == "Question"
                   for node in model["graph"]["nodes"])


def test_missing_investigation_and_invalid_graph_exit_2(tmp_path: Path,
                                                        capsys):
    with pytest.raises(ResearchUIError):
        load_state("missing", root=tmp_path)
    code = research_ui_cli.main([
        "--root", str(tmp_path), "--investigation", "missing",
        "--view", "library",
    ])
    assert code == 2
    assert "not found" in capsys.readouterr().err

    bad_root = tmp_path / "bad"
    bad_path = state_path(bad_root, "research", "bad.json")
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text("{not json", encoding="utf-8")
    code = research_ui_cli.main([
        "--root", str(bad_root), "--investigation", "bad",
        "--view", "library",
    ])
    assert code == 2
    assert "error:" in capsys.readouterr().err

    list_path = state_path(bad_root, "research", "list.json")
    list_path.write_text("[]", encoding="utf-8")
    code = research_ui_cli.main([
        "--root", str(bad_root), "--investigation", "list",
        "--view", "library",
    ])
    assert code == 2
    assert "invalid research graph" in capsys.readouterr().err


@pytest.mark.parametrize("view", VIEWS)
def test_json_is_deterministic_and_order_independent(research, view: str):
    state = research["graph"].state()
    reversed_state = {
        **state,
        "nodes": list(reversed(state["nodes"])),
        "edges": list(reversed(state["edges"])),
    }
    first = build_research_view(state, view, root=research["root"])
    second = build_research_view(
        reversed_state, view, root=research["root"])
    assert render_json(first) == render_json(second)
    assert render_json(first) == render_json(
        build_research_view(state, view, root=research["root"]))


def test_html_escapes_recorded_text(research):
    graph = research["graph"]
    graph.amend(
        research["ids"]["ref1"],
        title='<script>alert("x")</script>')
    model = build_research_view(graph.state(), "library", root=research["root"])
    document = render_html(model)
    assert "<script" not in document.lower()
    assert "&lt;script&gt;" in document
    assert "&lt;/script&gt;" in document
    escaped_title = render_html(model, title="<b>Title</b>")
    assert "&lt;b&gt;Title&lt;/b&gt;" in escaped_title


def test_cli_json_html_and_out(research, tmp_path: Path, capsys):
    graph_path = research["graph"].path
    before = graph_path.read_bytes()
    code = research_ui_cli.main([
        "--root", str(research["root"]),
        "--investigation", "research-ui-test",
        "--view", "library", "--format", "json",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["view"] == "library"
    assert payload["investigation_id"] == "research-ui-test"

    out = tmp_path / "consensus.html"
    code = research_ui_cli.main([
        "--root", str(research["root"]),
        "--investigation", "research-ui-test",
        "--view", "consensus", "--format", "html", "--out", str(out),
    ])
    assert code == 0
    assert capsys.readouterr().out == ""
    document = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in document
    assert "Review consensus" in document
    assert graph_path.read_bytes() == before
