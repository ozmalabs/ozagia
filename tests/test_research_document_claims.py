"""Document parsing and claim candidate tests; all inputs are local fixtures."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from ozagia.research import document_claims
from ozagia.research.document_claims import DocumentClaimError
from ozagia.research.graph import ResearchError

CHAPTER_ONE = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body>
<h1>Chapter One</h1>
<p>Theorem 1. For every widget x, P(x).</p>
<p>We define a widget as a device that stores data.</p>
</body>
</html>
"""

CHAPTER_TWO = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Two</title></head>
<body>
<h1>Chapter Two</h1>
<p>We show that widgets increase reliability.</p>
</body>
</html>
"""

CONTAINER_XML = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _epub_bytes(
    *,
    spine: tuple[str, ...] = ("ch2", "ch1"),
    opf_doctype: bool = False,
    chapter_one_href: str = "ch1.xhtml",
    extra_entries: dict[str, bytes | str] | None = None,
) -> bytes:
    opf_header = '<?xml version="1.0" encoding="utf-8"?>\n'
    if opf_doctype:
        opf_header += (
            '<!DOCTYPE package [\n'
            '<!ENTITY xxe SYSTEM "file:///etc/passwd">\n'
            ']>\n'
        )
    itemrefs = "".join(f'    <itemref idref="{idref}"/>\n' for idref in spine)
    opf = (
        opf_header
        + '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "    <dc:title>Widgets: A Book</dc:title>\n"
        "    <dc:creator>Ada Author</dc:creator>\n"
        "    <dc:creator>Ben Builder</dc:creator>\n"
        "    <dc:language>en</dc:language>\n"
        '    <dc:identifier id="bookid">urn:isbn:9780000000000</dc:identifier>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        f'    <item id="ch1" href="{chapter_one_href}" '
        'media-type="application/xhtml+xml"/>\n'
        '    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>\n'
        "  </manifest>\n"
        "  <spine>\n"
        f"{itemrefs}"
        "  </spine>\n"
        "</package>\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml", CONTAINER_XML, zipfile.ZIP_DEFLATED
        )
        archive.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/ch1.xhtml", CHAPTER_ONE, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/ch2.xhtml", CHAPTER_TWO, zipfile.ZIP_DEFLATED)
        for name, content in (extra_entries or {}).items():
            archive.writestr(name, content, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def test_document_claim_error_is_a_research_error():
    assert issubclass(DocumentClaimError, ResearchError)


def test_parse_note_markdown_headings_sections_title_and_pages():
    text = (
        "# Widget reliability\n"
        "\n"
        "Lead paragraph.\n"
        "\n"
        "## Theorem 1: widgets\n"
        "\n"
        "Theorem 1. For every widget x, P(x).\n"
        "\n"
        "### Method\n"
        "\n"
        "We tested many widgets.\n"
    )
    result = document_claims.parse_note(text)

    assert result["format"] == "note"
    assert result["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert result["title"] == "Widget reliability"
    assert [(section["heading"], section["level"]) for section in result["sections"]] == [
        ("Widget reliability", 1),
        ("Theorem 1: widgets", 2),
        ("Method", 3),
    ]
    assert result["sections"][0]["text"] == "Lead paragraph."
    assert result["sections"][1]["text"] == "Theorem 1. For every widget x, P(x)."
    assert result["pages"] == [{"number": 1, "text": text.strip()}]
    assert result["references"] == []
    assert result["warnings"] == []
    assert result["degraded_math"] is False
    assert result["truth_assessed"] is False


def test_parse_note_plain_text_is_one_section_with_optional_title():
    result = document_claims.parse_note(
        "No headings here.\n\nA second paragraph.", title="Field note"
    )

    assert result["title"] == "Field note"
    assert result["sections"] == [{
        "heading": "",
        "text": "No headings here.\n\nA second paragraph.",
        "level": 0,
        "page": 1,
    }]
    assert result["warnings"] == []

    untitled = document_claims.parse_note("Just text.")
    assert untitled["title"] == ""
    assert untitled["sections"] == [{
        "heading": "",
        "text": "Just text.",
        "level": 0,
        "page": 1,
    }]
    assert any("no title" in warning for warning in untitled["warnings"])


def test_parse_note_ignores_hash_lines_inside_fenced_code():
    result = document_claims.parse_note(
        "# Real heading\n\n```text\n# not a heading\n```\n"
    )
    assert [section["heading"] for section in result["sections"]] == ["Real heading"]
    assert "# not a heading" in result["sections"][0]["text"]


def test_parse_note_rejects_empty_or_non_text_input():
    with pytest.raises(DocumentClaimError, match="empty"):
        document_claims.parse_note("   \n")
    with pytest.raises(DocumentClaimError, match="string"):
        document_claims.parse_note(b"# bytes")


HTML = b"""<html>
<head>
  <title>Widget Guide</title>
  <style>.widget { color: red; }</style>
  <script>window.evil = true;</script>
