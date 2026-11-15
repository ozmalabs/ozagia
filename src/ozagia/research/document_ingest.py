"""Ingest selected notes, HTML/website snapshots and EPUB books as claims."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

from ozagia.research import document_claims, paper, source_formats
from ozagia.research.graph import ResearchError


class DocumentIngestError(ResearchError):
    """A selected document cannot be parsed or recorded safely."""


SUFFIX_TYPES = {
    ".md": "note",
    ".markdown": "note",
    ".txt": "note",
    ".html": "website",
    ".htm": "website",
    ".epub": "book",
}


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _source_type(path: str, source_type: str | None) -> str:
    if source_type:
        value = str(source_type).strip().casefold()
        if value not in source_formats.SOURCE_FORMATS:
            raise DocumentIngestError(f"unknown source type: {source_type}")
        return value
    suffix = Path(path).suffix.casefold()
    value = SUFFIX_TYPES.get(suffix)
    if value is None:
        raise DocumentIngestError(
            "document source must be .md/.markdown/.txt, .html/.htm or .epub")
    return value


def _metadata(
    source_type: str,
    path: str,
    document: Mapping[str, Any],
    supplied: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata = dict(supplied or {})
    title = str(metadata.get("title") or document.get("title") or Path(path).stem)
    authors = document.get("authors") or metadata.get("authors") or []
    if source_type == "note":
        metadata.setdefault("title", title)
        metadata.setdefault("context", path)
    elif source_type == "website":
        metadata.setdefault("url", path)
        metadata.setdefault("accessed_at", _utc())
    elif source_type == "book":
        metadata.setdefault("title", title)
        metadata.setdefault("authors", list(authors) or ["unknown"])
        year = document.get("year") or metadata.get("year")
        if year:
            metadata.setdefault("year", str(year))
    else:
        metadata.setdefault("title", title)
    return metadata


def _parse(path: str, data: bytes) -> dict[str, Any]:
    suffix = Path(path).suffix.casefold()
    if suffix in {".md", ".markdown", ".txt"}:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentIngestError(f"note is not UTF-8: {exc}") from exc
        return document_claims.parse_note(text, title=None)
    if suffix in {".html", ".htm"}:
        return document_claims.parse_html(data, url=path)
    if suffix == ".epub":
        return document_claims.parse_epub(data)
    raise DocumentIngestError(f"unsupported document suffix: {suffix}")


def preview_document_claims(
    graph: Any,
    *,
    root_id: str,
    path: str,
    source_type: str | None = None,
    title: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_claims: int = 256,
) -> dict[str, Any]:
    """Parse and preview a selected note, website snapshot or EPUB book."""
    root, source_path = paper._root_file(graph, root_id, path)
    kind = _source_type(path, source_type)
    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise DocumentIngestError(f"cannot read selected document: {exc}") from exc
    document = _parse(path, data)
    if title:
        document["title"] = title
    result = document_claims.candidate_claims_from_document(
        document, max_claims=max_claims)
    meta = _metadata(kind, path, document, metadata)
    warnings = list(result["warnings"])
    if kind == "website" and "accessed_at" not in (metadata or {}):
        warnings.append("website accessed_at was auto-recorded; confirm or replace it")
    if kind == "book" and not meta.get("year"):
        warnings.append("book year is unknown")
    return {
        "root_id": root_id,
        "repository": root.get("repository", root_id),
        "path": path,
        "source_type": kind,
        "document": {
            "format": document.get("format"),
            "sha256": document.get("sha256"),
            "title": document.get("title", ""),
            "authors": document.get("authors", []),
            "warnings": list(document.get("warnings", [])),
        },
        "metadata": meta,
        "claims": result["claims"],
        "count": result["count"],
        "truncated": result["truncated"],
        "methods": result["methods"],
        "warnings": list(dict.fromkeys(warnings)),
        "truth_assessed": False,
    }


def _existing_claims(state: Mapping[str, Any], source_id: str) -> dict[str, dict[str, Any]]:
    linked = {
        edge.get("src")
        for edge in state.get("edges", [])
        if edge.get("type") == "cites" and edge.get("tgt") == source_id
    }
    return {
        str(node.get("candidate_id")): dict(node)
        for node in state.get("nodes", [])
        if node.get("type") == "Claim"
        and node.get("id") in linked
        and node.get("candidate_id")
    }


def ingest_document_claims(
    graph: Any,
    *,
    root_id: str,
    path: str,
    source_type: str | None = None,
    title: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_claims: int = 256,
    reviewer: str | None = None,
    holding: Mapping[str, Any] | None = None,
    resolve_citations: bool = True,
) -> dict[str, Any]:
    """Record a note/website/EPUB Source, holding and claim candidates."""
    preview = preview_document_claims(
        graph,
        root_id=root_id,
        path=path,
        source_type=source_type,
        title=title,
        metadata=metadata,
        max_claims=max_claims,
    )
    kind = preview["source_type"]
    sha256 = str(preview["document"].get("sha256") or "")
    if not sha256:
        raise DocumentIngestError("parsed document has no content hash")
    registered = source_formats.register_source(
        graph,
        source_type=kind,
        metadata=preview["metadata"],
        root_id=root_id,
        path=path,
        sha256=sha256,
        holding=holding,
        reviewer=reviewer,
    )
    source = registered["source"]
    state = graph.state()
    existing = _existing_claims(state, source["id"])
    claims = []
    for candidate in preview["claims"]:
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_id in existing:
            claims.append(existing[candidate_id])
            continue
        locator = dict(candidate.get("source_locator") or {})
        locator.update({
            "kind": kind,
            "root_id": root_id,
            "repository": preview["repository"],
            "path": path,
            "sha256": sha256,
            "backend": preview["document"].get("format") or kind,
        })
        fields = {
            "wording": candidate["wording"],
            "raw_excerpt": candidate["raw_excerpt"],
            "claim_type": candidate["claim_type"],
            "status": "unresolved",
            "label": candidate.get("label", ""),
            "paper_label": candidate.get("label", ""),
            "section": candidate.get("section"),
            "page": candidate.get("page"),
            "source_locator": locator,
            "extraction_method": candidate["extraction_method"],
            "confidence": candidate["confidence"],
            "citation_hints": candidate.get("citation_hints", []),
            "math_degraded": bool(candidate.get("math_degraded", False)),
            "parsed_assertion": True,
            "semantics_verified": False,
            "candidate_id": candidate_id,
            "scope": candidate.get("section") or "document",
            "author": "; ".join(preview["document"].get("authors") or []) or "unknown",
            "truth_assessed": False,
        }
        if reviewer:
            fields["reviewer"] = reviewer
        claim = graph.record("Claim", **fields)
        graph.link(
            claim["id"], "cites", source["id"],
            provenance={
                "kind": f"{kind}_claim_extraction",
                "candidate_id": candidate_id,
                "path": path,
                "sha256": sha256,
                "backend": preview["document"].get("format") or kind,
                "extraction_method": candidate["extraction_method"],
            },
            rationale="The candidate wording was extracted from this document; this locates text and does not assess truth.",
        )
        existing[candidate_id] = claim
        claims.append(claim)

    citations = []
    if resolve_citations:
        from ozagia.research.citation_resolution import resolve_claim_citations
        for claim in claims:
            citations.append(resolve_claim_citations(graph, claim["id"], apply=True))

    return {
        "source": source,
        "reference": registered.get("reference"),
        "holding": registered.get("holding"),
        "claims": claims,
        "count": len(claims),
        "source_type": kind,
        "document": preview["document"],
        "metadata": registered["metadata"],
        "warnings": list(dict.fromkeys(
            [*preview["warnings"],
             *[
                 f"{item.get('code', 'warning')}: {item.get('reason', '')}"
                 if isinstance(item, Mapping) else str(item)
                 for item in registered.get("warnings", [])
             ]])),
        "citations": citations,
        "truth_assessed": False,
    }


__all__ = [
    "DocumentIngestError",
    "ingest_document_claims",
    "preview_document_claims",
]
