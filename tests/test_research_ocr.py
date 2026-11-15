"""OCR tests use injected runners and fake PyMuPDF modules; no real tools."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ozagia.research import ocr

PDF = b"%PDF-1.7\nfixture bytes\n%%EOF\n"
OCR_PDF = b"%PDF-1.7\nocr output\n%%EOF\n"


def _completed(argv, stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class _FakeTextPage:
    def __init__(self, text):
        self._text = text

    def get_text(self, kind):
        assert kind == "text"
        return self._text


class _FakeTextDocument:
    def __init__(self, texts):
        self._pages = [_FakeTextPage(text) for text in texts]
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        self.closed = True


class _FakeTextModule:
    def __init__(self, texts):
        self.document = _FakeTextDocument(texts)

    def open(self, *, stream, filetype):
        assert stream == OCR_PDF
        assert filetype == "pdf"
        return self.document


class _FakePixmap:
    def __init__(self, payload):
        self.payload = payload

    def save(self, path):
        Path(path).write_bytes(self.payload)


class _FakeImagePage:
    def __init__(self, text):
        self.text = text
        self.render_dpi = []

    def get_pixmap(self, *, dpi):
        self.render_dpi.append(dpi)
        return _FakePixmap(f"png:{self.text}".encode())


class _FakeImageDocument:
    def __init__(self, texts):
        self.pages = [_FakeImagePage(text) for text in texts]
        self.closed = False

    def __iter__(self):
        return iter(self.pages)

    def close(self):
        self.closed = True


class _FakeImageModule:
    def __init__(self, texts):
        self.document = _FakeImageDocument(texts)

    def open(self, *, stream, filetype):
        assert stream == PDF
        assert filetype == "pdf"
        return self.document


def test_text_is_sparse_counts_stripped_text_across_pages():
    assert ocr.text_is_sparse([]) is True
    assert ocr.text_is_sparse([{"number": 1, "text": "  \n\t "}]) is True
    assert ocr.text_is_sparse([{"number": 1, "text": "short"}]) is True
    assert (
        ocr.text_is_sparse(
            [
                {"number": 1, "text": "1234567890"},
                {"number": 2, "text": "abcdefghij"},
            ]
        )
        is False
    )
    assert ocr.text_is_sparse([{"number": 1, "text": "x" * 20}]) is False
    assert ocr.text_is_sparse([{"number": 1, "text": "x" * 10}], min_chars=10) is False
    assert ocr.text_is_sparse([{"number": 1, "text": "x" * 9}], min_chars=10) is True

    with pytest.raises(ocr.OCRError, match="min_chars"):
        ocr.text_is_sparse([], min_chars=-1)
    with pytest.raises(ocr.OCRError, match="mappings"):
        ocr.text_is_sparse(["not a page"])


def test_ocrmypdf_runner_writes_output_and_pdftotext_splits_pages(monkeypatch):
    monkeypatch.setattr(ocr, "_pymupdf_module", lambda: None)
    calls = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if argv[0] == "ocrmypdf":
            assert argv[1:4] == ["--skip-text", "--output-type", "pdf"]
            assert Path(argv[4]).read_bytes() == PDF
            Path(argv[5]).write_bytes(OCR_PDF)
            return _completed(argv)
        if argv[0] == "pdftotext":
            assert argv[1] == "-layout"
            assert Path(argv[2]).read_bytes() == OCR_PDF
            assert argv[3] == "-"
            return _completed(argv, b"First OCR page\fSecond OCR page\f")
        raise AssertionError(f"unexpected command: {argv}")

    result = ocr.ocr_pdf(PDF, backend="ocrmypdf", runner=runner)

    assert result["format"] == "pdf"
    assert result["backend"] == "ocr:ocrmypdf"
    assert result["pages"] == [
        {"number": 1, "text": "First OCR page", "ocr": True, "lang": "eng"},
        {"number": 2, "text": "Second OCR page", "ocr": True, "lang": "eng"},
    ]
    assert result["degraded_math"] is True
    assert result["truth_assessed"] is False
    assert any("machine-recognized" in warning for warning in result["warnings"])
    assert calls[0][0][0] == "ocrmypdf"
    assert calls[0][1]["timeout"] == ocr.COMMAND_TIMEOUT


def test_ocrmypdf_uses_pymupdf_for_output_text_when_available():
    calls = []
    module = _FakeTextModule(["Page one OCR\n", "Page two OCR\n"])

    def runner(argv, **kwargs):
        calls.append(list(argv))
        assert argv[0] == "ocrmypdf"
        Path(argv[-1]).write_bytes(OCR_PDF)
        return _completed(argv)

    result = ocr.ocr_pdf(
        PDF, backend="ocrmypdf", runner=runner, pymupdf_module=module
    )

    assert result["backend"] == "ocr:ocrmypdf"
    assert result["pages"] == [
        {"number": 1, "text": "Page one OCR", "ocr": True, "lang": "eng"},
        {"number": 2, "text": "Page two OCR", "ocr": True, "lang": "eng"},
    ]
    assert module.document.closed is True
    assert [call[0] for call in calls] == ["ocrmypdf"]


def test_ocrmypdf_accepts_runner_bytes_as_output_pdf():
    def runner(argv, **kwargs):
        assert argv[0] == "ocrmypdf"
        return OCR_PDF

    module = _FakeTextModule(["Bytes output OCR"])
    result = ocr.ocr_pdf(
        PDF, backend="ocrmypdf", runner=runner, pymupdf_module=module
    )

    assert result["pages"] == [
        {"number": 1, "text": "Bytes output OCR", "ocr": True, "lang": "eng"}
    ]


def test_tesseract_renders_with_pymupdf_and_reads_runner_stdout():
    module = _FakeImageModule(["Erste Seite", "Zweite Seite"])
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        assert argv[0] == "tesseract"
        assert argv[2] == "stdout"
        assert argv[3:] == ["-l", "deu"]
        image = Path(argv[1])
        payload = image.read_bytes().decode()
        assert payload.startswith("png:")
        return _completed(argv, (payload.removeprefix("png:") + "\n").encode())

    result = ocr.ocr_pdf(
        PDF,
        backend="tesseract",
        lang="deu",
        dpi=200,
        runner=runner,
        pymupdf_module=module,
    )

    assert result["backend"] == "ocr:tesseract"
    assert result["pages"] == [
        {"number": 1, "text": "Erste Seite", "ocr": True, "lang": "deu"},
        {"number": 2, "text": "Zweite Seite", "ocr": True, "lang": "deu"},
    ]
    assert [page.render_dpi for page in module.document.pages] == [[200], [200]]
    assert module.document.closed is True
    assert len(calls) == 2
    assert result["degraded_math"] is True
    assert result["truth_assessed"] is False
    assert any("Tesseract" in warning for warning in result["warnings"])


def test_tesseract_uses_pdftoppm_when_pymupdf_is_missing_and_bounds_pages(monkeypatch):
    monkeypatch.setattr(ocr, "_pymupdf_module", lambda: None)
    rendered = []
    transcribed = []

    def runner(argv, **kwargs):
        if argv[0] == "pdftoppm":
            assert argv[1:4] == ["-r", "150", "-png"]
            assert Path(argv[4]).read_bytes() == PDF
            prefix = Path(argv[5])
            for number in (1, 2, 3):
                rendered.append(number)
                Path(f"{prefix}-{number}.png").write_bytes(f"image-{number}".encode())
            return _completed(argv)
        if argv[0] == "tesseract":
            image = Path(argv[1])
            transcribed.append(image.name)
            return _completed(argv, b"Recognized text\n")
        raise AssertionError(f"unexpected command: {argv}")

    result = ocr.ocr_pdf(
        PDF, backend="tesseract", dpi=150, max_pages=2, runner=runner
    )

    assert result["backend"] == "ocr:tesseract"
    assert result["pages"] == [
        {"number": 1, "text": "Recognized text", "ocr": True, "lang": "eng"},
        {"number": 2, "text": "Recognized text", "ocr": True, "lang": "eng"},
    ]
    assert rendered == [1, 2, 3]
    assert transcribed == ["page-1.png", "page-2.png"]
    assert any("first 2 pages" in warning for warning in result["warnings"])


def test_missing_backends_raise_clear_errors(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr, "_pymupdf_module", lambda: None)

    with pytest.raises(ocr.OCRError, match="ocrmypdf is not installed"):
        ocr.ocr_pdf(PDF, backend="ocrmypdf")
    with pytest.raises(ocr.OCRError, match="tesseract is not installed"):
        ocr.ocr_pdf(PDF, backend="tesseract")
    with pytest.raises(ocr.OCRError, match="no OCR backend") as info:
        ocr.ocr_pdf(PDF)

    message = str(info.value)
    assert "ocrmypdf is not installed" in message
    assert "tesseract is not installed" in message
    assert "install OCRmyPDF" in message
    assert "PyMuPDF ('pip install pymupdf')" in message


def test_invalid_input_is_rejected_before_any_backend_runs():
    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("backend must not run for invalid input")

    with pytest.raises(ocr.OCRError, match="bytes"):
        ocr.ocr_pdf("%PDF-1.7", runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="empty"):
        ocr.ocr_pdf(b"", runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="magic"):
        ocr.ocr_pdf(b"not a pdf", backend="ocrmypdf", runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="magic"):
        ocr.ocr_pdf(bytearray(b"not a pdf"), runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="dpi"):
        ocr.ocr_pdf(PDF, dpi=0, runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="max_pages"):
        ocr.ocr_pdf(PDF, max_pages=0, runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="max_bytes"):
        ocr.ocr_pdf(PDF, max_bytes=0, runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="language"):
        ocr.ocr_pdf(PDF, lang=" ", runner=unexpected_runner)
    with pytest.raises(ocr.OCRError, match="backend"):
        ocr.ocr_pdf(PDF, backend="unknown", runner=unexpected_runner)


def test_oversized_input_is_rejected_with_explicit_and_module_limits(monkeypatch):
    def unexpected_runner(*_args, **_kwargs):
        raise AssertionError("backend must not run for oversized input")

    with pytest.raises(ocr.OCRError, match="exceeds"):
        ocr.ocr_pdf(PDF, max_bytes=8, runner=unexpected_runner)

    monkeypatch.setattr(ocr, "MAX_PDF_BYTES", 1024)
    with pytest.raises(ocr.OCRError, match="exceeds"):
        ocr.ocr_pdf(b"%PDF-1.7" + b"x" * 1024, runner=unexpected_runner)


def test_runner_failures_are_typed(monkeypatch):
    monkeypatch.setattr(ocr, "_pymupdf_module", lambda: None)

    def failing(argv, **kwargs):
        return _completed(argv, stderr=b"bad ocr", returncode=1)

    with pytest.raises(ocr.OCRError, match="exit code 1") as info:
        ocr.ocr_pdf(PDF, backend="ocrmypdf", runner=failing)
    assert "bad ocr" in str(info.value)

    def timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(ocr.OCRError, match="timed out"):
        ocr.ocr_pdf(PDF, backend="ocrmypdf", runner=timing_out)


def test_auto_falls_back_to_tesseract_when_ocrmypdf_fails(monkeypatch):
    monkeypatch.setattr(ocr, "_pymupdf_module", lambda: None)
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "ocrmypdf":
            return _completed(argv, stderr=b"ocrmypdf broke", returncode=2)
        if argv[0] == "pdftoppm":
            Path(f"{argv[5]}-1.png").write_bytes(b"image")
            return _completed(argv)
        if argv[0] == "tesseract":
            return _completed(argv, b"Fallback OCR text\n")
        raise AssertionError(f"unexpected command: {argv}")

    result = ocr.ocr_pdf(PDF, runner=runner)

    assert result["backend"] == "ocr:tesseract"
    assert result["pages"] == [
        {"number": 1, "text": "Fallback OCR text", "ocr": True, "lang": "eng"}
    ]
    assert [call[0] for call in calls] == ["ocrmypdf", "pdftoppm", "tesseract"]


def test_auto_reports_both_underlying_failures(monkeypatch):
    monkeypatch.setattr(ocr, "_pymupdf_module", lambda: None)

    def runner(argv, **kwargs):
        if argv[0] == "pdftoppm":
            return _completed(argv, stderr=b"render failed", returncode=1)
        return _completed(argv, stderr=f"{argv[0]} failed".encode(), returncode=1)

    with pytest.raises(ocr.OCRError, match="no OCR backend") as info:
        ocr.ocr_pdf(PDF, runner=runner)

    message = str(info.value)
    assert "ocrmypdf" in message
    assert "tesseract" in message
    assert "render failed" in message
    assert "install OCRmyPDF" in message
