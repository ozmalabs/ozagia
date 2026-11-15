"""PDF claim extraction and ingestion tests using injected fake extractors."""

from __future__ import annotations

import hashlib

import pytest

from ozagia.research import pdf_claims
from ozagia.research.graph import ResearchError, ResearchGraph

PDF = b"%PDF-1.7\nfixture pdf bytes\n%%EOF\n"
SHA = hashlib.sha256(PDF).hexdigest()


def _graph(tmp_path, *, relpath="paper.pdf", content=PDF):
    source_root = tmp_path / "sources"
    source_root.mkdir(exist_ok=True)
    path = source_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ResearchGraph.create(
        "Review a PDF paper",
        root=tmp_path,
        investigation_id="pdf-claims-test",
        roots=[
            {
                "root_id": "papers",
                "path": str(source_root),
                "repository": "fixture",
                "files": {relpath: "selected"},
            }
        ],
    ), content


def _tei_extraction(*, reference_key="b0"):
    return {
        "format": "pdf",
        "sha256": SHA,
        "backend": "grobid",
        "pages": [],
        "title": "Widgets and Reliability",
        "authors": ["Ada Author", "Ben Builder"],
        "abstract": "We show that widgets increase reliability [12]. We propose a reusable widget method.",
        "sections": [
            {
                "heading": "1 Introduction",
                "text": "We present a study of widgets.",
                "xml_id": "s1",
            },
            {
                "heading": "2 Theorem 1 (Main result)",
                "text": "For every widget x, P(x).",
                "xml_id": "s2",
            },
            {
                "heading": "3 Definition 2.1",
                "text": "Widgets are central. We define a widget as a device that stores data.",
                "xml_id": "s3",
            },
            {
                "heading": "4 References",
                "text": "[1] A. Smith. A cited work. Journal, 2020.",
                "xml_id": "s4",
            },
            {
                "heading": "Acknowledgements",
                "text": "Thanks to the widget laboratory.",
                "xml_id": "s5",
            },
        ],
        "references": [
            {
                "title": "A cited work",
                "authors": ["A. Smith"],
                "year": "2020",
                "citation_key": reference_key,
                "citation_keys": [reference_key],
                "raw": "A. Smith. A cited work. Journal, 2020.",
                "metadata_status": "parsed_tei_unverified",
            }
        ],
        "warnings": ["GROBID TEI has no page numbers; use section ids or citation quotes as locators."],
        "degraded_math": True,
        "truth_assessed": False,
    }


def _page_extraction():
    page_text = (
        "Abstract\n"
        "We find that widgets increase reliability [4].\n"
        "\n"
        "Theorem 1. For every widget x, P(x).\n"
        "\n"
        "We propose a reusable widget method.\n"
        "\n"
        "Definition. A widget is defined as a device that stores data.\n"
        "\n"
        "References\n"
        "[1] A. Smith. A cited work. Journal, 2020.\n"
    )
    return {
        "format": "pdf",
        "sha256": SHA,
        "backend": "pdftotext",
        "pages": [
            {"number": 1, "text": page_text},
            {"number": 2, "text": "[2] B. Jones. Another cited work. Journal, 2021."},
        ],
        "sections": [],
        "title": "",
        "authors": [],
        "abstract": "",
        "references": [],
        "warnings": ["pdftotext -layout returns plain text; column order, tables and equations are degraded."],
        "degraded_math": True,
        "truth_assessed": False,
    }


