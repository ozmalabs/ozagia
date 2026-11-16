"""Bibliographic references: import, normalize, export and check.

The reference layer is deliberately offline and standard-library only. It does
not fetch metadata, read PDFs or decide whether a citation supports a claim.
Those are separate, optional adapters. A ``Reference`` is the bibliographic
work; a ``Source`` is one local or cached artifact of it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.research.graph import ResearchError

IDENTIFIER_KEYS = ("doi", "arxiv", "pmid", "pmcid", "isbn", "openalex", "s2")
REFERENCE_FIELDS = (
    "title", "authors", "year", "venue", "reference_type", "abstract", "url",
    "license", "open_access", "identifiers", "citation_key", "citation_keys",
    "raw", "metadata_status", "retraction", "metadata_provenance",
    "source_locators", "conflicts",
)
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "of", "on", "or", "that", "the", "to", "using", "via", "with",
})
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.I)
_ARXIV_RE = re.compile(r"(?:arXiv\s*:\s*)?((?:\d{4}\.\d{4,5})|(?:[a-z-]+/\d{7}))(?:v\d+)?", re.I)
_PMID_RE = re.compile(r"\bPMID\s*:?\s*(\d{1,9})\b", re.I)
_PMCID_RE = re.compile(r"\bPMC\d{5,9}\b", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(?:1[0-9]{3}|2[0-9]{3})(?!\d)")


def _utc() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _collapse(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


# --------------------------------------------------------------------------- #
# identifiers and common normalization
# --------------------------------------------------------------------------- #
def normalize_doi(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", text, flags=re.I)
    text = text.rstrip(".,;)\"]}")
    if re.match(r"^10\.\d{4,9}/\S+$", text):
        return text.lower()
    return None


def normalize_arxiv(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    text = re.sub(r"^(?:arXiv\s*:\s*|https?://arxiv\.org/(?:abs|pdf)/)", "", text, flags=re.I)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    match = _ARXIV_RE.search(text)
    return match.group(1) if match else None


def normalize_pmid(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return match.group(0) if match else None


def normalize_pmcid(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"PMC\d+", str(value), re.I)
    return match.group(0).upper() if match else None


def normalize_isbn(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[^0-9Xx]", "", str(value)).upper()
    return text if len(text) in {10, 13} else None


def normalize_openalex(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"(?:openalex\.org/)?(W\d+)", str(value), re.I)
    return match.group(1).upper() if match else None


def normalize_s2(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_identifiers(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, Mapping) else {}
    functions = {
        "doi": normalize_doi,
        "arxiv": normalize_arxiv,
        "pmid": normalize_pmid,
        "pmcid": normalize_pmcid,
        "isbn": normalize_isbn,
        "openalex": normalize_openalex,
        "s2": normalize_s2,
    }
    result: dict[str, str] = {}
    for key in IDENTIFIER_KEYS:
        normalized = functions[key](raw.get(key))
        if normalized:
            result[key] = normalized
    return result


def normalize_title(value: Any) -> str:
    text = _collapse(value).casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return " ".join(text.split())


def _year(value: Any) -> str:
    if value is None:
        return ""
    match = _YEAR_RE.search(str(value))
    return match.group(0) if match else ""


# --------------------------------------------------------------------------- #
# BibTeX
# --------------------------------------------------------------------------- #
def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _consume_braced(text: str, index: int) -> tuple[str, int]:
    depth = 0
    escaped = False
    start = index
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
        index += 1
    raise ResearchError("unbalanced braces in BibTeX value")


def _consume_quoted(text: str, index: int) -> tuple[str, int]:
    depth = 0
    escaped = False
    start = index
    index += 1
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == '"' and depth == 0:
            return text[start + 1:index], index + 1
        index += 1
    raise ResearchError("unterminated quoted BibTeX value")


def _consume_entry(text: str, index: int, opener: str) -> tuple[str, int]:
    if opener == "{":
        body, end = _consume_braced(text, index)
        return body, end
    depth = 1
    braces = 0
    quoted = False
    escaped = False
    start = index + 1
    index += 1
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "{":
            braces += 1
        elif char == "}":
            braces = max(0, braces - 1)
        elif char == '"' and braces == 0:
            quoted = not quoted
        elif not quoted and braces == 0:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return text[start:index], index + 1
        index += 1
    raise ResearchError("unterminated BibTeX entry")


def _parse_bib_value(text: str, index: int, strings: Mapping[str, str]) -> tuple[str, int]:
    parts: list[str] = []
    while True:
        index = _skip_space(text, index)
        if index >= len(text):
            break
        char = text[index]
        if char == "{":
            value, index = _consume_braced(text, index)
        elif char == '"':
            value, index = _consume_quoted(text, index)
        else:
            end = index
            while end < len(text) and text[end] not in ",}#" and not text[end].isspace():
                end += 1
            token = text[index:end].strip()
            value = strings.get(token.casefold(), token)
            index = end
        parts.append(value)
        index = _skip_space(text, index)
        if index < len(text) and text[index] == "#":
            index += 1
            continue
        break
    return "".join(parts), index


def _parse_bib_fields(text: str, strings: Mapping[str, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    index = 0
    while index < len(text):
        while index < len(text) and (text[index].isspace() or text[index] == ","):
            index += 1
        match = re.match(r"[A-Za-z][A-Za-z0-9_:-]*", text[index:])
        if not match:
            break
        name = match.group(0).lower()
        index += len(match.group(0))
        index = _skip_space(text, index)
        if index >= len(text) or text[index] != "=":
            break
        index += 1
        value, index = _parse_bib_value(text, index, strings)
        fields[name] = value
    return fields


def _latex_to_text(value: Any) -> str:
    text = _collapse(value)
    text = re.sub(r"\\(?:emph|textit|textbf|textrm|textsf|texttt|mathrm|text|url)\s*\{([^{}]*)\}",
                  r"\1", text)
    text = re.sub(r"\\([&%$#_{}])", r"\1", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\\[A-Za-z]+\s*", "", text)
    text = text.replace("~", " ")
    return _collapse(text)


def _authors(value: Any) -> list[str]:
    text = _collapse(value)
    if not text:
        return []
    parts = re.split(r"\s+and\s+", text, flags=re.I)
    return [_latex_to_text(part).strip(" ,") for part in parts if part.strip()]


def _bib_type(kind: str) -> str:
    return {
        "article": "article",
        "book": "book",
        "booklet": "book",
        "inbook": "chapter",
        "incollection": "chapter",
        "inproceedings": "inproceedings",
        "conference": "inproceedings",
        "phdthesis": "thesis",
        "mastersthesis": "thesis",
        "techreport": "report",
        "unpublished": "preprint",
        "misc": "misc",
        "online": "web",
        "software": "software",
        "dataset": "dataset",
    }.get(kind.casefold(), "misc")


def _identifiers_from_fields(fields: Mapping[str, str]) -> dict[str, str]:
    identifiers: dict[str, Any] = {
        "doi": fields.get("doi"),
        "pmid": fields.get("pmid"),
        "pmcid": fields.get("pmcid"),
        "isbn": fields.get("isbn"),
        "openalex": fields.get("openalex"),
        "s2": fields.get("s2"),
    }
    archive = (fields.get("archiveprefix") or fields.get("eprinttype") or "").casefold()
    if "arxiv" in archive:
        identifiers["arxiv"] = fields.get("eprint")
    elif fields.get("eprint") and re.match(r"^(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})", fields["eprint"]):
        identifiers["arxiv"] = fields["eprint"]
    if fields.get("arxiv"):
        identifiers["arxiv"] = fields["arxiv"]
    return normalize_identifiers(identifiers)


def _entry_to_reference(kind: str, key: str, fields: Mapping[str, str], raw: str) -> dict[str, Any]:
    year = _year(fields.get("year") or fields.get("date"))
    venue = (
        fields.get("journal") or fields.get("booktitle") or fields.get("publisher")
        or fields.get("school") or fields.get("institution") or fields.get("howpublished")
        or ""
    )
    return {
        "title": _latex_to_text(fields.get("title", "")),
        "authors": _authors(fields.get("author") or fields.get("editor")),
        "year": year,
        "venue": _latex_to_text(venue),
        "reference_type": _bib_type(kind),
        "abstract": _latex_to_text(fields.get("abstract", "")),
        "url": _collapse(fields.get("url", "")),
        "identifiers": _identifiers_from_fields(fields),
        "citation_key": key.strip(),
        "citation_keys": [key.strip()] if key.strip() else [],
        "raw": raw.strip(),
        "metadata_status": "parsed_bibtex",
    }


def parse_bibtex(text: str) -> list[dict[str, Any]]:
    """Parse common BibTeX/BibLaTeX entries, including @string and crossref."""
    text = text.lstrip("\ufeff")
    strings: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    index = 0
    while True:
        at = text.find("@", index)
        if at < 0:
            break
        cursor = at + 1
        while cursor < len(text) and (text[cursor].isalnum() or text[cursor] in "_-"):
            cursor += 1
        kind = text[at + 1:cursor].strip().casefold()
        cursor = _skip_space(text, cursor)
        if cursor >= len(text) or text[cursor] not in "{(":
            index = at + 1
            continue
        body, index = _consume_entry(text, cursor, text[cursor])
        if kind in {"comment", "preamble"}:
            continue
        if kind == "string":
            parsed = _parse_bib_fields(body, strings)
            strings.update({name.casefold(): value for name, value in parsed.items()})
            continue
        key, separator, remainder = body.partition(",")
        key = key.strip()
        if not key:
            continue
        fields = _parse_bib_fields(remainder if separator else "", strings)
        entries.append({"kind": kind, "key": key, "fields": fields, "raw": text[at:index]})
    by_key = {entry["key"].casefold(): entry for entry in entries}
    for entry in entries:
        crossref = entry["fields"].get("crossref")
        if crossref:
            parent = by_key.get(crossref.casefold())
            if parent:
                for name, value in parent["fields"].items():
                    entry["fields"].setdefault(name, value)
    return [
        _entry_to_reference(entry["kind"], entry["key"], entry["fields"], entry["raw"])
        for entry in entries
    ]


# --------------------------------------------------------------------------- #
# .bbl / thebibliography, CSL-JSON and RIS
# --------------------------------------------------------------------------- #
_BIBITEM_RE = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")


def _bbl_reference(key: str, body: str) -> dict[str, Any]:
    raw = body.strip()
    title_match = re.search(r"\\(?:emph|textit|textbf)\s*\{([^{}]*)\}", raw)
    title = _latex_to_text(title_match.group(1)) if title_match else ""
    cleaned = _latex_to_text(raw)
    if not title:
        sentence = re.split(r"(?<=[.!?])\s+", cleaned.strip(), maxsplit=1)[0]
        title = sentence[:300].strip(" .")
    identifiers: dict[str, Any] = {}
    doi = _DOI_RE.search(cleaned)
    if doi:
        identifiers["doi"] = doi.group(0)
    arxiv = re.search(r"arXiv\s*:\s*([A-Za-z0-9./-]+)", raw, re.I)
    if arxiv:
        identifiers["arxiv"] = arxiv.group(1)
    pmid = _PMID_RE.search(raw)
    if pmid:
        identifiers["pmid"] = pmid.group(1)
    pmcid = _PMCID_RE.search(raw)
    if pmcid:
        identifiers["pmcid"] = pmcid.group(0)
    return {
        "title": title,
        "authors": [],
        "year": _year(cleaned),
        "venue": "",
        "reference_type": "misc",
        "abstract": "",
        "url": "",
        "identifiers": normalize_identifiers(identifiers),
        "citation_key": key.strip(),
        "citation_keys": [key.strip()],
        "raw": raw,
        "metadata_status": "parsed_bbl_unverified",
    }


def parse_bbl(text: str) -> list[dict[str, Any]]:
    """Parse ``\\bibitem`` entries without claiming structured metadata."""
    matches = list(_BIBITEM_RE.finditer(text))
    references: list[dict[str, Any]] = []
    for number, match in enumerate(matches):
        end = matches[number + 1].start() if number + 1 < len(matches) else len(text)
        end_env = text.find(r"\end{thebibliography}", match.end(), end)
        body = text[match.end():end_env if end_env >= 0 else end]
        references.append(_bbl_reference(match.group(1), body))
    return references


def parse_tex_bibliography(text: str) -> list[dict[str, Any]]:
    """Parse the embedded ``thebibliography`` environment, if present."""
    begin = re.search(r"\\begin\{thebibliography\}", text)
    if begin:
        end = text.find(r"\end{thebibliography}", begin.end())
        if end >= 0:
            return parse_bbl(text[begin.end():end])
    return []


def _csl_author(author: Any) -> str:
    if isinstance(author, str):
        return _collapse(author)
    if isinstance(author, Mapping):
        literal = author.get("literal")
        if literal:
            return _collapse(literal)
        family = _collapse(author.get("family", ""))
        given = _collapse(author.get("given", ""))
        return ", ".join(part for part in (family, given) if part)
    return ""


def _csl_year(issued: Any) -> str:
    if isinstance(issued, Mapping):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            return _year(parts[0][0])
        return _year(issued.get("literal"))
    return _year(issued)


def _csl_type(value: Any) -> str:
    return {
        "article-journal": "article",
        "article-magazine": "article",
        "article-newspaper": "article",
        "paper-conference": "inproceedings",
        "book": "book",
        "chapter": "chapter",
        "dataset": "dataset",
        "software": "software",
        "thesis": "thesis",
        "report": "report",
        "preprint": "preprint",
        "webpage": "web",
    }.get(_collapse(value).casefold(), "misc")


def _csl_to_reference(item: Mapping[str, Any]) -> dict[str, Any]:
    raw_authors = item.get("author") or []
    if isinstance(raw_authors, Mapping):
        raw_authors = [raw_authors]
    elif isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    authors = [_csl_author(author) for author in raw_authors]
    identifier_payload = {
        "doi": item.get("DOI"),
        "pmid": item.get("PMID"),
        "pmcid": item.get("PMCID"),
        "isbn": item.get("ISBN"),
        "url": item.get("URL"),
    }
    custom = item.get("custom")
    if isinstance(custom, Mapping):
        identifier_payload.update(custom)
    identifiers = normalize_identifiers(identifier_payload)
    raw_id = _collapse(item.get("id", ""))
    keys: list[str] = []
    explicit = item.get("citation-key") or item.get("citationKey")
    if explicit:
        keys.append(_collapse(explicit))
    if raw_id and not normalize_doi(raw_id) and not raw_id.startswith(("http://", "https://")):
        keys.append(raw_id)
    return {
        "title": _collapse(item.get("title", "")),
        "authors": [author for author in authors if author],
        "year": _csl_year(item.get("issued")),
        "venue": _collapse(item.get("container-title") or item.get("publisher") or ""),
        "reference_type": _csl_type(item.get("type")),
        "abstract": _collapse(item.get("abstract", "")),
        "url": _collapse(item.get("URL", "")),
        "identifiers": identifiers,
        "citation_key": keys[0] if keys else "",
        "citation_keys": list(dict.fromkeys(keys)),
        "raw": json.dumps(item, ensure_ascii=False, sort_keys=True),
        "metadata_status": "parsed_csl_json",
    }


def parse_csl_json(text: str) -> list[dict[str, Any]]:
    payload = json.loads(text)
    if isinstance(payload, Mapping):
        payload = payload.get("items", [payload])
    if not isinstance(payload, list):
        raise ResearchError("CSL-JSON must be a list or an object with an items list")
    return [_csl_to_reference(item) for item in payload if isinstance(item, Mapping)]


def _ris_type(value: str) -> str:
    return {
        "JOUR": "article",
        "JFULL": "article",
        "CONF": "inproceedings",
        "CPAPER": "inproceedings",
        "BOOK": "book",
        "CHAP": "chapter",
        "THES": "thesis",
        "RPRT": "report",
        "DATA": "dataset",
        "COMP": "software",
        "ELEC": "web",
        "GEN": "misc",
    }.get(value.strip().upper(), "misc")


def parse_ris(text: str) -> list[dict[str, Any]]:
    records: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9]{2})  - (.*)$", line)
        if not match:
            continue
        tag, value = match.groups()
        if tag == "TY":
            current = [("TY", value)]
        elif tag == "ER":
            if current:
                records.append(current)
            current = []
        elif current:
            current.append((tag, value))
    if current:
        records.append(current)
    references = []
    for record in records:
        fields: dict[str, list[str]] = {}
        for tag, value in record:
            fields.setdefault(tag, []).append(value)
        first = lambda tag: (fields.get(tag) or [""])[0]
        identifiers = normalize_identifiers({
            "doi": first("DO"),
            "pmid": first("PM"),
            "isbn": first("SN"),
        })
        key = first("ID")
        references.append({
            "title": _collapse(first("TI") or first("T1")),
            "authors": [_collapse(value) for value in fields.get("AU", []) + fields.get("A1", [])],
            "year": _year(first("PY") or first("Y1")),
            "venue": _collapse(first("JO") or first("T2")),
            "reference_type": _ris_type(first("TY")),
            "abstract": _collapse(first("AB")),
            "url": _collapse(first("UR")),
            "identifiers": identifiers,
            "citation_key": key,
            "citation_keys": [key] if key else [],
            "raw": "\n".join(f"{tag}  - {value}" for tag, value in record),
            "metadata_status": "parsed_ris",
        })
    return references


def parse_reference_file(path: str | Path, text: str) -> list[dict[str, Any]]:
    suffix = Path(path).suffix.casefold()
    if suffix in {".bib", ".bibtex"}:
        return parse_bibtex(text)
    if suffix == ".bbl":
        return parse_bbl(text)
    if suffix in {".json", ".csl", ".csljson"}:
        return parse_csl_json(text)
    if suffix == ".ris":
        return parse_ris(text)
    if suffix == ".tex":
        return parse_tex_bibliography(text)
    if "\\bibitem" in text:
        return parse_bbl(text)
    raise ResearchError(
        "reference source must be .bib, .bbl, .json/.csl, .ris, or .tex")


# --------------------------------------------------------------------------- #
# normalization, identity and graph recording
# --------------------------------------------------------------------------- #
def _clean_reference_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else list(value) if isinstance(value, Iterable) else []
    return list(dict.fromkeys(_collapse(item) for item in values if _collapse(item)))


def _author_key(authors: Sequence[str]) -> str:
    if not authors:
        return "anon"
    first = authors[0]
    last = first.split(",", 1)[0] if "," in first else first.split()[-1]
    return re.sub(r"[^a-z0-9]+", "", last.casefold()) or "anon"


def _title_word(title: str) -> str:
    for word in re.findall(r"[A-Za-z0-9]+", title.casefold()):
        if word not in _STOPWORDS and len(word) > 2:
            return word
    return ""


def generate_citation_key(reference: Mapping[str, Any], used: Iterable[str] = ()) -> str:
    base = f"{_author_key(reference.get('authors') or [])}{_year(reference.get('year'))}{_title_word(_collapse(reference.get('title')))}"
    base = re.sub(r"[^a-z0-9]+", "", base.casefold()) or "ref"
    taken = {item.casefold() for item in used}
    if base not in taken:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = base + suffix
        if candidate not in taken:
            return candidate
    number = 2
    while f"{base}-{number}" in taken:
        number += 1
    return f"{base}-{number}"


def normalize_reference(reference: Mapping[str, Any], *, used_keys: Iterable[str] = ()) -> dict[str, Any]:
    if not isinstance(reference, Mapping):
        raise ResearchError("reference must be an object")
    keys = _clean_reference_list(reference.get("citation_keys"))
    single = _collapse(reference.get("citation_key"))
    if single and single not in keys:
        keys.insert(0, single)
    if not keys:
        keys = [generate_citation_key(reference, used_keys)]
    ref: dict[str, Any] = {
        "title": _collapse(reference.get("title")),
        "authors": _clean_reference_list(reference.get("authors")),
        "year": _year(reference.get("year")),
        "venue": _collapse(reference.get("venue")),
        "reference_type": _collapse(reference.get("reference_type")) or "misc",
        "abstract": _collapse(reference.get("abstract")),
        "url": _collapse(reference.get("url")),
        "license": _collapse(reference.get("license")),
        "identifiers": normalize_identifiers(reference.get("identifiers")),
        "citation_key": keys[0],
        "citation_keys": keys,
        "raw": _collapse(reference.get("raw")),
        "metadata_status": _collapse(reference.get("metadata_status")) or "unverified",
    }
    if isinstance(reference.get("open_access"), Mapping):
        ref["open_access"] = dict(reference["open_access"])
    if not ref["title"] and not ref["raw"] and not ref["citation_key"]:
        raise ResearchError("reference needs a title, raw text, or citation key")
    retraction = reference.get("retraction")
    if isinstance(retraction, Mapping):
        status = _collapse(retraction.get("status")).casefold()
        if status not in {"none", "unknown", "concern", "retracted", "corrected"}:
            raise ResearchError("retraction.status must be none, unknown, concern, retracted, or corrected")
        ref["retraction"] = {
            "status": status,
            "provider": _collapse(retraction.get("provider")),
            "checked_at": _collapse(retraction.get("checked_at")),
            "notices": _clean_reference_list(retraction.get("notices")),
        }
    provenance = reference.get("metadata_provenance")
    if isinstance(provenance, list):
        ref["metadata_provenance"] = provenance
    source_locators = reference.get("source_locators")
    if isinstance(source_locators, list):
        ref["source_locators"] = source_locators
    conflicts = reference.get("conflicts")
    if isinstance(conflicts, list):
        ref["conflicts"] = conflicts
    return ref


def reference_identity(reference: Mapping[str, Any]) -> str | None:
    identifiers = normalize_identifiers(reference.get("identifiers"))
    for key in IDENTIFIER_KEYS:
        if identifiers.get(key):
            return f"{key}:{identifiers[key]}"
    key = _collapse(reference.get("citation_key"))
    if key:
        return f"key:{key.casefold()}"
    keys = _clean_reference_list(reference.get("citation_keys"))
    if keys:
        return f"key:{keys[0].casefold()}"
    title = normalize_title(reference.get("title"))
    if title:
        return f"title:{title}|{_year(reference.get('year'))}"
    return None


def _reference_nodes(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [node for node in state.get("nodes", []) if node.get("type") == "Reference"]


def find_reference(state: Mapping[str, Any], reference: Mapping[str, Any]) -> dict[str, Any] | None:
    identity = reference_identity(reference)
    incoming_ids = normalize_identifiers(reference.get("identifiers"))
    incoming_keys = {key.casefold() for key in _clean_reference_list(reference.get("citation_keys"))}
    if reference.get("citation_key"):
        incoming_keys.add(_collapse(reference["citation_key"]).casefold())
    for node in _reference_nodes(state):
        node_ids = normalize_identifiers(node.get("identifiers"))
        node_keys = {key.casefold() for key in _clean_reference_list(node.get("citation_keys"))}
        if node.get("citation_key"):
            node_keys.add(_collapse(node["citation_key"]).casefold())
        if identity and reference_identity(node) == identity:
            return node
        if any(key in node_ids and node_ids[key] == value for key, value in incoming_ids.items()):
            return node
        if incoming_keys & node_keys:
            return node
    return None


def _merge_reference(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    identifiers = dict(existing.get("identifiers") or {})
    conflicts = list(existing.get("conflicts") or [])
    for key, value in (incoming.get("identifiers") or {}).items():
        if not identifiers.get(key):
            identifiers[key] = value
        elif identifiers[key] != value:
            conflicts.append({"field": f"identifiers.{key}", "kept": identifiers[key], "ignored": value})
    if identifiers:
        merged["identifiers"] = identifiers
    keys = _clean_reference_list(existing.get("citation_keys"))
    for key in _clean_reference_list(incoming.get("citation_keys")):
        if key not in keys:
            keys.append(key)
    if keys:
        merged["citation_keys"] = keys
        merged["citation_key"] = existing.get("citation_key") or keys[0]
    prefer_incoming = (
        _collapse(existing.get("metadata_status")) == "parsed_bbl_unverified"
        and _collapse(incoming.get("metadata_status")) != "parsed_bbl_unverified"
    )
    for field in REFERENCE_FIELDS:
        if field in {"identifiers", "citation_key", "citation_keys", "conflicts"}:
            continue
        value = incoming.get(field)
        if not value:
            continue
        if not existing.get(field) or prefer_incoming:
            merged[field] = value
    retraction = incoming.get("retraction")
    if isinstance(retraction, Mapping):
        current = existing.get("retraction") or {}
        if not current or current.get("status") in {None, "", "unknown"}:
            merged["retraction"] = dict(retraction)
    provenance = list(existing.get("metadata_provenance") or [])
    provenance.extend(incoming.get("metadata_provenance") or [])
    if provenance:
        merged["metadata_provenance"] = provenance
    locators = list(existing.get("source_locators") or [])
    locators.extend(incoming.get("source_locators") or [])
    if locators:
        merged["source_locators"] = locators
    if conflicts:
        merged["conflicts"] = conflicts
    return {
        key: value for key, value in merged.items()
        if existing.get(key) != value and key not in {"id", "type", "created_at", "updated_at"}
    }


def record_reference(
    graph: Any,
    reference: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None = None,
    source_locator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record or enrich one Reference; returns ``{reference, created}``."""
    state = graph.state()
    used = {
        key.casefold()
        for node in _reference_nodes(state)
        for key in _clean_reference_list(node.get("citation_keys"))
    }
    incoming = normalize_reference(reference, used_keys=used)
    if provenance:
        incoming["metadata_provenance"] = [{**dict(provenance), "recorded_at": _utc()}]
    if source_locator:
        incoming["source_locators"] = [dict(source_locator)]
    existing = find_reference(state, incoming)
    if existing is None:
        created = graph.record("Reference", **incoming)
        return {"reference": created, "created": True}
    changes = _merge_reference(existing, incoming)
    if not changes:
        return {"reference": existing, "created": False}
    updated = graph.amend(existing["id"], **changes)
    return {"reference": updated, "created": False}


