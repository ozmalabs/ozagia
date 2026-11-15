"""PDF text extraction for selected or fetched research sources.

Backends are chosen in this order: an explicitly named backend, GROBID when a
URL is configured, optional PyMuPDF, then the Poppler ``pdftotext`` executable.
GROBID returns structured TEI; the local backends return page-located plain
text. None of them reconstruct layout or preserve equations, so results are
marked ``degraded_math``. This module does not extract claims, assess truth, or
create ``supports`` edges.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from ozagia.research import tei
from ozagia.research.fetch import USER_AGENT
from ozagia.research.graph import ResearchError

MAX_PDF_BYTES = 50_000_000
GROBID_PATH = "/api/processFulltextDocument"
SUPPORTED_BACKENDS = ("grobid", "pymupdf", "pdftotext", "ocr")
_BACKEND_ALIASES = {
    "fitz": "pymupdf",
    "poppler": "pdftotext",
    "pdftotext-layout": "pdftotext",
    "tesseract": "ocr",
    "ocrmypdf": "ocr",
}


class PDFError(ResearchError):
    """A PDF could not be read safely with the selected backend."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _validate(data: Any) -> tuple[bytes, str]:
    if isinstance(data, memoryview):
        data = data.tobytes()
    elif isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise PDFError("PDF data must be bytes")
    if not data:
        raise PDFError("PDF data is empty")
    if len(data) > MAX_PDF_BYTES:
        raise PDFError(f"PDF exceeds the {MAX_PDF_BYTES} byte extraction limit")
    if not data.startswith(b"%PDF-"):
        raise PDFError("data does not start with the %PDF- magic bytes")
    return data, hashlib.sha256(data).hexdigest()


def _normalize_backend(backend: str) -> str:
    if not isinstance(backend, str) or not backend.strip():
        raise PDFError("PDF backend must be grobid, pymupdf, pdftotext, or ocr")
    name = backend.strip().casefold().replace("_", "-")
    name = _BACKEND_ALIASES.get(name, name)
    if name not in SUPPORTED_BACKENDS:
        raise PDFError("PDF backend must be grobid, pymupdf, pdftotext, or ocr")
    return name


def _grobid_endpoint(grobid_url: str | None) -> str:
    base = (grobid_url or os.environ.get("OZAGIA_GROBID_URL") or "").strip()
    if not base:
        raise PDFError("GROBID backend needs grobid_url or OZAGIA_GROBID_URL")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise PDFError("GROBID URL must be an absolute http(s) URL with a host")
    if parts.username or parts.password:
        raise PDFError("GROBID URL must not contain credentials")
    path = parts.path.rstrip("/")
    if not path.endswith(GROBID_PATH):
        path = f"{path}{GROBID_PATH}"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _multipart(data: bytes) -> tuple[bytes, str]:
    boundary = f"ozagia-{hashlib.sha256(data).hexdigest()[:32]}"
    while boundary.encode("ascii") in data:
        boundary += "0"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="input"; filename="paper.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + data + tail, boundary


def _http_post(
    url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
) -> dict[str, Any]:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            content = response.read(MAX_PDF_BYTES + 1)
            if len(content) > MAX_PDF_BYTES:
                raise PDFError(f"GROBID response exceeds {MAX_PDF_BYTES} bytes")
            return {
                "content": content,
                "status": getattr(response, "status", 200),
                "media_type": response.headers.get_content_type(),
                "final_url": response.geturl(),
            }
    except PDFError:
        raise
    except HTTPError as exc:
        raise PDFError(f"GROBID returned HTTP {exc.code} for {url}") from exc
    except Exception as exc:  # noqa: BLE001 - reported as a typed PDF error
        raise PDFError(f"GROBID request failed for {url}: {exc}") from exc


