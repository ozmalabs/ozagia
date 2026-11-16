"""Read-only presentation views over a recorded research graph.

The module is a projection layer, not a research operation. It loads an
existing ``ResearchGraph`` state and returns deterministic JSON models for the
library, fetch, screening, evidence, consensus and citation surfaces. It never
writes to the graph, never fetches from a provider, never calls a model and
never assesses truth, support, consensus or freshness.

Every view uses the same envelope::

    {
      "schema": 1,
      "schema_name": "ozagia.research_ui.view",
      "view": "library",
      "investigation_id": "research-...",
      "nodes": [...],          # compact records involved in the view
      "rows": [...],           # the tabular projection
      "summary": {...},
      "provenance": {...},
      "omitted": {"rows": 0, "reason": "none", ...},
      "not_assessed": [...],
      "truth_assessed": false,
      "note": "Read-only projection ..."
    }

Graph-shaped views also carry a ``graph`` key produced by
:func:`ozagia.core.view.build_view`. Its aperture only resolves node and edge
types the research graph actually records; unresolved material is grouped as
``"other"`` rather than dropped. Table row order is fixed by id and type, so
the same state always serialises to the same bytes.

Unknown or missing recorded fields are explicit: a value is ``null``, a
retraction status defaults to ``"unknown"``, and each row lists any required
field it lacks under ``missing``.
"""
from __future__ import annotations

import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ozagia.config import state_path
from ozagia.core.view import ViewError, build_view
from ozagia.research.graph import (
    LEGACY_GRAPH_DIR,
    ResearchError,
    ResearchGraph,
)
from ozagia.research.references import (
    check_references,
    normalize_identifiers,
    reference_identity,
)
from ozagia.research.review import screening_report

SCHEMA_VERSION = 1
SCHEMA_NAME = "ozagia.research_ui.view"
MAX_ROWS = 5000

VIEWS = ("library", "fetch", "screening", "evidence", "consensus", "citation")

VIEW_TITLES = {
    "library": "Reference library",
    "fetch": "Fetch record",
    "screening": "Screening decisions",
    "evidence": "Evidence map",
    "consensus": "Review consensus",
    "citation": "Citation graph",
}

_NOTE = (
    "Read-only projection of recorded research records. It does not assess "
    "truth, support, consensus, reviewer accuracy or freshness, and it does "
    "not fetch or call a model."
)

_COMMON_NOT_ASSESSED = (
    "truth or correctness of any recorded claim or finding",
    "support or opposition implied by recorded edges",
    "freshness: no provider, index or cache refresh was run",
)

_VIEW_NOT_ASSESSED = {
    "library": (
        "metadata completeness or current provider metadata",
        "retraction status beyond the recorded checked_at and notices",
        "whether duplicate identities are truly the same work",
    ),
    "fetch": (
        "provider availability or whether a recorded URL still resolves",
        "artifact integrity beyond the recorded SHA-256 value",
        "whether a recorded candidate is the best available copy",
        "candidates that were previewed but never recorded",
    ),
    "screening": (
        "eligibility beyond the recorded reason and criteria",
        "reviewer independence or agreement",
        "the completeness of the screened record set",
    ),
    "evidence": (
        "whether evidence supports or opposes a claim",
        "whether a recorded locator still matches the source text",
        "whether unrecorded evidence exists",
    ),
    "consensus": (
        "whether reviewer agreement implies correctness",
        "reviewer, model or run quality",
        "consensus that was not recorded in a Reconciliation",
    ),
    "citation": (
        "whether provider-asserted citation links are accurate or complete",
        "whether a citation implies support for any claim",
        "retraction notices not present in the recorded metadata",
    ),
}

_EVIDENCE_RELATIONS = frozenset({"supports", "opposes", "cites", "derived_from"})
_CITATION_EDGE_TYPES = frozenset(
    {"cites", "cites_work", "manifestation_of", "same_as", "supersedes"}
)

_CITATION_APERTURE = {
    "schema": 1,
    "id": "research-citation",
    "title": "Research citation",
    "description": "References, artifacts and recorded citation or version edges.",
    "resolves": {
        "nodes": ["Reference", "Source"],
        "edges": sorted(_CITATION_EDGE_TYPES),
        "attrs": ["title", "citation_key", "status", "name", "created_at"],
    },
    "grain": "reference",
    "scope": {
        "node_types": ["Reference", "Source"],
        "edge_types": sorted(_CITATION_EDGE_TYPES),
    },
    "collapse": {
        "unresolved": "other",
        "groups": {"Reference": "reference", "Source": "source"},
        "labels": {"reference": "References", "source": "Sources"},
    },
    "render": {
        "layout": "force",
        "seed": 31,
        "encoding": {"color": "group", "size": "weight", "label": "label"},
    },
    "limits": {"max_nodes": 400, "max_edges": 1200, "truncate": "degree"},
}


class ResearchUIError(ValueError):
    """A research state or view request that cannot be projected."""


