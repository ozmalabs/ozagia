"""Optional local OCR for image-only or sparse PDFs.

OCR is chosen only when a PDF has little or no text layer, or when a caller
asks for it explicitly. Backends are local executables: OCRmyPDF, or Tesseract
with PyMuPDF/Poppler for page rendering. Recognized text is marked as OCR and
remains an unverified evidence aid; this module does not extract claims or
assess truth.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from ozagia.research.graph import ResearchError

MAX_PDF_BYTES = 50_000_000
SUPPORTED_BACKENDS = ("auto", "ocrmypdf", "tesseract")
COMMAND_TIMEOUT = 300.0
_IMAGE_NUMBER = re.compile(r"-(\d+)$")


class OCRError(ResearchError):
    """OCR could not run or returned unusable output."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _stream_bytes(value: Any) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return b""


def _stream_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, memoryview):
        return value.tobytes().decode("utf-8", errors="replace")
    return str(value)


def _validate(data: Any, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise OCRError("max_bytes must be a positive integer")
    limit = min(max_bytes, MAX_PDF_BYTES)
    if isinstance(data, memoryview):
        data = data.tobytes()
    elif isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise OCRError("PDF data must be bytes")
    if not data:
        raise OCRError("PDF data is empty")
    if len(data) > limit:
        raise OCRError(f"PDF exceeds the {limit} byte OCR limit")
    if not data.startswith(b"%PDF-"):
        raise OCRError("data does not start with the %PDF- magic bytes")
    return data


def _normalize_backend(backend: str) -> str:
    if not isinstance(backend, str) or not backend.strip():
        raise OCRError("OCR backend must be auto, ocrmypdf, or tesseract")
    name = backend.strip().casefold().replace("_", "-")
    if name not in SUPPORTED_BACKENDS:
        raise OCRError("OCR backend must be auto, ocrmypdf, or tesseract")
    return name


def _positive_dpi(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise OCRError("dpi must be a positive number")
    return value


def _validate_max_pages(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OCRError("max_pages must be a positive integer or None")
    return value


def _pymupdf_module() -> Any | None:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz
        except ImportError:
            return None
        return fitz
    return pymupdf


def _run(argv: list[str], *, runner: Callable[..., Any] | None) -> Any:
    try:
        if runner is None:
            return subprocess.run(
                argv, capture_output=True, check=False, timeout=COMMAND_TIMEOUT
            )
        return runner(argv, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise OCRError(
            f"{argv[0]} timed out after {COMMAND_TIMEOUT:g} seconds"
        ) from exc
    except OSError as exc:
        raise OCRError(f"cannot run {argv[0]}: {exc}") from exc


def _completed_parts(value: Any) -> tuple[Any, Any, Any]:
    if isinstance(value, (bytes, bytearray, memoryview, str)):
        return value, b"", 0
    return (
        getattr(value, "stdout", ""),
        getattr(value, "stderr", ""),
        getattr(value, "returncode", 0),
    )


def _require_success(value: Any, command: str) -> str:
    stdout, stderr, returncode = _completed_parts(value)
    if returncode not in (0, None):
        detail = _clean(_stream_text(stderr))[:300]
        suffix = f": {detail}" if detail else ""
        raise OCRError(f"{command} failed with exit code {returncode}{suffix}")
    return _stream_text(stdout)


def _result_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview, str)):
        return _stream_bytes(value)
    return _stream_bytes(getattr(value, "stdout", b""))


def _output_pdf(value: Any, output: Path) -> bytes:
    content = b""
    try:
        if output.is_file():
            content = output.read_bytes()
    except OSError as exc:
        raise OCRError(f"cannot read ocrmypdf output: {exc}") from exc
    if content.startswith(b"%PDF-"):
        return content
    if content:
        raise OCRError("ocrmypdf wrote output that is not a PDF")
    raw = _result_bytes(value)
    if raw.startswith(b"%PDF-"):
        return raw
    raise OCRError("ocrmypdf produced no output PDF")


def _write_pdf(path: Path, data: bytes) -> None:
    try:
        path.write_bytes(data)
    except OSError as exc:
        raise OCRError(f"cannot write temporary OCR PDF: {exc}") from exc


def _text_pages(text: str) -> tuple[list[dict[str, Any]], bool]:
    parts = text.split("\f")
    separated = len(parts) > 1
    if separated and not parts[-1].strip():
        parts.pop()
    if not parts:
        parts = [""]
    pages = [
        {"number": number, "text": part.strip("\n")}
        for number, part in enumerate(parts, start=1)
    ]
    return pages, separated


def _pages_pymupdf(
    data: bytes, module: Any, max_pages: int | None
) -> tuple[list[dict[str, Any]], bool]:
    try:
        document = module.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - reported as a typed OCR error
        raise OCRError(f"PyMuPDF could not open the PDF for OCR: {exc}") from exc
    pages: list[dict[str, Any]] = []
    truncated = False
    try:
        for number, page in enumerate(document, start=1):
            if max_pages is not None and number > max_pages:
                truncated = True
                break
            try:
                text = _stream_text(page.get_text("text")).rstrip()
            except Exception as exc:  # noqa: BLE001 - reported as a typed OCR error
                raise OCRError(
                    f"PyMuPDF text extraction failed for page {number}: {exc}"
                ) from exc
            pages.append({"number": number, "text": text})
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()
    return pages, truncated


def _pages_pdftotext(
    path: Path, *, runner: Callable[..., Any] | None, max_pages: int | None
) -> tuple[list[dict[str, Any]], bool, bool]:
    if runner is None and shutil.which("pdftotext") is None:
        raise OCRError(
            "pdftotext is not installed; install Poppler "
            "(for example 'apt install poppler-utils') or install PyMuPDF "
            "with 'pip install pymupdf'."
        )
    text = _require_success(
        _run(["pdftotext", "-layout", str(path), "-"], runner=runner), "pdftotext"
    )
    pages, separated = _text_pages(text)
    truncated = False
    if max_pages is not None and len(pages) > max_pages:
        pages = pages[:max_pages]
        truncated = True
    return pages, truncated, separated


def _image_page_key(path: Path) -> tuple[int, Any]:
    match = _IMAGE_NUMBER.search(path.stem)
    if match:
        return (0, int(match.group(1)))
    return (1, path.name)


def _render_pages(
    data: bytes,
    *,
    source: Path,
    directory: Path,
    dpi: float,
    max_pages: int | None,
    module: Any | None,
    runner: Callable[..., Any] | None,
) -> tuple[list[tuple[int, Path]], bool]:
    if module is not None:
        try:
            document = module.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001 - reported as a typed OCR error
            raise OCRError(f"PyMuPDF could not open the PDF for OCR: {exc}") from exc
        images: list[tuple[int, Path]] = []
        truncated = False
        try:
            for number, page in enumerate(document, start=1):
                if max_pages is not None and number > max_pages:
                    truncated = True
                    break
                image = directory / f"page-{number}.png"
                try:
                    pixmap = page.get_pixmap(dpi=dpi)
                    save = getattr(pixmap, "save", None)
                    if callable(save):
                        save(str(image))
                    else:
                        image.write_bytes(pixmap.tobytes("png"))
                except Exception as exc:  # noqa: BLE001 - reported as a typed OCR error
                    raise OCRError(
                        f"PyMuPDF page rendering failed for page {number}: {exc}"
                    ) from exc
                images.append((number, image))
        finally:
            close = getattr(document, "close", None)
            if callable(close):
                close()
        return images, truncated

    if runner is None and shutil.which("pdftoppm") is None:
        raise OCRError(
            "pdftoppm is not installed; install Poppler "
            "(for example 'apt install poppler-utils') or install PyMuPDF "
            "with 'pip install pymupdf'."
        )
    prefix = directory / "page"
    _require_success(
        _run(
            ["pdftoppm", "-r", str(dpi), "-png", str(source), str(prefix)],
            runner=runner,
        ),
        "pdftoppm",
    )
    candidates = sorted(directory.glob("page-*.png"), key=_image_page_key)
    truncated = False
    if max_pages is not None and len(candidates) > max_pages:
        candidates = candidates[:max_pages]
        truncated = True
    return list(enumerate(candidates, start=1)), truncated


def _page_records(
    pages: Iterable[Mapping[str, Any]], lang: str
) -> list[dict[str, Any]]:
    return [
        {
            "number": page["number"],
            "text": str(page.get("text") or ""),
            "ocr": True,
            "lang": lang,
        }
        for page in pages
    ]


def _finish_warnings(
    warnings: list[str],
    pages: list[dict[str, Any]],
    *,
    truncated: bool,
    max_pages: int | None,
) -> list[str]:
    if truncated and max_pages is not None:
        warnings.append(f"OCR was limited to the first {max_pages} pages.")
    if not pages:
        warnings.append("OCR produced no pages.")
    elif not any(str(page.get("text") or "").strip() for page in pages):
        warnings.append(
            "OCR produced no text; the PDF may be blank or the OCR language may be wrong."
        )
    return warnings


def _result(
    *, backend: str, pages: list[dict[str, Any]], warnings: Iterable[str]
) -> dict[str, Any]:
    return {
        "format": "pdf",
        "backend": backend,
        "pages": pages,
        "warnings": list(warnings),
        "degraded_math": True,
        "truth_assessed": False,
    }


def _ocr_ocrmypdf(
    data: bytes,
    *,
    lang: str,
    max_pages: int | None,
    module: Any | None,
    runner: Callable[..., Any] | None,
) -> dict[str, Any]:
    if runner is None and shutil.which("ocrmypdf") is None:
        raise OCRError(
            "ocrmypdf is not installed; install OCRmyPDF "
            "(for example 'pip install ocrmypdf') or select backend='tesseract'."
        )
    warnings = [
        "OCRmyPDF text is machine-recognized; characters, equations, tables "
        "and layout may be incorrect.",
        "OCRmyPDF keeps existing text layers and only OCRs pages without usable text.",
        "OCR output is an evidence aid; verify quotations against the page image.",
    ]
    with tempfile.TemporaryDirectory(prefix="ozagia-ocr-") as directory:
        root = Path(directory)
        source = root / "input.pdf"
        output = root / "output.pdf"
        _write_pdf(source, data)
        completed = _run(
            [
                "ocrmypdf",
                "--skip-text",
                "--output-type",
                "pdf",
                str(source),
                str(output),
            ],
            runner=runner,
        )
        _require_success(completed, "ocrmypdf")
        ocr_data = _output_pdf(completed, output)
        _write_pdf(output, ocr_data)
        if module is not None:
            pages, truncated = _pages_pymupdf(ocr_data, module, max_pages)
        else:
            pages, truncated, separated = _pages_pdftotext(
                output, runner=runner, max_pages=max_pages
            )
            if not separated:
                warnings.append(
                    "pdftotext returned no page separators; OCR text is "
                    "reported as a single page."
                )
    records = _page_records(pages, lang)
    _finish_warnings(warnings, records, truncated=truncated, max_pages=max_pages)
    return _result(backend="ocr:ocrmypdf", pages=records, warnings=warnings)


def _ocr_tesseract(
    data: bytes,
    *,
    lang: str,
    dpi: float,
    max_pages: int | None,
    module: Any | None,
    runner: Callable[..., Any] | None,
) -> dict[str, Any]:
    if runner is None and shutil.which("tesseract") is None:
        raise OCRError(
            "tesseract is not installed; install Tesseract "
            "(for example 'apt install tesseract-ocr') or use OCRmyPDF."
        )
    warnings = [
        "Tesseract OCR text is machine-recognized; characters, equations, "
        "tables and layout may be incorrect.",
        f"Pages were rendered at {float(dpi):g} dpi.",
        "OCR output is an evidence aid; verify quotations against the page image.",
    ]
    with tempfile.TemporaryDirectory(prefix="ozagia-ocr-") as directory:
        root = Path(directory)
        source = root / "input.pdf"
        _write_pdf(source, data)
        images, truncated = _render_pages(
            data,
            source=source,
            directory=root,
            dpi=dpi,
            max_pages=max_pages,
            module=module,
            runner=runner,
        )
        pages: list[dict[str, Any]] = []
        for number, image in images:
            text = _require_success(
                _run(
                    ["tesseract", str(image), "stdout", "-l", lang],
                    runner=runner,
                ),
                "tesseract",
            ).strip("\n")
            pages.append({"number": number, "text": text})
            if not text.strip():
                warnings.append(f"tesseract returned no text for page {number}.")
    records = _page_records(pages, lang)
    _finish_warnings(warnings, records, truncated=truncated, max_pages=max_pages)
    return _result(backend="ocr:tesseract", pages=records, warnings=warnings)


def _ocr_auto(
    data: bytes,
    *,
    lang: str,
    dpi: float,
    max_pages: int | None,
    module: Any | None,
    runner: Callable[..., Any] | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if runner is not None or shutil.which("ocrmypdf") is not None:
        try:
            return _ocr_ocrmypdf(
                data,
                lang=lang,
                max_pages=max_pages,
                module=module,
                runner=runner,
            )
        except OCRError as exc:
            reasons.append(f"ocrmypdf: {exc}")
    else:
        reasons.append("ocrmypdf is not installed")

    has_tesseract = runner is not None or shutil.which("tesseract") is not None
    has_renderer = (
        runner is not None
        or module is not None
        or shutil.which("pdftoppm") is not None
    )
    if has_tesseract and has_renderer:
        try:
            return _ocr_tesseract(
                data,
                lang=lang,
                dpi=dpi,
                max_pages=max_pages,
                module=module,
                runner=runner,
            )
        except OCRError as exc:
            reasons.append(f"tesseract: {exc}")
    elif not has_tesseract:
        reasons.append("tesseract is not installed")
    else:
        reasons.append(
            "tesseract has no page renderer; install PyMuPDF or Poppler pdftoppm"
        )

    raise OCRError(
        "no OCR backend is available: install OCRmyPDF "
        "('pip install ocrmypdf') or Tesseract with Poppler "
        "('apt install tesseract-ocr poppler-utils') or PyMuPDF "
        "('pip install pymupdf'). Attempts: " + "; ".join(reasons)
    )


def text_is_sparse(
    pages: Iterable[Mapping[str, Any]], min_chars: int = 20
) -> bool:
    """Return true when the pages total fewer than ``min_chars`` stripped characters.

    ``pages`` is an iterable of mappings with at least a ``text`` field. Empty
    pages and whitespace-only pages count as no text.
    """
    if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars < 0:
        raise OCRError("min_chars must be a non-negative integer")
    total = 0
    for page in pages:
        if not isinstance(page, Mapping):
            raise OCRError("pages must contain mappings with a text field")
        total += len(str(page.get("text") or "").strip())
        if total >= min_chars:
            return False
    return total < min_chars


def ocr_pdf(
    data: Any,
    *,
    backend: str = "auto",
    lang: str = "eng",
    dpi: float = 300,
    runner: Callable[..., Any] | None = None,
    pymupdf_module: Any | None = None,
    max_pages: int | None = None,
    max_bytes: int = MAX_PDF_BYTES,
) -> dict[str, Any]:
    """OCR PDF bytes with a local backend and return page-located text.

    ``backend`` may be ``auto``, ``ocrmypdf`` or ``tesseract``. ``auto`` tries
    OCRmyPDF first, then Tesseract, and reports both underlying failures when
    neither works. ``runner`` is called as ``runner(argv, timeout=...)`` and may
    return a ``subprocess.CompletedProcess``, bytes, or text; it lets callers
    test or run the backends without a real tool installation. ``pymupdf_module``
    may supply an already-imported PyMuPDF module.

    Page records are 1-based and carry ``ocr=True`` and the OCR language. The
    result never extracts claims, assesses truth, or creates graph edges.
    """
    payload = _validate(data, max_bytes)
    selected = _normalize_backend(backend)
    dpi_value = _positive_dpi(dpi)
    max_page_value = _validate_max_pages(max_pages)
    if not isinstance(lang, str) or not lang.strip():
        raise OCRError("OCR language must be a non-empty Tesseract language code")
    language = lang.strip()
    if runner is not None and not callable(runner):
        raise OCRError("runner must be callable")
    module = pymupdf_module if pymupdf_module is not None else _pymupdf_module()

    if selected == "ocrmypdf":
        return _ocr_ocrmypdf(
            payload,
            lang=language,
            max_pages=max_page_value,
            module=module,
            runner=runner,
        )
    if selected == "tesseract":
        return _ocr_tesseract(
            payload,
            lang=language,
            dpi=dpi_value,
            max_pages=max_page_value,
            module=module,
            runner=runner,
        )
    return _ocr_auto(
        payload,
        lang=language,
        dpi=dpi_value,
        max_pages=max_page_value,
        module=module,
        runner=runner,
    )
