"""PDF extraction tests use injected GROBID/pdftotext callables; no network."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from ozagia.research import pdf

PDF = b"%PDF-1.7\nfixture bytes\n%%EOF\n"

TEI_XML = b"""<?xml version="1.0"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>A GROBID Paper</title><author>A. Author</author></titleStmt>
      <sourceDesc>
        <listBibl>
          <biblStruct xml:id="b0">
            <analytic><title>Cited Work</title></analytic>
            <idno type="DOI">10.1234/cited.1</idno>
            <date>2020</date>
          </biblStruct>
        </listBibl>
      </sourceDesc>
    </fileDesc>
    <profileDesc><abstract><p>An abstract.</p></abstract></profileDesc>
  </teiHeader>
  <text><body>
    <div xml:id="s1"><head>Introduction</head>
      <p>Evidence text <ref type="bibr" target="#b0">[1]</ref>.</p>
    </div>
  </body></text>
</TEI>
"""


def _fake_post(captured, content=TEI_XML):
    def post(url, *, body, headers, timeout):
        captured.update(url=url, body=body, headers=headers, timeout=timeout)
        return {
            "content": content,
            "status": 200,
            "media_type": "application/xml",
            "final_url": url,
        }

    return post


def _runner_for(stdout, returncode=0, stderr=b"", captured=None):
    def runner(argv, *, timeout):
        if captured is not None:
            captured.update(
                argv=list(argv), timeout=timeout, data=Path(argv[2]).read_bytes()
            )
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return runner


def test_grobid_posts_pdf_and_returns_tei_structure_without_network():
    captured = {}
    result = pdf.extract_pdf(
        PDF,
        backend="grobid",
        grobid_url="http://grobid.example:8070/grobid/",
        post=_fake_post(captured),
        timeout=12,
    )

    assert captured["url"] == "http://grobid.example:8070/grobid/api/processFulltextDocument"
    assert captured["timeout"] == 12
    assert captured["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="input"; filename="paper.pdf"' in captured["body"]
    assert PDF in captured["body"]

    assert result["format"] == "pdf"
    assert result["backend"] == "grobid"
    assert result["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert result["pages"] == []
    assert result["title"] == "A GROBID Paper"
    assert result["authors"] == ["A. Author"]
    assert result["abstract"] == "An abstract."
    assert result["sections"][0]["heading"] == "Introduction"
    assert result["sections"][0]["text"] == "Evidence text [1]."
    assert result["references"][0]["title"] == "Cited Work"
    assert result["references"][0]["identifiers"]["doi"] == "10.1234/cited.1"
    assert result["citations"][0]["citation_keys"] == ["b0"]
    assert result["degraded_math"] is True
    assert result["truth_assessed"] is False
    assert "claims" not in result
    assert any("page numbers" in warning for warning in result["warnings"])


def test_grobid_is_selected_from_environment_and_accepts_a_full_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setenv("OZAGIA_GROBID_URL", "http://grobid.local:8070")
    result = pdf.extract_pdf(PDF, post=_fake_post(captured))
    assert result["backend"] == "grobid"
    assert captured["url"] == "http://grobid.local:8070/api/processFulltextDocument"

    captured.clear()
    full = "http://grobid.local:8070/api/processFulltextDocument"
    same = pdf.extract_pdf(PDF, backend="grobid", grobid_url=full, post=_fake_post(captured))
    assert same["backend"] == "grobid"
    assert captured["url"] == full


def test_explicit_backend_overrides_configured_grobid(monkeypatch):
    monkeypatch.setenv("OZAGIA_GROBID_URL", "http://grobid.example:8070")

    def unexpected_post(*_args, **_kwargs):
        raise AssertionError("GROBID must not be called for an explicit local backend")

    captured = {}
    result = pdf.extract_pdf(
        PDF,
        backend="pdftotext",
        post=unexpected_post,
        runner=_runner_for(b"Text\f", captured=captured),
    )
    assert result["backend"] == "pdftotext"
    assert captured["data"] == PDF
    assert captured["argv"][-1] == "-"
    assert "-layout" in captured["argv"]


def test_pdftotext_runner_returns_numbered_pages_hash_and_boundaries():
    captured = {}
    result = pdf.extract_pdf(
        PDF,
        backend="pdftotext",
        runner=_runner_for(b"First page\nline\fSecond page\f", captured=captured),
        timeout=9,
    )

    assert captured["timeout"] == 9
    assert result["format"] == "pdf"
    assert result["backend"] == "pdftotext"
    assert result["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert result["pages"] == [
        {"number": 1, "text": "First page\nline"},
        {"number": 2, "text": "Second page"},
    ]
    assert result["title"] == ""
    assert result["authors"] == []
    assert result["references"] == []
    assert result["degraded_math"] is True
    assert result["truth_assessed"] is False
    assert not any("page separators" in warning for warning in result["warnings"])


def test_pdftotext_without_form_feed_reports_a_single_page_and_says_so():
    result = pdf.extract_pdf(PDF, backend="pdftotext", runner=_runner_for(b"Only text"))
    assert result["pages"] == [{"number": 1, "text": "Only text"}]
    assert any("page separators" in warning for warning in result["warnings"])


def test_pdftotext_empty_output_is_reported_not_silently_successful():
    result = pdf.extract_pdf(PDF, backend="pdftotext", runner=_runner_for(b""))
    assert result["pages"] == [{"number": 1, "text": ""}]
    assert any("no text" in warning for warning in result["warnings"])


def test_pdftotext_runner_errors_are_typed():
    with pytest.raises(pdf.PDFError, match="exit code 1"):
        pdf.extract_pdf(
            PDF,
            backend="pdftotext",
            runner=_runner_for(b"", returncode=1, stderr=b"Syntax Error"),
        )

    def timeout_runner(argv, *, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(pdf.PDFError, match="timed out"):
        pdf.extract_pdf(PDF, backend="pdftotext", runner=timeout_runner, timeout=1)


def test_pdftotext_auto_selection_when_pymupdf_is_unavailable(monkeypatch):
    monkeypatch.delenv("OZAGIA_GROBID_URL", raising=False)
    monkeypatch.setattr(pdf, "_pymupdf_module", lambda: None)
    result = pdf.extract_pdf(PDF, runner=_runner_for(b"Auto page\f"))
    assert result["backend"] == "pdftotext"
    assert result["pages"] == [{"number": 1, "text": "Auto page"}]


def test_bad_magic_is_rejected_before_any_backend_runs():
    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("backend must not run for invalid input")

    with pytest.raises(pdf.PDFError, match="magic"):
        pdf.extract_pdf(b"not a pdf", backend="pdftotext", runner=unexpected_runner)
    with pytest.raises(pdf.PDFError, match="bytes"):
        pdf.extract_pdf("%PDF-1.7")
    with pytest.raises(pdf.PDFError, match="empty"):
        pdf.extract_pdf(b"")
    with pytest.raises(pdf.PDFError, match="magic"):
        pdf.extract_pdf(bytearray(b"not a pdf"))


def test_oversized_input_is_rejected(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PDF_BYTES", 1024)

    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("backend must not run for oversized input")

    with pytest.raises(pdf.PDFError, match="exceeds"):
        pdf.extract_pdf(
            b"%PDF-1.7" + b"x" * 1024, backend="pdftotext", runner=unexpected_runner
        )


def test_grobid_rejects_entity_bearing_tei_and_http_errors(monkeypatch):
    monkeypatch.delenv("OZAGIA_GROBID_URL", raising=False)
    bad_tei = b'<!DOCTYPE TEI [<!ENTITY e "boom">]><TEI/>'
    with pytest.raises(pdf.PDFError, match="DTD/entity"):
        pdf.extract_pdf(
            PDF,
            backend="grobid",
            grobid_url="http://grobid.example:8070",
            post=_fake_post({}, bad_tei),
        )

    def failing_post(url, *, body, headers, timeout):
        return {"content": b"", "status": 503, "final_url": url}

    with pytest.raises(pdf.PDFError, match="503"):
        pdf.extract_pdf(
            PDF,
            backend="grobid",
            grobid_url="http://grobid.example:8070",
            post=failing_post,
        )


def test_missing_and_invalid_backends_raise_clear_typed_errors(monkeypatch):
    monkeypatch.delenv("OZAGIA_GROBID_URL", raising=False)
    with pytest.raises(pdf.PDFError, match="GROBID"):
        pdf.extract_pdf(PDF, backend="grobid")
    with pytest.raises(pdf.PDFError, match="http"):
        pdf.extract_pdf(PDF, backend="grobid", grobid_url="localhost:8070")
    with pytest.raises(pdf.PDFError, match="backend"):
        pdf.extract_pdf(PDF, backend="unknown")

    monkeypatch.setattr(pdf, "_pymupdf_module", lambda: None)
    with pytest.raises(pdf.PDFError, match="pymupdf"):
        pdf.extract_pdf(PDF, backend="pymupdf")
    monkeypatch.setattr(pdf.shutil, "which", lambda _name: None)
    with pytest.raises(pdf.PDFError, match="pdftotext"):
        pdf.extract_pdf(PDF, backend="pdftotext")
    with pytest.raises(pdf.PDFError, match="no PDF backend"):
        pdf.extract_pdf(PDF)


def _pymupdf():
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz
        except ImportError:
            return None
        return fitz
    return pymupdf


class _FakePage:
    def __init__(self, text):
        self._text = text

    def get_text(self, kind):
        assert kind == "text"
        return self._text


class _FakePymupdfDocument:
    metadata = {"title": "Fake metadata title"}

    def __init__(self, pages):
        self._pages = pages
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        self.closed = True


class _FakePymupdfModule:
    def __init__(self, document):
        self.document = document

    def open(self, *, stream, filetype):
        assert stream == PDF
        assert filetype == "pdf"
        return self.document


def test_pymupdf_backend_uses_page_text_and_title(monkeypatch):
    document = _FakePymupdfDocument([_FakePage("One\n"), _FakePage("Two\n")])
    monkeypatch.setattr(pdf, "_pymupdf_module", lambda: _FakePymupdfModule(document))
    result = pdf.extract_pdf(PDF, backend="pymupdf")
    assert result["backend"] == "pymupdf"
    assert result["title"] == "Fake metadata title"
    assert result["pages"] == [
        {"number": 1, "text": "One"},
        {"number": 2, "text": "Two"},
    ]
    assert document.closed is True
    assert result["degraded_math"] is True
    assert result["truth_assessed"] is False


def test_pymupdf_backend_extracts_numbered_pages_when_available():
    module = _pymupdf()
    if module is None:
        pytest.skip("pymupdf is not installed")
    document = module.open()
    try:
        document.new_page().insert_text((72, 72), "First page body")
        document.new_page().insert_text((72, 72), "Second page body")
        document.set_metadata({"title": "Fixture title"})
        data = document.tobytes()
    finally:
        document.close()

    result = pdf.extract_pdf(data, backend="pymupdf")
    assert result["format"] == "pdf"
    assert result["backend"] == "pymupdf"
    assert result["title"] == "Fixture title"
    assert [page["number"] for page in result["pages"]] == [1, 2]
    assert "First page body" in result["pages"][0]["text"]
    assert "Second page body" in result["pages"][1]["text"]
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["references"] == []
    assert result["degraded_math"] is True
    assert result["truth_assessed"] is False
