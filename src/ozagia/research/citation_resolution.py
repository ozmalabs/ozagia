"""Best-effort resolution of citation hints to recorded ``Reference`` nodes.

Resolution produces candidate links, not evidence: a DOI/arXiv match is an
exact identifier hit, a numeric bracket depends on a PDF's bibliography order,
and an author-year match is a conservative name/year guess. Numeric and
author-year matches require review. Unmatched hints stay unresolved, and this
module never creates ``supports`` or ``opposes`` edges or assesses truth.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError

PROVENANCE_KIND = "pdf_citation_resolution"
DEFAULT_THRESHOLD = 0.6
MAX_NUMERIC_RANGE = 500

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>\[\]]+", re.I)
_ARXIV_PREFIX_RE = re.compile(
    r"(?:\barxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)"
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})(?:v\d+)?)",
    re.I,
)
_ARXIV_BARE_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$", re.I)
_NUMERIC_RE = re.compile(r"\[(?P<body>\d[0-9,\s;\u2013\u2014-]*)\]")
_NAME = r"[A-Z][A-Za-z'’\-]+"
_NAMES = rf"{_NAME}(?:\s+(?:(?:et\s+al\.?)|(?:(?:and|&)\s+{_NAME})))?"
_AUTHOR_PAREN_YEAR_RE = re.compile(
    rf"(?P<names>{_NAMES})\s*\(\s*(?P<year>(?:19|20)\d{{2}})[a-z]?\s*\)"
)
_AUTHOR_COMMA_YEAR_RE = re.compile(
    rf"(?P<names>{_NAMES})\s*,\s*(?P<year>(?:19|20)\d{{2}})[a-z]?(?![0-9])"
)
_EXPLICIT_AUTHOR_YEAR_RE = re.compile(
    rf"^\s*\(?\s*(?P<names>{_NAMES})\s*,?\s*\(?\s*"
    rf"(?P<year>(?:19|20)\d{{2}})[a-z]?\s*\)?\s*$"
)


class CitationResolutionError(ResearchError):
    """A citation-resolution request was malformed or unusable."""


@dataclass(frozen=True)
class _PreparedReference:
    reference_id: str
    citation_key: str
    keys: tuple[str, ...]
    identifiers: Mapping[str, str]
    bibliography_index: int | None
    year: str
    surnames: tuple[str, ...]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise CitationResolutionError("citation resolution needs a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    nodes = state.get("nodes", [])
    if not isinstance(nodes, list):
        raise CitationResolutionError("graph state needs a nodes array")
    return [
        dict(node) for node in nodes
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _edges(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    edges = state.get("edges", [])
    if not isinstance(edges, list):
        raise CitationResolutionError("graph state needs an edges array")
    return [dict(edge) for edge in edges if isinstance(edge, Mapping)]


def _node_by_id(nodes: Iterable[Mapping[str, Any]], node_id: str) -> dict[str, Any] | None:
    return next((dict(node) for node in nodes if node.get("id") == node_id), None)


def _claim_node(
    state: Mapping[str, Any], claim_id: Any
) -> dict[str, Any]:
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise CitationResolutionError("claim_id must be a non-empty string")
    claim = _node_by_id(_nodes(state), claim_id)
    if claim is None:
        raise CitationResolutionError(f"unknown claim id: {claim_id}")
    if claim.get("type") != "Claim":
        raise CitationResolutionError(f"{claim_id!r} is not a Claim node")
    return claim


def _threshold(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CitationResolutionError("threshold must be a number between 0 and 1")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise CitationResolutionError("threshold must be between 0 and 1")
    return number


def _normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _surnames(authors: Any) -> tuple[str, ...]:
    if isinstance(authors, str):
        values = [authors]
    elif isinstance(authors, (list, tuple)):
        values = list(authors)
    else:
        values = []
    result: list[str] = []
    for author in values:
        text = _clean(author)
        if not text:
            continue
        if "," in text:
            surname = text.split(",", 1)[0]
        else:
            tokens = text.split()
            surname = tokens[-1] if tokens else ""
        normalized = _normalize_name(surname)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _reference_year(reference: Mapping[str, Any]) -> str:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(reference.get("year") or ""))
    return match.group(0) if match else ""


def _bibliography_index(reference: Mapping[str, Any]) -> int | None:
    value = reference.get("bibliography_index")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _reference_keys(reference: Mapping[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []
    single = _clean(reference.get("citation_key"))
    if single:
        keys.append(single)
    raw = reference.get("citation_keys")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        seen = {value.casefold() for value in keys}
        for item in raw:
            key = _clean(item)
            if key and key.casefold() not in seen:
                keys.append(key)
                seen.add(key.casefold())
    return tuple(keys)


def _identifiers(reference: Mapping[str, Any]) -> dict[str, str]:
    identifiers = dict(refs.normalize_identifiers(reference.get("identifiers")))
    doi = refs.normalize_doi(reference.get("doi"))
    arxiv = refs.normalize_arxiv(reference.get("arxiv"))
    if doi and not identifiers.get("doi"):
        identifiers["doi"] = doi
    if arxiv and not identifiers.get("arxiv"):
        identifiers["arxiv"] = arxiv
    return identifiers


def _prepare_references(references: Iterable[Any]) -> list[_PreparedReference]:
    prepared: list[_PreparedReference] = []
    seen: set[str] = set()
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        reference_id = _clean(reference.get("id"))
        if not reference_id or reference_id in seen:
            continue
        seen.add(reference_id)
        keys = _reference_keys(reference)
        prepared.append(_PreparedReference(
            reference_id=reference_id,
            citation_key=keys[0] if keys else "",
            keys=keys,
            identifiers=_identifiers(reference),
            bibliography_index=_bibliography_index(reference),
            year=_reference_year(reference),
            surnames=_surnames(reference.get("authors")),
        ))
    prepared.sort(key=lambda item: item.reference_id)
    return prepared


def _candidate(
    reference: _PreparedReference,
    *,
    hint: str,
    method: str,
    confidence: float,
    requires_review: bool,
    matched_by: str,
    ambiguous_refs: list[str] | None = None,
) -> dict[str, Any]:
    candidate = {
        "hint": hint,
        "method": method,
        "confidence": confidence,
        "requires_review": requires_review,
        "reference_id": reference.reference_id,
        "citation_key": reference.citation_key,
        "matched_by": matched_by,
    }
    if ambiguous_refs:
        candidate["ambiguous_refs"] = list(ambiguous_refs)
    return candidate


def _hit_candidates(
    hits: Iterable[_PreparedReference],
    *,
    hint: str,
    method: str,
    matched_by: str,
    unique_confidence: float,
    unique_requires_review: bool,
    ambiguous_confidence: float | None = None,
    ambiguous_requires_review: bool = True,
) -> list[dict[str, Any]]:
    ordered = sorted(hits, key=lambda item: item.reference_id)
    if not ordered:
        return []
    if len(ordered) == 1:
        return [_candidate(
            ordered[0],
            hint=hint,
            method=method,
            confidence=unique_confidence,
            requires_review=unique_requires_review,
            matched_by=matched_by,
        )]
    reference_ids = [item.reference_id for item in ordered]
    confidence = (
        unique_confidence if ambiguous_confidence is None else ambiguous_confidence
    )
    return [
        _candidate(
            item,
            hint=hint,
            method=method,
            confidence=confidence,
            requires_review=ambiguous_requires_review,
            matched_by=matched_by,
            ambiguous_refs=reference_ids,
        )
        for item in ordered
    ]


def _author_parts(names: str) -> tuple[str, ...] | None:
    cleaned = re.sub(r"\s*\bet\s+al\.?", "", names, flags=re.I).strip()
    parts = [
        part.strip()
        for part in re.split(r"\s+(?:and|&)\s+", cleaned, flags=re.I)
        if part.strip()
    ]
    surnames: list[str] = []
    for part in parts:
        tokens = part.split()
        if not tokens:
            continue
        normalized = _normalize_name(tokens[-1])
        if normalized and normalized not in surnames:
            surnames.append(normalized)
    return tuple(surnames) if surnames else None


def _parse_author_year(value: str) -> tuple[tuple[str, ...], str] | None:
    match = _EXPLICIT_AUTHOR_YEAR_RE.match(_clean(value))
    if match is None:
        return None
    names = _author_parts(match.group("names"))
    if names is None:
        return None
    return names, match.group("year")


def _expand_numeric_hint(value: str) -> list[int]:
    match = _NUMERIC_RE.fullmatch(_clean(value))
    if match is None:
        return []
    numbers: list[int] = []

    def add(number: int) -> None:
        if number not in numbers:
            numbers.append(number)

    for part in re.split(r"[,;]", match.group("body")):
        part = part.strip()
        if not part:
            continue
        range_match = re.fullmatch(r"(\d+)\s*[-\u2013\u2014]\s*(\d+)", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start <= end and end - start <= MAX_NUMERIC_RANGE:
                for number in range(start, end + 1):
                    add(number)
            continue
        if part.isdigit():
            add(int(part))
    return numbers


def _citation_key_forms(value: str) -> set[str]:
    text = _clean(value)
    forms = {text.casefold()} if text else set()
    if text.startswith("@"):
        forms.add(text[1:].strip().casefold())
    if text.startswith("{") and text.endswith("}"):
        forms.add(text[1:-1].strip().casefold())
    markdown = re.fullmatch(r"\[(@[^\]]+)\]", text)
    if markdown:
        for part in re.split(r"[;,]", markdown.group(1)):
            key = part.strip().lstrip("@")
            if key:
                forms.add(key.casefold())
    latex = re.fullmatch(
        r"\\(?:cite|citep|citet|citeauthor|citeyear|parencite|textcite|autocite)"
        r"\*?(?:\[[^\]]*\])*\{([^}]+)\}",
        text,
    )
    if latex:
        for key in re.split(r"[,;]", latex.group(1)):
            if key.strip():
                forms.add(key.strip().casefold())
    return {form for form in forms if form}


def _match_hint(
    raw_hint: Mapping[str, Any],
    prepared: list[_PreparedReference],
) -> list[dict[str, Any]]:
    kind = raw_hint.get("kind")
    hint = _clean(raw_hint.get("hint"))
    if not hint:
        return []
    if kind == "doi":
        doi = refs.normalize_doi(hint)
        if not doi:
            return []
        return _hit_candidates(
            (item for item in prepared if item.identifiers.get("doi") == doi),
            hint=hint,
            method="doi",
            matched_by="doi",
            unique_confidence=0.95,
            unique_requires_review=False,
            ambiguous_requires_review=True,
        )
    if kind == "arxiv":
        arxiv = refs.normalize_arxiv(hint)
        if not arxiv:
            return []
        return _hit_candidates(
            (item for item in prepared if item.identifiers.get("arxiv") == arxiv),
            hint=hint,
            method="arxiv",
            matched_by="arxiv",
            unique_confidence=0.95,
            unique_requires_review=False,
            ambiguous_requires_review=True,
        )
    if kind == "numeric":
        candidates: list[dict[str, Any]] = []
        for number in _expand_numeric_hint(hint):
            candidates.extend(_hit_candidates(
                (item for item in prepared if item.bibliography_index == number),
                hint=hint,
                method="numeric",
                matched_by="bibliography_index",
                unique_confidence=0.7,
                unique_requires_review=True,
                ambiguous_confidence=0.35,
                ambiguous_requires_review=True,
            ))
        return candidates
    if kind == "author_year":
        parsed = _parse_author_year(hint)
        if parsed is None:
            return []
        surnames, year = parsed
        hits = []
        for item in prepared:
            if item.year != year or not item.surnames:
                continue
            if item.surnames[0] != surnames[0]:
                continue
            if any(surname not in item.surnames for surname in surnames[1:]):
                continue
            hits.append(item)
        return _hit_candidates(
            hits,
            hint=hint,
            method="author_year",
            matched_by="author_year",
            unique_confidence=0.6,
            unique_requires_review=True,
            ambiguous_confidence=0.35,
            ambiguous_requires_review=True,
        )
    if kind == "citation_key":
        forms = _citation_key_forms(hint)
        if not forms:
            return []
        hits = [
            item for item in prepared
            if any(key.casefold() in forms for key in item.keys)
        ]
        return _hit_candidates(
            hits,
            hint=hint,
            method="citation_key",
            matched_by="citation_key",
            unique_confidence=0.9,
            unique_requires_review=False,
            ambiguous_confidence=0.35,
            ambiguous_requires_review=True,
        )
    return []


def _trim_literal(value: str) -> str:
    return _clean(value).rstrip(".,;:!?)]}\"'")


def _expanded_span(text: str, match: re.Match[str]) -> tuple[int, int]:
    start, end = match.span()
    if start > 0 and text[start - 1] == "(":
        closing = re.match(r"\s*\)", text[end:])
        if closing is not None:
            return start - 1, end + closing.end()
    return start, end


def _scan_text_hints(
    text: str,
    prepared: list[_PreparedReference],
) -> list[dict[str, Any]]:
    if not text:
        return []
    records: list[dict[str, Any]] = []
    doi_spans: list[tuple[int, int]] = []
    for match in _DOI_RE.finditer(text):
        hint = _trim_literal(match.group(0))
        if refs.normalize_doi(hint):
            doi_spans.append(match.span())
            records.append({"hint": hint, "kind": "doi", "start": match.start()})

    prefixed_arxiv: set[str] = set()
    for match in _ARXIV_PREFIX_RE.finditer(text):
        hint = _trim_literal(match.group(0))
        arxiv = refs.normalize_arxiv(hint)
        if arxiv:
            prefixed_arxiv.add(arxiv)
            records.append({"hint": hint, "kind": "arxiv", "start": match.start()})

    known_arxiv = sorted({
        arxiv
        for item in prepared
        for arxiv in [item.identifiers.get("arxiv")]
        if arxiv and _ARXIV_BARE_RE.fullmatch(arxiv)
    })
    for arxiv in known_arxiv:
        if arxiv in prefixed_arxiv:
            continue
        pattern = re.compile(
            r"(?<![0-9A-Za-z.])" + re.escape(arxiv) + r"(?:v\d+)?(?![0-9A-Za-z.])",
            re.I,
        )
        for match in pattern.finditer(text):
            if any(
                match.start() >= start and match.end() <= end
                for start, end in doi_spans
            ):
                continue
            records.append({
                "hint": _trim_literal(match.group(0)),
                "kind": "arxiv",
                "start": match.start(),
            })

    for match in _NUMERIC_RE.finditer(text):
        hint = _clean(match.group(0))
        if _expand_numeric_hint(hint):
            records.append({"hint": hint, "kind": "numeric", "start": match.start()})

    for pattern in (_AUTHOR_PAREN_YEAR_RE, _AUTHOR_COMMA_YEAR_RE):
        for match in pattern.finditer(text):
            start, end = _expanded_span(text, match)
            hint = _clean(text[start:end])
            if not hint or _parse_author_year(hint) is None:
                continue
            records.append({"hint": hint, "kind": "author_year", "start": start})

    records.sort(key=lambda item: (item["start"], item["kind"], item["hint"]))
    return records


def _resolve_raw_hints(
    raw_hints: Iterable[Mapping[str, Any]],
    prepared: list[_PreparedReference],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_hint in raw_hints:
        for match in _match_hint(raw_hint, prepared):
            signature = (match["hint"], match["reference_id"], match["method"])
            if signature in seen:
                continue
            seen.add(signature)
            matches.append(match)
    return matches


def resolve_text_citations(text: str, references: Iterable[Any]) -> list[dict[str, Any]]:
    """Return only candidate matches for citation hints found in ``text``.

    DOI/arXiv literals match identifiers exactly. Numeric brackets resolve
    against ``bibliography_index`` values and author-year hints against a
    normalized first-author surname and year. Unmatched hints produce no
    candidate. Ordering is deterministic by text position, method and
    reference id; exact ``(hint, reference_id, method)`` duplicates collapse.
    """
    if not isinstance(text, str):
        raise CitationResolutionError("text must be a string")
    if references is None:
        references = []
    if isinstance(references, (str, bytes, Mapping)) or not isinstance(references, Iterable):
        raise CitationResolutionError("references must be an iterable of mappings")
    prepared = _prepare_references(references)
    ordered: list[tuple[int, dict[str, Any]]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_hint in _scan_text_hints(text, prepared):
        for match in _match_hint(raw_hint, prepared):
            signature = (match["hint"], match["reference_id"], match["method"])
            if signature in seen:
                continue
            seen.add(signature)
            ordered.append((raw_hint["start"], match))
    ordered.sort(key=lambda pair: (
        pair[0], pair[1]["method"], pair[1]["reference_id"], pair[1]["hint"],
    ))
    return [match for _, match in ordered]


def _iter_hint_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key in ("citation_key", "key", "hint", "text", "value", "citation"):
            if key in value:
                yield from _iter_hint_values(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_hint_values(item)
    elif isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)


def _explicit_raw_hint(value: str) -> dict[str, Any]:
    text = _clean(value)
    doi = _DOI_RE.search(text)
    if doi is not None:
        hint = _trim_literal(doi.group(0))
        if refs.normalize_doi(hint):
            return {"hint": hint, "kind": "doi", "start": 0}
    arxiv = _ARXIV_PREFIX_RE.search(text)
    if arxiv is not None:
        hint = _trim_literal(arxiv.group(0))
        if refs.normalize_arxiv(hint):
            return {"hint": hint, "kind": "arxiv", "start": 0}
    if _ARXIV_BARE_RE.fullmatch(text):
        return {"hint": text, "kind": "arxiv", "start": 0}
    bracket = _NUMERIC_RE.search(text)
    if bracket is not None:
        hint = _clean(bracket.group(0))
        if _expand_numeric_hint(hint):
            return {"hint": hint, "kind": "numeric", "start": 0}
    if _parse_author_year(text) is not None:
        return {"hint": text, "kind": "author_year", "start": 0}
    return {"hint": text, "kind": "citation_key", "start": 0}


def _explicit_raw_hints(claim: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for field in ("citation_hints", "citations", "citation_keys"):
        for value in _iter_hint_values(claim.get(field)):
            raw = _explicit_raw_hint(value)
            if field == "citation_keys" and raw["kind"] not in {"doi", "arxiv"}:
                raw = {"hint": raw["hint"], "kind": "citation_key", "start": 0}
            signature = (raw["hint"].casefold(), raw["kind"])
            if signature in seen:
                continue
            seen.add(signature)
            records.append(raw)
    return records


def _claim_text(claim: Mapping[str, Any]) -> str:
    return "\n".join(
        value
        for field in ("raw_excerpt", "wording", "proof_text")
        for value in [claim.get(field)]
        if isinstance(value, str) and value.strip()
    )


def _claim_raw_hints(
    claim: Mapping[str, Any],
    prepared: list[_PreparedReference],
) -> list[dict[str, Any]]:
    records = _explicit_raw_hints(claim)
    seen = {(item["hint"].casefold(), item["kind"]) for item in records}
    for raw in _scan_text_hints(_claim_text(claim), prepared):
        signature = (raw["hint"].casefold(), raw["kind"])
        if signature in seen:
            continue
        seen.add(signature)
        records.append(raw)
    return records


def _claim_locator(claim: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for field in ("source_locator", "locator"):
        value = claim.get(field)
        if isinstance(value, Mapping) and value:
            return value
    return None


def _locator(record: Any) -> dict[str, str]:
    if not isinstance(record, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in ("sha256", "path", "root_id"):
        value = _clean(record.get(key))
        if value:
            result[key] = value
    if "sha256" not in result:
        for key in ("pdf_sha256", "source_sha256"):
            value = _clean(record.get(key))
            if value:
                result["sha256"] = value
                break
    if "path" not in result:
        for key in ("pdf_path", "source_path"):
            value = _clean(record.get(key))
            if value:
                result["path"] = value
                break
    return result


def _locator_matches(left: Any, right: Any) -> bool:
    left_locator = _locator(left)
    right_locator = _locator(right)
    if not left_locator or not right_locator:
        return False
    if left_locator.get("sha256") and right_locator.get("sha256"):
        return left_locator["sha256"].casefold() == right_locator["sha256"].casefold()
    if left_locator.get("path") and right_locator.get("path"):
        if left_locator["path"] != right_locator["path"]:
            return False
        if left_locator.get("root_id") and right_locator.get("root_id"):
            return left_locator["root_id"] == right_locator["root_id"]
        return True
    return False


def _reference_locators(reference: Mapping[str, Any]) -> list[Any]:
    values: list[Any] = []
    for field in ("source_locators", "locators"):
        raw = reference.get(field)
        if isinstance(raw, list):
            values.extend(item for item in raw if isinstance(item, Mapping))
    for field in ("source_locator", "locator"):
        raw = reference.get(field)
        if isinstance(raw, Mapping) and raw:
            values.append(raw)
    raw_direct = {}
    if reference.get("pdf_sha256"):
        raw_direct["sha256"] = reference.get("pdf_sha256")
    if reference.get("pdf_path"):
        raw_direct["path"] = reference.get("pdf_path")
    if raw_direct:
        values.append(raw_direct)
    provenance = reference.get("metadata_provenance")
    if isinstance(provenance, list):
        values.extend(item for item in provenance if isinstance(item, Mapping))
    return values


def _claim_source_ids(
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    source_ids: set[str] = set()
    claim_id = claim.get("id")
    for edge in _edges(state):
        if edge.get("type") != "cites" or edge.get("src") != claim_id:
            continue
        target = nodes_by_id.get(edge.get("tgt"))
        if target is not None and target.get("type") == "Source":
            source_ids.add(str(target["id"]))
    locator = _claim_locator(claim)
    if locator is not None:
        for node in nodes_by_id.values():
            if node.get("type") == "Source" and _locator_matches(
                locator, node.get("locator") or node.get("source_locator")
            ):
                source_ids.add(str(node["id"]))
    return sorted(source_ids)


def _reference_belongs_to_sources(
    reference: Mapping[str, Any],
    source_ids: set[str],
    source_nodes: list[Mapping[str, Any]],
) -> bool:
    source_id = reference.get("source_id")
    if isinstance(source_id, str) and source_id in source_ids:
        return True
    source_locators = [
        node.get("locator") or node.get("source_locator")
        for node in source_nodes
    ]
    source_locators = [value for value in source_locators if value]
    if not source_locators:
        return False
    return any(
        _locator_matches(locator, source_locator)
        for locator in _reference_locators(reference)
        for source_locator in source_locators
    )


def _claim_reference_pool(
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    reference_nodes = sorted(
        (
            dict(node) for node in nodes_by_id.values()
            if node.get("type") == "Reference"
        ),
        key=lambda node: str(node["id"]),
    )
    source_ids = _claim_source_ids(state, claim, nodes_by_id)
    if not source_ids:
        return reference_nodes
    source_nodes = [nodes_by_id[source_id] for source_id in source_ids]
    reference_ids = {str(node["id"]) for node in reference_nodes}
    linked_ids: set[str] = set()
    for edge in _edges(state):
        if (
            edge.get("type") == "manifestation_of"
            and edge.get("src") in source_ids
            and edge.get("tgt") in reference_ids
        ):
            linked_ids.add(str(edge["tgt"]))
    linked = [
        node for node in reference_nodes
        if str(node["id"]) in linked_ids
        or _reference_belongs_to_sources(
            node, set(source_ids), source_nodes
        )
    ]
    return linked or reference_nodes


def _unresolved_hints(
    raw_hints: Iterable[Mapping[str, Any]],
    matches: Iterable[Mapping[str, Any]],
    threshold: float,
) -> list[str]:
    resolved = {
        str(match["hint"]).casefold()
        for match in matches
        if float(match["confidence"]) >= threshold
    }
    unresolved: list[str] = []
    seen: set[str] = set()
    for raw_hint in raw_hints:
        hint = str(raw_hint["hint"])
        folded = hint.casefold()
        if folded in resolved or folded in seen:
            continue
        seen.add(folded)
        unresolved.append(hint)
    return unresolved


def _find_resolution_edge(
    edges: Iterable[Mapping[str, Any]],
    claim_id: str,
    match: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    for edge in edges:
        if (
            edge.get("type") != "cites"
            or edge.get("src") != claim_id
            or edge.get("tgt") != match["reference_id"]
        ):
            continue
        provenance = edge.get("provenance")
        if not isinstance(provenance, Mapping):
            continue
        if provenance.get("kind") != PROVENANCE_KIND:
            continue
        if _clean(provenance.get("hint")) != match["hint"]:
            continue
        if _clean(provenance.get("method")) != match["method"]:
            continue
        return edge
    return None


def resolve_claim_citations(
    graph: Any,
    claim_id: str,
    *,
    apply: bool = True,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Resolve one claim's citation hints to candidate ``Reference`` nodes.

    Hints come from explicit claim fields and from citation-like text in
    ``raw_excerpt``, ``wording`` and ``proof_text``. References belonging to
    the claim's PDF Source are preferred; otherwise all References are used.
    When ``apply`` is true, matches at or above ``threshold`` become
    ``Claim --cites--> Reference`` edges carrying method, confidence and
    review provenance. Lower-confidence candidates remain in ``candidates``.
    Identical resolution edges are reused, never repeated.
    """
    threshold = _threshold(threshold)
    if not isinstance(apply, bool):
        raise CitationResolutionError("apply must be a boolean")
    state = _as_state(graph)
    nodes = _nodes(state)
    nodes_by_id = {str(node["id"]): node for node in nodes}
    claim = _claim_node(state, claim_id)
    prepared = _prepare_references(_claim_reference_pool(state, claim, nodes_by_id))
    raw_hints = _claim_raw_hints(claim, prepared)
    matches = _resolve_raw_hints(raw_hints, prepared)
    resolved = [
        match for match in matches
        if float(match["confidence"]) >= threshold
    ]
    candidates = [
        match for match in matches
        if float(match["confidence"]) < threshold
    ]
    unresolved_hints = _unresolved_hints(raw_hints, matches, threshold)

    edges: list[dict[str, Any]] = []
    if apply and resolved:
        working_edges = _edges(state)
        for match in resolved:
            existing = _find_resolution_edge(working_edges, claim_id, match)
            if existing is not None:
                edge = dict(existing)
            else:
                edge = graph.link(
                    claim_id,
                    "cites",
                    match["reference_id"],
                    provenance={
                        "kind": PROVENANCE_KIND,
                        "hint": match["hint"],
                        "method": match["method"],
                        "confidence": match["confidence"],
                        "requires_review": match["requires_review"],
                        "threshold": threshold,
                    },
                    rationale=(
                        "Citation resolution candidate only; this link records "
                        "a bibliographic match and does not assess truth or support."
                    ),
                )
                working_edges.append(edge)
            edges.append(dict(edge))

    return {
        "claim_id": claim_id,
        "matches": matches,
        "resolved": resolved,
        "candidates": candidates,
        "unresolved_hints": unresolved_hints,
        "applied": bool(apply and resolved),
        "edges": edges,
        "threshold": threshold,
        "truth_assessed": False,
    }


