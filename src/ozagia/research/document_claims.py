"""Local document parsing and claim candidates for notes, HTML and EPUB.

The parsers are standard-library only and never read the network. Notes use
Markdown headings, HTML snapshots use an ``html.parser`` subclass, and EPUB
books use their ZIP/OPF spine. Claim extraction reuses the PDF candidate
extractor over the parsed sections and pages, with the document format as the
backend label. Every candidate remains an unverified, source-located statement;
this module never records a truth verdict and does not handle DRM.
"""
from __future__ import annotations

import codecs
import hashlib
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from ozagia.research.graph import ResearchError

DEFAULT_MAX_CLAIMS = 256
DEFAULT_MAX_EPUB_BYTES = 100_000_000
MAX_EPUB_ENTRIES = 4096
SUPPORTED_DOCUMENT_FORMATS = ("note", "html", "epub")

_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", re.I
)
_XML_DTD_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.I)
_XML_ENTITY_RE = re.compile(rb"<!\s*ENTITY", re.I)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WARNING_REPLACEMENTS = {
    "PDF extraction contains no usable section or page text.": (
        "Document contains no usable section or page text."
    ),
    "No candidate claims were extracted from this PDF extraction.": (
        "No candidate claims were extracted from this document."
    ),
}


class DocumentClaimError(ResearchError):
    """A note, HTML snapshot or EPUB could not be parsed safely."""


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def _clean_lines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if _clean(item)]
    return []


def _payload_bytes(value: Any, label: str) -> bytes:
    if isinstance(value, memoryview):
        value = value.tobytes()
    elif isinstance(value, bytearray):
        value = bytes(value)
    if not isinstance(value, bytes):
        raise DocumentClaimError(f"{label} data must be bytes")
    if not value:
        raise DocumentClaimError(f"{label} data is empty")
    return value


# ---------------------------------------------------------------------------
# Notes


def _note_sections(text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] = {
        "heading": "",
        "level": 0,
        "lines": [],
    }
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        body = "\n".join(current["lines"]).strip()
        heading = current["heading"]
        if heading or body:
            sections.append({
                "heading": heading,
                "text": body,
                "level": current["level"],
                "page": 1,
            })

    for line in text.split("\n"):
        fence = _FENCE_RE.match(line)
        if in_fence:
            current["lines"].append(line)
            close = _FENCE_CLOSE_RE.match(line)
            if (
                close is not None
                and close.group(1)[0] == fence_marker[0]
                and len(close.group(1)) >= len(fence_marker)
            ):
                in_fence = False
                fence_marker = ""
            continue
        if fence is not None:
            in_fence = True
            fence_marker = fence.group(1)
            current["lines"].append(line)
            continue
        match = _ATX_HEADING_RE.match(line)
        if match is None:
            current["lines"].append(line)
            continue
        flush()
        current = {
            "heading": match.group(2).strip(),
            "level": len(match.group(1)),
            "lines": [],
        }
    flush()
    return sections


def parse_note(text: str, *, title: str | None = None) -> dict[str, Any]:
    """Parse a note as one document with Markdown headings as sections.

    Plain text becomes one section. The title comes from ``title`` or the first
    level-1 Markdown heading. A note is personal context: no external evidence
    is inferred and no truth value is assessed.
    """
    if not isinstance(text, str):
        raise DocumentClaimError("note text must be a string")
    if not text.strip():
        raise DocumentClaimError("note text is empty")
    raw = text
    normalized = _clean_lines(text)
    sections = _note_sections(normalized)
    requested_title = _clean(title) if title is not None else ""
    heading_title = next(
        (
            section["heading"]
            for section in sections
            if section["level"] == 1 and section["heading"]
        ),
        "",
    ) or next(
        (section["heading"] for section in sections if section["heading"]),
        "",
    )
    resolved_title = requested_title or heading_title

    warnings: list[str] = []
    if not resolved_title:
        warnings.append("Note has no title.")
    if not any(section["text"].strip() for section in sections):
        warnings.append("Note contains no paragraph text.")
    return {
        "format": "note",
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "title": resolved_title,
        "sections": sections,
        "pages": [{"number": 1, "text": normalized.strip()}],
        "references": [],
        "warnings": warnings,
        "degraded_math": False,
        "truth_assessed": False,
    }


# ---------------------------------------------------------------------------
# HTML


