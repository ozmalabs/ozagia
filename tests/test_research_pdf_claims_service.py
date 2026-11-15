from __future__ import annotations

import hashlib
from pathlib import Path

from ozagia.research.service import ResearchService


def _fake_pdf(data, **_kwargs):
    return {
        "format": "pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "backend": "grobid",
        "pages": [],
        "sections": [
            {"heading": "Abstract",
             "text": "We show that the treatment helps adults. Results suggest a benefit."},
            {"heading": "Theorem 1", "xml_id": "thm1",
             "text": "The treatment helps adults under the tested conditions [1]."},
            {"heading": "Definition 2", "xml_id": "def2",
             "text": "We define response as a reduction in symptoms."},
            {"heading": "References", "text": "[1] A. Author. A cited paper. 2020."},
        ],
        "title": "A PDF Paper",
        "authors": ["A. Author"],
        "abstract": "We show that the treatment helps adults.",
        "references": [{
            "title": "A Cited Paper", "citation_key": "cited2020",
            "identifiers": {"doi": "10.1234/cited.1"},
        }],
        "warnings": ["GROBID returns structured text; equations are degraded."],
        "degraded_math": True,
        "truth_assessed": False,
    }


def test_pdf_claim_preview_ingest_and_paper_review_start(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "paper.pdf").write_bytes(b"%PDF-1.7 fixture")
    svc = ResearchService(tmp_path, pdf_extractor=_fake_pdf)
    sid = svc.call("init", {
        "question": "Review a PDF",
        "roots": [{
            "root_id": "papers", "path": str(source_root),
            "files": ["paper.pdf"],
        }],
    })["investigation_id"]

    preview = svc.call("pdf_claim_preview", {
        "session_id": sid, "root_id": "papers", "path": "paper.pdf"})
    assert preview["count"] >= 2
    assert preview["methods"]["section_formal_statement"] >= 2
    assert preview["truth_assessed"] is False

    ingested = svc.call("pdf_claim_ingest", {
        "session_id": sid, "root_id": "papers", "path": "paper.pdf",
        "reviewer": "reader"})
    assert ingested["source"]["type"] == "Source"
    assert ingested["claims"]
    assert ingested["references"]
    assert ingested["citation_count"] >= 1
    resolved_edges = [
        edge for edge in svc.call("inspect", {
            "session_id": sid, "node_id": ingested["claims"][0]["id"],
        })["edges"]
        if edge["type"] == "cites"
        and edge.get("provenance", {}).get("kind") == "pdf_citation_resolution"
    ]
    assert resolved_edges
    assert resolved_edges[0]["provenance"]["requires_review"] is True
    claim = ingested["claims"][0]
    assert claim["semantics_verified"] is False
    assert claim["truth_assessed"] is False
    assert claim["source_locator"]["kind"] == "pdf"
    assert any(
        edge["type"] == "cites" and edge["src"] == claim["id"]
        and edge["tgt"] == ingested["source"]["id"]
        for edge in svc.call("inspect", {
            "session_id": sid, "node_id": claim["id"]})["edges"]
    )

    again = svc.call("pdf_claim_ingest", {
        "session_id": sid, "root_id": "papers", "path": "paper.pdf"})
    assert again["source"]["id"] == ingested["source"]["id"]
    assert len(again["claims"]) == len(ingested["claims"])

    extended = svc.call("pdf_preview", {
        "session_id": sid, "root_id": "papers", "path": "paper.pdf",
        "extract_claims": True})
    assert extended["claims"]
    assert extended["degraded_math"] is True

    started = svc.call("paper_review_start", {
        "session_id": sid, "root_id": "papers", "path": "paper.pdf",
        "question": "Does the treatment help?",
        "roles": ["methods", "statistics"],
        "models": ["model-a", "model-b"],
        "reviewer": "reader",
    })
    assert started["plan"]["target_id"] == ingested["source"]["id"]
    assert started["packet"]["claim_ids"]
    assert started["packet"]["truth_assessed"] is False

    bundle = svc.call("paper_review_bundle", {
        "session_id": sid, "plan_id": started["plan"]["id"],
        "output_dir": str(tmp_path / "pdf-review"),
    })
    for name in ("review.md", "review.json", "claims.csv", "evidence.csv",
                 "references.bib", "arguments.json", "checks.json"):
        assert Path(bundle["paths"][name]).is_file()
