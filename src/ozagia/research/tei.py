"""Structured TEI/JATS reading for GROBID, PMC and Europe PMC output.

The parser returns sections, bibliographic references and citation targets. It
does not extract claims or decide that a citation supports anything.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError


class TEIError(ResearchError):
    """TEI/JATS XML is invalid or too large."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return refs._collapse("".join(element.itertext()))


def _first_text(root: ET.Element, *names: str) -> str:
    wanted = {name.casefold() for name in names}
    for element in root.iter():
        if _local(element.tag) in wanted:
            value = _text(element)
            if value:
                return value
    return ""


def _idno_value(bibl: ET.Element, kind: str) -> str | None:
    for element in bibl.iter():
        if _local(element.tag) == "idno":
            if _text(element) and _local(element.attrib.get("type", "")) == kind.casefold():
                return _text(element)
    return None


def _bibl_reference(bibl: ET.Element) -> dict[str, Any] | None:
    xml_id = bibl.attrib.get("{http://www.w3.org/XML/1998/namespace}id") or bibl.attrib.get("id")
    title = ""
    for element in bibl.iter():
        if _local(element.tag) in {"title", "analytic"}:
            title = _text(element)
            if title:
                break
    authors = []
    for element in bibl.iter():
        if _local(element.tag) == "author":
            name = _text(element)
            if name:
                authors.append(name)
    identifiers = refs.normalize_identifiers({
        "doi": _idno_value(bibl, "DOI"),
        "pmid": _idno_value(bibl, "PMID"),
        "pmcid": _idno_value(bibl, "PMCID"),
        "arxiv": _idno_value(bibl, "arXiv"),
        "isbn": _idno_value(bibl, "ISBN"),
    })
    year = ""
    for element in bibl.iter():
        if _local(element.tag) in {"date", "year"}:
            year = refs._year(_text(element))
            if year:
                break
    if not title and not xml_id:
        return None
    key = xml_id or ""
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "venue": "",
        "reference_type": "misc",
        "abstract": "",
        "url": "",
        "identifiers": identifiers,
        "citation_key": key,
        "citation_keys": [key] if key else [],
        "raw": ET.tostring(bibl, encoding="unicode")[:4000],
        "metadata_status": "parsed_tei_unverified",
    }


def parse_tei(xml_bytes: bytes, *, max_bytes: int = 20_000_000) -> dict[str, Any]:
    """Parse TEI/JATS into sections, references and citation targets."""
    if len(xml_bytes) > max_bytes:
        raise TEIError(f"TEI document exceeds the {max_bytes} byte parsing limit")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", xml_bytes, re.I):
        raise TEIError("TEI document with DTD/entity declarations is not accepted")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise TEIError(f"invalid TEI document: {exc}") from exc
    digest = hashlib.sha256(xml_bytes).hexdigest()
    sections = []
    for division in root.iter():
        if _local(division.tag) != "div":
            continue
        heading = next(
            (_text(child) for child in division if _local(child.tag) == "head"), "")
        paragraphs = [
            _text(child) for child in division.iter() if _local(child.tag) == "p" and _text(child)
        ]
        if not heading and not paragraphs:
            continue
        sections.append({
            "heading": heading or "Untitled section",
            "text": "\n\n".join(paragraphs),
            "xml_id": division.attrib.get("{http://www.w3.org/XML/1998/namespace}id")
            or division.attrib.get("id"),
        })
    references = []
    for element in root.iter():
        if _local(element.tag) not in {"biblstruct", "bibl"}:
            continue
        reference = _bibl_reference(element)
        if reference is not None:
            references.append(reference)
    citations = []
    for element in root.iter():
        if _local(element.tag) != "ref":
            continue
        target = element.attrib.get("target", "")
        if not target.startswith("#"):
            continue
        parent_text = ""
        for child in root.iter():
            if element in list(child):
                parent_text = _text(child)
                break
        citations.append({
            "citation_keys": [target.lstrip("#")],
            "quote": parent_text[:800],
            "relation_candidate": "unclassified",
            "truth_assessed": False,
        })
    return {
        "format": "tei",
        "sha256": digest,
        "title": _first_text(root, "title"),
        "authors": [
            _text(element) for element in root.iter()
            if _local(element.tag) == "author" and _text(element)
        ],
        "abstract": _first_text(root, "abstract"),
        "sections": sections,
        "references": references,
        "citations": citations,
        "source_note": "Structured TEI text; no claim extraction or truth assessment.",
    }
