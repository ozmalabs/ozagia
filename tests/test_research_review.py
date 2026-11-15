from __future__ import annotations

from ozagia.research import references as refs
from ozagia.research import review, search
from ozagia.research.graph import ResearchGraph
from ozagia.research.service import ResearchService


def _graph(tmp_path, files: dict[str, str], investigation_id: str = "review-test"):
    source_root = tmp_path / "sources"
    source_root.mkdir(exist_ok=True)
    for name, content in files.items():
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return ResearchGraph.create(
        "Review sources",
        root=tmp_path,
        investigation_id=investigation_id,
        roots=[{
            "root_id": "papers",
            "path": str(source_root),
            "files": {name: "selected" for name in files},
        }],
    )


def test_fts_search_returns_cited_snippets_and_filters(tmp_path):
    graph = _graph(tmp_path, {
        "a.md": "# Graph methods\nA graph traversal method with a citation.\n",
        "b.md": "# Other\nUnrelated prose.\n",
    })
    result = search.search(graph.state(), "graph traversal", db_path=tmp_path / "index.sqlite")
    assert result["engine"] in {"sqlite-fts5", "lexical"}
    assert result["results"]
    assert result["results"][0]["citation"]["path"] == "a.md"
    assert result["results"][0]["excerpt"]
    assert result["truth_assessed"] is False

    filtered = search.search(
        graph.state(), "graph", db_path=tmp_path / "index.sqlite",
        root_id="papers", path="b.md")
    assert filtered["results"] == []


def test_citation_contexts_extract_sentences_without_support_edges():
    tex = (
        "Background sentence. The method cites \\cite{smith2024, doe2023}. "
        "A later sentence.\n"
        "Another line \\citep[p. 2]{lee2022}.\n"
    )
    contexts = refs.citation_contexts(tex)
    assert contexts[0]["citation_keys"] == ["smith2024", "doe2023"]
    assert contexts[0]["quote"].startswith("The method cites")
    assert contexts[0]["relation_candidate"] == "unclassified"
    assert contexts[0]["truth_assessed"] is False
    assert contexts[1]["citation_keys"] == ["lee2022"]

    markdown = refs.citation_contexts("See [@doe2024; @smith2024].")
    assert markdown[0]["citation_keys"] == ["doe2024", "smith2024"]


def test_evidence_table_projects_claims_and_references(tmp_path):
    graph = _graph(tmp_path, {})
    reference = refs.record_reference(graph, {
        "title": "A Graph Paper",
        "citation_key": "smith2024",
        "identifiers": {"doi": "10.1234/abc.1"},
    })["reference"]
    claim = graph.record(
        "Claim",
        wording="The graph method converges.",
        claim_type="theorem",
        status="unresolved",
        citations=["smith2024"],
        source_locator={"path": "paper.tex", "start_line": 4, "end_line": 6},
    )
    graph.link(claim["id"], "cites", reference["id"])
    graph.record("Evidence", evidence_type="citation_context_candidate",
                 wording="A quoted context.", source_locator={"path": "paper.tex"})

    markdown = review.evidence_table(graph.state(), format="markdown")
    assert markdown["count"] == 2
    assert "The graph method converges." in markdown["content"]
    assert "smith2024" in markdown["content"]
    assert "paper.tex:4-6" in markdown["content"]

    csv_table = review.evidence_table(graph.state(), format="csv")
    assert csv_table["content"].startswith("record_id,kind,wording")
    assert "The graph method converges." in csv_table["content"]


def test_screening_decisions_and_prisma_counts(tmp_path):
    graph = _graph(tmp_path, {})
    first = refs.record_reference(graph, {
        "title": "Included Work", "citation_key": "inc2024"})["reference"]
    second = refs.record_reference(graph, {
        "title": "Excluded Work", "citation_key": "exc2023"})["reference"]

    review.screen_reference(
        graph, first["id"], stage="title_abstract", decision="include",
        reason="Relevant to the question", reviewer="reader")
    review.screen_reference(
        graph, second["id"], stage="title_abstract", decision="exclude",
        reason="Wrong population", reviewer="reader")
    report = review.screening_report(graph.state())
    assert report["counts"]["records_identified"] == 2
    assert report["counts"]["title_abstract_screened"] == 2
    assert report["counts"]["title_abstract_included"] == 1
    assert report["counts"]["title_abstract_excluded"] == 1
    assert report["included_references"] == [first["id"]]
    assert report["truth_assessed"] is False

    revised = review.screen_reference(
        graph, first["id"], stage="title_abstract", decision="exclude",
        reason="Superseded by full-text assessment", reviewer="reader")
    assert revised["superseded"]
    report = review.screening_report(graph.state())
    assert report["counts"]["title_abstract_included"] == 0
    assert report["counts"]["title_abstract_excluded"] == 2


def test_service_reference_import_contexts_and_tei(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "refs.bib").write_text(
        "@article{smith2024, title={A Graph Paper}, author={Smith, Jane}, "
        "year={2024}, doi={10.1234/abc.1}}", encoding="utf-8")
    (source_root / "paper.tex").write_text(
        "The method cites \\cite{smith2024}. Another sentence.\n", encoding="utf-8")
    (source_root / "paper.xml").write_text(
        "<TEI xmlns='http://www.tei-c.org/ns/1.0'><teiHeader><fileDesc><titleStmt>"
        "<title>TEI</title></titleStmt><sourceDesc><listBibl><biblStruct xml:id='b0'>"
        "<analytic><title>Cited</title></analytic></biblStruct></listBibl></sourceDesc>"
        "</fileDesc></teiHeader><text><body><div><head>Intro</head><p>Text.</p>"
        "</div></body></text></TEI>", encoding="utf-8")
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {
        "question": "Review", "roots": [{
            "root_id": "papers", "path": str(source_root),
            "files": ["refs.bib", "paper.tex", "paper.xml"]}],
    })["investigation_id"]

    preview = svc.call("ref_preview", {
        "session_id": sid, "root_id": "papers", "path": "refs.bib"})
    assert preview["count"] == 1
    imported = svc.call("ref_import", {
        "session_id": sid, "root_id": "papers", "path": "refs.bib"})
    assert imported["created"] == 1
    exported = svc.call("ref_export", {"session_id": sid, "format": "bibtex"})
    assert "@article{smith2024" in exported["content"]

    contexts = svc.call("cite_context", {
        "session_id": sid, "root_id": "papers", "path": "paper.tex", "record": True})
    assert contexts["contexts"][0]["citation_keys"] == ["smith2024"]
    assert contexts["recorded"]
    assert any(edge["type"] == "cites"
               and edge["src"] == contexts["recorded"][0]["id"]
               for edge in svc.call("inspect", {
                   "session_id": sid, "node_id": contexts["recorded"][0]["id"]})["edges"])

    parsed = svc.call("tei_preview", {
        "session_id": sid, "root_id": "papers", "path": "paper.xml",
        "record_references": True})
    assert parsed["references"][0]["title"] == "Cited"
    assert parsed["recorded_references"][0]["citation_key"] == "b0"

    table = svc.call("evidence_table", {"session_id": sid, "format": "csv"})
    assert table["count"] >= 1
