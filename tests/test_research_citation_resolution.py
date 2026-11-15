"""Citation hint resolution and PDF claim citation-link tests."""

from __future__ import annotations

import pytest

from ozagia.research import citation_resolution as resolution
from ozagia.research.citation_resolution import CitationResolutionError
from ozagia.research.graph import ResearchError, ResearchGraph


def _graph(tmp_path):
    return ResearchGraph.create(
        "Resolve citations",
        root=tmp_path,
        investigation_id="citation-resolution-test",
    )


def _reference(
    graph,
    key,
    *,
    title=None,
    authors=(),
    year="2020",
    doi=None,
    arxiv=None,
    index=None,
    source_locator=None,
):
    identifiers = {}
    if doi is not None:
        identifiers["doi"] = doi
    if arxiv is not None:
        identifiers["arxiv"] = arxiv
    fields = {
        "title": title or key,
        "authors": list(authors),
        "year": year,
        "citation_key": key,
        "citation_keys": [key],
        "identifiers": identifiers,
    }
    if index is not None:
        fields["bibliography_index"] = index
    if source_locator is not None:
        fields["source_locators"] = [source_locator]
    return graph.record("Reference", **fields)


def _claim(graph, **fields):
    base = {
        "wording": "A test claim.",
        "status": "unresolved",
        "claim_type": "empirical_observation",
        "scope": "test",
        "author": "tester",
    }
    base.update(fields)
    return graph.record("Claim", **base)


def test_numeric_ranges_expand_to_bibliography_indices(tmp_path):
    graph = _graph(tmp_path)
    references = [_reference(graph, f"ref{number}", index=number) for number in range(1, 8)]

    matches = resolution.resolve_text_citations(
        "See [1,3-5] and [2] and [6\u20137].", references)

    assert [match["hint"] for match in matches] == [
        "[1,3-5]", "[1,3-5]", "[1,3-5]", "[1,3-5]", "[2]", "[6\u20137]", "[6\u20137]",
    ]
    assert [match["reference_id"] for match in matches] == [
        references[0]["id"],
        references[2]["id"],
        references[3]["id"],
        references[4]["id"],
        references[1]["id"],
        references[5]["id"],
        references[6]["id"],
    ]
    assert all(match["method"] == "numeric" for match in matches)
    assert all(match["matched_by"] == "bibliography_index" for match in matches)
    assert all(match["confidence"] == pytest.approx(0.7) for match in matches)
    assert all(match["requires_review"] is True for match in matches)


def test_doi_and_arxiv_literals_match_exact_identifiers(tmp_path):
    graph = _graph(tmp_path)
    doi_reference = _reference(graph, "doi2024", doi="10.1234/ABC.1")
    arxiv_reference = _reference(graph, "arxiv2021", arxiv="2101.00001")

    matches = resolution.resolve_text_citations(
        "Data: https://doi.org/10.1234/abc.1 and arXiv:2101.00001v2.",
        [doi_reference, arxiv_reference],
    )

    assert [match["method"] for match in matches] == ["doi", "arxiv"]
    assert [match["hint"] for match in matches] == [
        "10.1234/abc.1", "arXiv:2101.00001v2"]
    assert [match["reference_id"] for match in matches] == [
        doi_reference["id"], arxiv_reference["id"]]
    assert [match["matched_by"] for match in matches] == ["doi", "arxiv"]
    assert all(match["confidence"] == pytest.approx(0.95) for match in matches)
    assert all(match["requires_review"] is False for match in matches)

    bare = resolution.resolve_text_citations(
        "Preprint 2101.00001 is relevant.", [doi_reference, arxiv_reference])
    assert len(bare) == 1
    assert bare[0]["hint"] == "2101.00001"
    assert bare[0]["reference_id"] == arxiv_reference["id"]