def test_tei_sections_yield_high_confidence_formal_and_abstract_candidates():
    result = pdf_claims.candidate_claims(_tei_extraction())

    assert result["truth_assessed"] is False
    assert result["truncated"] is False
    assert result["count"] == len(result["claims"])
    assert result["methods"] == {
        "section_formal_statement": 2,
        "abstract_claim": 1,
        "contribution_claim": 2,
        "definition": 1,
    }
    formal = [
        claim for claim in result["claims"]
        if claim["extraction_method"] == "section_formal_statement"
    ]
    assert [claim["label"] for claim in formal] == ["Theorem 1", "Definition 2.1"]
    assert [claim["claim_type"] for claim in formal] == ["theorem", "definition"]
    assert all(claim["confidence"] >= 0.8 for claim in formal)
    assert formal[0]["section"] == "2 Theorem 1 (Main result)"
    assert formal[0]["source_locator"] == {
        "kind": "pdf",
        "sha256": SHA,
        "backend": "grobid",
        "page": None,
        "section": "2 Theorem 1 (Main result)",
        "xml_id": "s2",
        "start_offset": 0,
        "end_offset": len("For every widget x, P(x)."),
    }
    abstract = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "abstract_claim"
    )
    assert abstract["claim_type"] == "empirical_observation"
    assert abstract["citation_hints"] == ["[12]"]
    assert abstract["section"] == "Abstract"
    assert all(claim["semantics_verified"] is False for claim in result["claims"])
    assert all(claim["truth_assessed"] is False for claim in result["claims"])
    assert all(claim["math_degraded"] is True for claim in result["claims"])
    assert not any("cited work" in claim["wording"].casefold() for claim in result["claims"])
    assert not any("widget laboratory" in claim["wording"] for claim in result["claims"])


def test_page_only_text_yields_inline_abstract_contribution_and_definition_candidates():
    result = pdf_claims.candidate_claims(_page_extraction())

    assert result["methods"] == {
        "inline_formal_statement": 1,
        "abstract_claim": 1,
        "contribution_claim": 1,
        "definition": 1,
    }
    assert all(claim["page"] == 1 for claim in result["claims"])
    inline = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "inline_formal_statement"
    )
    assert inline["label"] == "Theorem 1"
    assert inline["claim_type"] == "theorem"
    assert inline["confidence"] == pytest.approx(0.6)
    assert inline["source_locator"]["start_offset"] < inline["source_locator"]["end_offset"]
    assert inline["source_locator"]["kind"] == "pdf"
    assert inline["source_locator"]["backend"] == "pdftotext"
    abstract = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "abstract_claim"
    )
    assert abstract["citation_hints"] == ["[4]"]
    assert abstract["confidence"] == pytest.approx(0.4)
    definition = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "definition"
    )
    assert definition["claim_type"] == "definition"
    assert definition["confidence"] == pytest.approx(0.6)
    assert not any("cited work" in claim["wording"].casefold() for claim in result["claims"])
    assert not any("another cited work" in claim["wording"].casefold() for claim in result["claims"])


def test_page_formal_heading_lines_use_inline_method_with_page_locators():
    extraction = {
        "format": "pdf",
        "sha256": SHA,
        "backend": "pdftotext",
        "pages": [{
            "number": 1,
            "text": (
                "Theorem 1\nFor every widget x, P(x).\n\n"
                "Definition\nA widget is defined as a device that stores data."
            ),
        }],
        "sections": [],
        "references": [],
        "warnings": [],
        "degraded_math": True,
        "truth_assessed": False,
    }
    result = pdf_claims.candidate_claims(extraction)
    inline = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "inline_formal_statement"
    )
    assert inline["label"] == "Theorem 1"
    assert inline["page"] == 1
    assert inline["source_locator"]["start_offset"] == 0
    definition = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "definition"
    )
    assert definition["claim_type"] == "definition"
    assert definition["page"] == 1
    assert result["methods"] == {
        "inline_formal_statement": 1,
        "definition": 1,
    }


def test_candidate_ids_are_stable_and_duplicate_wording_is_removed():
    first = pdf_claims.candidate_claims(_tei_extraction())
    second = pdf_claims.candidate_claims(_tei_extraction())
    assert [claim["candidate_id"] for claim in first["claims"]] == [
        claim["candidate_id"] for claim in second["claims"]
    ]

    duplicate = pdf_claims.candidate_claims({
        "format": "pdf",
        "sha256": SHA,
        "backend": "grobid",
        "pages": [],
        "sections": [
            {"heading": "Theorem 1", "text": "The same statement."},
            {"heading": "Theorem 2", "text": "The same statement."},
        ],
        "references": [],
        "warnings": [],
        "degraded_math": True,
        "truth_assessed": False,
    })
    assert duplicate["count"] == 1
    assert duplicate["claims"][0]["label"] == "Theorem 1"


