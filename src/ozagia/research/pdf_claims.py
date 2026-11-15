"""Source-located claim candidates extracted from PDF text.

PDF text is lossy: equations, tables and layout are degraded. This module
therefore records statements as unverified candidates with method, confidence
and a source locator. It never treats a candidate as a proof, a formula
interpretation, or a truth assessment. Numeric citation hints are retained as
text and are not turned into ``cites`` edges.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from ozagia.research import paper, pdf
from ozagia.research import references as refs
from ozagia.research.graph import ResearchError

MAX_CLAIM_CHARS = 1200
DEFAULT_MAX_CLAIMS = 256


class PDFClaimError(ResearchError):
    """PDF claim extraction input or output is malformed."""


_FORMAL_KINDS = {
    "theorem": "theorem",
    "lemma": "theorem",
    "proposition": "theorem",
    "corollary": "theorem",
    "claim": "theorem",
    "definition": "definition",
    "hypothesis": "hypothesis",
    "conjecture": "hypothesis",
}
_SECTION_CONFIDENCE = {
    "theorem": 0.9,
    "definition": 0.85,
    "hypothesis": 0.8,
}
_FORMAL_HEADING_RE = re.compile(
    r"^\s*(?:(?:[0-9]+(?:\.[0-9]+)*)\.?\s+)?"
    r"(?P<kind>Theorem|Lemma|Proposition|Corollary|Definition|Claim|Hypothesis|Conjecture)"
    r"(?=[\s:.,(]|$)(?P<rest>.*)$",
    re.I,
)
_INLINE_NUMBERED_RE = re.compile(
    r"(?<![\w])(?P<kind>Theorem|Lemma|Proposition|Corollary|Definition|Claim|Hypothesis|Conjecture)"
    r"\s+(?P<number>[A-Z]?\.?\d+(?:\.\d+)*|[IVXLC]+)(?:[.:)])?(?=\s|$)",
    re.I,
)
_INLINE_UNNUMBERED_RE = re.compile(
    r"(?<![\w])(?P<kind>Theorem|Lemma|Proposition|Corollary|Definition|Claim|Hypothesis|Conjecture)"
    r"(?:\s*[.:]\s+|\s*\n\s+)(?=\S)",
    re.I,
)
_NUMBER_RE = re.compile(
    r"^(?P<number>[A-Z]?\.?\d+(?:\.\d+)*|[IVXLC]+)\b[.:)]?"
)
_PAGE_HEADINGS = (
    "abstract",
    "introduction",
    "background",
    "related work",
    "literature review",
    "materials and methods",
    "methods",
    "method",
    "methodology",
    "results and discussion",
    "results",
    "result",
    "findings",
    "discussion",
    "conclusions",
    "conclusion",
    "limitations",
    "future work",
    "references and notes",
    "references",
    "bibliography",
    "works cited",
    "literature cited",
    "acknowledgments",
    "acknowledgements",
    "acknowledgment",
    "acknowledgement",
    "funding",
    "appendix",
    "supplementary material",
)
_PAGE_HEADING_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*)\.?\s+)?"
    r"(?P<heading>" + "|".join(re.escape(item) for item in _PAGE_HEADINGS) + r")"
    r"\s*[.:]?\s*$",
    re.I,
)
_ABSTRACT_CUES = (
    "we show",
    "we find",
    "we found",
    "we demonstrate",
    "we demonstrated",
    "we report",
    "we reported",
    "we observe",
    "we observed",
    "results suggest",
    "findings suggest",
    "significantly",
    "associated with",
    "was associated",
    "were associated",
    "increase",
    "decrease",
    "we conclude",
)
_CONTRIBUTION_CUES = (
    "we propose",
    "we present",
    "our results",
    "this paper shows",
    "this paper presents",
    "we introduce",
    "we contribute",
)
_DEFINE_RE = re.compile(
    r"\bwe\s+define\s+(?P<term>[^.;:]{1,120}?)\s+as\s+"
    r"|\b(?P<term2>[A-Za-z][A-Za-z0-9 _-]{0,80}?)\s+is\s+defined\s+as\s+",
    re.I,
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_BRACKET_HINT_RE = re.compile(r"\[[0-9][0-9,\s–-]*\]")
_AUTHOR_YEAR_PAREN_RE = re.compile(
    r"\((?:[A-Z][^()]{0,120}?)(?:19|20)\d{2}[a-z]?(?:[^()]{0,30})\)"
)
_AUTHOR_YEAR_NAME_RE = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+(?:and|&)\s+[A-Z][a-z]{2,}|\s+et\s+al\.)?"
    r",?\s+(?:19|20)\d{2}[a-z]?\b"
)
_AUTHOR_YEAR_PAREN_NAME_RE = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+et\s+al\.)?\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)"
)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|doi:\s*\S+|10\.\d{4,9}/\S+", re.I)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


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
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if _clean(item)]
    return []


def _page_value(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _normalize_wording(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _citation_hints(text: str) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for pattern in (
        _BRACKET_HINT_RE,
        _AUTHOR_YEAR_PAREN_RE,
        _AUTHOR_YEAR_PAREN_NAME_RE,
        _AUTHOR_YEAR_NAME_RE,
    ):
        for match in pattern.finditer(text):
            hint = _clean(match.group(0))
            if hint:
                matches.append((match.start(), match.end(), hint))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str]] = []
    for start, end, hint in matches:
        if any(start >= left and end <= right for left, right, _ in selected):
            continue
        selected.append((start, end, hint))
    hints: list[str] = []
    for _, _, hint in selected:
        if hint not in hints:
            hints.append(hint)
    return hints


def _citation_or_url_only(text: str) -> bool:
    hints = _citation_hints(text)
    cleaned = _URL_RE.sub(" ", text)
    for hint in hints:
        cleaned = cleaned.replace(hint, " ")
    cleaned = re.sub(r"\b(?:et\s+al\.?|and|&)\b", " ", cleaned, flags=re.I)
    return not re.search(r"[A-Za-z]", cleaned)


def _heading_key(heading: str) -> str:
    key = _clean(heading).casefold()
    key = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", key)
    return key.strip(" .:;-")


def _skip_heading(heading: str) -> str | None:
    key = _heading_key(heading)
    if re.match(r"^(?:references?|bibliography|works\s+cited|literature\s+cited)\b", key):
        return "references"
    if re.match(r"^acknowledg", key):
        return "acknowledgements"
    return None


def _context_keys(heading: str) -> set[str]:
    key = _heading_key(heading)
    keys: set[str] = set()
    if key.startswith("abstract"):
        keys.add("abstract")
    if key.startswith("conclusion") or key.startswith("concluding"):
        keys.add("conclusion")
    if re.search(r"\bdiscussion\b", key) or re.search(r"\b(?:findings?|results?)\b", key):
        keys.add("discussion")
    if key.startswith("introduction"):
        keys.add("introduction")
    return keys


def _formal_heading_info(heading: str) -> dict[str, Any] | None:
    match = _FORMAL_HEADING_RE.match(heading.strip())
    if not match:
        return None
    kind = match.group("kind").casefold()
    rest = match.group("rest").strip()
    number = ""
    number_match = _NUMBER_RE.match(rest)
    if number_match:
        number = number_match.group("number")
        rest = rest[number_match.end():].strip()
    label = kind.title() + (f" {number}" if number else "")
    return {
        "kind": kind,
        "label": label,
        "number": number,
        "rest": rest,
        "claim_type": _FORMAL_KINDS[kind],
    }


def _looks_like_page_heading(heading: str) -> bool:
    stripped = heading.strip()
    if not stripped or len(stripped) > 160:
        return False
    return bool(_PAGE_HEADING_RE.match(stripped))


def _page_blocks(text: str, page_number: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    heading = ""
    block_start = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and _looks_like_page_heading(stripped):
            start, end = _trim_span(text, block_start, offset)
            if start < end or heading:
                blocks.append({
                    "heading": heading,
                    "text": text[start:end],
                    "page": page_number,
                    "xml_id": None,
                    "start_offset": start,
                    "allow_default_context": not heading,
                })
            heading = stripped
            block_start = offset + len(line)
        offset += len(line)
    start, end = _trim_span(text, block_start, offset)
    if start < end or heading:
        blocks.append({
            "heading": heading,
            "text": text[start:end],
            "page": page_number,
            "xml_id": None,
            "start_offset": start,
            "allow_default_context": not heading,
        })
    return blocks


def _section_blocks(sections: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, Mapping):
            raise PDFClaimError("PDF sections must be objects")
        heading = _clean(section.get("heading"))
        raw_text = _text(section.get("text"))
        start, end = _trim_span(raw_text, 0, len(raw_text))
        if start >= end and not heading:
            continue
        blocks.append({
            "heading": heading,
            "text": raw_text[start:end],
            "page": _page_value(section.get("page")),
            "xml_id": section.get("xml_id"),
            "start_offset": start,
            "allow_default_context": False,
        })
    return blocks


def _abstract_block(extraction: Mapping[str, Any]) -> dict[str, Any] | None:
    abstract = _text(extraction.get("abstract"))
    start, end = _trim_span(abstract, 0, len(abstract))
    if start >= end:
        return None
    return {
        "heading": "Abstract",
        "text": abstract[start:end],
        "page": None,
        "xml_id": None,
        "start_offset": start,
        "allow_default_context": False,
    }


def _blocks(extraction: Mapping[str, Any]) -> list[dict[str, Any]]:
    sections = extraction.get("sections")
    pages = extraction.get("pages")
    if sections is not None and not isinstance(sections, list):
        raise PDFClaimError("PDF sections must be a list")
    if pages is not None and not isinstance(pages, list):
        raise PDFClaimError("PDF pages must be a list")

    section_blocks = _section_blocks(sections or [])
    if any(block["text"].strip() for block in section_blocks):
        blocks = []
        for block in section_blocks:
            skip = _skip_heading(block["heading"])
            if skip is not None:
                continue
            blocks.append(block)
    else:
        blocks = []
        references_tail = False
        for page in pages or []:
            if not isinstance(page, Mapping):
                raise PDFClaimError("PDF pages must be objects")
            page_number = _page_value(page.get("number"))
            page_text = _text(page.get("text"))
            for block in _page_blocks(page_text, page_number):
                skip = _skip_heading(block["heading"])
                if skip == "references":
                    references_tail = True
                    continue
                if references_tail or skip is not None:
                    continue
                blocks.append(block)
    abstract_block = _abstract_block(extraction)
    if abstract_block is not None:
        blocks.append(abstract_block)
    return blocks


def _bounded_end(text: str, start: int, limit: int | None = None) -> int:
    maximum = min(len(text), start + (MAX_CLAIM_CHARS if limit is None else limit))
    chunk = text[start:maximum]
    blank = re.search(r"\n\s*\n", chunk)
    if blank:
        maximum = start + blank.start()
    return _trim_span(text, start, maximum)[1]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    position = 0
    for boundary in _SENTENCE_BOUNDARY_RE.finditer(text):
        start, end = _trim_span(text, position, boundary.start())
        if start < end:
            spans.append((start, end))
        position = boundary.end()
    start, end = _trim_span(text, position, len(text))
    if start < end:
        spans.append((start, end))
    return spans


def _inline_matches(text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for pattern in (_INLINE_NUMBERED_RE, _INLINE_UNNUMBERED_RE):
        for match in pattern.finditer(text):
            kind = match.group("kind")
            if not kind[:1].isupper():
                continue
            if kind.casefold() == "definition":
                tail = text[match.end():match.end() + 400]
                if re.search(r"\b(?:we\s+define|is\s+defined\s+as)\b", tail, re.I):
                    continue
            number = match.groupdict().get("number") or ""
            matches.append({
                "start": match.start(),
                "marker_end": match.end(),
                "kind": kind.casefold(),
                "number": number,
                "label": kind.title() + (f" {number}" if number else ""),
            })
    matches.sort(key=lambda item: (item["start"], item["marker_end"]))
    unique: list[dict[str, Any]] = []
    for match in matches:
        if unique and match["start"] < unique[-1]["marker_end"]:
            continue
        unique.append(match)
    return unique


def _claim(
    *,
    sha256: str,
    backend: str,
    math_degraded: bool,
    method: str,
    wording: str,
    raw_excerpt: str,
    claim_type: str,
    label: str,
    section: str,
    page: Any,
    xml_id: Any,
    start_offset: int,
    end_offset: int,
    confidence: float,
) -> dict[str, Any]:
    normalized = _normalize_wording(wording)
    candidate_id = "pdf-claim-" + hashlib.sha256(
        "\0".join(
            [
                sha256,
                method,
                "" if page is None else str(page),
                section,
                normalized,
            ]
        ).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "candidate_id": candidate_id,
        "wording": wording,
        "raw_excerpt": raw_excerpt,
        "claim_type": claim_type,
        "label": label,
        "section": section,
        "page": page,
        "source_locator": {
            "kind": "pdf",
            "sha256": sha256,
            "backend": backend,
            "page": page,
            "section": section,
            "xml_id": xml_id,
            "start_offset": start_offset,
            "end_offset": end_offset,
        },
        "extraction_method": method,
        "confidence": confidence,
        "citation_hints": _citation_hints(raw_excerpt),
        "math_degraded": math_degraded,
        "semantics_verified": False,
        "truth_assessed": False,
    }


def _add_candidate(
    candidates: list[dict[str, Any]],
    seen: set[str],
    candidate: dict[str, Any],
) -> None:
    normalized = _normalize_wording(candidate["wording"])
    if not normalized or normalized in seen:
        return
    if _citation_or_url_only(candidate["raw_excerpt"]):
        return
    seen.add(normalized)
    candidates.append(candidate)


def candidate_claims(extraction: Any, *, max_claims: int = DEFAULT_MAX_CLAIMS) -> dict[str, Any]:
    """Extract unverified claim candidates from a ``pdf.extract_pdf`` result.

    Structured sections are preferred; page text is used when sections are
    absent or empty. Each candidate has a stable id, method, confidence and a
    PDF source locator. References, bibliographies and acknowledgements are
    skipped. Output is bounded and never truth-assessed.
    """
    if not isinstance(extraction, Mapping):
        raise PDFClaimError("PDF extraction must be an object")
    if (
        not isinstance(max_claims, int)
        or isinstance(max_claims, bool)
        or max_claims < 0
    ):
        raise PDFClaimError("max_claims must be a nonnegative integer")

    sha256 = _clean(extraction.get("sha256"))
    backend = _clean(extraction.get("backend"))
    math_degraded = bool(extraction.get("degraded_math", True))
    blocks = _blocks(extraction)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for block in blocks:
        heading = block["heading"]
        if not heading:
            continue
        info = _formal_heading_info(heading)
        if info is None or not block["text"].strip():
            continue
        raw_start, raw_end = _trim_span(block["text"], 0, len(block["text"]))
        raw_end = _bounded_end(block["text"], raw_start)
        excerpt = block["text"][raw_start:raw_end]
        wording = _clean(excerpt)
        if not wording:
            continue
        _add_candidate(candidates, seen, _claim(
            sha256=sha256,
            backend=backend,
            math_degraded=math_degraded,
            method="section_formal_statement",
            wording=wording,
            raw_excerpt=excerpt,
            claim_type=info["claim_type"],
            label=info["label"],
            section=heading,
            page=block["page"],
            xml_id=block["xml_id"],
            start_offset=block["start_offset"] + raw_start,
            end_offset=block["start_offset"] + raw_end,
            confidence=_SECTION_CONFIDENCE[info["claim_type"]],
        ))

    for block in blocks:
        text = block["text"]
        heading_info = _formal_heading_info(block["heading"]) if block["heading"] else None
        matches = _inline_matches(text)
        for index, match in enumerate(matches):
            if (
                heading_info is not None
                and match["start"] == 0
                and match["label"] == heading_info["label"]
            ):
                continue
            marker_end = match["marker_end"]
            while marker_end < len(text) and text[marker_end].isspace():
                marker_end += 1
            next_start = matches[index + 1]["start"] if index + 1 < len(matches) else None
            body_start, _ = _trim_span(text, marker_end, len(text))
            if body_start >= len(text):
                continue
            body_end = _bounded_end(text, body_start)
            if next_start is not None and body_start < next_start < body_end:
                body_end = _trim_span(text, body_start, next_start)[1]
            raw_end = min(body_end, match["start"] + MAX_CLAIM_CHARS)
            body_end = min(body_end, raw_end)
            body = text[body_start:body_end]
            wording = _clean(body)
            if not wording:
                continue
            excerpt = text[match["start"]:raw_end]
            _add_candidate(candidates, seen, _claim(
                sha256=sha256,
                backend=backend,
                math_degraded=math_degraded,
                method="inline_formal_statement",
                wording=wording,
                raw_excerpt=excerpt,
                claim_type=_FORMAL_KINDS[match["kind"]],
                label=match["label"],
                section=block["heading"],
                page=block["page"],
                xml_id=block["xml_id"],
                start_offset=block["start_offset"] + match["start"],
                end_offset=block["start_offset"] + raw_end,
                confidence=0.6,
            ))

    for block in blocks:
        contexts = _context_keys(block["heading"])
        if not contexts.intersection({"abstract", "conclusion", "discussion"}):
            if not (block["allow_default_context"] and not contexts):
                continue
        for start, end in _sentence_spans(block["text"]):
            sentence = block["text"][start:end]
            if _citation_or_url_only(sentence):
                continue
            folded = sentence.casefold()
            if not any(cue in folded for cue in _ABSTRACT_CUES):
                continue
            claim_type = (
                "inference"
                if any(cue in folded for cue in (
                    "we conclude", "results suggest", "findings suggest", "suggest that"
                ))
                else "empirical_observation"
            )
            bounded_end = _bounded_end(block["text"], start)
            excerpt = block["text"][start:bounded_end]
            _add_candidate(candidates, seen, _claim(
                sha256=sha256,
                backend=backend,
                math_degraded=math_degraded,
                method="abstract_claim",
                wording=_clean(excerpt),
                raw_excerpt=excerpt,
                claim_type=claim_type,
                label="",
                section=block["heading"],
                page=block["page"],
                xml_id=block["xml_id"],
                start_offset=block["start_offset"] + start,
                end_offset=block["start_offset"] + bounded_end,
                confidence=0.4,
            ))

    for block in blocks:
        contexts = _context_keys(block["heading"])
        if not contexts.intersection({"abstract", "introduction", "conclusion"}):
            if not (block["allow_default_context"] and not contexts):
                continue
        for start, end in _sentence_spans(block["text"]):
            sentence = block["text"][start:end]
            if _citation_or_url_only(sentence):
                continue
            folded = sentence.casefold()
            if not any(cue in folded for cue in _CONTRIBUTION_CUES):
                continue
            bounded_end = _bounded_end(block["text"], start)
            excerpt = block["text"][start:bounded_end]
            _add_candidate(candidates, seen, _claim(
                sha256=sha256,
                backend=backend,
                math_degraded=math_degraded,
                method="contribution_claim",
                wording=_clean(excerpt),
                raw_excerpt=excerpt,
                claim_type="inference",
                label="",
                section=block["heading"],
                page=block["page"],
                xml_id=block["xml_id"],
                start_offset=block["start_offset"] + start,
                end_offset=block["start_offset"] + bounded_end,
                confidence=0.5,
            ))

    for block in blocks:
        sentences = _sentence_spans(block["text"])
        for match in _DEFINE_RE.finditer(block["text"]):
            span = next(
                (pair for pair in sentences if pair[0] <= match.start() < pair[1]),
                None,
            )
            if span is None:
                continue
            start, sentence_end = span
            bounded_end = _bounded_end(block["text"], start, sentence_end - start)
            excerpt = block["text"][start:bounded_end]
            _add_candidate(candidates, seen, _claim(
                sha256=sha256,
                backend=backend,
                math_degraded=math_degraded,
                method="definition",
                wording=_clean(excerpt),
                raw_excerpt=excerpt,
                claim_type="definition",
                label="Definition",
                section=block["heading"],
                page=block["page"],
                xml_id=block["xml_id"],
                start_offset=block["start_offset"] + start,
                end_offset=block["start_offset"] + bounded_end,
                confidence=0.6,
            ))

    truncated = len(candidates) > max_claims
    claims = candidates[:max_claims]
    methods: dict[str, int] = {}
    for claim in claims:
        method = claim["extraction_method"]
        methods[method] = methods.get(method, 0) + 1
    warnings = list(dict.fromkeys(
        item for item in _string_list(extraction.get("warnings")) if item
    ))
    if not blocks:
        warnings.append("PDF extraction contains no usable section or page text.")
    if not claims:
        warnings.append("No candidate claims were extracted from this PDF extraction.")
    if truncated:
        warnings.append(
            f"Claim extraction was truncated at max_claims={max_claims}."
        )
    return {
        "sha256": sha256,
        "backend": backend,
        "claims": claims,
        "count": len(claims),
        "total_candidates": len(candidates),
        "truncated": truncated,
        "methods": dict(sorted(methods.items())),
        "warnings": list(dict.fromkeys(warnings)),
        "truth_assessed": False,
    }


def preview_pdf_claims(
    graph: Any,
    *,
    root_id: str,
    path: str,
    pdf_extractor: Any = None,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    backend: str | None = None,
) -> dict[str, Any]:
    """Extract PDF claim candidates without writing to the research graph."""
    _, source_path = paper._root_file(graph, root_id, path)
    if source_path.suffix.lower() != ".pdf":
        raise PDFClaimError("PDF claim preview requires a selected .pdf source")
    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise PDFClaimError(f"cannot read selected PDF: {exc}") from exc
    extractor = pdf_extractor or pdf.extract_pdf
    extraction = extractor(data, backend=backend)
    if not isinstance(extraction, Mapping):
        raise PDFClaimError("PDF extractor must return an extraction object")
    extraction = dict(extraction)
    if not _clean(extraction.get("sha256")):
        extraction["sha256"] = hashlib.sha256(data).hexdigest()
    if not _clean(extraction.get("backend")):
        extraction["backend"] = _clean(backend)
    result = candidate_claims(extraction, max_claims=max_claims)
    sha256 = result["sha256"]
    backend_name = result["backend"]
    title = _clean(extraction.get("title"))
    authors = _string_list(extraction.get("authors"))
    sections = extraction.get("sections") or []
    pages = extraction.get("pages") or []
    references = extraction.get("references") or []
    extraction_warnings = list(dict.fromkeys(
        item for item in _string_list(extraction.get("warnings")) if item
    ))
    source = {
        "root_id": root_id,
        "path": path,
        "sha256": sha256,
        "backend": backend_name,
        "title": title,
        "authors": authors,
    }
    extraction_summary = {
        "format": _clean(extraction.get("format")) or "pdf",
        "sha256": sha256,
        "backend": backend_name,
        "title": title,
        "authors": authors,
        "warnings": extraction_warnings,
        "degraded_math": bool(extraction.get("degraded_math", True)),
        "page_count": len(pages) if isinstance(pages, list) else 0,
        "section_count": len(sections) if isinstance(sections, list) else 0,
        "reference_count": len(references) if isinstance(references, list) else 0,
        "truth_assessed": False,
    }
    return {
        "root_id": root_id,
        "path": path,
        "sha256": sha256,
        "backend": backend_name,
        "title": title,
        "authors": authors,
        "source": source,
        "extraction": extraction_summary,
        "claims": result["claims"],
        "count": result["count"],
        "truncated": result["truncated"],
        "methods": result["methods"],
        "warnings": list(result["warnings"]),
        "references": list(references) if isinstance(references, list) else [],
        "truth_assessed": False,
    }


def _existing_source(
    state: Mapping[str, Any],
    *,
    root_id: str,
    path: str,
    sha256: str,
) -> dict[str, Any] | None:
    return next(
        (
            node
            for node in state.get("nodes", [])
            if node.get("type") == "Source"
            and node.get("paper_import") is True
            and isinstance(node.get("locator"), Mapping)
            and node["locator"].get("root_id") == root_id
            and node["locator"].get("path") == path
            and node["locator"].get("sha256") == sha256
        ),
        None,
    )


def _source_claims(
    state: Mapping[str, Any],
    source_id: str,
) -> dict[str, dict[str, Any]]:
    linked = {
        edge.get("src")
        for edge in state.get("edges", [])
        if edge.get("type") == "cites" and edge.get("tgt") == source_id
    }
    return {
        node["candidate_id"]: node
        for node in state.get("nodes", [])
        if node.get("type") == "Claim"
        and node.get("id") in linked
        and node.get("candidate_id")
    }


def _reference_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping):
        return None
    reference = dict(entry)
    reference["metadata_status"] = "parsed_tei_unverified"
    keys = _string_list(reference.get("citation_keys"))
    if (
        not _clean(reference.get("title"))
        and not _clean(reference.get("raw"))
        and not _clean(reference.get("citation_key"))
        and not keys
    ):
        return None
    return reference


def ingest_pdf_claims(
    graph: Any,
    *,
    root_id: str,
    path: str,
    pdf_extractor: Any = None,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    backend: str | None = None,
    reviewer: str | None = None,
    resolve_citations: bool = True,
) -> dict[str, Any]:
    """Preview then record PDF Source, Claim and Reference candidates.

    Claims are unresolved source-located candidates. Citation hints remain
    text and never become ``cites`` edges to references. Re-running the same
    PDF hash reuses the Source and existing candidate claims.
    """
    preview = preview_pdf_claims(
        graph,
        root_id=root_id,
        path=path,
        pdf_extractor=pdf_extractor,
        max_claims=max_claims,
        backend=backend,
    )
    state = graph.state()
    reviewer_text = _clean(reviewer) if reviewer is not None else ""
    source = _existing_source(
        state,
        root_id=root_id,
        path=path,
        sha256=preview["sha256"],
    )
    duplicate = source is not None
    if source is None:
        root = next(
            (
                item
                for item in state.get("roots", [])
                if item.get("root_id") == root_id
            ),
            {},
        )
        repository = root.get("repository", root_id)
        source_fields: dict[str, Any] = {
            "name": Path(path).name,
            "paper_import": True,
            "sha256": preview["sha256"],
            "backend": preview["backend"],
            "title": preview["title"],
            "authors": preview["authors"],
            "locator": {
                "root_id": root_id,
                "repository": repository,
                "path": path,
                "sha256": preview["sha256"],
                "backend": preview["backend"],
                "kind": "pdf",
            },
        }
        if reviewer_text:
            source_fields["reviewer"] = reviewer_text
        source = graph.record("Source", **source_fields)
    else:
        repository = (
            source.get("locator", {}).get("repository", root_id)
            if isinstance(source.get("locator"), Mapping)
            else root_id
        )

    existing_claims = _source_claims(graph.state(), source["id"])
    claims: list[dict[str, Any]] = []
    for candidate in preview["claims"]:
        candidate_id = candidate["candidate_id"]
        existing = existing_claims.get(candidate_id)
        if existing is not None:
            claims.append(existing)
            continue
        fields: dict[str, Any] = {
            "wording": candidate["wording"],
            "raw_excerpt": candidate["raw_excerpt"],
            "claim_type": candidate["claim_type"],
            "status": "unresolved",
            "label": candidate["label"],
            "paper_label": candidate["label"],
            "section": candidate["section"],
            "page": candidate["page"],
            "source_locator": candidate["source_locator"],
            "extraction_method": candidate["extraction_method"],
            "confidence": candidate["confidence"],
            "citation_hints": candidate["citation_hints"],
            "math_degraded": candidate["math_degraded"],
            "parsed_assertion": True,
            "semantics_verified": False,
            "candidate_id": candidate_id,
            "scope": candidate["section"] or "PDF document",
            "author": "; ".join(preview["authors"]) or "unknown",
            "truth_assessed": False,
        }
        if reviewer_text:
            fields["reviewer"] = reviewer_text
        claim = graph.record("Claim", **fields)
        graph.link(
            claim["id"],
            "cites",
            source["id"],
            provenance={
                "kind": "pdf_claim_extraction",
                "candidate_id": candidate_id,
                "pdf_path": path,
                "pdf_sha256": preview["sha256"],
                "path": path,
                "sha256": preview["sha256"],
                "backend": preview["backend"],
                "extraction_method": candidate["extraction_method"],
            },
            rationale=(
                "The candidate wording was extracted from this PDF; this link "
                "locates text and does not assess truth."
            ),
        )
        existing_claims[candidate_id] = claim
        claims.append(claim)

    references: list[dict[str, Any]] = []
    seen_reference_ids: set[str] = set()
    for order, entry in enumerate(preview["references"], start=1):
        reference = _reference_entry(entry)
        if reference is None:
            continue
        existing = refs.find_reference(graph.state(), reference)
        if existing is None:
            recorded = refs.record_reference(
                graph,
                reference,
                provenance={
                    "kind": "pdf_tei_reference",
                    "pdf_path": path,
                    "pdf_sha256": preview["sha256"],
                    "path": path,
                    "sha256": preview["sha256"],
                    "pdf_backend": preview["backend"],
                    "metadata_status": "parsed_tei_unverified",
                },
                source_locator={
                    "kind": "pdf",
                    "root_id": root_id,
                    "repository": repository,
                    "path": path,
                    "sha256": preview["sha256"],
                },
            )
            node = recorded["reference"]
        else:
            node = existing
        entries = [
            item for item in (node.get("bibliography_indices") or [])
            if isinstance(item, Mapping)
        ]
        marker = {
            "sha256": preview["sha256"],
            "path": path,
            "index": order,
        }
        if not any(
            item.get("sha256") == preview["sha256"]
            and item.get("path") == path
            and item.get("index") == order
            for item in entries
        ):
            entries.append(marker)
        changes = {}
        if node.get("bibliography_index") != order:
            changes["bibliography_index"] = order
        if node.get("bibliography_source_sha256") != preview["sha256"]:
            changes["bibliography_source_sha256"] = preview["sha256"]
        if changes or entries != (node.get("bibliography_indices") or []):
            changes["bibliography_indices"] = entries
            node = graph.amend(node["id"], **changes)
        if node["id"] not in seen_reference_ids:
            references.append(node)
            seen_reference_ids.add(node["id"])

    citations: list[dict[str, Any]] = []
    if resolve_citations:
        from ozagia.research.citation_resolution import resolve_claim_citations
        for claim in claims:
            citations.append(resolve_claim_citations(graph, claim["id"], apply=True))

    return {
        "source": source,
        "claims": claims,
        "count": len(claims),
        "references": references,
        "extraction": {
            "sha256": preview["sha256"],
            "backend": preview["backend"],
            "warnings": list(preview["extraction"]["warnings"]),
            "degraded_math": preview["extraction"]["degraded_math"],
        },
        "warnings": list(preview["warnings"]),
        "citations": citations,
        "citation_count": sum(len(item.get("resolved", [])) for item in citations),
        "citation_unresolved": sum(
            len(item.get("unresolved_hints", [])) for item in citations),
        "duplicate": duplicate,
        "truth_assessed": False,
    }


__all__ = ["PDFClaimError", "candidate_claims", "preview_pdf_claims", "ingest_pdf_claims"]