def test_author_year_matches_unique_and_marks_ambiguity(tmp_path):
    graph = _graph(tmp_path)
    smith = _reference(
        graph, "smith2020", authors=["Smith, Jane"], year="2020")
    smith_doe = _reference(
        graph, "smithdoe2021",
        authors=["Smith, Jane", "Doe, John"], year="2021")

    unique = resolution.resolve_text_citations(
        "(Smith et al., 2020) and Smith and Doe (2021).",
        [smith, smith_doe],
    )

    assert [match["hint"] for match in unique] == [
        "(Smith et al., 2020)", "Smith and Doe (2021)"]
    assert [match["reference_id"] for match in unique] == [
        smith["id"], smith_doe["id"]]
    assert all(match["method"] == "author_year" for match in unique)
    assert all(match["confidence"] == pytest.approx(0.6) for match in unique)
    assert all(match["requires_review"] is True for match in unique)

    narrative = resolution.resolve_text_citations(
        "Smith, 2020 and Smith et al. (2020).", [smith])
    assert [match["hint"] for match in narrative] == [
        "Smith, 2020", "Smith et al. (2020)"]
    assert all(match["reference_id"] == smith["id"] for match in narrative)

    other_smith = _reference(
        graph, "smith2020b", authors=["Smith, John"], year="2020")
    ambiguous = resolution.resolve_text_citations(
        "(Smith, 2020)", [smith, other_smith])

    expected_ids = sorted([smith["id"], other_smith["id"]])
    assert len(ambiguous) == 2
    assert [match["reference_id"] for match in ambiguous] == expected_ids
    assert all(match["confidence"] == pytest.approx(0.35) for match in ambiguous)
    assert all(match["requires_review"] is True for match in ambiguous)
    assert all(match["ambiguous_refs"] == expected_ids for match in ambiguous)