# --------------------------------------------------------------------------- #
# state, JSON and ordering helpers
# --------------------------------------------------------------------------- #
def _plain(value: Any) -> Any:
    """A deterministic JSON-safe copy of a recorded value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _as_state(value: Any, *, investigation_id: str | None = None) -> dict[str, Any]:
    if isinstance(value, ResearchGraph):
        data = value.state()
    elif isinstance(value, Mapping):
        data = dict(value)
        bundle = data.get("bundle")
        if isinstance(bundle, Mapping):
            data = dict(bundle)
    else:
        raise ResearchUIError("research state must be a mapping or ResearchGraph")
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ResearchUIError("research state needs nodes and edges arrays")
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise ResearchUIError(f"research state nodes[{index}] is not an object")
        if not isinstance(node.get("id"), str) or not node["id"].strip():
            raise ResearchUIError(f"research state nodes[{index}] has no id")
    for index, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            raise ResearchUIError(f"research state edges[{index}] is not an object")
    recorded = data.get("investigation_id")
    if investigation_id is not None:
        if recorded is not None and str(recorded) != investigation_id:
            raise ResearchUIError(
                "research state investigation id does not match the request")
        recorded = investigation_id
    data["investigation_id"] = str(recorded or "unknown")
    return data


def load_state(investigation_id: str, *, root: str | Path = "."
               ) -> tuple[dict[str, Any], Path]:
    """Load one investigation read-only and return its state and path."""
    try:
        graph = ResearchGraph.load(investigation_id, root=root)
    except ResearchError as exc:
        raise ResearchUIError(str(exc)) from exc
    except (UnicodeError, AttributeError, TypeError) as exc:
        raise ResearchUIError(f"invalid research graph: {exc}") from exc
    path = graph.path
    if not path.exists():
        legacy = (Path(root).resolve() / LEGACY_GRAPH_DIR
                  / f"{investigation_id}.json")
        if legacy.is_file():
            path = legacy
    return graph.state(), path


def _nodes(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(node) for node in state.get("nodes", [])
            if isinstance(node, Mapping)]


def _edges(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(edge) for edge in state.get("edges", [])
            if isinstance(edge, Mapping)]


def _by_id(nodes: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(node["id"]): dict(node) for node in nodes
            if isinstance(node.get("id"), str)}


def _text(node: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, (str, int, float)):
            text = str(item).strip()
            if text:
                out.append(text)
    return out


def _mapping(value: Any) -> dict[str, Any] | None:
    return _plain(dict(value)) if isinstance(value, Mapping) else None


def _label(node: Mapping[str, Any]) -> str:
    for key in ("wording", "title", "name", "question", "summary", "text"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(node.get("id") or node.get("type") or "record")


def _node_ref(node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(node.get("id")),
        "type": str(node.get("type") or "unknown"),
        "label": _label(node),
    }


def _node_refs(nodes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        out.append(_node_ref(node))
    out.sort(key=lambda item: item["id"])
    return out


def _created_sort(node: Mapping[str, Any]) -> tuple[str, str]:
    return (str(node.get("created_at") or ""), str(node.get("id") or ""))


def _missing(fields: Mapping[str, Any]) -> list[str]:
    out = []
    for key, value in fields.items():
        if value is None or value == "" or value == [] or value == {}:
            out.append(key)
    return sorted(out)


def _citation_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("citation_key",):
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            keys.append(value.strip())
    for field in ("citations", "citation_keys"):
        keys.extend(_string_list(node.get(field)))
    return list(dict.fromkeys(keys))


def _locator(node: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(node, Mapping):
        return None
    value = node.get("source_locator")
    if not isinstance(value, Mapping):
        value = node.get("locator")
    if isinstance(value, Mapping):
        return _mapping(value)
    locators = node.get("source_locators")
    if isinstance(locators, list):
        for item in locators:
            if isinstance(item, Mapping):
                return _mapping(item)
    return None


def _locator_label(locator: Mapping[str, Any] | None) -> str | None:
    if not locator:
        return None
    path = _text(locator, "path", "repository")
    if path:
        start = locator.get("start_line")
        end = locator.get("end_line")
        if isinstance(start, int) and not isinstance(start, bool):
            suffix = f":{start}"
            if isinstance(end, int) and not isinstance(end, bool) and end != start:
                suffix += f"-{end}"
            return f"{path}{suffix}"
        return path
    url = _text(locator, "url")
    if url:
        return url
    digest = _text(locator, "sha256")
    if digest:
        return f"sha256:{digest[:12]}"
    return _text(locator, "kind")


def _retraction(node: Mapping[str, Any]) -> dict[str, Any]:
    raw = node.get("retraction")
    data = dict(raw) if isinstance(raw, Mapping) else {}
    status = str(data.get("status") or "unknown").casefold()
    return {
        "status": status,
        "provider": _text(data, "provider"),
        "checked_at": _text(data, "checked_at"),
        "notices": _string_list(data.get("notices")),
    }


def _limit_rows(rows: list[dict[str, Any]]
                ) -> tuple[list[dict[str, Any]], int]:
    if len(rows) <= MAX_ROWS:
        return rows, 0
    return rows[:MAX_ROWS], len(rows) - MAX_ROWS


def _provenance(state: Mapping[str, Any], *, source: Any, root: Any
                ) -> dict[str, Any]:
    node_counts = Counter(str(node.get("type") or "unknown")
                          for node in state.get("nodes", [])
                          if isinstance(node, Mapping))
    edge_counts = Counter(str(edge.get("type") or "unknown")
                          for edge in state.get("edges", [])
                          if isinstance(edge, Mapping))
    return {
        "derived": True,
        "generator": "ozagia.core.research_ui",
        "view_schema": SCHEMA_VERSION,
        "graph_schema": state.get("schema_version"),
        "investigation": state.get("investigation_id"),
        "source": str(source) if source is not None else None,
        "root": str(root) if root is not None else None,
        "record_counts": {
            "nodes": dict(sorted(node_counts.items())),
            "edges": dict(sorted(edge_counts.items())),
        },
        "truth_assessed": False,
    }


def _not_assessed(view: str) -> list[str]:
    out = list(_COMMON_NOT_ASSESSED)
    out.extend(_VIEW_NOT_ASSESSED.get(view, ()))
    return list(dict.fromkeys(out))


def _envelope(
    state: Mapping[str, Any],
    view: str,
    *,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    nodes: Iterable[Mapping[str, Any]],
    summary: Mapping[str, Any],
    root: str | Path | None,
    source: str | Path | None,
    graph: dict[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    kept, omitted_rows = _limit_rows(_plain(rows))
    omitted: dict[str, Any] = {
        "rows": omitted_rows,
        "reason": "row cap" if omitted_rows else "none",
        "limit": MAX_ROWS,
    }
    if graph is not None:
        omitted["graph"] = _plain(graph.get("omitted"))
    model: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "schema_name": SCHEMA_NAME,
        "view": view,
        "title": VIEW_TITLES[view],
        "investigation_id": state.get("investigation_id"),
        "columns": _plain(columns),
        "nodes": _node_refs(nodes),
        "rows": kept,
        "summary": {**_plain(summary), "row_count": len(kept)},
        "provenance": _provenance(state, source=source, root=root),
        "omitted": omitted,
        "not_assessed": _not_assessed(view),
        "truth_assessed": False,
        "note": _NOTE,
    }
    if graph is not None:
        model["graph"] = graph
    if extra:
        model.update(_plain(extra))
    return model


def _graph_view(state: Mapping[str, Any], aperture: str | dict[str, Any],
                *, root: str | Path | None, source: str | Path | None
                ) -> dict[str, Any]:
    try:
        return build_view(
            state, aperture,
            root=str(root) if root is not None else None,
            source=str(source) if source is not None else None,
        )
    except ViewError as exc:
        raise ResearchUIError(f"cannot project research graph: {exc}") from exc


# --------------------------------------------------------------------------- #
# library
# --------------------------------------------------------------------------- #
def _reference_brief(node: Mapping[str, Any]) -> dict[str, Any]:
    keys = _citation_keys(node)
    return {
        "reference_id": str(node.get("id")),
        "title": _text(node, "title", "raw"),
        "citation_key": keys[0] if keys else None,
        "identity": reference_identity(node),
    }


def _duplicate_groups(references: list[dict[str, Any]]
                      ) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    identity_groups: dict[str, set[str]] = defaultdict(set)
    key_groups: dict[str, dict[str, Any]] = {}
    for reference in references:
        reference_id = str(reference["id"])
        identity = reference_identity(reference)
        if identity:
            identity_groups[identity].add(reference_id)
        for key in _citation_keys(reference):
            folded = key.casefold()
            group = key_groups.setdefault(folded, {"value": key, "ids": set()})
            group["ids"].add(reference_id)
    duplicates: list[dict[str, Any]] = []
    duplicate_of: dict[str, set[str]] = defaultdict(set)
    for identity, ids in identity_groups.items():
        if len(ids) < 2:
            continue
        sorted_ids = sorted(ids)
        duplicates.append({
            "kind": "identity",
            "value": identity,
            "reference_ids": sorted_ids,
        })
        for reference_id in sorted_ids:
            duplicate_of[reference_id].update(set(sorted_ids) - {reference_id})
    for group in key_groups.values():
        ids = group["ids"]
        if len(ids) < 2:
            continue
        sorted_ids = sorted(ids)
        duplicates.append({
            "kind": "citation_key",
            "value": group["value"],
            "reference_ids": sorted_ids,
        })
        for reference_id in sorted_ids:
            duplicate_of[reference_id].update(set(sorted_ids) - {reference_id})
    duplicates.sort(key=lambda item: (item["kind"], str(item["value"]).casefold(),
                                      str(item["value"])))
    return duplicates, duplicate_of


def _library(state: Mapping[str, Any], *, root: str | Path | None,
             source: str | Path | None) -> dict[str, Any]:
    nodes = _nodes(state)
    edges = _edges(state)
    by_id = _by_id(nodes)
    references = sorted(
        (node for node in nodes if node.get("type") == "Reference"),
        key=lambda node: str(node["id"]),
    )
    reference_ids = {str(node["id"]) for node in references}

    same_as: dict[str, set[str]] = defaultdict(set)
    supersedes: dict[str, set[str]] = defaultdict(set)
    superseded_by: dict[str, set[str]] = defaultdict(set)
    manifested_by: dict[str, set[str]] = defaultdict(set)
    cited_by: dict[str, set[str]] = defaultdict(set)
    work_out: dict[str, set[str]] = defaultdict(set)
    work_in: dict[str, set[str]] = defaultdict(set)
    work_edge_count = 0
    for edge in edges:
        edge_type = edge.get("type")
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        if edge_type == "same_as":
            if src in reference_ids and tgt in reference_ids:
                same_as[src].add(tgt)
                same_as[tgt].add(src)
        elif edge_type == "supersedes":
            if src in reference_ids and tgt in reference_ids:
                supersedes[src].add(tgt)
                superseded_by[tgt].add(src)
        elif edge_type == "manifestation_of":
            if tgt in reference_ids and src in by_id:
                manifested_by[tgt].add(src)
        elif edge_type == "cites":
            if tgt in reference_ids and src in by_id:
                cited_by[tgt].add(src)
        elif edge_type == "cites_work":
            work_edge_count += 1
            if src in reference_ids and tgt in reference_ids:
                work_out[src].add(tgt)
                work_in[tgt].add(src)

    report = check_references(state)
    unresolved: list[dict[str, Any]] = []
    seen_unresolved: set[tuple[str, str]] = set()
    for issue in report.get("issues", []):
        if issue.get("code") != "unresolved_citation":
            continue
        key = str(issue.get("citation_key") or "")
        owner_id = str(issue.get("node_id") or "")
        if (key, owner_id) in seen_unresolved:
            continue
        seen_unresolved.add((key, owner_id))
        owner = by_id.get(owner_id)
        unresolved.append({
            "citation_key": key,
            "node_id": owner_id,
            "node_type": str(owner.get("type")) if owner else None,
            "reason": str(issue.get("reason") or ""),
        })
    unresolved.sort(key=lambda item: (item["citation_key"].casefold(),
                                      item["citation_key"], item["node_id"]))

    duplicates, duplicate_of = _duplicate_groups(references)
    rows: list[dict[str, Any]] = []
    for reference in references:
        reference_id = str(reference["id"])
        keys = _citation_keys(reference)
        identifiers = normalize_identifiers(reference.get("identifiers"))
        retraction = _retraction(reference)
        title = _text(reference, "title", "raw")
        identity = reference_identity(reference)
        status = retraction["status"]
        flags = []
        if status == "retracted":
            flags.append("retracted")
        elif status == "concern":
            flags.append("concern")
        elif status == "corrected":
            flags.append("corrected")
        if duplicate_of.get(reference_id):
            flags.append("duplicate")
        versions = [
            _reference_brief(by_id[other])
            for other in sorted(same_as.get(reference_id, set()))
            if other in by_id
        ]
        rows.append({
            "reference_id": reference_id,
            "title": title,
            "citation_key": keys[0] if keys else None,
            "citation_keys": keys,
            "identity": identity,
            "identifiers": identifiers,
            "year": _text(reference, "year"),
            "authors": _string_list(reference.get("authors")),
            "venue": _text(reference, "venue"),
            "reference_type": _text(reference, "reference_type"),
            "metadata_status": _text(reference, "metadata_status") or "unknown",
            "license": _text(reference, "license"),
            "url": _text(reference, "url"),
            "retraction_status": status,
            "retraction": retraction,
            "retracted": status == "retracted",
            "concern": status == "concern",
            "versions": versions,
            "supersedes": sorted(supersedes.get(reference_id, set())),
            "superseded_by": sorted(superseded_by.get(reference_id, set())),
            "source_ids": sorted(manifested_by.get(reference_id, set())),
            "cited_by": sorted(cited_by.get(reference_id, set())),
            "work_citations": sorted(work_out.get(reference_id, set())),
            "work_cited_by": sorted(work_in.get(reference_id, set())),
            "duplicate_of": sorted(duplicate_of.get(reference_id, set())),
            "metadata_provenance": (
                _plain(reference.get("metadata_provenance") or [])
                if isinstance(reference.get("metadata_provenance"), list)
                else []),
            "source_locators": _plain(reference.get("source_locators") or []),
            "conflicts": _plain(reference.get("conflicts") or []),
            "flags": flags,
            "missing": _missing({
                "title": title,
                "identity": identity,
                "citation_key": keys[0] if keys else None,
            }),
            "truth_assessed": False,
        })

    summary = {
        "references": len(references),
        "with_identity": sum(1 for row in rows if row["identity"]),
        "without_identity": sum(1 for row in rows if not row["identity"]),
        "retracted": sum(1 for row in rows if row["retracted"]),
        "concern": sum(1 for row in rows if row["concern"]),
        "corrected": sum(
            1 for row in rows if row["retraction_status"] == "corrected"),
        "unknown_retraction_status": sum(
            1 for row in rows if row["retraction_status"] == "unknown"),
        "duplicate_groups": len(duplicates),
        "duplicate_references": len(duplicate_of),
        "unresolved_keys": len({item["citation_key"] for item in unresolved}),
        "unresolved_citations": len(unresolved),
        "with_versions": sum(1 for row in rows if row["versions"]),
        "with_sources": sum(1 for row in rows if row["source_ids"]),
        "cites_work_edges": work_edge_count,
    }
    columns = [
        {"key": "reference_id", "label": "Reference"},
        {"key": "title", "label": "Title"},
        {"key": "citation_key", "label": "Citekey"},
        {"key": "identity", "label": "Identity"},
        {"key": "retraction_status", "label": "Retraction"},
        {"key": "metadata_status", "label": "Metadata"},
        {"key": "versions", "label": "Versions"},
        {"key": "duplicate_of", "label": "Duplicate of"},
        {"key": "source_ids", "label": "Sources"},
    ]
    return _envelope(
        state, "library", columns=columns, rows=rows, nodes=references,
        summary=summary, root=root, source=source,
        extra={"duplicates": duplicates, "unresolved_keys": unresolved},
    )


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def _cache_root(root: str | Path | None) -> Path | None:
    if root is None:
        return None
    return state_path(Path(root), "research", "cache")


def _cache_details(locator: Mapping[str, Any], cache_root: Path | None
                   ) -> tuple[str | None, bool | None, str]:
    relative = locator.get("cache_relative")
    if not isinstance(relative, str) or not relative.strip():
        return None, None, "recorded"
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        return None, None, "invalid_cache_path"
    if cache_root is None:
        return None, None, "cache_path_unchecked"
    target = cache_root.joinpath(*pure.parts)
    present = target.is_file()
    return str(target), present, "cached" if present else "missing_cache"


def _fetch(state: Mapping[str, Any], *, root: str | Path | None,
           source: str | Path | None) -> dict[str, Any]:
    nodes = _nodes(state)
    edges = _edges(state)
    references = sorted(
        (node for node in nodes if node.get("type") == "Reference"),
        key=lambda node: str(node["id"]),
    )
    sources = sorted(
        (node for node in nodes if node.get("type") == "Source"),
        key=lambda node: str(node["id"]),
    )
    cache_root = _cache_root(root)
    rows: list[dict[str, Any]] = []
    sources_by_reference: dict[str, set[str]] = defaultdict(set)

    for source_node in sources:
        locator = _locator(source_node) or {}
        locator_kind = _text(locator, "kind") or "unknown"
        is_artifact = locator_kind == "cached_fetch"
        reference_id = (_text(locator, "reference_id")
                        or _text(source_node, "reference_id"))
        if reference_id:
            sources_by_reference[reference_id].add(str(source_node["id"]))
        cache_path, cache_present, cache_status = _cache_details(
            locator, cache_root)
        provider = _text(locator, "provider")
        license_value = _text(locator, "license")
        digest = _text(locator, "sha256")
        url = _text(locator, "url")
        status = cache_status if is_artifact else "recorded_source"
        if is_artifact and cache_status == "recorded":
            status = "artifact_recorded"
        rows.append({
            "row_kind": "artifact" if is_artifact else "source",
            "source_id": str(source_node["id"]),
            "reference_id": reference_id,
            "name": _text(source_node, "name", "title"),
            "provider": provider,
            "license": license_value,
            "sha256": digest,
            "cache_relative": _text(locator, "cache_relative"),
            "cache_path": cache_path,
            "cache_present": cache_present,
            "url": url,
            "media_type": _text(locator, "media_type"),
            "version": _text(locator, "version"),
            "size": (locator.get("size")
                     if isinstance(locator.get("size"), int)
                     and not isinstance(locator.get("size"), bool) else None),
            "retrieved_at": _text(locator, "retrieved_at"),
            "external": (bool(locator.get("external"))
                         if "external" in locator else None),
            "unofficial": (bool(locator.get("unofficial"))
                           if "unofficial" in locator else None),
            "locator_kind": locator_kind,
            "status": status,
            "missing": _missing({
                "provider": provider,
                "license": license_value,
                "sha256": digest,
                "cache_path": cache_path,
                "reference_id": reference_id,
            }),
            "truth_assessed": False,
        })

    for edge in edges:
        if edge.get("type") != "manifestation_of":
            continue
        source_id = str(edge.get("src") or "")
        reference_id = str(edge.get("tgt") or "")
        if source_id and reference_id:
            sources_by_reference[reference_id].add(source_id)

    candidate_references: set[str] = set()
    for reference in references:
        reference_id = str(reference["id"])
        open_access = reference.get("open_access")
        if not isinstance(open_access, Mapping):
            continue
        url = _text(open_access, "url")
        candidate_references.add(reference_id)
        rows.append({
            "row_kind": "candidate",
            "source_id": None,
            "reference_id": reference_id,
            "name": _text(reference, "title", "raw"),
            "provider": "recorded_open_access",
            "license": _text(reference, "license"),
            "sha256": None,
            "cache_relative": None,
            "cache_path": None,
            "cache_present": None,
            "url": url,
            "media_type": None,
            "version": _text(open_access, "status"),
            "size": None,
            "retrieved_at": None,
            "external": None,
            "unofficial": None,
            "locator_kind": "open_access_metadata",
            "status": "metadata_candidate" if url else "candidate_without_url",
            "missing": _missing({"url": url}),
            "truth_assessed": False,
        })

    for reference in references:
        reference_id = str(reference["id"])
        if reference_id in sources_by_reference or reference_id in candidate_references:
            continue
        identifiers = normalize_identifiers(reference.get("identifiers"))
        rows.append({
            "row_kind": "unresolved_reference",
            "source_id": None,
            "reference_id": reference_id,
            "name": _text(reference, "title", "raw"),
            "provider": None,
            "license": _text(reference, "license"),
            "sha256": None,
            "cache_relative": None,
            "cache_path": None,
            "cache_present": None,
            "url": None,
            "media_type": None,
            "version": None,
            "size": None,
            "retrieved_at": None,
            "external": None,
            "unofficial": None,
            "locator_kind": None,
            "status": "identifier_only" if identifiers else "no_identifier",
            "identifiers": identifiers,
            "missing": [] if identifiers else ["identifiers"],
            "truth_assessed": False,
        })

    row_rank = {"artifact": 0, "candidate": 1, "source": 2,
                "unresolved_reference": 3}
    rows.sort(key=lambda row: (
        row_rank.get(str(row.get("row_kind")), 9),
        str(row.get("source_id") or row.get("reference_id") or ""),
        str(row.get("provider") or ""),
        str(row.get("url") or ""),
    ))

    artifact_rows = [row for row in rows if row["row_kind"] == "artifact"]
    candidate_rows = [row for row in rows if row["row_kind"] == "candidate"]
    unresolved_rows = [row for row in rows
                       if row["row_kind"] == "unresolved_reference"]
    providers = Counter(str(row["provider"]) for row in rows
                        if row.get("provider"))
    summary = {
        "references": len(references),
        "sources": len(sources),
        "artifacts": len(artifact_rows),
        "cached_artifacts": sum(1 for row in artifact_rows
                                if row["cache_present"] is True),
        "missing_artifacts": sum(1 for row in artifact_rows
                                 if row["cache_present"] is False),
        "candidates": len(candidate_rows),
        "unresolved_references": len(unresolved_rows),
        "identifier_only": sum(1 for row in unresolved_rows
                               if row["status"] == "identifier_only"),
        "no_identifier": sum(1 for row in unresolved_rows
                             if row["status"] == "no_identifier"),
        "with_license": sum(1 for row in rows if row.get("license")),
        "with_sha256": sum(1 for row in rows if row.get("sha256")),
        "total_recorded_bytes": sum(
            row["size"] for row in artifact_rows
            if isinstance(row.get("size"), int)),
        "provider_counts": dict(sorted(providers.items())),
    }
    columns = [
        {"key": "row_kind", "label": "Kind"},
        {"key": "reference_id", "label": "Reference"},
        {"key": "source_id", "label": "Source"},
        {"key": "provider", "label": "Provider"},
        {"key": "license", "label": "Licence"},
        {"key": "sha256", "label": "SHA-256"},
        {"key": "cache_path", "label": "Cache path"},
        {"key": "status", "label": "Status"},
        {"key": "url", "label": "URL"},
    ]
    involved = [*sources, *references]
    return _envelope(
        state, "fetch", columns=columns, rows=rows, nodes=involved,
        summary=summary, root=root, source=source,
    )


# --------------------------------------------------------------------------- #
# screening
# --------------------------------------------------------------------------- #
def _screening(state: Mapping[str, Any], *, root: str | Path | None,
               source: str | Path | None) -> dict[str, Any]:
    nodes = _nodes(state)
    edges = _edges(state)
    by_id = _by_id(nodes)
    ordered_nodes = sorted(nodes, key=_created_sort)
    # sorting before replay makes the existing PRISMA projection independent of
    # input node order: the last decision for a (reference, stage) wins.
    report = screening_report({"nodes": ordered_nodes, "edges": edges})
    decisions = [
        node for node in ordered_nodes
        if node.get("type") == "Decision" and node.get("screening") is True
    ]
    decision_ids = {str(node["id"]) for node in decisions}
    supersedes: dict[str, set[str]] = defaultdict(set)
    superseded_by: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("type") != "supersedes":
            continue
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        if src in decision_ids and tgt in decision_ids:
            supersedes[src].add(tgt)
            superseded_by[tgt].add(src)

    latest: dict[tuple[str, str], str] = {}
    for decision in decisions:
        key = (str(decision.get("reference_id") or ""),
               str(decision.get("stage") or ""))
        latest[key] = str(decision["id"])

    rows: list[dict[str, Any]] = []
    for decision in decisions:
        decision_id = str(decision["id"])
        reference_id = _text(decision, "reference_id")
        reference = by_id.get(reference_id or "")
        stage = _text(decision, "stage")
        current = latest.get((reference_id or "", stage or "")) == decision_id
        rows.append({
            "decision_id": decision_id,
            "reference_id": reference_id,
            "reference_title": _text(reference or {}, "title", "raw"),
            "citation_key": (
                _citation_keys(reference)[0]
                if reference and _citation_keys(reference) else None),
            "stage": stage,
            "decision": _text(decision, "decision"),
            "reason": _text(decision, "reason"),
            "reviewer": _text(decision, "reviewer"),
            "criteria": _plain(decision.get("criteria") or []),
            "created_at": _text(decision, "created_at"),
            "supersedes": sorted(supersedes.get(decision_id, set())),
            "superseded_by": sorted(superseded_by.get(decision_id, set())),
            "latest_for_stage": current,
            "truth_assessed": False,
            "missing": _missing({
                "reference_id": reference_id,
                "stage": stage,
                "decision": _text(decision, "decision"),
                "reason": _text(decision, "reason"),
                "reviewer": _text(decision, "reviewer"),
            }),
        })
    rows.sort(key=lambda row: str(row["decision_id"]))

    reference_nodes = sorted(
        (by_id[reference_id] for reference_id in {
            str(decision.get("reference_id") or "") for decision in decisions
        } if reference_id in by_id),
        key=lambda node: str(node["id"]),
    )
    reviewers = sorted({
        str(decision.get("reviewer"))
        for decision in decisions if decision.get("reviewer")
    })
    counts = report.get("counts") or {}
    summary = {
        "decisions": len(decisions),
        "superseded": sum(1 for row in rows if row["superseded_by"]),
        "reviewers": len(reviewers),
        "references": sum(
            1 for node in nodes if node.get("type") == "Reference"),
        **counts,
    }
    columns = [
        {"key": "decision_id", "label": "Decision"},
        {"key": "reference_id", "label": "Reference"},
        {"key": "citation_key", "label": "Citekey"},
        {"key": "stage", "label": "Stage"},
        {"key": "decision", "label": "Decision"},
        {"key": "reason", "label": "Reason"},
        {"key": "reviewer", "label": "Reviewer"},
        {"key": "latest_for_stage", "label": "Latest"},
        {"key": "superseded_by", "label": "Superseded by"},
    ]
    prisma = {
        "counts": counts,
        "included_references": report.get("included_references", []),
        "excluded_references": report.get("excluded_references", []),
        "unresolved_references": report.get("unresolved_references", []),
    }
    return _envelope(
        state, "screening", columns=columns, rows=rows,
        nodes=[*decisions, *reference_nodes], summary=summary,
        root=root, source=source, extra={"prisma": prisma},
    )


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #
def _reference_ids_for(
    node: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    cites_by_node: Mapping[str, set[str]],
) -> set[str]:
    reference_ids = set(cites_by_node.get(str(node.get("id")), set()))
    if node.get("type") == "Reference":
        reference_ids.add(str(node.get("id")))
    locator = _locator(node)
    if locator:
        reference_id = locator.get("reference_id")
        if isinstance(reference_id, str) and by_id.get(
                reference_id, {}).get("type") == "Reference":
            reference_ids.add(reference_id)
    return reference_ids


def _evidence_row(
    claim: Mapping[str, Any],
    relation: str,
    direction: str,
    other: Mapping[str, Any] | None,
    *,
    edge: Mapping[str, Any] | None,
    by_id: Mapping[str, Mapping[str, Any]],
    cites_by_node: Mapping[str, set[str]],
    status: str,
) -> dict[str, Any]:
    claim_locator = _locator(claim)
    evidence_locator = _locator(other)
    locator = evidence_locator or claim_locator
    reference_ids: set[str] = set()
    citation_keys = _citation_keys(claim)
    reference_ids.update(_reference_ids_for(claim, by_id, cites_by_node))
    if other is not None:
        citation_keys.extend(_citation_keys(other))
        reference_ids.update(_reference_ids_for(other, by_id, cites_by_node))
    reference_keys: list[str] = []
    for reference_id in sorted(reference_ids):
        reference = by_id.get(reference_id)
        if reference is not None:
            reference_keys.extend(_citation_keys(reference))
    citation_keys = sorted(set(citation_keys))
    reference_keys = sorted(set(reference_keys))
    return {
        "status": status,
        "claim_id": str(claim["id"]),
        "claim_wording": _label(claim),
        "claim_status": _text(claim, "status"),
        "claim_type": _text(claim, "claim_type"),
        "relation": relation,
        "direction": direction,
        "edge_id": str(edge["id"]) if edge is not None else None,
        "edge_rationale": _text(edge or {}, "rationale"),
        "edge_provenance": _mapping((edge or {}).get("provenance")),
        "evidence_id": str(other["id"]) if other is not None else None,
        "evidence_kind": str(other.get("type")) if other is not None else None,
        "evidence_wording": _label(other) if other is not None else None,
        "evidence_status": _text(other or {}, "status"),
        "claim_locator": claim_locator,
        "evidence_locator": evidence_locator,
        "locator": locator,
        "locator_label": _locator_label(locator),
        "source_id": (str(other["id"]) if other is not None
                      and other.get("type") == "Source" else None),
        "reference_ids": sorted(reference_ids),
        "citation_keys": citation_keys,
        "reference_citation_keys": reference_keys,
        "missing": _missing({
            "evidence_id": str(other["id"]) if other is not None else None,
            "locator": locator,
        }),
        "truth_assessed": False,
    }


def _evidence(state: Mapping[str, Any], *, root: str | Path | None,
              source: str | Path | None) -> dict[str, Any]:
    nodes = _nodes(state)
    edges = _edges(state)
    by_id = _by_id(nodes)
    claims = sorted(
        (node for node in nodes if node.get("type") == "Claim"),
        key=lambda node: str(node["id"]),
    )
    cites_by_node: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("type") != "cites":
            continue
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        if by_id.get(tgt, {}).get("type") == "Reference":
            cites_by_node[src].add(tgt)
        if by_id.get(src, {}).get("type") == "Reference":
            cites_by_node[tgt].add(src)

    rows: list[dict[str, Any]] = []
    linked_evidence: set[str] = set()
    for claim in claims:
        claim_id = str(claim["id"])
        incident = [
            edge for edge in edges
            if edge.get("type") in _EVIDENCE_RELATIONS
            and claim_id in (str(edge.get("src") or ""),
                             str(edge.get("tgt") or ""))
        ]
        incident.sort(key=lambda edge: (
            str(edge.get("type") or ""),
            str(edge.get("id") or ""),
            str(edge.get("src") or ""),
            str(edge.get("tgt") or ""),
        ))
        if not incident:
            rows.append(_evidence_row(
                claim, "none", "none", None, edge=None, by_id=by_id,
                cites_by_node=cites_by_node, status="no_recorded_evidence"))
            continue
        for edge in incident:
            src = str(edge.get("src") or "")
            tgt = str(edge.get("tgt") or "")
            other_id = tgt if src == claim_id else src
            other = by_id.get(other_id)
            linked_evidence.add(other_id)
            rows.append(_evidence_row(
                claim, str(edge.get("type") or "unknown"),
                "outgoing" if src == claim_id else "incoming",
                other, edge=edge, by_id=by_id, cites_by_node=cites_by_node,
                status="recorded_relation"))

    orphaned = 0
    for node in sorted(nodes, key=lambda item: str(item["id"])):
        if node.get("type") not in {"Evidence", "Observation"}:
            continue
        node_id = str(node["id"])
        if node_id in linked_evidence:
            continue
        orphaned += 1
        rows.append({
            "status": "unlinked_evidence",
            "claim_id": None,
            "claim_wording": None,
            "claim_status": None,
            "claim_type": None,
            "relation": "none",
            "direction": "none",
            "edge_id": None,
            "edge_rationale": None,
            "edge_provenance": None,
            "evidence_id": node_id,
            "evidence_kind": str(node.get("type")),
            "evidence_wording": _label(node),
            "evidence_status": _text(node, "status"),
            "claim_locator": None,
            "evidence_locator": _locator(node),
            "locator": _locator(node),
            "locator_label": _locator_label(_locator(node)),
            "source_id": node_id if node.get("type") == "Source" else None,
            "reference_ids": sorted(
                _reference_ids_for(node, by_id, cites_by_node)),
            "citation_keys": sorted(set(_citation_keys(node))),
            "reference_citation_keys": sorted({
                key for reference_id in _reference_ids_for(
                    node, by_id, cites_by_node)
                for key in _citation_keys(by_id.get(reference_id, {}))
            }),
            "missing": _missing({"claim_id": None, "locator": _locator(node)}),
            "truth_assessed": False,
        })

    rows.sort(key=lambda row: (
        0 if row.get("claim_id") else 1,
        str(row.get("claim_id") or ""),
        str(row.get("relation") or ""),
        str(row.get("evidence_id") or ""),
        str(row.get("edge_id") or ""),
    ))
    relation_counts = Counter(str(row["relation"]) for row in rows
                              if row["status"] == "recorded_relation")
    claims_with_evidence = {
        str(row["claim_id"]) for row in rows
        if row.get("claim_id") and row["status"] == "recorded_relation"
        and row["relation"] in {"supports", "opposes", "derived_from"}
    }
    claims_with_citations = {
        str(row["claim_id"]) for row in rows
        if row.get("claim_id") and row["status"] == "recorded_relation"
        and row["relation"] == "cites"
    }
    rows_without_locator = sum(
        1 for row in rows
        if row["status"] == "recorded_relation" and row["locator"] is None)
    evidence_nodes = [
        node for node in nodes
        if node.get("type") in {"Evidence", "Observation"}
    ]
    summary = {
        "claims": len(claims),
        "claims_with_evidence": len(claims_with_evidence),
        "claims_without_evidence": len(claims) - len(claims_with_evidence),
        "claims_with_citations": len(claims_with_citations),
        "evidence_records": len([node for node in evidence_nodes
                                 if node.get("type") == "Evidence"]),
        "observations": len([node for node in evidence_nodes
                             if node.get("type") == "Observation"]),
        "unlinked_evidence": orphaned,
        "support_edges": relation_counts.get("supports", 0),
        "oppose_edges": relation_counts.get("opposes", 0),
        "cite_edges": relation_counts.get("cites", 0),
        "derived_edges": relation_counts.get("derived_from", 0),
        "rows_without_locator": rows_without_locator,
    }
    columns = [
        {"key": "claim_id", "label": "Claim"},
        {"key": "claim_status", "label": "Claim status"},
        {"key": "relation", "label": "Relation"},
        {"key": "evidence_id", "label": "Evidence"},
        {"key": "evidence_kind", "label": "Kind"},
        {"key": "locator_label", "label": "Locator"},
        {"key": "citation_keys", "label": "Citation keys"},
        {"key": "reference_ids", "label": "References"},
        {"key": "status", "label": "Status"},
    ]
    graph = _graph_view(state, "evidence", root=root, source=source)
    return _envelope(
        state, "evidence", columns=columns, rows=rows,
        nodes=[*claims, *evidence_nodes, *[
            node for node in nodes if node.get("type") == "Reference"
        ]], summary=summary, root=root, source=source, graph=graph,
    )


# --------------------------------------------------------------------------- #
# consensus
# --------------------------------------------------------------------------- #
def _edge_targets(edges: Iterable[Mapping[str, Any]], node_id: str,
                  edge_type: str) -> list[str]:
    return sorted(str(edge.get("tgt")) for edge in edges
                  if edge.get("type") == edge_type
                  and str(edge.get("src") or "") == node_id
                  and edge.get("tgt") is not None)


def _cluster_sort_key(item: Mapping[str, Any]) -> str:
    return json.dumps(_plain(item), sort_keys=True, ensure_ascii=False,
                      allow_nan=False)


def _consensus(state: Mapping[str, Any], *, root: str | Path | None,
               source: str | Path | None) -> dict[str, Any]:
    nodes = _nodes(state)
    edges = _edges(state)
    by_id = _by_id(nodes)
    plans = sorted((node for node in nodes
                    if node.get("type") == "ReviewPlan"),
                   key=lambda node: str(node["id"]))
    runs = sorted((node for node in nodes
                   if node.get("type") == "ReviewRun"),
                  key=lambda node: str(node["id"]))
    findings = sorted((node for node in nodes
                       if node.get("type") == "ReviewFinding"),
                      key=lambda node: str(node["id"]))
    reconciliations = sorted((node for node in nodes
                              if node.get("type") == "Reconciliation"),
                             key=lambda node: str(node["id"]))

    plan_runs: dict[str, set[str]] = defaultdict(set)
    run_plan: dict[str, str] = {}
    for run in runs:
        run_id = str(run["id"])
        plan_id = _text(run, "plan_id")
        if plan_id:
            plan_runs[plan_id].add(run_id)
            run_plan[run_id] = plan_id
    for edge in edges:
        if edge.get("type") != "implements":
            continue
        run_id = str(edge.get("src") or "")
        plan_id = str(edge.get("tgt") or "")
        if by_id.get(run_id, {}).get("type") == "ReviewRun" \
                and by_id.get(plan_id, {}).get("type") == "ReviewPlan":
            plan_runs[plan_id].add(run_id)
            run_plan[run_id] = plan_id

    run_findings: dict[str, set[str]] = defaultdict(set)
    finding_run: dict[str, str] = {}
    for finding in findings:
        finding_id = str(finding["id"])
        run_id = _text(finding, "run_id")
        if run_id:
            run_findings[run_id].add(finding_id)
            finding_run[finding_id] = run_id
    for edge in edges:
        if edge.get("type") != "finds":
            continue
        run_id = str(edge.get("src") or "")
        finding_id = str(edge.get("tgt") or "")
        if by_id.get(run_id, {}).get("type") == "ReviewRun" \
                and by_id.get(finding_id, {}).get("type") == "ReviewFinding":
            run_findings[run_id].add(finding_id)
            finding_run[finding_id] = run_id

    plan_findings: dict[str, set[str]] = defaultdict(set)
    for finding in findings:
        finding_id = str(finding["id"])
        plan_id = _text(finding, "plan_id")
        if plan_id:
            plan_findings[plan_id].add(finding_id)
        run_id = finding_run.get(finding_id)
        if run_id and run_id in run_plan:
            plan_findings[run_plan[run_id]].add(finding_id)

    plan_reconciliations: dict[str, set[str]] = defaultdict(set)
    for reconciliation in reconciliations:
        reconciliation_id = str(reconciliation["id"])
        plan_id = _text(reconciliation, "plan_id")
        if plan_id:
            plan_reconciliations[plan_id].add(reconciliation_id)

    kind_rank = {"plan": 0, "run": 1, "finding": 2, "reconciliation": 3}
    rows: list[dict[str, Any]] = []
    for plan in plans:
        plan_id = str(plan["id"])
        run_ids = sorted(plan_runs.get(plan_id, set()))
        finding_ids = sorted(plan_findings.get(plan_id, set()))
        plan_targets = set(_edge_targets(edges, plan_id, "addresses"))
        plan_target = _text(plan, "target_id")
        if plan_target:
            plan_targets.add(plan_target)
        rows.append({
            "row_kind": "plan",
            "id": plan_id,
            "created_at": _text(plan, "created_at"),
            "question": _text(plan, "question"),
            "kind": _text(plan, "kind"),
            "status": _text(plan, "status"),
            "roles": _string_list(plan.get("roles")),
            "models": _string_list(plan.get("models")),
            "rounds": _string_list(plan.get("rounds")),
            "blinding": plan.get("blinding"),
            "min_reviewers": plan.get("min_reviewers"),
            "adjudication": _text(plan, "adjudication"),
            "target_ids": sorted(plan_targets),
            "run_ids": run_ids,
            "finding_ids": finding_ids,
            "reconciliation_ids": sorted(
                plan_reconciliations.get(plan_id, set())),
            "truth_assessed": False,
            "missing": _missing({
                "question": _text(plan, "question"),
                "kind": _text(plan, "kind"),
                "status": _text(plan, "status"),
            }),
        })
    for run in runs:
        run_id = str(run["id"])
        run_targets = sorted(
            set(_edge_targets(edges, run_id, "reviews"))
            | set(_string_list(run.get("target_ids"))))
        rows.append({
            "row_kind": "run",
            "id": run_id,
            "created_at": _text(run, "created_at"),
            "plan_id": _text(run, "plan_id") or run_plan.get(run_id),
            "reviewer_id": _text(run, "reviewer_id"),
            "role": _text(run, "role"),
            "model": _text(run, "model"),
            "provider": _text(run, "provider"),
            "round": _text(run, "round"),
            "status": _text(run, "status"),
            "summary": _text(run, "summary"),
            "recommendation": _text(run, "recommendation"),
            "confidence": run.get("confidence"),
            "finding_count": run.get("finding_count"),
            "finding_ids": sorted(run_findings.get(run_id, set())),
            "target_ids": run_targets,
            "warnings": _string_list(run.get("warnings")),
            "tokens": _plain(run.get("tokens")),
            "cost": run.get("cost"),
            "output_sha256": _text(run, "output_sha256"),
            "truth_assessed": False,
            "missing": _missing({
                "plan_id": _text(run, "plan_id") or run_plan.get(run_id),
                "reviewer_id": _text(run, "reviewer_id"),
                "role": _text(run, "role"),
                "model": _text(run, "model"),
                "packet_sha256": _text(run, "packet_sha256"),
            }),
        })
    for finding in findings:
        finding_id = str(finding["id"])
        agrees = sorted(str(edge.get("tgt")) for edge in edges
                        if edge.get("type") == "agrees_with"
                        and str(edge.get("src") or "") == finding_id)
        disagrees = sorted(str(edge.get("tgt")) for edge in edges
                           if edge.get("type") == "disagrees_with"
                           and str(edge.get("src") or "") == finding_id)
        agreed_by = sorted(str(edge.get("src")) for edge in edges
                           if edge.get("type") == "agrees_with"
                           and str(edge.get("tgt") or "") == finding_id)
        disagreed_by = sorted(str(edge.get("src")) for edge in edges
                              if edge.get("type") == "disagrees_with"
                              and str(edge.get("tgt") or "") == finding_id)
        rows.append({
            "row_kind": "finding",
            "id": finding_id,
            "created_at": _text(finding, "created_at"),
            "plan_id": _text(finding, "plan_id"),
            "run_id": _text(finding, "run_id") or finding_run.get(finding_id),
            "wording": _text(finding, "wording"),
            "severity": _text(finding, "severity"),
            "issue_type": _text(finding, "issue_type"),
            "quote": _text(finding, "quote"),
            "recommendation": _text(finding, "recommendation"),
            "confidence": finding.get("confidence"),
            "citation_keys": _string_list(finding.get("citation_keys")),
            "claim_id": _text(finding, "claim_id"),
            "reference_id": _text(finding, "reference_id"),
            "evidence_ids": _string_list(finding.get("evidence_ids")),
            "argument_id": _text(finding, "argument_id"),
            "target_ids": _edge_targets(edges, finding_id, "about"),
            "agrees_with": agrees,
            "disagrees_with": disagrees,
            "agreed_by": agreed_by,
            "disagreed_by": disagreed_by,
            "truth_assessed": False,
            "missing": _missing({
                "wording": _text(finding, "wording"),
                "severity": _text(finding, "severity"),
                "issue_type": _text(finding, "issue_type"),
                "run_id": _text(finding, "run_id") or finding_run.get(
                    finding_id),
            }),
        })
    for reconciliation in reconciliations:
        reconciliation_id = str(reconciliation["id"])
        rows.append({
            "row_kind": "reconciliation",
            "id": reconciliation_id,
            "created_at": _text(reconciliation, "created_at"),
            "plan_id": _text(reconciliation, "plan_id"),
            "method": _text(reconciliation, "method"),
            "min_reviewers": reconciliation.get("min_reviewers"),
            "run_ids": _string_list(reconciliation.get("run_ids")),
            "reconciles_run_ids": _edge_targets(
                edges, reconciliation_id, "reconciles"),
            "metrics": _plain(reconciliation.get("metrics") or {}),
            "clusters": _plain(reconciliation.get("clusters") or []),
            "consensus": _plain(reconciliation.get("consensus") or []),
            "dissent": _plain(reconciliation.get("dissent") or []),
            "status": _text(reconciliation, "status"),
            "adjudicator": _plain(reconciliation.get("adjudicator")),
            "rationale": _text(reconciliation, "rationale"),
            "truth_assessed": False,
            "missing": _missing({
                "plan_id": _text(reconciliation, "plan_id"),
                "method": _text(reconciliation, "method"),
            }),
        })
    rows.sort(key=lambda row: (
        str(row.get("created_at") or ""),
        kind_rank.get(str(row.get("row_kind")), 9),
        str(row.get("id") or ""),
    ))

    consensus_items: list[dict[str, Any]] = []
    dissent_items: list[dict[str, Any]] = []
    for reconciliation in reconciliations:
        reconciliation_id = str(reconciliation["id"])
        plan_id = _text(reconciliation, "plan_id")
        recorded_consensus = reconciliation.get("consensus")
        if isinstance(recorded_consensus, list):
            for item in recorded_consensus:
                entry = _plain(item) if isinstance(item, Mapping) else {
                    "item": _plain(item)}
                consensus_items.append({
                    **entry,
                    "reconciliation_id": reconciliation_id,
                    "plan_id": plan_id,
                })
        recorded_dissent = reconciliation.get("dissent")
        if isinstance(recorded_dissent, list):
            for item in recorded_dissent:
                entry = _plain(item) if isinstance(item, Mapping) else {
                    "item": _plain(item)}
                dissent_items.append({
                    **entry,
                    "reconciliation_id": reconciliation_id,
                    "plan_id": plan_id,
                })
    consensus_items.sort(key=lambda item: (
        str(item.get("reconciliation_id") or ""), _cluster_sort_key(item)))
    dissent_items.sort(key=lambda item: (
        str(item.get("reconciliation_id") or ""), _cluster_sort_key(item)))

    plans_without_reconciliation = sum(
        1 for plan in plans
        if not plan_reconciliations.get(str(plan["id"])))
    summary = {
        "plans": len(plans),
        "runs": len(runs),
        "findings": len(findings),
        "reconciliations": len(reconciliations),
        "consensus_items": len(consensus_items),
        "dissent_items": len(dissent_items),
        "plans_without_runs": sum(
            1 for plan in plans if not plan_runs.get(str(plan["id"]))),
        "plans_without_reconciliation": plans_without_reconciliation,
        "runs_without_plan": sum(
            1 for run in runs
            if (_text(run, "plan_id") or run_plan.get(str(run["id"])))
            not in by_id),
        "findings_without_run": sum(
            1 for finding in findings
            if (_text(finding, "run_id") or finding_run.get(
                str(finding["id"]))) not in by_id),
    }
    columns = [
        {"key": "row_kind", "label": "Record"},
        {"key": "id", "label": "ID"},
        {"key": "plan_id", "label": "Plan"},
        {"key": "wording", "label": "Wording"},
        {"key": "severity", "label": "Severity"},
        {"key": "model", "label": "Model"},
        {"key": "status", "label": "Status"},
        {"key": "consensus", "label": "Consensus"},
        {"key": "dissent", "label": "Dissent"},
    ]
    graph = _graph_view(state, "consensus", root=root, source=source)
    return _envelope(
        state, "consensus", columns=columns, rows=rows,
        nodes=[*plans, *runs, *findings, *reconciliations], summary=summary,
        root=root, source=source, graph=graph,
        extra={"consensus": consensus_items, "dissent": dissent_items},
    )


# --------------------------------------------------------------------------- #
# citation
# --------------------------------------------------------------------------- #
def _edge_scope(edge_type: str) -> str:
    if edge_type == "cites_work":
        return "work_to_work"
    if edge_type == "cites":
        return "record_to_reference"
    if edge_type in {"same_as", "supersedes"}:
        return "version"
    if edge_type == "manifestation_of":
        return "artifact"
    return "other"


def _citation(state: Mapping[str, Any], *, root: str | Path | None,
              source: str | Path | None) -> dict[str, Any]:
    nodes = _nodes(state)
    edges = _edges(state)
    by_id = _by_id(nodes)
    references = sorted(
        (node for node in nodes if node.get("type") == "Reference"),
        key=lambda node: str(node["id"]),
    )
    reference_ids = {str(node["id"]) for node in references}
    sources = sorted(
        (node for node in nodes if node.get("type") == "Source"),
        key=lambda node: str(node["id"]),
    )

    work_out: dict[str, set[str]] = defaultdict(set)
    work_in: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("type") != "cites_work":
            continue
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        if src in reference_ids and tgt in reference_ids:
            work_out[src].add(tgt)
            work_in[tgt].add(src)

    alert_ids: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if node.get("type") != "Alert":
            continue
        target_id = _text(node, "target_id")
        if target_id:
            alert_ids[target_id].add(str(node["id"]))
    for edge in edges:
        if edge.get("type") != "alerts":
            continue
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        if by_id.get(src, {}).get("type") == "Alert":
            alert_ids[tgt].add(src)

    edge_rows: list[dict[str, Any]] = []
    for edge in edges:
        edge_type = str(edge.get("type") or "unknown")
        if edge_type not in _CITATION_EDGE_TYPES:
            continue
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        src_node = by_id.get(src)
        tgt_node = by_id.get(tgt)
        edge_rows.append({
            "edge_id": str(edge.get("id") or f"e:{src}:{edge_type}:{tgt}"),
            "edge_type": edge_type,
            "scope": _edge_scope(edge_type),
            "src_id": src,
            "src_type": str(src_node.get("type")) if src_node else None,
            "src_title": _text(src_node or {}, "title", "wording", "name"),
            "src_citation_key": (
                _citation_keys(src_node)[0]
                if src_node and _citation_keys(src_node) else None),
            "src_retraction_status": (
                _retraction(src_node)["status"] if src_node else "unknown"),
            "tgt_id": tgt,
            "tgt_type": str(tgt_node.get("type")) if tgt_node else None,
            "tgt_title": _text(tgt_node or {}, "title", "wording", "name"),
            "tgt_citation_key": (
                _citation_keys(tgt_node)[0]
                if tgt_node and _citation_keys(tgt_node) else None),
            "tgt_retraction_status": (
                _retraction(tgt_node)["status"] if tgt_node else "unknown"),
            "provenance": _mapping(edge.get("provenance")),
            "rationale": _text(edge, "rationale"),
            "created_at": _text(edge, "created_at"),
            "truth_assessed": False,
        })
    edge_rank = {name: index for index, name in enumerate(sorted(
        _CITATION_EDGE_TYPES))}
    edge_rows.sort(key=lambda row: (
        edge_rank.get(str(row["edge_type"]), 9),
        str(row["src_id"]),
        str(row["tgt_id"]),
        str(row["edge_id"]),
    ))

    retraction_rows: list[dict[str, Any]] = []
    for reference in references:
        reference_id = str(reference["id"])
        retraction = _retraction(reference)
        if retraction["status"] in {"none", "unknown"}:
            continue
        retraction_rows.append({
            "reference_id": reference_id,
            "title": _text(reference, "title", "raw"),
            "citation_key": (
                _citation_keys(reference)[0]
                if _citation_keys(reference) else None),
            "identity": reference_identity(reference),
            "retraction_status": retraction["status"],
            "provider": retraction["provider"],
            "checked_at": retraction["checked_at"],
            "notices": retraction["notices"],
            "cited_by": sorted(
                str(edge.get("src") or "") for edge in edges
                if edge.get("type") == "cites"
                and str(edge.get("tgt") or "") == reference_id),
            "work_citations": sorted(work_out.get(reference_id, set())),
            "work_cited_by": sorted(work_in.get(reference_id, set())),
            "alert_ids": sorted(alert_ids.get(reference_id, set())),
            "truth_assessed": False,
            "missing": _missing({
                "checked_at": retraction["checked_at"],
                "provider": retraction["provider"],
            }),
        })
    retraction_rows.sort(key=lambda row: str(row["reference_id"]))

    neighbourhood_rows: list[dict[str, Any]] = []
    for reference in references:
        reference_id = str(reference["id"])
        outgoing = sorted(work_out.get(reference_id, set()))
        incoming = sorted(work_in.get(reference_id, set()))
        total = len(outgoing) + len(incoming)
        neighbourhood_rows.append({
            "reference_id": reference_id,
            "title": _text(reference, "title", "raw"),
            "citation_key": (
                _citation_keys(reference)[0]
                if _citation_keys(reference) else None),
            "outgoing": outgoing,
            "incoming": incoming,
            "neighbours": sorted(set(outgoing) | set(incoming)),
            "out_degree": len(outgoing),
            "in_degree": len(incoming),
            "total_degree": total,
            "depth": 1,
            "status": "linked" if total else "isolated",
            "truth_assessed": False,
        })
    neighbourhood_rows.sort(key=lambda row: str(row["reference_id"]))

    statuses = Counter(_retraction(node)["status"] for node in references)
    linked_references = sum(
        1 for row in neighbourhood_rows if row["status"] == "linked")
    summary = {
        "references": len(references),
        "sources": len(sources),
        "cites_work_edges": sum(
            1 for edge in edges if edge.get("type") == "cites_work"),
        "cites_edges": sum(
            1 for edge in edges if edge.get("type") == "cites"),
        "same_as_edges": sum(
            1 for edge in edges if edge.get("type") == "same_as"),
        "manifestation_edges": sum(
            1 for edge in edges if edge.get("type") == "manifestation_of"),
        "supersedes_edges": sum(
            1 for edge in edges if edge.get("type") == "supersedes"),
        "retracted": statuses.get("retracted", 0),
        "concern": statuses.get("concern", 0),
        "corrected": statuses.get("corrected", 0),
        "unknown_retraction_status": statuses.get("unknown", 0),
        "linked_references": linked_references,
        "isolated_references": len(references) - linked_references,
        "max_total_degree": max(
            (row["total_degree"] for row in neighbourhood_rows), default=0),
    }
    columns = [
        {"key": "edge_type", "label": "Edge"},
        {"key": "scope", "label": "Scope"},
        {"key": "src_id", "label": "Source"},
        {"key": "src_citation_key", "label": "Source key"},
        {"key": "tgt_id", "label": "Target"},
        {"key": "tgt_citation_key", "label": "Target key"},
        {"key": "src_retraction_status", "label": "Source retraction"},
        {"key": "tgt_retraction_status", "label": "Target retraction"},
    ]
    graph = _graph_view(
        state, _CITATION_APERTURE, root=root, source=source)
    return _envelope(
        state, "citation", columns=columns, rows=edge_rows,
        nodes=[*references, *sources], summary=summary, root=root,
        source=source, graph=graph,
        extra={
            "retractions": retraction_rows,
            "neighbourhoods": neighbourhood_rows,
        },
    )


_BUILDERS = {
    "library": _library,
    "fetch": _fetch,
    "screening": _screening,
    "evidence": _evidence,
    "consensus": _consensus,
    "citation": _citation,
}


def build_research_view(
    state: Any,
    view: str,
    *,
    root: str | Path | None = None,
    source: str | Path | None = None,
    investigation_id: str | None = None,
) -> dict[str, Any]:
    """Project a research graph state into one read-only view model."""
    if view not in VIEWS:
        raise ResearchUIError(
            f"unknown research view: {view!r} (choose from {', '.join(VIEWS)})")
    data = _as_state(state, investigation_id=investigation_id)
    return _BUILDERS[view](data, root=root, source=source)


def render_json(model: Mapping[str, Any]) -> str:
    """Serialise a research view deterministically."""
    return json.dumps(model, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"


# --------------------------------------------------------------------------- #
# self-contained HTML
# --------------------------------------------------------------------------- #
_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; font-family: system-ui, -apple-system,
       "Segoe UI", sans-serif; color: #1d2733; background: #fbfcfd; }
header, section { max-width: 1280px; margin: 0 auto 18px; }
h1 { margin: 0 0 6px; font-size: 1.7rem; }
h2 { margin: 0 0 8px; font-size: 1.05rem; }
h3 { margin: 12px 0 6px; font-size: .95rem; }
.meta { margin: 0 0 8px; color: #52616f; font-size: .9rem; }
.note { margin: 0; padding: 8px 10px; border-left: 3px solid #b07d2b;
        background: #fff8e8; font-size: .88rem; }
section { background: #fff; border: 1px solid #dde4ea; border-radius: 8px;
          padding: 12px 14px; }
.facts { display: grid; grid-template-columns: 190px 1fr; gap: 3px 10px;
         font-size: .88rem; margin: 0; }
.facts dt { color: #52616f; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.tables { overflow: auto; }
table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #e7ecf1;
         vertical-align: top; white-space: pre-wrap; overflow-wrap: anywhere; }
th { color: #52616f; font-weight: 600; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre { background: #f5f7f9; padding: 10px; border-radius: 6px; overflow: auto;
      font-size: .8rem; white-space: pre-wrap; overflow-wrap: anywhere; }
.empty, .muted { color: #52616f; }
ul { margin: 0; padding-left: 20px; }
@media (max-width: 760px) { body { padding: 12px; } }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                          allow_nan=False)
    return str(value)


def _html_summary(summary: Mapping[str, Any]) -> str:
    parts = ['<section><h2>Summary</h2><dl class="facts">']
    for key in sorted(summary):
        parts.append(f"<dt>{_esc(key)}</dt><dd>{_esc(_display(summary[key]))}</dd>")
    parts.append("</dl></section>")
    return "\n".join(parts)


def _html_rows(model: Mapping[str, Any]) -> str:
    rows = model.get("rows") or []
    columns = model.get("columns") or []
    if not columns:
        keys: set[str] = set()
        for row in rows:
            if isinstance(row, Mapping):
                keys.update(str(key) for key in row)
        columns = [{"key": key, "label": key} for key in sorted(keys)]
    parts = ['<section><h2>Rows</h2><div class="tables">']
    if not rows:
        parts.append('<p class="empty">No rows recorded for this view.</p>')
    else:
        parts.append("<table><thead><tr>")
        for column in columns:
            parts.append(f"<th>{_esc(column.get('label', column.get('key')))}</th>")
        parts.append("</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>")
            for column in columns:
                key = str(column.get("key"))
                parts.append(f"<td>{_esc(_display(row.get(key)))}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
    parts.append("</div></section>")
    return "\n".join(parts)


def _html_graph(graph: Mapping[str, Any]) -> str:
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    parts = [
        '<section><h2>Graph projection</h2>',
        f'<p class="meta">aperture <strong>{_esc(graph.get("aperture"))}</strong>'
        f' · {len(nodes)} node(s) · {len(edges)} edge(s) · schema '
        f'{_esc(graph.get("schema"))}</p>',
    ]
    if not nodes:
        parts.append('<p class="empty">The graph projection is empty.</p>')
    else:
        parts.append('<div class="tables"><table><thead><tr>'
                     '<th>ID</th><th>Type</th><th>Label</th><th>Group</th>'
                     '</tr></thead><tbody>')
        for node in nodes:
            parts.append(
                "<tr>"
                f"<td>{_esc(node.get('id'))}</td>"
                f"<td>{_esc(node.get('type'))}</td>"
                f"<td>{_esc(node.get('label'))}</td>"
                f"<td>{_esc(node.get('group'))}</td>"
                "</tr>")
        parts.append("</tbody></table></div>")
    if edges:
        parts.append('<div class="tables"><table><thead><tr>'
                     '<th>Source</th><th>Edge</th><th>Target</th>'
                     '</tr></thead><tbody>')
        for edge in edges:
            parts.append(
                "<tr>"
                f"<td>{_esc(edge.get('src'))}</td>"
                f"<td>{_esc(edge.get('raw_type') or edge.get('type'))}</td>"
                f"<td>{_esc(edge.get('tgt'))}</td>"
                "</tr>")
        parts.append("</tbody></table></div>")
    parts.append("</section>")
    return "\n".join(parts)


def _html_extra(model: Mapping[str, Any]) -> str:
    skip = {
        "schema", "schema_name", "view", "title", "investigation_id",
        "columns", "nodes", "rows", "summary", "provenance", "omitted",
        "not_assessed", "truth_assessed", "note", "graph",
    }
    titles = {
        "prisma": "PRISMA counts",
        "unresolved_keys": "Unresolved keys",
        "duplicates": "Duplicate groups",
        "retractions": "Retractions",
        "neighbourhoods": "Citation neighbourhoods",
        "consensus": "Recorded consensus",
        "dissent": "Recorded dissent",
    }
    sections: list[str] = []
    for key in sorted(model):
        if key in skip:
            continue
        value = model[key]
        heading = _esc(titles.get(key, str(key).replace("_", " ").title()))
        if isinstance(value, list):
            parts = [f"<section><h2>{heading}</h2>"]
            if not value:
                parts.append('<p class="empty">No recorded items.</p>')
            elif all(isinstance(item, Mapping) for item in value):
                keys = sorted({str(item_key) for item in value
                               for item_key in item})
                parts.append('<div class="tables"><table><thead><tr>')
                for item_key in keys:
                    parts.append(f"<th>{_esc(item_key)}</th>")
                parts.append("</tr></thead><tbody>")
                for item in value:
                    parts.append("<tr>")
                    for item_key in keys:
                        parts.append(
                            f"<td>{_esc(_display(item.get(item_key)))}</td>")
                    parts.append("</tr>")
                parts.append("</tbody></table></div>")
            else:
                parts.append("<pre>" + _esc(_display(value)) + "</pre>")
            parts.append("</section>")
            sections.append("\n".join(parts))
        elif isinstance(value, Mapping):
            sections.append(
                f"<section><h2>{heading}</h2><pre>"
                + _esc(_display(value)) + "</pre></section>")
    return "\n".join(sections)


def _html_nodes(model: Mapping[str, Any]) -> str:
    nodes = model.get("nodes") or []
    parts = ['<section><h2>Records</h2>']
    if not nodes:
        parts.append('<p class="empty">No records in this view.</p>')
    else:
        parts.append('<div class="tables"><table><thead><tr>'
                     '<th>ID</th><th>Type</th><th>Label</th>'
                     '</tr></thead><tbody>')
        for node in nodes:
            parts.append(
                "<tr>"
                f"<td>{_esc(node.get('id'))}</td>"
                f"<td>{_esc(node.get('type'))}</td>"
                f"<td>{_esc(node.get('label'))}</td>"
                "</tr>")
        parts.append("</tbody></table></div>")
    parts.append("</section>")
    return "\n".join(parts)


def render_html(model: Mapping[str, Any], *, title: str | None = None) -> str:
    """Render one research view as a self-contained HTML document."""
    page_title = title or str(model.get("title") or "Research view")
    rows = len(model.get("rows") or [])
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(page_title)} — ozagia research view</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        "<header>",
        f"<h1>{_esc(page_title)}</h1>",
        '<p class="meta">view <strong>' + _esc(model.get("view")) +
        "</strong> · investigation <strong>" +
        _esc(model.get("investigation_id")) + "</strong> · schema " +
        _esc(model.get("schema")) + f" · {rows} row(s)</p>",
        '<p class="note">' + _esc(model.get("note")) + "</p>",
        "</header>",
        _html_summary(model.get("summary") or {}),
        _html_rows(model),
        _html_nodes(model),
        _html_extra(model),
    ]
    if isinstance(model.get("graph"), Mapping):
        parts.append(_html_graph(model["graph"]))
    parts.append('<section><h2>Not assessed</h2><ul>')
    for item in model.get("not_assessed") or []:
        parts.append(f"<li>{_esc(item)}</li>")
    parts.append("</ul></section>")
    parts.append('<section><h2>Omitted</h2><pre>'
                 + _esc(_display(model.get("omitted"))) + "</pre></section>")
    parts.append('<section><h2>Provenance</h2><pre>'
                 + _esc(_display(model.get("provenance"))) + "</pre></section>")
    parts.append('<section><details><summary>Full JSON model</summary><pre>'
                 + _esc(render_json(model)) + "</pre></details></section>")
    parts.extend(["</body>", "</html>", ""])
    return "\n".join(parts)


__all__ = [
    "MAX_ROWS",
    "ResearchUIError",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "VIEWS",
    "VIEW_TITLES",
    "build_research_view",
    "load_state",
    "render_html",
    "render_json",
]