def test_max_claims_reports_truncation_and_zero_returns_no_claims():
    limited = pdf_claims.candidate_claims(_tei_extraction(), max_claims=2)
    assert limited["count"] == 2
    assert len(limited["claims"]) == 2
    assert limited["truncated"] is True
    assert limited["total_candidates"] == 6
    assert any("truncated" in warning for warning in limited["warnings"])

    empty = pdf_claims.candidate_claims(_tei_extraction(), max_claims=0)
    assert empty["claims"] == []
    assert empty["count"] == 0
    assert empty["truncated"] is True

    with pytest.raises(pdf_claims.PDFClaimError, match="max_claims"):
        pdf_claims.candidate_claims(_tei_extraction(), max_claims=-1)
    with pytest.raises(pdf_claims.PDFClaimError, match="object"):
        pdf_claims.candidate_claims([])


def test_pdf_claim_error_is_a_research_error():
    assert issubclass(pdf_claims.PDFClaimError, ResearchError)


def test_preview_uses_injected_extractor_and_does_not_write(tmp_path):
    graph, content = _graph(tmp_path)
    calls = []

    def fake_extractor(data, *, backend=None):
        calls.append({"data": data, "backend": backend})
        return _tei_extraction()

    before = graph.state()
    preview = pdf_claims.preview_pdf_claims(
        graph, root_id="papers", path="paper.pdf", pdf_extractor=fake_extractor,
        backend="grobid",
    )
    after = graph.state()

    assert calls == [{"data": content, "backend": "grobid"}]
    assert after["events"] == before["events"]
    assert after["nodes"] == before["nodes"]
    assert after["edges"] == before["edges"]
    assert preview["truth_assessed"] is False
    assert preview["source"] == {
        "root_id": "papers",
        "path": "paper.pdf",
        "sha256": SHA,
        "backend": "grobid",
        "title": "Widgets and Reliability",
        "authors": ["Ada Author", "Ben Builder"],
    }
    assert preview["extraction"]["degraded_math"] is True
    assert preview["extraction"]["warnings"]
    assert preview["claims"] == pdf_claims.candidate_claims(_tei_extraction())["claims"]
    assert preview["references"][0]["citation_key"] == "b0"
    assert len(preview["references"]) == 1


def test_preview_rejects_non_pdf_and_unselected_sources(tmp_path):
    graph, _ = _graph(tmp_path)
    (tmp_path / "sources" / "notes.txt").write_text("not a pdf", encoding="utf-8")
    with pytest.raises(ResearchError, match="selected"):
        pdf_claims.preview_pdf_claims(graph, root_id="papers", path="notes.txt")
    with pytest.raises(ResearchError, match="unknown"):
        pdf_claims.preview_pdf_claims(graph, root_id="missing", path="paper.pdf")

    # A selected non-PDF file is rejected before extraction is attempted.
    graph2 = ResearchGraph.create(
        "Review a text file",
        root=tmp_path,
        investigation_id="pdf-claims-other",
        roots=[
            {
                "root_id": "papers",
                "path": str(tmp_path / "sources"),
                "repository": "fixture",
                "files": {"notes.txt": "selected"},
            }
        ],
    )
    with pytest.raises(pdf_claims.PDFClaimError, match=r"\.pdf"):
        pdf_claims.preview_pdf_claims(graph2, root_id="papers", path="notes.txt")