def citation_resolution_report(
    state: Any,
    claim_id: str | None = None,
) -> dict[str, Any]:
    """Report recorded citation-resolution edges and remaining hint state.

    Counts come from actual ``cites`` edges with ``pdf_citation_resolution``
    provenance. Hints without such an edge are reported as unresolved, and
    author-year/numeric ambiguity is reported separately. This function never
    writes and never assesses truth.
    """
    current = _as_state(state)
    nodes = _nodes(current)
    nodes_by_id = {str(node["id"]): node for node in nodes}
    edges = _edges(current)
    if claim_id is not None:
        claim = _claim_node(current, claim_id)
        claims = [claim]
    else:
        claims = sorted(
            (node for node in nodes if node.get("type") == "Claim"),
            key=lambda node: str(node["id"]),
        )
    selected_ids = {str(claim["id"]) for claim in claims}
    reference_ids = {
        str(node["id"]) for node in nodes if node.get("type") == "Reference"
    }
    resolution_edges = [
        edge for edge in edges
        if edge.get("type") == "cites"
        and edge.get("src") in selected_ids
        and edge.get("tgt") in reference_ids
        and isinstance(edge.get("provenance"), Mapping)
        and edge["provenance"].get("kind") == PROVENANCE_KIND
    ]

    resolved_by_method: dict[str, int] = {}
    resolved_requires_review = 0
    for edge in resolution_edges:
        provenance = edge["provenance"]
        method = _clean(provenance.get("method")) or "unknown"
        resolved_by_method[method] = resolved_by_method.get(method, 0) + 1
        if bool(provenance.get("requires_review")):
            resolved_requires_review += 1

    claims_with_hints = 0
    unresolved: list[dict[str, str]] = []
    unresolved_by_method: dict[str, int] = {}
    ambiguous: list[dict[str, Any]] = []
    seen_unresolved: set[tuple[str, str]] = set()
    seen_ambiguous: set[tuple[str, str]] = set()
    for claim in claims:
        current_claim_id = str(claim["id"])
        prepared = _prepare_references(
            _claim_reference_pool(current, claim, nodes_by_id)
        )
        raw_hints = _claim_raw_hints(claim, prepared)
        if raw_hints:
            claims_with_hints += 1
        matches = _resolve_raw_hints(raw_hints, prepared)
        ambiguous_refs: dict[str, set[str]] = {}
        for match in matches:
            if not match.get("ambiguous_refs"):
                continue
            ambiguous_refs.setdefault(match["hint"], set()).update(
                match["ambiguous_refs"]
            )
        for hint in sorted(ambiguous_refs, key=str.casefold):
            signature = (current_claim_id, hint.casefold())
            if signature in seen_ambiguous:
                continue
            seen_ambiguous.add(signature)
            ambiguous.append({
                "claim_id": current_claim_id,
                "hint": hint,
                "reference_ids": sorted(ambiguous_refs[hint]),
            })
        edge_hints = {
            _clean(edge["provenance"].get("hint")).casefold()
            for edge in resolution_edges
            if edge.get("src") == current_claim_id
        }
        for raw_hint in raw_hints:
            hint = raw_hint["hint"]
            signature = (current_claim_id, hint.casefold())
            if hint.casefold() in edge_hints or signature in seen_unresolved:
                continue
            seen_unresolved.add(signature)
            method = str(raw_hint["kind"])
            unresolved.append({
                "claim_id": current_claim_id,
                "hint": hint,
                "method": method,
            })
            unresolved_by_method[method] = unresolved_by_method.get(method, 0) + 1

    unresolved.sort(key=lambda item: (item["claim_id"], item["hint"].casefold(), item["hint"]))
    ambiguous.sort(key=lambda item: (item["claim_id"], item["hint"].casefold(), item["hint"]))
    resolved_by_method = dict(sorted(resolved_by_method.items()))
    unresolved_by_method = dict(sorted(unresolved_by_method.items()))
    claims_with_resolved = len({str(edge["src"]) for edge in resolution_edges})
    return {
        "claim_id": claim_id,
        "claims": len(claims),
        "claims_with_citation_hints": claims_with_hints,
        "claims_with_resolved_citations": claims_with_resolved,
        "resolved_edges": len(resolution_edges),
        "resolved_by_method": resolved_by_method,
        "resolved_by_requires_review": {
            "requires_review": resolved_requires_review,
            "no_review": len(resolution_edges) - resolved_requires_review,
        },
        "resolved_requires_review": resolved_requires_review,
        "resolved_no_review": len(resolution_edges) - resolved_requires_review,
        "unresolved_hints": unresolved,
        "unresolved_count": len(unresolved),
        "unresolved_by_method": unresolved_by_method,
        "ambiguous_matches": ambiguous,
        "ambiguous_count": len(ambiguous),
        "counts": {
            "claims": len(claims),
            "claims_with_citation_hints": claims_with_hints,
            "claims_with_resolved_citations": claims_with_resolved,
            "resolved_edges": len(resolution_edges),
            "unresolved_hints": len(unresolved),
            "unresolved_by_method": unresolved_by_method,
            "ambiguous_matches": len(ambiguous),
        },
        "truth_assessed": False,
    }


__all__ = [
    "CitationResolutionError",
    "DEFAULT_THRESHOLD",
    "PROVENANCE_KIND",
    "citation_resolution_report",
    "resolve_claim_citations",
    "resolve_text_citations",
]