def import_references(
    graph: Any,
    references: Sequence[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any] | None = None,
    source_locator: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    results = []
    for reference in references:
        results.append(record_reference(
            graph, reference, provenance=provenance, source_locator=source_locator))
    return results


# --------------------------------------------------------------------------- #
# checks, export and citation contexts
# --------------------------------------------------------------------------- #
def check_references(state: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic citation-integrity checks; truth is not assessed."""
    nodes = list(state.get("nodes", []))
    references = _reference_nodes(state)
    edges = list(state.get("edges", []))
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    keys: dict[str, dict[str, Any]] = {}
    for reference in references:
        all_keys = _clean_reference_list(reference.get("citation_keys"))
        if reference.get("citation_key"):
            all_keys.append(_collapse(reference["citation_key"]))
        for key in dict.fromkeys(all_keys):
            folded = key.casefold()
            if folded in keys and keys[folded]["id"] != reference["id"]:
                issues.append({
                    "code": "duplicate_citation_key",
                    "node_id": reference["id"],
                    "reason": f"Citation key {key!r} also belongs to {keys[folded]['id']}.",
                })
            else:
                keys[folded] = reference
        if not reference.get("title") and not reference.get("raw"):
            issues.append({
                "code": "reference_title_missing",
                "node_id": reference["id"],
                "reason": "Reference has no title or raw citation text.",
            })
        if not reference.get("identifiers") and not reference.get("raw"):
            warnings.append({
                "code": "reference_identifier_missing",
                "node_id": reference["id"],
                "reason": "Reference has no DOI/arXiv/PMID and cannot be enriched automatically.",
            })
        status = (reference.get("retraction") or {}).get("status")
        if status == "retracted":
            issues.append({
                "code": "reference_retracted",
                "node_id": reference["id"],
                "reason": "Reference is marked retracted; review any claim that cites it.",
            })
        elif status == "concern":
            warnings.append({
                "code": "reference_expression_of_concern",
                "node_id": reference["id"],
                "reason": "Reference has an expression of concern.",
            })

    cited_keys: set[str] = set()
    for node in nodes:
        citations = node.get("citations")
        citation_keys = node.get("citation_keys")
        values = []
        if isinstance(citations, list):
            values.extend(citations)
        if isinstance(citation_keys, list):
            values.extend(citation_keys)
        for key in values:
            key = _collapse(key)
            if not key:
                continue
            cited_keys.add(key.casefold())
            if key.casefold() not in keys:
                issues.append({
                    "code": "unresolved_citation",
                    "node_id": node.get("id"),
                    "citation_key": key,
                    "reason": f"Citation key {key!r} has no Reference record.",
                })

    manifested: set[str] = set()
    cited_reference_ids: set[str] = set()
    reference_ids = {reference["id"] for reference in references}
    for edge in edges:
        if edge.get("type") == "manifestation_of":
            manifested.add(edge.get("tgt"))
        elif edge.get("type") == "cites" and edge.get("tgt") in reference_ids:
            cited_reference_ids.add(edge["tgt"])
    for reference in references:
        if reference["id"] not in manifested and reference["id"] not in cited_reference_ids:
            warnings.append({
                "code": "reference_unused",
                "node_id": reference["id"],
                "reason": "Reference is neither cited by a record nor linked to a Source.",
            })

    identities: dict[str, str] = {}
    for reference in references:
        identity = reference_identity(reference)
        if identity and identity in identities:
            issues.append({
                "code": "duplicate_reference",
                "node_id": reference["id"],
                "reason": f"Same identity as {identities[identity]}: {identity}.",
            })
        elif identity:
            identities[identity] = reference["id"]

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "references": len(references),
            "citation_keys": len(keys),
            "cited_keys": len(cited_keys),
        },
        "truth_assessed": False,
    }


def _bibtex_type(reference: Mapping[str, Any]) -> str:
    return {
        "article": "article",
        "book": "book",
        "chapter": "incollection",
        "inproceedings": "inproceedings",
        "thesis": "phdthesis",
        "report": "techreport",
        "dataset": "misc",
        "software": "misc",
        "preprint": "misc",
        "web": "misc",
    }.get(_collapse(reference.get("reference_type")).casefold(), "misc")


def _bibtex_value(value: Any) -> str:
    text = _collapse(value).replace("{", "").replace("}", "")
    return text


def export_bibtex(state: Mapping[str, Any]) -> str:
    entries = []
    for reference in sorted(
        _reference_nodes(state), key=lambda node: _collapse(node.get("citation_key"))
    ):
        key = _collapse(reference.get("citation_key")) or generate_citation_key(reference)
        fields: list[tuple[str, str]] = []
        if reference.get("title"):
            fields.append(("title", _bibtex_value(reference["title"])))
        if reference.get("authors"):
            fields.append(("author", " and ".join(_bibtex_value(a) for a in reference["authors"])))
        if reference.get("year"):
            fields.append(("year", _bibtex_value(reference["year"])))
        if reference.get("venue"):
            reference_type = _collapse(reference.get("reference_type"))
            field = (
                "journal" if reference_type == "article"
                else "publisher" if reference_type == "book"
                else "school" if reference_type == "thesis"
                else "institution" if reference_type == "report"
                else "booktitle"
            )
            fields.append((field, _bibtex_value(reference["venue"])))
        identifiers = normalize_identifiers(reference.get("identifiers"))
        if identifiers.get("doi"):
            fields.append(("doi", identifiers["doi"]))
        if identifiers.get("arxiv"):
            fields.append(("eprint", identifiers["arxiv"]))
            fields.append(("archivePrefix", "arXiv"))
        if identifiers.get("pmid"):
            fields.append(("pmid", identifiers["pmid"]))
        if identifiers.get("isbn"):
            fields.append(("isbn", identifiers["isbn"]))
        if reference.get("url"):
            fields.append(("url", _bibtex_value(reference["url"])))
        if reference.get("abstract"):
            fields.append(("abstract", _bibtex_value(reference["abstract"])))
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
        entries.append(f"@{_bibtex_type(reference)}{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _csl_type_from_reference(reference: Mapping[str, Any]) -> str:
    return {
        "article": "article-journal",
        "inproceedings": "paper-conference",
        "book": "book",
        "chapter": "chapter",
        "thesis": "thesis",
        "report": "report",
        "dataset": "dataset",
        "software": "software",
        "preprint": "preprint",
        "web": "webpage",
    }.get(_collapse(reference.get("reference_type")).casefold(), "misc")


def export_csl_json(state: Mapping[str, Any]) -> str:
    items = []
    for reference in sorted(
        _reference_nodes(state), key=lambda node: _collapse(node.get("citation_key"))
    ):
        item: dict[str, Any] = {
            "id": _collapse(reference.get("citation_key")) or generate_citation_key(reference),
            "type": _csl_type_from_reference(reference),
            "title": _collapse(reference.get("title")),
        }
        if reference.get("authors"):
            item["author"] = []
            for author in reference["authors"]:
                if "," in author:
                    family, given = author.split(",", 1)
                    item["author"].append({"family": family.strip(), "given": given.strip()})
                else:
                    item["author"].append({"literal": author})
        if reference.get("year"):
            item["issued"] = {"date-parts": [[int(reference["year"])]]}
        if reference.get("venue"):
            item["container-title"] = reference["venue"]
        if reference.get("abstract"):
            item["abstract"] = reference["abstract"]
        identifiers = normalize_identifiers(reference.get("identifiers"))
        if identifiers.get("doi"):
            item["DOI"] = identifiers["doi"]
        if identifiers.get("pmid"):
            item["PMID"] = identifiers["pmid"]
        if identifiers.get("isbn"):
            item["ISBN"] = identifiers["isbn"]
        if reference.get("url"):
            item["URL"] = reference["url"]
        items.append(item)
    return json.dumps(items, ensure_ascii=False, indent=2) + "\n"


def export_references(state: Mapping[str, Any], format: str = "bibtex") -> dict[str, Any]:
    normalized = format.casefold().replace("_", "-")
    if normalized in {"bibtex", "bib"}:
        content = export_bibtex(state)
    elif normalized in {"csl-json", "csljson", "json"}:
        content = export_csl_json(state)
    else:
        raise ResearchError("reference export format must be bibtex or csl-json")
    return {
        "format": "bibtex" if normalized in {"bibtex", "bib"} else "csl-json",
        "content": content,
        "count": len(_reference_nodes(state)),
    }


_CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citeauthor|citeyear|parencite|textcite|autocite)"
    r"\*?(?:\[[^\]]*\])*\{([^}]+)\}"
)
_MD_CITE_RE = re.compile(r"\[@([^\]]+)\]")


def citation_contexts(text: str) -> list[dict[str, Any]]:
    """Return sentence/line candidates around citation markers.

    This is extraction, not interpretation: no ``supports`` or ``opposes``
    relation is produced.
    """
    matches: list[tuple[re.Match[str], str]] = [
        (match, "latex") for match in _CITE_RE.finditer(text)
    ]
    matches.extend((match, "markdown") for match in _MD_CITE_RE.finditer(text))
    matches.sort(key=lambda pair: pair[0].start())
    contexts = []
    seen = set()
    for match, syntax in matches:
        keys = [key.strip().lstrip("@") for key in re.split(r"[,;]", match.group(1))]
        keys = [key for key in keys if key]
        if not keys:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        previous = max(text.rfind(". ", line_start, match.start()),
                       text.rfind(".\n", line_start, match.start()))
        start = previous + 2 if previous >= 0 else line_start
        ends = [end for end in (text.find(". ", match.end()), text.find("\n", match.end()))
                if end >= 0]
        if ends:
            end = min(ends)
            if text[end:end + 2] == ". ":
                end += 2
        else:
            end = len(text)
        quote = _collapse(text[start:end])
        if len(quote) > 800:
            quote = quote[:800]
        start_line = text.count("\n", 0, start) + 1
        end_line = text.count("\n", 0, max(start, end - 1)) + 1
        signature = (tuple(keys), start_line, quote)
        if signature in seen:
            continue
        seen.add(signature)
        contexts.append({
            "citation_keys": keys,
            "quote": quote,
            "start_line": start_line,
            "end_line": end_line,
            "syntax": syntax,
            "relation_candidate": "unclassified",
            "truth_assessed": False,
        })
    return contexts
