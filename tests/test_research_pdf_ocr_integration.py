from __future__ import annotations

import subprocess

from ozagia.research import ocr, pdf


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_sparse_pdf_uses_ocr_fallback(monkeypatch):
    def fake_ocr(data, **_kwargs):
        return {
            "format": "pdf",
            "backend": "ocr:tesseract",
            "pages": [{"number": 1, "text": "OCR theorem text.", "ocr": True, "lang": "eng"}],
            "warnings": ["OCR page text is approximate."],
            "degraded_math": True,
            "truth_assessed": False,
        }

    monkeypatch.setattr(ocr, "ocr_pdf", fake_ocr)
    result = pdf.extract_pdf(
        b"%PDF-1.7 fixture",
        backend="pdftotext",
        runner=lambda *_args, **_kwargs: _completed(""),
        ocr="auto",
    )
    assert result["backend"] == "ocr:tesseract"
    assert result["pages"][0]["text"] == "OCR theorem text."
    assert result["ocr"]["status"] == "applied"
    assert result["warnings"]


def test_explicit_ocr_backend(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_pdf", lambda data, **_kwargs: {
        "format": "pdf",
        "backend": "ocr:ocrmypdf",
        "pages": [{"number": 1, "text": "Explicit OCR.", "ocr": True, "lang": "eng"}],
        "warnings": [],
        "degraded_math": True,
        "truth_assessed": False,
    })
    result = pdf.extract_pdf(b"%PDF-1.7 fixture", backend="ocr")
    assert result["backend"] == "ocr:ocrmypdf"
    assert result["pages"][0]["text"] == "Explicit OCR."