class _HTMLDocumentParser(HTMLParser):
    """Collect headings, paragraphs, title and a flat page text from HTML."""

    _SKIP_TAGS = frozenset({"script", "style", "nav", "footer", "header"})
    _HEADING_TAGS = frozenset({"h1", "h2", "h3"})
    _BLOCK_TAGS = frozenset({
        "address", "article", "aside", "blockquote", "body", "br", "dd", "div",
        "dl", "dt", "figcaption", "figure", "hr", "html", "li", "main", "ol",
        "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
        "tr", "ul",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.sections: list[dict[str, Any]] = []
        self._title_depth = 0
        self._head_depth = 0
        self._skip_stack: list[str] = []
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._paragraph_parts: list[str] = []
        self._section: dict[str, Any] | None = None
        self._page_parts: list[str] = []

    @property
    def page_text(self) -> str:
        return "\n\n".join(self._page_parts).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self._skip_stack:
            if tag == self._skip_stack[-1] or tag in self._SKIP_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in self._SKIP_TAGS:
            self._flush_paragraph()
            self._skip_stack.append(tag)
            return
        if tag == "head":
            self._head_depth += 1
            return
        if tag == "title":
            self._title_depth += 1
            return
        if tag in self._HEADING_TAGS:
            self._flush_paragraph()
            self._finish_section()
            self._section = {
                "heading": "",
                "text": [],
                "level": int(tag[1]),
                "page": 1,
            }
            self._heading_level = int(tag[1])
            self._heading_parts = []
            return
        if tag in self._BLOCK_TAGS:
            self._flush_paragraph()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag == "title":
            if self._title_depth:
                self._title_depth -= 1
            return
        if tag == "head":
            if self._head_depth:
                self._head_depth -= 1
            return
        if tag in self._HEADING_TAGS:
            self._close_heading()
            return
        if tag in self._BLOCK_TAGS:
            self._flush_paragraph()

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        if self._title_depth:
            self.title_parts.append(data)
            return
        if self._head_depth:
            return
        if self._heading_level:
            self._heading_parts.append(data)
            return
        self._paragraph_parts.append(data)

    def finish(self) -> None:
        self.close()
        self._close_heading()
        self._flush_paragraph()
        self._finish_section()

    def _close_heading(self) -> None:
        if not self._heading_level:
            return
        heading = _clean(" ".join(self._heading_parts))
        if self._section is None:
            self._section = {
                "heading": "",
                "text": [],
                "level": self._heading_level,
                "page": 1,
            }
        self._section["heading"] = heading
        if heading:
            self._page_parts.append(heading)
        self._heading_level = 0
        self._heading_parts = []

    def _flush_paragraph(self) -> None:
        paragraph = _clean(" ".join(self._paragraph_parts))
        self._paragraph_parts = []
        if not paragraph:
            return
        if self._section is None:
            self._section = {
                "heading": "",
                "text": [],
                "level": 0,
                "page": 1,
            }
        self._section["text"].append(paragraph)
        self._page_parts.append(paragraph)

    def _finish_section(self) -> None:
        if self._section is None:
            return
        heading = self._section["heading"]
        text = "\n\n".join(self._section["text"]).strip()
        if heading or text:
            self.sections.append({
                "heading": heading,
                "text": text,
                "level": self._section["level"],
                "page": 1,
            })
        self._section = None


def _decode_html(payload: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    encoding = "utf-8"
    if payload.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        match = _META_CHARSET_RE.search(payload[:8192])
        if match is not None:
            declared = match.group(1).decode("ascii", errors="ignore")
            try:
                encoding = codecs.lookup(declared).name
            except LookupError:
                warnings.append(
                    f"HTML declares unknown charset {declared!r}; UTF-8 was used."
                )
    try:
        text = payload.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        warnings.append("HTML bytes could not be decoded as declared; UTF-8 was used.")
        text = payload.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        warnings.append(
            "HTML bytes were not valid for the declared encoding; "
            "replacement characters were used."
        )
    return text, warnings


def _parse_html_document(text: str) -> _HTMLDocumentParser:
    parser = _HTMLDocumentParser()
    parser.feed(text)
    parser.finish()
    return parser


def parse_html(
    data: bytes | str,
    *,
    url: str | None = None,
    accessed_at: str | None = None,
) -> dict[str, Any]:
    """Parse an HTML snapshot locally, without fetching or resolving the URL.

    The document title comes from ``<title>`` and h1-h3 headings become
    sections. Script, style, navigation, footer and header text is ignored.
    ``url`` and ``accessed_at`` are recorded as supplied; they are not checked.
    """
    if isinstance(data, str):
        payload = data.encode("utf-8")
        text = data
        decode_warnings: list[str] = []
    else:
        payload = _payload_bytes(data, "HTML")
        text, decode_warnings = _decode_html(payload)
    if not text.strip():
        raise DocumentClaimError("HTML data is empty")

    parser = _parse_html_document(text)
    title = _clean(" ".join(parser.title_parts))
    warnings = list(decode_warnings)
    if not title:
        fallback = next(
            (
                section["heading"]
                for section in parser.sections
                if section["level"] == 1 and section["heading"]
            ),
            "",
        )
        if fallback:
            title = fallback
            warnings.append(
                "HTML document has no <title>; used the first h1 heading."
            )
        else:
            warnings.append("HTML document has no title.")
    page_text = parser.page_text
    if not page_text:
        warnings.append("HTML document contains no readable text.")
    return {
        "format": "html",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "title": title,
        "url": _clean(url),
        "accessed_at": _clean(accessed_at),
        "sections": parser.sections,
        "pages": [{"number": 1, "text": page_text}],
        "references": [],
        "warnings": warnings,
        "degraded_math": False,
        "truth_assessed": False,
    }


# ---------------------------------------------------------------------------
# EPUB


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _xml_root(
    data: bytes,
    *,
    label: str,
) -> ET.Element:
    if _XML_DTD_RE.search(data):
        raise DocumentClaimError(
            f"{label} contains a DTD or entity declaration, which is not accepted"
        )
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentClaimError(f"invalid {label}: {exc}") from exc


def _safe_entry_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise DocumentClaimError("EPUB archive contains an unsafe empty entry name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise DocumentClaimError(f"EPUB archive contains an unsafe path: {name!r}")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part == ".." for part in parts):
        raise DocumentClaimError(f"EPUB archive contains an unsafe path: {name!r}")
    cleaned = posixpath.normpath(normalized)
    if cleaned in {".", ""}:
        raise DocumentClaimError(f"EPUB archive contains an unsafe path: {name!r}")
    return cleaned


def _resolve_href(base_dir: str, href: str) -> str:
    if not isinstance(href, str) or not href.strip():
        raise DocumentClaimError("EPUB manifest item is missing an href")
    split = urlsplit(href.strip())
    if split.scheme or split.netloc:
        raise DocumentClaimError(
            f"EPUB manifest href must be relative, not {href!r}"
        )
    target = unquote(split.path)
    if not target:
        raise DocumentClaimError(f"EPUB manifest href is empty: {href!r}")
    joined = (
        posixpath.normpath(posixpath.join(base_dir, target))
        if base_dir
        else posixpath.normpath(target)
    )
    return _safe_entry_name(joined)


def _find_entry(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo | None:
    try:
        return archive.getinfo(name)
    except KeyError:
        pass
    for info in archive.infolist():
        if _safe_entry_name(info.filename) == name:
            return info
    return None


def _read_entry(
    archive: zipfile.ZipFile,
    name: str,
    *,
    max_bytes: int,
    required: bool,
) -> bytes | None:
    info = _find_entry(archive, name)
    if info is None:
        if required:
            raise DocumentClaimError(f"EPUB is missing the required entry {name!r}")
        return None
    if info.flag_bits & 0x1:
        raise DocumentClaimError(
            f"EPUB entry {name!r} is encrypted; DRM is not supported"
        )
    if info.file_size > max_bytes:
        raise DocumentClaimError(
            f"EPUB entry {name!r} exceeds the {max_bytes} byte entry limit"
        )
    try:
        with archive.open(info) as handle:
            payload = handle.read(max_bytes + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise DocumentClaimError(
            f"cannot read EPUB entry {name!r}: {exc}"
        ) from exc
    if len(payload) > max_bytes:
        raise DocumentClaimError(
            f"EPUB entry {name!r} exceeds the {max_bytes} byte entry limit"
        )
    return payload


def _validate_archive(
    archive: zipfile.ZipFile,
    *,
    max_bytes: int,
) -> tuple[set[str], list[zipfile.ZipInfo]]:
    infos = archive.infolist()
    if len(infos) > MAX_EPUB_ENTRIES:
        raise DocumentClaimError(
            "EPUB archive has too many entries "
            f"({len(infos)} > {MAX_EPUB_ENTRIES})"
        )
    names: set[str] = set()
    total = 0
    for info in infos:
        names.add(_safe_entry_name(info.filename))
        total += info.file_size
    if total > max_bytes:
        raise DocumentClaimError(
            "EPUB uncompressed content exceeds the "
            f"{max_bytes} byte extraction limit"
        )
    return names, infos


def _epub_package(
    root: ET.Element,
) -> tuple[str, list[str], str, str, dict[str, Any], list[str]]:
    title = ""
    authors: list[str] = []
    language = ""
    identifier = ""
    for element in root.iter():
        local = _local(element.tag)
        if local == "title" and not title:
            title = _clean("".join(element.itertext()))
        elif local == "creator":
            creator = _clean("".join(element.itertext()))
            if creator and creator not in authors:
                authors.append(creator)
        elif local == "language" and not language:
            language = _clean("".join(element.itertext()))
        elif local == "identifier" and not identifier:
            identifier = _clean("".join(element.itertext()))

    manifest: dict[str, Any] = {}
    for element in root.iter():
        if _local(element.tag) != "item":
            continue
        item_id = element.attrib.get("id", "").strip()
        href = element.attrib.get("href", "").strip()
        if item_id and href:
            manifest[item_id] = {
                "href": href,
                "media_type": element.attrib.get("media-type", "").strip().casefold(),
            }

    spine: list[str] = []
    saw_spine = False
    for element in root.iter():
        if _local(element.tag) != "spine":
            continue
        saw_spine = True
        for child in element:
            if _local(child.tag) != "itemref":
                continue
            idref = child.attrib.get("idref", "").strip()
            linear = child.attrib.get("linear", "yes").strip().casefold()
            if idref and linear != "no":
                spine.append(idref)

    warnings: list[str] = []
    if not saw_spine:
        warnings.append("EPUB package has no spine element.")
    if not spine:
        warnings.append("EPUB spine is empty or non-linear; reading manifest XHTML items.")
        spine = [
            item_id
            for item_id, item in manifest.items()
            if _is_xhtml(item)
        ]
    return title, authors, language, identifier, {"manifest": manifest, "spine": spine}, warnings


def _is_xhtml(item: Mapping[str, Any]) -> bool:
    media_type = _clean(item.get("media_type")).casefold()
    href = _clean(item.get("href")).casefold()
    if media_type in {
        "application/xhtml+xml",
        "text/html",
        "application/xml",
        "text/xml",
    }:
        return True
    return href.endswith((".xhtml", ".html", ".htm"))


def _epub_chapter(
    payload: bytes,
    *,
    path: str,
    page_number: int,
) -> tuple[str, list[dict[str, Any]], str, list[str]]:
    text, warnings = _decode_html(payload)
    parser = _parse_html_document(text)
    chapter_title = _clean(" ".join(parser.title_parts))
    if not chapter_title:
        chapter_title = next(
            (section["heading"] for section in parser.sections if section["heading"]),
            "",
        )
    if not chapter_title:
        chapter_title = f"Chapter {page_number}"
    sections = [
        {
            "heading": section["heading"] or chapter_title,
            "text": section["text"],
            "level": section["level"],
            "page": page_number,
            "xml_id": path,
        }
        for section in parser.sections
    ]
    page_text = parser.page_text
    if not sections and page_text:
        sections = [{
            "heading": chapter_title,
            "text": page_text,
            "level": 0,
            "page": page_number,
            "xml_id": path,
        }]
    if not page_text:
        page_text = chapter_title
    return chapter_title, sections, page_text, warnings


def parse_epub(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_EPUB_BYTES,
) -> dict[str, Any]:
    """Parse EPUB bytes through the ZIP container, OPF package and spine.

    Archive paths, XML DTD/entity declarations, entry counts and uncompressed
    sizes are bounded before text is read. Chapters are parsed as XHTML and
    become sections in spine order. Encryption/DRM is reported but not handled.
    """
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes <= 0
    ):
        raise DocumentClaimError("max_bytes must be a positive integer")
    payload = _payload_bytes(data, "EPUB")
    if len(payload) > max_bytes:
        raise DocumentClaimError(
            f"EPUB exceeds the {max_bytes} byte extraction limit"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise DocumentClaimError(
            f"EPUB data is not a valid ZIP archive: {exc}"
        ) from exc

    with archive:
        names, infos = _validate_archive(archive, max_bytes=max_bytes)
        warnings: list[str] = []
        if "META-INF/encryption.xml" in names or any(
            info.flag_bits & 0x1 for info in infos
        ):
            warnings.append(
                "EPUB encryption/DRM is not supported; encrypted chapters may be missing."
            )

        container_data = _read_entry(
            archive,
            "META-INF/container.xml",
            max_bytes=max_bytes,
            required=True,
        )
        if container_data is None:
            raise DocumentClaimError("EPUB container.xml could not be read")
        container = _xml_root(container_data, label="EPUB container")
        rootfile = next(
            (
                element.attrib.get("full-path", "").strip()
                for element in container.iter()
                if _local(element.tag) == "rootfile"
                and element.attrib.get("full-path", "").strip()
            ),
            "",
        )
        if not rootfile:
            raise DocumentClaimError(
                "EPUB container does not declare an OPF package rootfile"
            )
        opf_path = _resolve_href("", rootfile)
        opf_data = _read_entry(archive, opf_path, max_bytes=max_bytes, required=True)
        if opf_data is None:
            raise DocumentClaimError(f"EPUB package {opf_path!r} could not be read")
        package = _xml_root(opf_data, label="EPUB package")
        (
            title,
            authors,
            language,
            identifier,
            package_entries,
            package_warnings,
        ) = _epub_package(package)
        warnings.extend(package_warnings)
        manifest = package_entries["manifest"]
        spine = package_entries["spine"]
        base_dir = posixpath.dirname(opf_path)

        sections: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        for idref in spine:
            item = manifest.get(idref)
            if item is None:
                warnings.append(
                    f"EPUB spine references missing manifest item {idref!r}."
                )
                continue
            if not _is_xhtml(item):
                warnings.append(
                    f"EPUB spine item {idref!r} is not XHTML and was skipped."
                )
                continue
            path = _resolve_href(base_dir, item["href"])
            chapter_data = _read_entry(
                archive,
                path,
                max_bytes=max_bytes,
                required=False,
            )
            if chapter_data is None:
                warnings.append(
                    f"EPUB spine item {idref!r} is missing from the archive."
                )
                continue
            if _XML_ENTITY_RE.search(chapter_data):
                raise DocumentClaimError(
                    f"EPUB chapter {path!r} contains an entity declaration, "
                    "which is not accepted"
                )
            page_number = len(pages) + 1
            (
                chapter_title,
                chapter_sections,
                page_text,
                chapter_warnings,
            ) = _epub_chapter(
                chapter_data,
                path=path,
                page_number=page_number,
            )
            warnings.extend(chapter_warnings)
            if not chapter_sections and not page_text.strip():
                warnings.append(f"EPUB chapter {chapter_title!r} has no readable text.")
                continue
            sections.extend(chapter_sections)
            pages.append({
                "number": page_number,
                "text": page_text,
            })

        if not title:
            fallback = next(
                (section["heading"] for section in sections if section["heading"]),
                "",
            )
            if fallback:
                title = fallback
                warnings.append(
                    "EPUB package has no title; used the first chapter heading."
                )
            else:
                warnings.append("EPUB package has no title.")
        if not authors:
            warnings.append("EPUB package has no creator metadata.")
        if not sections:
            warnings.append("EPUB contains no readable text.")
        return {
            "format": "epub",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "title": _clean(title),
            "authors": authors,
            "language": language,
            "identifier": identifier,
            "sections": sections,
            "pages": pages,
            "references": [],
            "warnings": list(dict.fromkeys(warnings)),
            "degraded_math": False,
            "truth_assessed": False,
        }


# ---------------------------------------------------------------------------
# Claim candidates


def _document_pages(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    pages = document.get("pages", [])
    if pages is None:
        return []
    if not isinstance(pages, list):
        raise DocumentClaimError("document pages must be a list")
    if any(not isinstance(page, Mapping) for page in pages):
        raise DocumentClaimError("document pages must be objects")
    return [dict(page) for page in pages]


def _document_sections(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    sections = document.get("sections", [])
    if sections is None:
        return []
    if not isinstance(sections, list):
        raise DocumentClaimError("document sections must be a list")
    if any(not isinstance(section, Mapping) for section in sections):
        raise DocumentClaimError("document sections must be objects")
    return [dict(section) for section in sections]


def _document_authors(document: Mapping[str, Any]) -> list[str]:
    authors = document.get("authors", [])
    if authors is None:
        return []
    if isinstance(authors, str):
        return _string_list(authors)
    if not isinstance(authors, (list, tuple)):
        raise DocumentClaimError("document authors must be a list of strings")
    return _string_list(authors)


def candidate_claims_from_document(
    document: Mapping[str, Any],
    *,
    max_claims: int = DEFAULT_MAX_CLAIMS,
) -> dict[str, Any]:
    """Extract unverified claim candidates from a parsed note, HTML or EPUB.

    The document is converted to the PDF extractor's extraction shape with the
    document format as its backend, then handed to
    ``pdf_claims.candidate_claims`` lazily. Candidate ids and source locators
    are relabelled with the document format. The result never assesses truth.
    """
    if not isinstance(document, Mapping):
        raise DocumentClaimError("document must be an object")
    if (
        not isinstance(max_claims, int)
        or isinstance(max_claims, bool)
        or max_claims < 0
    ):
        raise DocumentClaimError("max_claims must be a nonnegative integer")
    document_format = _clean(document.get("format")).casefold()
    if document_format not in SUPPORTED_DOCUMENT_FORMATS:
        raise DocumentClaimError(
            "document format must be one of "
            + ", ".join(SUPPORTED_DOCUMENT_FORMATS)
        )
    sha256 = _clean(document.get("sha256")).casefold()
    if not _SHA256_RE.fullmatch(sha256):
        raise DocumentClaimError(
            "document sha256 must be a 64-character hexadecimal digest"
        )
    title = document.get("title", "")
    if title is not None and not isinstance(title, str):
        raise DocumentClaimError("document title must be a string")
    authors = _document_authors(document)
    references = document.get("references", [])
    if references is None:
        references = []
    if not isinstance(references, list):
        raise DocumentClaimError("document references must be a list")
    if any(not isinstance(item, Mapping) for item in references):
        raise DocumentClaimError("document references must be objects")
    warnings = document.get("warnings", [])
    if warnings is None:
        warnings = []
    if not isinstance(warnings, list):
        raise DocumentClaimError("document warnings must be a list")
    degraded_math = document.get("degraded_math", False)
    if not isinstance(degraded_math, bool):
        raise DocumentClaimError("document degraded_math must be a boolean")

    extraction = {
        "format": document_format,
        "sha256": sha256,
        "backend": document_format,
        "pages": _document_pages(document),
        "sections": _document_sections(document),
        "title": _clean(title),
        "authors": authors,
        "abstract": "",
        "references": [dict(item) for item in references],
        "warnings": _string_list(warnings),
        "degraded_math": degraded_math,
        "truth_assessed": False,
    }

    from ozagia.research import pdf_claims

    try:
        result = pdf_claims.candidate_claims(extraction, max_claims=max_claims)
    except pdf_claims.PDFClaimError as exc:
        raise DocumentClaimError(
            f"document claim extraction failed: {exc}"
        ) from exc

    claims = result["claims"]
    for claim in claims:
        candidate_id = _text(claim.get("candidate_id"))
        if candidate_id.startswith("pdf-claim-"):
            claim["candidate_id"] = (
                f"{document_format}-claim-"
                f"{candidate_id[len('pdf-claim-'):]}"
            )
        locator = claim.get("source_locator")
        if isinstance(locator, Mapping):
            locator = dict(locator)
            locator["kind"] = document_format
            locator["backend"] = document_format
            claim["source_locator"] = locator

    result_warnings = [
        _WARNING_REPLACEMENTS.get(warning, warning)
        for warning in result["warnings"]
    ]
    return {
        "claims": claims,
        "count": result["count"],
        "truncated": result["truncated"],
        "methods": result["methods"],
        "warnings": list(dict.fromkeys(result_warnings)),
        "format": document_format,
        "truth_assessed": False,
    }


__all__ = [
    "DocumentClaimError",
    "candidate_claims_from_document",
    "parse_epub",
    "parse_html",
    "parse_note",
]