def _response_content(response: Any) -> bytes:
    content: Any = response
    status: Any = None
    if isinstance(response, Mapping):
        content = response.get("content")
        status = response.get("status")
    if isinstance(status, int) and not 200 <= status < 300:
        raise PDFError(f"GROBID returned HTTP {status}")
    if isinstance(content, str):
        content = content.encode("utf-8")
    if isinstance(content, (bytearray, memoryview)):
        content = bytes(content)
    if not isinstance(content, bytes):
        raise PDFError("GROBID post must return TEI bytes or a response mapping with content")
    return content


def _result(
    *,
    backend: str,
    digest: str,
    pages: list[dict[str, Any]],
    title: str = "",
    authors: list[str] | None = None,
    abstract: str = "",
    sections: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "format": "pdf",
        "sha256": digest,
        "backend": backend,
        "pages": pages,
        "title": _clean(title),
        "authors": list(authors or []),
        "abstract": _clean(abstract),
        "sections": list(sections or []),
        "references": list(references or []),
        "citations": list(citations or []),
        "warnings": list(warnings or []),
        "degraded_math": True,
        "truth_assessed": False,
    }


def _extract_grobid(
    data: bytes,
    digest: str,
    *,
    grobid_url: str | None,
    post: Callable[..., Any] | None,
    timeout: float,
) -> dict[str, Any]:
    endpoint = _grobid_endpoint(grobid_url)
    body, boundary = _multipart(data)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/tei+xml, application/xml, text/xml",
        "User-Agent": USER_AGENT,
    }
    sender = post or _http_post
    try:
        response = sender(endpoint, body=body, headers=headers, timeout=timeout)
    except PDFError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a typed PDF error
        raise PDFError(f"GROBID request failed: {exc}") from exc
    content = _response_content(response)
    try:
        parsed = tei.parse_tei(content)
    except tei.TEIError as exc:
        raise PDFError(f"GROBID returned invalid TEI: {exc}") from exc
    warnings = [
        "GROBID returns structured text; equation, table and figure layout is degraded.",
        "GROBID TEI has no page numbers; use section ids or citation quotes as locators.",
        "GROBID bibliography entries are parsed metadata, not verified publisher records.",
    ]
    if not parsed["sections"]:
        warnings.append("GROBID returned no section divisions.")
    if not parsed["title"]:
        warnings.append("GROBID returned no document title.")
    return _result(
        backend="grobid",
        digest=digest,
        pages=[],
        title=parsed["title"],
        authors=parsed["authors"],
        abstract=parsed["abstract"],
        sections=parsed["sections"],
        references=parsed["references"],
        citations=parsed["citations"],
        warnings=warnings,
    )


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


def _extract_pymupdf(data: bytes, digest: str, module: Any) -> dict[str, Any]:
    try:
        document = module.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - reported as a typed PDF error
        raise PDFError(f"PyMuPDF could not open the PDF: {exc}") from exc
    warnings = [
        "PyMuPDF returns plain text; table columns, figures and equations are degraded.",
    ]
    try:
        metadata = document.metadata or {}
        title = _clean(metadata.get("title", ""))
        pages = [
            {"number": number, "text": (page.get_text("text") or "").rstrip()}
            for number, page in enumerate(document, start=1)
        ]
    except Exception as exc:  # noqa: BLE001 - reported as a typed PDF error
        raise PDFError(f"PyMuPDF text extraction failed: {exc}") from exc
    finally:
        document.close()
    if not pages:
        warnings.append("PyMuPDF reported no pages.")
    if not any(page["text"].strip() for page in pages):
        warnings.append(
            "PyMuPDF extracted no text; the PDF may be image-only (OCR is not implemented)."
        )
    return _result(
        backend="pymupdf", digest=digest, pages=pages, title=title, warnings=warnings,
    )


def _stream_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


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