</head>
<body>
  <header>Site header text</header>
  <nav><a href="/">Home navigation</a></nav>
  <h1>Widgets</h1>
  <p>We show that widgets increase reliability.</p>
  <h2>Method</h2>
  <p>We tested <em>many</em> widgets across two sites.</p>
  <h3>Theorem 1</h3>
  <p>For every widget x, P(x).</p>
  <script>stealSecrets();</script>
  <style>.hidden { display: none; }</style>
  <footer>Footer text</footer>
</body>
</html>
"""


def test_parse_html_captures_title_headings_paragraphs_and_skips_chrome():
    result = document_claims.parse_html(
        HTML,
        url="https://example.test/widget-guide",
        accessed_at="2026-11-16",
    )

    assert result["format"] == "html"
    assert result["sha256"] == hashlib.sha256(HTML).hexdigest()
    assert result["title"] == "Widget Guide"
    assert result["url"] == "https://example.test/widget-guide"
    assert result["accessed_at"] == "2026-11-16"
    assert [section["heading"] for section in result["sections"]] == [
        "Widgets",
        "Method",
        "Theorem 1",
    ]
    assert result["sections"][0]["text"] == (
        "We show that widgets increase reliability."
    )
    assert result["sections"][1]["text"] == (
        "We tested many widgets across two sites."
    )
    page_text = result["pages"][0]["text"]
    assert "increase reliability" in page_text
    assert "For every widget x, P(x)." in page_text
    for ignored in (
        "Site header text",
        "Home navigation",
        "window.evil",
        "stealSecrets",
        "Footer text",
        "color: red",
    ):
        assert ignored not in page_text
        assert all(ignored not in section["text"] for section in result["sections"])
    assert result["references"] == []
    assert result["warnings"] == []
    assert result["degraded_math"] is False
    assert result["truth_assessed"] is False


def test_parse_html_accepts_text_and_falls_back_to_the_first_h1():
    result = document_claims.parse_html(
        "<html><body><h1>Fallback title</h1><p>Body text.</p></body></html>"
    )
    assert result["title"] == "Fallback title"
    assert result["sections"][0]["heading"] == "Fallback title"
    assert result["sections"][0]["text"] == "Body text."
    assert any("first h1" in warning for warning in result["warnings"])


def test_parse_html_collects_body_text_outside_paragraphs_and_rejects_empty():
    result = document_claims.parse_html("<body><div>Loose body text</div></body>")
    assert result["sections"] == [{
        "heading": "",
        "text": "Loose body text",
        "level": 0,
        "page": 1,
    }]
    assert any("no title" in warning for warning in result["warnings"])

    with pytest.raises(DocumentClaimError, match="empty"):
        document_claims.parse_html(b"   ")


def test_parse_epub_reads_container_opf_spine_order_and_chapters():
    data = _epub_bytes()
    result = document_claims.parse_epub(data)

    assert result["format"] == "epub"
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["title"] == "Widgets: A Book"
    assert result["authors"] == ["Ada Author", "Ben Builder"]
    assert result["language"] == "en"
    assert result["identifier"] == "urn:isbn:9780000000000"
    assert [section["heading"] for section in result["sections"]] == [
        "Chapter Two",
        "Chapter One",
    ]
    assert [section["page"] for section in result["sections"]] == [1, 2]
    assert [section["xml_id"] for section in result["sections"]] == [
        "OEBPS/ch2.xhtml",
        "OEBPS/ch1.xhtml",
    ]
    assert "For every widget x, P(x)." in result["sections"][1]["text"]
    assert [page["number"] for page in result["pages"]] == [1, 2]
    assert "We show that widgets increase reliability." in result["pages"][0]["text"]
    assert "For every widget x, P(x)." in result["pages"][1]["text"]
    assert result["references"] == []
    assert result["warnings"] == []
    assert result["degraded_math"] is False
    assert result["truth_assessed"] is False


def test_parse_epub_rejects_unsafe_archive_paths():
    data = _epub_bytes(extra_entries={"../evil.xhtml": b"<p>evil</p>"})
    with pytest.raises(DocumentClaimError, match="unsafe"):
        document_claims.parse_epub(data)

    absolute = _epub_bytes(extra_entries={"/tmp/evil.xhtml": b"<p>evil</p>"})
    with pytest.raises(DocumentClaimError, match="unsafe"):
        document_claims.parse_epub(absolute)

    escaping = _epub_bytes(chapter_one_href="../../evil.xhtml")
    with pytest.raises(DocumentClaimError, match="unsafe"):
        document_claims.parse_epub(escaping)

    remote = _epub_bytes(chapter_one_href="https://example.test/evil.xhtml")
    with pytest.raises(DocumentClaimError, match="relative"):
        document_claims.parse_epub(remote)


def test_parse_epub_rejects_zip_bombs_oversized_data_and_entry_floods(monkeypatch):
    bomb = _epub_bytes(extra_entries={"OEBPS/zeros.bin": b"0" * 200_000})
    with pytest.raises(DocumentClaimError, match="uncompressed"):
        document_claims.parse_epub(bomb, max_bytes=100_000)

    data = _epub_bytes()
    with pytest.raises(DocumentClaimError, match="extraction limit"):
        document_claims.parse_epub(data, max_bytes=len(data) - 1)

    monkeypatch.setattr(document_claims, "MAX_EPUB_ENTRIES", 3)
    with pytest.raises(DocumentClaimError, match="too many entries"):
        document_claims.parse_epub(data)


def test_parse_epub_rejects_dtd_entity_and_invalid_zip():
    data = _epub_bytes(opf_doctype=True)
    with pytest.raises(DocumentClaimError, match="DTD or entity"):
        document_claims.parse_epub(data)

    with pytest.raises(DocumentClaimError, match="ZIP"):
        document_claims.parse_epub(b"not a zip archive")

    with pytest.raises(DocumentClaimError, match="empty"):
        document_claims.parse_epub(b"")


def test_parse_epub_reports_drm_without_treating_it_as_supported():
    data = _epub_bytes(
        extra_entries={"META-INF/encryption.xml": b"<encryption/>"}
    )
    result = document_claims.parse_epub(data)
    assert any(
        "DRM" in warning or "encryption" in warning
        for warning in result["warnings"]
    )
    assert result["sections"]


def test_candidate_claims_from_note_are_format_labelled_and_unverified():
    note = document_claims.parse_note(
        "# Widget reliability\n\n"
        "## Theorem 1: widgets\n\n"
        "Theorem 1. For every widget x, P(x).\n\n"
        "## Conclusion\n\n"
        "We conclude that widgets are reliable.\n"
    )
    result = document_claims.candidate_claims_from_document(note)

    assert result["format"] == "note"
    assert result["count"] == len(result["claims"]) == 2
    assert result["truncated"] is False
    assert result["methods"] == {
        "abstract_claim": 1,
        "section_formal_statement": 1,
    }
    assert result["warnings"] == []
    assert result["truth_assessed"] is False
    theorem = next(
        claim for claim in result["claims"]
        if claim["extraction_method"] == "section_formal_statement"
    )
    assert theorem["label"] == "Theorem 1"
    assert theorem["section"] == "Theorem 1: widgets"
    assert theorem["source_locator"]["kind"] == "note"
    assert theorem["source_locator"]["backend"] == "note"
    assert theorem["candidate_id"].startswith("note-claim-")
    assert all(claim["truth_assessed"] is False for claim in result["claims"])


def test_candidate_claims_from_html_and_epub_use_format_backends():
    html = document_claims.parse_html(HTML)
    html_result = document_claims.candidate_claims_from_document(html)
    assert html_result["format"] == "html"
    theorem = next(
        claim for claim in html_result["claims"]
        if claim["extraction_method"] == "section_formal_statement"
    )
    assert theorem["label"] == "Theorem 1"
    assert theorem["source_locator"]["kind"] == "html"
    assert theorem["source_locator"]["backend"] == "html"
    assert theorem["candidate_id"].startswith("html-claim-")

    epub = document_claims.parse_epub(_epub_bytes())
    epub_result = document_claims.candidate_claims_from_document(epub)
    assert epub_result["format"] == "epub"
    assert epub_result["count"] >= 2
    assert set(epub_result["methods"]) == {
        "inline_formal_statement",
        "definition",
    }
    for claim in epub_result["claims"]:
        assert claim["source_locator"]["kind"] == "epub"
        assert claim["source_locator"]["backend"] == "epub"
        assert claim["page"] == 2
        assert claim["source_locator"]["xml_id"] == "OEBPS/ch1.xhtml"
        assert claim["candidate_id"].startswith("epub-claim-")
        assert claim["math_degraded"] is False
        assert claim["semantics_verified"] is False
        assert claim["truth_assessed"] is False
    assert epub_result["truth_assessed"] is False


def test_candidate_claims_respect_max_claims_and_report_truncation():
    note = document_claims.parse_note(
        "## Theorem 1\n\nFirst statement.\n\n"
        "## Theorem 2\n\nSecond statement.\n"
    )
    result = document_claims.candidate_claims_from_document(note, max_claims=1)
    assert result["count"] == 1
    assert len(result["claims"]) == 1
    assert result["truncated"] is True
    assert result["methods"] == {"section_formal_statement": 1}
    assert any("truncated" in warning for warning in result["warnings"])


def test_candidate_claims_validate_the_document_shape():
    note = document_claims.parse_note("A note.")
    with pytest.raises(DocumentClaimError, match="object"):
        document_claims.candidate_claims_from_document([])
    with pytest.raises(DocumentClaimError, match="format"):
        document_claims.candidate_claims_from_document({
            "format": "pdf",
            "sha256": "0" * 64,
        })
    with pytest.raises(DocumentClaimError, match="sha256"):
        document_claims.candidate_claims_from_document({
            "format": "note",
            "sha256": "not-a-digest",
        })
    with pytest.raises(DocumentClaimError, match="max_claims"):
        document_claims.candidate_claims_from_document(note, max_claims=-1)
    bad_pages = dict(note)
    bad_pages["pages"] = "not-a-list"
    with pytest.raises(DocumentClaimError, match="pages"):
        document_claims.candidate_claims_from_document(bad_pages)