def test_ingest_links_claims_to_source_records_references_and_is_idempotent(tmp_path):
    graph, _ = _graph(tmp_path)
    extraction = _tei_extraction(reference_key="12")

    def fake_extractor(data, *, backend=None):
        return extraction

    result = pdf_claims.ingest_pdf_claims(
        graph,
        root_id="papers",
        path="paper.pdf",
        pdf_extractor=fake_extractor,
        backend="grobid",
        reviewer="reviewer-a",
    )

    assert result["duplicate"] is False
    assert result["truth_assessed"] is False
    assert result["extraction"] == {
        "sha256": SHA,
        "backend": "grobid",
        "warnings": extraction["warnings"],
        "degraded_math": True,
    }
    source = result["source"]
    assert source["type"] == "Source"
    assert source["paper_import"] is True
    assert source["name"] == "paper.pdf"
    assert source["sha256"] == SHA
    assert source["backend"] == "grobid"
    assert source["reviewer"] == "reviewer-a"
    assert source["locator"]["kind"] == "pdf"
    assert source["locator"]["root_id"] == "papers"
    assert source["locator"]["path"] == "paper.pdf"
    assert source["locator"]["sha256"] == SHA

    state = graph.state()
    edges = state["edges"]
    assert len(result["claims"]) == 6
    for claim in result["claims"]:
        assert claim["type"] == "Claim"
        assert claim["status"] == "unresolved"
        assert claim["parsed_assertion"] is True
        assert claim["semantics_verified"] is False
        assert claim["truth_assessed"] is False
        assert claim["scope"] == (claim["section"] or "PDF document")
        assert claim["author"] == "Ada Author; Ben Builder"
        assert claim["reviewer"] == "reviewer-a"
        assert claim["label"] == claim["paper_label"]
        assert claim["source_locator"]["sha256"] == SHA
        assert any(
            edge["type"] == "cites"
            and edge["src"] == claim["id"]
            and edge["tgt"] == source["id"]
            for edge in edges
        )

    reference_ids = {reference["id"] for reference in result["references"]}
    assert len(reference_ids) == 1
    reference = result["references"][0]
    assert reference["type"] == "Reference"
    assert reference["metadata_status"] == "parsed_tei_unverified"
    assert reference["metadata_provenance"][0]["pdf_path"] == "paper.pdf"
    assert reference["metadata_provenance"][0]["pdf_sha256"] == SHA
    assert reference["source_locators"][0]["kind"] == "pdf"
    assert not any(
        edge["type"] == "cites" and edge["tgt"] in reference_ids for edge in edges
    )

    abstract_claim = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "abstract_claim"
    )
    assert abstract_claim["citation_hints"] == ["[12]"]
    assert not any(
        edge["type"] == "cites" and edge["src"] == abstract_claim["id"]
        and edge["tgt"] in reference_ids
        for edge in edges
    )

    before_events = len(state["events"])
    second = pdf_claims.ingest_pdf_claims(
        graph,
        root_id="papers",
        path="paper.pdf",
        pdf_extractor=fake_extractor,
        backend="grobid",
    )
    after = graph.state()
    assert second["duplicate"] is True
    assert second["source"]["id"] == source["id"]
    assert [claim["id"] for claim in second["claims"]] == [
        claim["id"] for claim in result["claims"]
    ]
    assert len(second["references"]) == 1
    assert len(after["events"]) == before_events
    assert [node["id"] for node in after["nodes"]] == [
        node["id"] for node in state["nodes"]
    ]


def test_ingest_reuses_existing_paper_import_source_for_same_hash(tmp_path):
    graph, _ = _graph(tmp_path)
    extraction = _tei_extraction()
    existing = graph.record(
        "Source",
        name="paper.pdf",
        paper_import=True,
        locator={
            "root_id": "papers",
            "repository": "fixture",
            "path": "paper.pdf",
            "sha256": SHA,
            "kind": "pdf",
        },
    )

    result = pdf_claims.ingest_pdf_claims(
        graph,
        root_id="papers",
        path="paper.pdf",
        pdf_extractor=lambda data, *, backend=None: extraction,
    )

    assert result["duplicate"] is True
    assert result["source"]["id"] == existing["id"]
    assert result["claims"]
    assert any(
        node["id"] == existing["id"] for node in graph.state()["nodes"]
    )


def test_references_only_pages_produce_no_claims():
    extraction = {
        "format": "pdf",
        "sha256": SHA,
        "backend": "pdftotext",
        "pages": [
            {"number": 1, "text": "References\n[1] A. Smith. A cited work. Journal, 2020."},
            {"number": 2, "text": "[2] B. Jones. Another cited work. Journal, 2021."},
        ],
        "sections": [],
        "references": [],
        "warnings": [],
        "degraded_math": True,
        "truth_assessed": False,
    }
    result = pdf_claims.candidate_claims(extraction)
    assert result["claims"] == []
    assert result["count"] == 0
    assert any("No candidate claims" in warning for warning in result["warnings"])
