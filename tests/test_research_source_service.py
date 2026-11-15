from __future__ import annotations

import io
import zipfile

from ozagia.research.service import ResearchService


def _epub() -> bytes:
    container = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf"
 media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    opf = b"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>A Book of Widgets</dc:title>
  <dc:creator>Ada Author</dc:creator>
 </metadata>
 <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine>
</package>"""
    chapter = b"""<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head>
<body><h1>Chapter One</h1>
<p>Theorem 1. Widgets store data.</p>
<p>We define a widget as a device that stores data.</p></body></html>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/c1.xhtml", chapter)
    return buffer.getvalue()


def test_source_registration_and_document_claim_ingest(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "notes.md").write_text(
        "# Hypothesis\n\nWe propose that widgets improve reliability.\n",
        encoding="utf-8",
    )
    (source_root / "site.html").write_text(
        "<html><head><title>Widget site</title></head><body>"
        "<h1>Findings</h1><p>We show that widgets increase reliability.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    (source_root / "book.epub").write_bytes(_epub())
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {
        "question": "Source formats",
        "roots": [{
            "root_id": "sources", "path": str(source_root),
            "files": ["notes.md", "site.html", "book.epub"],
        }],
    })["investigation_id"]

    types = svc.call("source_types", {"session_id": sid})["source_types"]
    assert {item["source_type"] for item in types} >= {"book", "note", "website"}

    registered = svc.call("source_register", {
        "session_id": sid,
        "source_type": "book",
        "metadata": {
            "title": "A Strict Book",
            "authors": ["Ada Author"],
            "year": "2020",
            "publisher": "Test Press",
            "isbn": "9783161484100",
        },
        "root_id": "sources",
        "path": "book.epub",
        "reviewer": "reader",
    })
    assert registered["reference"]["reference_type"] == "book"
    assert registered["source"]["source_type"] == "book"
    assert registered["holding"]["epistemic_role"] == "interpretation"
    assert registered["truth_assessed"] is False

    evidence_hold = svc.call("holding_record", {
        "session_id": sid,
        "source_id": registered["source"]["id"],
        "source_type": "book",
        "epistemic_role": "evidence",
        "rationale": "The book reports a controlled study.",
        "reviewer": "reader",
    })["holding"]
    checked = svc.call("holding_check", {
        "session_id": sid, "holding_id": evidence_hold["id"]})
    assert checked["truth_assessed"] is False

    note_preview = svc.call("document_claim_preview", {
        "session_id": sid, "root_id": "sources", "path": "notes.md"})
    assert note_preview["source_type"] == "note"
    assert note_preview["claims"]
    note = svc.call("document_claim_ingest", {
        "session_id": sid, "root_id": "sources", "path": "notes.md",
        "reviewer": "reader",
    })
    assert note["source"]["source_type"] == "note"
    assert note["holding"]["epistemic_role"] == "personal_note"
    assert note["claims"]
    context = svc.call("claim_holding", {
        "session_id": sid, "claim_id": note["claims"][0]["id"]})
    assert context["truth_assessed"] is False

    website = svc.call("document_claim_ingest", {
        "session_id": sid, "root_id": "sources", "path": "site.html",
        "reviewer": "reader",
    })
    assert website["source"]["source_type"] == "website"
    assert website["holding"]["epistemic_role"] == "background"
    assert website["claims"]
    assert any("accessed_at" in warning for warning in website["warnings"])

    book = svc.call("document_claim_ingest", {
        "session_id": sid, "root_id": "sources", "path": "book.epub",
        "reviewer": "reader",
    })
    assert book["source"]["source_type"] == "book"
    assert book["reference"]["reference_type"] == "book"
    assert book["holding"]["epistemic_role"] == "interpretation"
    assert book["claims"]

    report = svc.call("holding_report", {"session_id": sid})
    assert report["counts"]["holdings"] >= 4
    assert report["truth_assessed"] is False