def test_unmatched_hints_produce_no_candidates(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(
        graph, "known", authors=["Smith, Jane"], year="2020",
        doi="10.1234/known", index=1)

    assert resolution.resolve_text_citations(
        "See [99], (Jones, 1999), and 10.9999/missing.", [reference]) == []


def test_apply_false_reads_only_and_keeps_candidates_below_threshold(tmp_path):
    graph = _graph(tmp_path)
    _reference(graph, "numeric1", index=1)
    claim = _claim(graph, citation_hints=["[1]"])
    before = graph.state()

    result = resolution.resolve_claim_citations(graph, claim["id"], apply=False)
    after = graph.state()

    assert after == before
    assert result["claim_id"] == claim["id"]
    assert result["applied"] is False
    assert result["edges"] == []
    assert len(result["resolved"]) == 1
    assert result["matches"] == result["resolved"]
    assert result["candidates"] == []
    assert result["unresolved_hints"] == []
    assert result["threshold"] == pytest.approx(0.6)
    assert result["truth_assessed"] is False


def test_apply_creates_provenanced_cites_edges_without_support(tmp_path):
    graph = _graph(tmp_path)
    numeric = _reference(graph, "numeric1", index=1)
    arxiv = _reference(graph, "arxiv2021", arxiv="2101.00001")
    author = _reference(
        graph, "smith2020", authors=["Smith, Jane"], year="2020")
    claim = _claim(
        graph,
        raw_excerpt="See [1], arXiv:2101.00001 and (Smith et al., 2020).",
    )

    result = resolution.resolve_claim_citations(graph, claim["id"])

    assert result["applied"] is True
    assert len(result["resolved"]) == 3
    assert result["candidates"] == []
    assert result["unresolved_hints"] == []
    assert len(result["edges"]) == 3

    state = graph.state()
    edges = [edge for edge in state["edges"] if edge["type"] == "cites"]
    assert len(edges) == 3
    by_target = {edge["tgt"]: edge for edge in edges}
    assert set(by_target) == {numeric["id"], arxiv["id"], author["id"]}
    for match in result["resolved"]:
        edge = by_target[match["reference_id"]]
        assert edge["src"] == claim["id"]
        assert edge["provenance"] == {
            "kind": "pdf_citation_resolution",
            "hint": match["hint"],
            "method": match["method"],
            "confidence": match["confidence"],
            "requires_review": match["requires_review"],
            "threshold": pytest.approx(0.6),
        }
    assert not any(edge["type"] in {"supports", "opposes"} for edge in state["edges"])
    assert result["truth_assessed"] is False


def test_threshold_keeps_lower_confidence_matches_as_candidates(tmp_path):
    graph = _graph(tmp_path)
    _reference(graph, "numeric1", index=1)
    claim = _claim(graph, citation_hints=["[1]"])

    result = resolution.resolve_claim_citations(
        graph, claim["id"], apply=True, threshold=0.8)

    assert result["resolved"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["confidence"] == pytest.approx(0.7)
    assert result["unresolved_hints"] == ["[1]"]
    assert result["applied"] is False
    assert result["edges"] == []
    assert graph.state()["edges"] == []


def test_duplicate_apply_reuses_identical_edges(tmp_path):
    graph = _graph(tmp_path)
    _reference(graph, "numeric1", index=1)
    claim = _claim(graph, citation_hints=["[1]"])

    first = resolution.resolve_claim_citations(graph, claim["id"])
    edge_id = first["edges"][0]["id"]
    events_after_first = len(graph.state()["events"])

    second = resolution.resolve_claim_citations(graph, claim["id"])

    assert [edge["id"] for edge in second["edges"]] == [edge_id]
    assert second["edges"] == first["edges"]
    assert second["applied"] is True
    assert len(graph.state()["edges"]) == 1
    assert len(graph.state()["events"]) == events_after_first


def test_claim_citation_keys_use_exact_key_matching(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(
        graph, "Smith2020", authors=["Smith, Jane"], year="2020")
    claim = _claim(graph, citation_keys=["Smith2020"])

    result = resolution.resolve_claim_citations(
        graph, claim["id"], apply=False)

    assert len(result["resolved"]) == 1
    match = result["resolved"][0]
    assert match["reference_id"] == reference["id"]
    assert match["method"] == "citation_key"
    assert match["confidence"] == pytest.approx(0.9)
    assert match["requires_review"] is False


def test_pdf_linked_references_are_preferred_over_unrelated_ones(tmp_path):
    graph = _graph(tmp_path)
    locator = {
        "kind": "pdf",
        "root_id": "papers",
        "path": "paper.pdf",
        "sha256": "abc123",
    }
    source = graph.record(
        "Source", name="paper.pdf", paper_import=True, locator=locator)
    linked = _reference(graph, "linked", index=1, source_locator=locator)
    unlinked = _reference(graph, "unlinked", index=2)
    claim = _claim(
        graph,
        citation_hints=["[1]", "[2]"],
        source_locator={"kind": "pdf", "sha256": "abc123"},
    )
    graph.link(
        claim["id"], "cites", source["id"],
        provenance={"kind": "pdf_claim_extraction"},
    )

    result = resolution.resolve_claim_citations(
        graph, claim["id"], apply=False)

    assert [match["reference_id"] for match in result["resolved"]] == [
        linked["id"]]
    assert result["unresolved_hints"] == ["[2]"]
    assert unlinked["id"] not in {
        match["reference_id"] for match in result["matches"]}


def test_report_counts_resolved_unresolved_and_ambiguous_matches(tmp_path):
    graph = _graph(tmp_path)
    _reference(graph, "numeric1", index=1)
    _reference(graph, "arxiv2021", arxiv="2101.00001")
    _reference(graph, "smith2021", authors=["Smith, Jane"], year="2021")
    smith_a = _reference(graph, "smith2020a", authors=["Smith, Jane"], year="2020")
    smith_b = _reference(graph, "smith2020b", authors=["Smith, John"], year="2020")

    resolved_claim = _claim(
        graph,
        raw_excerpt=(
            "See [1], arXiv:2101.00001, (Smith et al., 2021) and [99]."
        ),
    )
    ambiguous_claim = _claim(graph, citation_hints=["(Smith, 2020)"])

    applied = resolution.resolve_claim_citations(graph, resolved_claim["id"])
    assert len(applied["resolved"]) == 3

    report = resolution.citation_resolution_report(graph.state())

    assert report["claim_id"] is None
    assert report["claims"] == 2
    assert report["claims_with_citation_hints"] == 2
    assert report["claims_with_resolved_citations"] == 1
    assert report["resolved_edges"] == 3
    assert report["resolved_by_method"] == {
        "arxiv": 1, "author_year": 1, "numeric": 1}
    assert report["resolved_by_requires_review"] == {
        "requires_review": 2, "no_review": 1}
    assert report["resolved_requires_review"] == 2
    assert report["resolved_no_review"] == 1
    assert report["unresolved_count"] == 2
    assert {
        (item["claim_id"], item["hint"], item["method"])
        for item in report["unresolved_hints"]
    } == {
        (resolved_claim["id"], "[99]", "numeric"),
        (ambiguous_claim["id"], "(Smith, 2020)", "author_year"),
    }
    assert report["unresolved_by_method"] == {
        "author_year": 1, "numeric": 1}
    assert report["ambiguous_count"] == 1
    ambiguous = report["ambiguous_matches"][0]
    assert ambiguous["claim_id"] == ambiguous_claim["id"]
    assert ambiguous["hint"] == "(Smith, 2020)"
    assert ambiguous["reference_ids"] == sorted([smith_a["id"], smith_b["id"]])
    assert report["truth_assessed"] is False

    filtered = resolution.citation_resolution_report(
        graph.state(), resolved_claim["id"])
    assert filtered["claim_id"] == resolved_claim["id"]
    assert filtered["claims"] == 1
    assert filtered["resolved_edges"] == 3
    assert filtered["unresolved_count"] == 1
    assert filtered["ambiguous_count"] == 0


def test_resolution_and_report_are_deterministic(tmp_path):
    graph = _graph(tmp_path)
    _reference(graph, "numeric1", index=1)
    _reference(graph, "smith2020", authors=["Smith, Jane"], year="2020")
    claim = _claim(
        graph,
        citation_hints=["[1]"],
        raw_excerpt="See [1] and (Smith et al., 2020).",
    )

    text = "See [1] and (Smith et al., 2020)."
    references = [
        node for node in graph.state()["nodes"] if node["type"] == "Reference"
    ]
    assert resolution.resolve_text_citations(text, references) == (
        resolution.resolve_text_citations(text, references)
    )

    first = resolution.resolve_claim_citations(graph, claim["id"], apply=False)
    second = resolution.resolve_claim_citations(graph, claim["id"], apply=False)
    assert first == second

    report = resolution.citation_resolution_report(graph.state())
    assert report == resolution.citation_resolution_report(graph.state())


def test_claim_and_threshold_errors_are_typed(tmp_path):
    graph = _graph(tmp_path)
    reference = _reference(graph, "numeric1", index=1)
    claim = _claim(graph, citation_hints=["[1]"])

    assert issubclass(CitationResolutionError, ResearchError)
    with pytest.raises(CitationResolutionError, match="unknown claim"):
        resolution.resolve_claim_citations(graph, "claim-9999", apply=False)
    with pytest.raises(CitationResolutionError, match="not a Claim"):
        resolution.resolve_claim_citations(graph, reference["id"], apply=False)
    with pytest.raises(CitationResolutionError, match="threshold"):
        resolution.resolve_claim_citations(graph, claim["id"], threshold=1.5)
    with pytest.raises(CitationResolutionError, match="unknown claim"):
        resolution.citation_resolution_report(graph.state(), "claim-9999")