def _extract_pdftotext(
    data: bytes,
    digest: str,
    *,
    runner: Callable[..., Any] | None,
    timeout: float,
) -> dict[str, Any]:
    executable = shutil.which("pdftotext")
    if runner is None and executable is None:
        raise PDFError(
            "pdftotext is not installed; install Poppler (for example "
            "'apt install poppler-utils') or install PyMuPDF with 'pip install pymupdf'."
        )
    with tempfile.TemporaryDirectory(prefix="ozagia-pdf-") as directory:
        source = Path(directory) / "input.pdf"
        try:
            source.write_bytes(data)
        except OSError as exc:
            raise PDFError(f"cannot write temporary PDF: {exc}") from exc
        argv = [executable or "pdftotext", "-layout", str(source), "-"]
        try:
            if runner is None:
                completed = subprocess.run(
                    argv, capture_output=True, check=False, timeout=timeout
                )
            else:
                completed = runner(argv, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise PDFError(f"pdftotext timed out after {timeout} seconds") from exc
        except OSError as exc:
            raise PDFError(f"cannot run pdftotext: {exc}") from exc
    if isinstance(completed, bytes):
        stdout, stderr, returncode = completed, b"", 0
    elif isinstance(completed, str):
        stdout, stderr, returncode = completed, "", 0
    else:
        stdout = getattr(completed, "stdout", "")
        stderr = getattr(completed, "stderr", "")
        returncode = getattr(completed, "returncode", 0)
    if returncode not in (0, None):
        detail = _clean(_stream_text(stderr))[:300]
        suffix = f": {detail}" if detail else ""
        raise PDFError(f"pdftotext failed with exit code {returncode}{suffix}")
    pages, separated = _text_pages(_stream_text(stdout))
    warnings = [
        "pdftotext -layout returns plain text; column order, tables and equations are degraded.",
    ]
    if not separated:
        warnings.append("pdftotext returned no page separators; text is reported as a single page.")
    if not any(page["text"].strip() for page in pages):
        warnings.append(
            "pdftotext extracted no text; the PDF may be image-only (OCR is not implemented)."
        )
    return _result(backend="pdftotext", digest=digest, pages=pages, warnings=warnings)


def _extract_ocr(
    data: bytes,
    digest: str,
    *,
    lang: str,
    dpi: int,
    runner: Callable[..., Any] | None,
    max_pages: int | None,
) -> dict[str, Any]:
    from ozagia.research import ocr
    result = ocr.ocr_pdf(
        data,
        backend="auto",
        lang=lang,
        dpi=dpi,
        runner=runner,
        max_pages=max_pages,
    )
    return _result(
        backend=str(result.get("backend") or "ocr"),
        digest=digest,
        pages=list(result.get("pages") or []),
        warnings=list(result.get("warnings") or []),
    )


def _maybe_ocr(
    data: bytes,
    digest: str,
    result: dict[str, Any],
    *,
    ocr_mode: bool | str,
    lang: str,
    dpi: int,
    runner: Callable[..., Any] | None,
    max_pages: int | None,
) -> dict[str, Any]:
    if ocr_mode is False:
        return result
    if ocr_mode not in {True, "auto"}:
        raise PDFError("ocr must be true, false, or 'auto'")
    from ozagia.research.ocr import OCRError, text_is_sparse
    if not text_is_sparse(result.get("pages") or []):
        return result
    try:
        ocr_result = _extract_ocr(
            data, digest, lang=lang, dpi=dpi,
            runner=runner, max_pages=max_pages,
        )
    except OCRError as exc:
        result["warnings"] = [
            *result.get("warnings", []),
            f"OCR was not applied: {exc}",
        ]
        result["ocr"] = {"attempted": True, "status": "unavailable", "reason": str(exc)}
        return result
    merged = dict(result)
    merged["pages"] = ocr_result["pages"]
    merged["backend"] = ocr_result["backend"]
    merged["warnings"] = [
        *result.get("warnings", []),
        *ocr_result.get("warnings", []),
    ]
    merged["ocr"] = {
        "attempted": True,
        "status": "applied",
        "backend": ocr_result["backend"],
        "lang": lang,
        "dpi": dpi,
    }
    return merged


def extract_pdf(
    data: bytes,
    *,
    backend: str | None = None,
    grobid_url: str | None = None,
    runner: Callable[..., Any] | None = None,
    post: Callable[..., Any] | None = None,
    timeout: float = 60.0,
    ocr: bool | str = "auto",
    ocr_lang: str = "eng",
    ocr_dpi: int = 300,
    ocr_runner: Callable[..., Any] | None = None,
    ocr_max_pages: int | None = None,
) -> dict[str, Any]:
    """Extract page-located text from PDF bytes without assessing claims.

    ``backend`` may be ``grobid``, ``pymupdf`` or ``pdftotext``. Without it,
    GROBID is used when ``grobid_url`` or ``OZAGIA_GROBID_URL`` is set, then
    PyMuPDF when importable, then ``pdftotext``. Page records have 1-based
    ``number`` and ``text`` fields. References are populated only by GROBID.

    ``post`` is called as ``post(url, body=..., headers=..., timeout=...)``
    and may return TEI bytes or a response mapping with ``content`` (and an
    optional ``status``). ``runner`` is called as ``runner(argv, timeout=...)``
    and may return a ``subprocess.CompletedProcess``, bytes, or text. Both are
    injection points for tests and offline operation; neither performs a
    network call by itself.

    ``ocr`` is ``"auto"`` by default: an extraction with empty or sparse text
    is retried with ``research.ocr``. Set it to ``False`` to disable OCR or
    ``True`` to allow it explicitly. ``ocr_runner`` is the injectable OCR
    subprocess runner; ``backend="ocr"`` forces OCR.
    """
    payload, digest = _validate(data)
    if timeout <= 0:
        raise PDFError("timeout must be positive")
    if ocr not in {True, False, "auto"}:
        raise PDFError("ocr must be true, false, or 'auto'")

    if backend is not None:
        selected = _normalize_backend(backend)
        if selected == "ocr":
            return _extract_ocr(
                payload, digest, lang=ocr_lang, dpi=ocr_dpi,
                runner=ocr_runner, max_pages=ocr_max_pages,
            )
        if selected == "grobid":
            result = _extract_grobid(
                payload, digest, grobid_url=grobid_url, post=post, timeout=timeout
            )
        elif selected == "pymupdf":
            module = _pymupdf_module()
            if module is None:
                raise PDFError(
                    "PyMuPDF is not installed; install it with 'pip install pymupdf' "
                    "or select backend='pdftotext'."
                )
            result = _extract_pymupdf(payload, digest, module)
        else:
            result = _extract_pdftotext(payload, digest, runner=runner, timeout=timeout)
        return _maybe_ocr(
            payload, digest, result, ocr_mode=ocr, lang=ocr_lang, dpi=ocr_dpi,
            runner=ocr_runner, max_pages=ocr_max_pages,
        )

    configured = (grobid_url or os.environ.get("OZAGIA_GROBID_URL") or "").strip()
    if configured:
        result = _extract_grobid(
            payload, digest, grobid_url=grobid_url, post=post, timeout=timeout
        )
    else:
        module = _pymupdf_module()
        if module is not None:
            result = _extract_pymupdf(payload, digest, module)
        elif runner is not None or shutil.which("pdftotext"):
            result = _extract_pdftotext(payload, digest, runner=runner, timeout=timeout)
        else:
            raise PDFError(
                "no PDF backend is available: set OZAGIA_GROBID_URL to a GROBID service, "
                "install PyMuPDF ('pip install pymupdf'), or install Poppler pdftotext."
            )
    return _maybe_ocr(
        payload, digest, result, ocr_mode=ocr, lang=ocr_lang, dpi=ocr_dpi,
        runner=ocr_runner, max_pages=ocr_max_pages,
    )
