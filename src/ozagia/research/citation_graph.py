"""Provider-asserted citation links and local ``cites_work`` traversal.

OpenAlex is the only provider used here. ``fetch_related`` reads a work's
``referenced_works`` or the works that cite it, records bounded ``Reference``
candidates, and adds ``cites_work`` edges with provider provenance. A citation
is a bibliographic relation: it is not evidence that either work supports a
claim, so this module never creates ``supports`` edges and never mirrors a
provider's whole graph.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Mapping, NamedTuple
from urllib.parse import quote

from ozagia.research import references as refs
from ozagia.research.fetch import FetchError, http_get
from ozagia.research.graph import ResearchError

PROVIDER = "openalex"
OPENALEX_API = "https://api.openalex.org"
MAX_LIMIT = 100
MAX_NEIGHBORHOOD_DEPTH = 4
MAX_PATH_DEPTH = 16
_CITES_WORK = "cites_work"
_RATIONALE = (
    "OpenAlex asserts this bibliographic citation; it is not evidence that "
    "either work supports any claim."
)


class CitationGraphError(ResearchError):
    """A citation-graph request was invalid or a provider response was unusable."""


class _Payload(NamedTuple):
    data: Any
    response_sha256: str
    retrieved_at: str


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CitationGraphError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise CitationGraphError(f"{name} must be between {minimum} and {maximum}")
    return value


def _fetch_direction(value: Any) -> str:
    if not isinstance(value, str) or value not in {"references", "citations"}:
        raise CitationGraphError("direction must be references or citations")
    return value


def _traverse_direction(value: Any) -> str:
    if not isinstance(value, str):
        raise CitationGraphError("direction must be both, references, or citations")
    aliases = {
        "both": "both",
        "references": "references",
        "out": "references",
        "outgoing": "references",
        "citations": "citations",
        "in": "citations",
        "incoming": "citations",
    }
    normalized = aliases.get(value.strip().casefold())
    if normalized is None:
        raise CitationGraphError("direction must be both, references, or citations")
    return normalized


def _state_nodes(state: Any) -> list[dict[str, Any]]:
    if not isinstance(state, Mapping):
        raise CitationGraphError("citation traversal needs a graph state mapping")
    nodes = state.get("nodes")
    edges = state.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise CitationGraphError("graph state needs nodes and edges arrays")
    return [dict(node) for node in nodes if isinstance(node, Mapping)]


def _reference_node(state: Any, reference_id: Any) -> dict[str, Any]:
    if not isinstance(reference_id, str) or not reference_id:
        raise CitationGraphError("reference_id must be a non-empty string")
    for node in _state_nodes(state):
        if node.get("id") == reference_id:
            if node.get("type") != "Reference":
                raise CitationGraphError(f"{reference_id!r} is not a Reference node")
            return node
    raise CitationGraphError(f"unknown Reference id: {reference_id}")


def _cites_work_edges(state: Any) -> list[dict[str, Any]]:
    _state_nodes(state)
    return [
        dict(edge)
        for edge in state.get("edges", [])
        if isinstance(edge, Mapping) and edge.get("type") == _CITES_WORK
    ]


def _response_bytes(response: Any, url: str) -> bytes:
    if isinstance(response, Mapping):
        content = response.get("content", b"")
    elif isinstance(response, (bytes, bytearray, str)):
        content = response
    else:
        raise CitationGraphError(f"fetcher returned no usable content for {url}")
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    raise CitationGraphError(f"fetcher returned no usable content for {url}")


def _fetch_json(fetcher: Callable[..., Any], url: str) -> _Payload:
    try:
        response = fetcher(url, timeout=20, max_bytes=5_000_000)
    except (FetchError, ResearchError) as exc:
        raise CitationGraphError(f"OpenAlex request failed for {url}: {exc}") from exc
    content = _response_bytes(response, url)
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CitationGraphError(f"OpenAlex returned invalid JSON for {url}: {exc}") from exc
    return _Payload(
        data=data,
        response_sha256=hashlib.sha256(content).hexdigest(),
        retrieved_at=_utc(),
    )


def _source_work_url(reference: Mapping[str, Any]) -> str:
    identifiers = refs.normalize_identifiers(reference.get("identifiers"))
    if identifiers.get("openalex"):
        return f"{OPENALEX_API}/works/{identifiers['openalex']}"
    if identifiers.get("doi"):
        return f"{OPENALEX_API}/works/https://doi.org/{identifiers['doi']}"
    raise CitationGraphError(
        "citation fetch needs a Reference with an OpenAlex work ID or DOI")


def _resolve_work_id(reference: Mapping[str, Any],
                     fetcher: Callable[..., Any]) -> str:
    identifiers = refs.normalize_identifiers(reference.get("identifiers"))
    if identifiers.get("openalex"):
        return identifiers["openalex"]
    payload = _fetch_json(fetcher, _source_work_url(reference))
    if isinstance(payload.data, Mapping):
        work_id = refs.normalize_openalex(payload.data.get("id"))
        if work_id:
            return work_id
    raise CitationGraphError("OpenAlex work lookup returned no work id")


def _referenced_work_ids(payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        raise CitationGraphError("OpenAlex work response is not an object")
    raw = payload.get("referenced_works")
    if not isinstance(raw, list):
        return []
    work_ids: list[str] = []
    seen: set[str] = set()
    for item in raw:
        work_id = refs.normalize_openalex(item)
        if work_id and work_id not in seen:
            seen.add(work_id)
            work_ids.append(work_id)
    return work_ids


def _citation_results(payload: Any) -> list[Any]:
    if not isinstance(payload, Mapping):
        raise CitationGraphError("OpenAlex citation response is not an object")
    results = payload.get("results")
    if results is None:
        return []
    if not isinstance(results, list):
        raise CitationGraphError("OpenAlex citation response has no results array")
    return results


def _work_reference(payload: Any, info: _Payload) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CitationGraphError("OpenAlex work record is not an object")
    authors: list[str] = []
    authorships = payload.get("authorships")
    if isinstance(authorships, list):
        for authorship in authorships:
            if not isinstance(authorship, Mapping):
                continue
            author = authorship.get("author")
            if isinstance(author, Mapping):
                name = refs._collapse(author.get("display_name", ""))
                if name:
                    authors.append(name)
    primary = payload.get("primary_location")
    source = primary.get("source") if isinstance(primary, Mapping) else None
    venue = refs._collapse(source.get("display_name", "")) if isinstance(source, Mapping) else ""
    reference: dict[str, Any] = {
        "title": refs._collapse(payload.get("title") or payload.get("display_name") or ""),
        "authors": authors,
        "year": refs._year(payload.get("publication_year")),
        "venue": venue,
        "reference_type": refs._collapse(payload.get("type")) or "misc",
        "abstract": "",
        "url": refs._collapse(payload.get("id", "")),
        "identifiers": {
            "doi": payload.get("doi"),
            "openalex": payload.get("id"),
        },
        "metadata_status": "resolved_openalex",
        "metadata_provenance": [{
            "provider": PROVIDER,
            "retrieved_at": info.retrieved_at,
            "response_sha256": info.response_sha256,
        }],
    }
    retracted = payload.get("is_retracted")
    if retracted is not None:
        reference["retraction"] = {
            "status": "retracted" if retracted else "none",
            "provider": PROVIDER,
            "checked_at": info.retrieved_at,
            "notices": [],
        }
    open_access = payload.get("open_access")
    if isinstance(open_access, Mapping):
        reference["open_access"] = {
            "is_oa": bool(open_access.get("is_oa")),
            "status": open_access.get("oa_status"),
            "url": open_access.get("oa_url"),
        }
    return refs.normalize_reference(reference)


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = refs.reference_identity(candidate)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        unique.append(candidate)
    return unique


def _provenance_key(item: Any) -> tuple[str, str] | None:
    if not isinstance(item, Mapping):
        return None
    return (
        str(item.get("provider", "")),
        str(item.get("response_sha256", "")),
    )


def _candidate_for_recording(
    state: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Avoid appending the same provider response provenance twice."""
    existing = refs.find_reference(state, candidate)
    provenance = candidate.get("metadata_provenance")
    if existing is None or not isinstance(provenance, list):
        return dict(candidate)
    known = existing.get("metadata_provenance")
    if not isinstance(known, list):
        return dict(candidate)
    known_keys = {_provenance_key(item) for item in known}
    incoming_keys = {_provenance_key(item) for item in provenance}
    if incoming_keys and None not in incoming_keys and incoming_keys <= known_keys:
        return {
            key: value for key, value in candidate.items()
            if key != "metadata_provenance"
        }
    return dict(candidate)


def _ensure_edge(
    graph: Any,
    existing: dict[tuple[str, str], dict[str, Any]],
    src: str,
    tgt: str,
    provenance: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    edge = existing.get((src, tgt))
    if edge is not None:
        return edge, False
    edge = graph.link(src, _CITES_WORK, tgt, provenance=provenance, rationale=_RATIONALE)
    existing[(src, tgt)] = edge
    return edge, True


def fetch_related(
    graph: Any,
    reference_id: str,
    *,
    direction: str = "references",
    limit: int = 25,
    fetcher: Callable[..., Any] | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    """Fetch provider-asserted references or citations for one Reference.

    ``direction="references"`` reads the source work's ``referenced_works`` and
    records ``A --cites_work--> B`` edges. ``direction="citations"`` queries
    works that cite the source and records ``B --cites_work--> A``. With
    ``apply=False`` nothing is written and ``recorded``/``edges`` stay empty.
    """
    direction = _fetch_direction(direction)
    limit = _bounded_int(limit, name="limit", minimum=1, maximum=MAX_LIMIT)
    state = graph.state()
    reference = _reference_node(state, reference_id)
    fetcher = http_get if fetcher is None else fetcher

    if direction == "references":
        payload = _fetch_json(fetcher, _source_work_url(reference))
        work_ids = _referenced_work_ids(payload.data)[:limit]
        candidates = []
        for work_id in work_ids:
            work = _fetch_json(fetcher, f"{OPENALEX_API}/works/{work_id}")
            candidates.append(_work_reference(work.data, work))
    else:
        work_id = _resolve_work_id(reference, fetcher)
        payload = _fetch_json(
            fetcher,
            f"{OPENALEX_API}/works?filter=cites:{quote(work_id)}&per-page={limit}",
        )
        candidates = [
            _work_reference(item, payload)
            for item in _citation_results(payload.data)[:limit]
        ]

    candidates = _dedupe_candidates(candidates)
    result: dict[str, Any] = {
        "reference": reference,
        "direction": direction,
        "candidates": candidates,
        "recorded": [],
        "edges": [],
        "count": len(candidates),
        "limit": limit,
        "provider": PROVIDER,
        "truth_assessed": False,
    }
    if not apply:
        return result

    provenance = {
        "provider": PROVIDER,
        "retrieved_at": payload.retrieved_at,
        "response_sha256": payload.response_sha256,
        "direction": direction,
        "source_reference_id": reference_id,
    }
    existing = {
        (edge.get("src"), edge.get("tgt")): edge
        for edge in _cites_work_edges(state)
    }
    recorded: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for candidate in candidates:
        recorded_reference = refs.record_reference(
            graph, _candidate_for_recording(state, candidate))
        target = recorded_reference["reference"]
        if direction == "references":
            src, tgt = reference_id, target["id"]
        else:
            src, tgt = target["id"], reference_id
        if src == tgt:
            recorded.append({
                "reference": target,
                "created": recorded_reference["created"],
                "edge": None,
                "edge_created": False,
            })
            continue
        edge, created = _ensure_edge(graph, existing, src, tgt, provenance)
        edges.append(edge)
        recorded.append({
            "reference": target,
            "created": recorded_reference["created"],
            "edge": edge,
            "edge_created": created,
        })
    result["recorded"] = recorded
    result["edges"] = edges
    return result


def _neighborhood_neighbors(
    current: str,
    direction: str,
    by_src: Mapping[str, list[dict[str, Any]]],
    by_tgt: Mapping[str, list[dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    if direction in {"both", "references"}:
        pairs.extend((edge["tgt"], edge) for edge in by_src.get(current, []))
    if direction in {"both", "citations"}:
        pairs.extend((edge["src"], edge) for edge in by_tgt.get(current, []))
    pairs.sort(key=lambda pair: (pair[0], str(pair[1].get("id", ""))))
    unique: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for other, edge in pairs:
        key = (other, str(edge.get("id", "")))
        if key in seen:
            continue
        seen.add(key)
        unique.append((other, edge))
    return unique


def neighborhood(
    state: Mapping[str, Any],
    reference_id: str,
    *,
    depth: int = 1,
    direction: str = "both",
) -> dict[str, Any]:
    """Traverse recorded ``cites_work`` edges around one Reference locally."""
    depth = _bounded_int(
        depth, name="depth", minimum=1, maximum=MAX_NEIGHBORHOOD_DEPTH)
    direction = _traverse_direction(direction)
    nodes = _state_nodes(state)
    by_id = {node["id"]: node for node in nodes if isinstance(node.get("id"), str)}
    root = _reference_node(state, reference_id)
    references = {
        node["id"] for node in nodes if node.get("type") == "Reference"
    }
    by_src: dict[str, list[dict[str, Any]]] = {}
    by_tgt: dict[str, list[dict[str, Any]]] = {}
    for edge in sorted(
        _cites_work_edges(state),
        key=lambda item: (
            str(item.get("src")), str(item.get("tgt")), str(item.get("id"))),
    ):
        if edge.get("src") not in references or edge.get("tgt") not in references:
            continue
        by_src.setdefault(edge["src"], []).append(edge)
        by_tgt.setdefault(edge["tgt"], []).append(edge)

    paths: dict[str, list[str]] = {reference_id: [reference_id]}
    reached: set[str] = {reference_id}
    frontier = [reference_id]
    collected: dict[str, dict[str, Any]] = {}
    for _level in range(depth):
        next_frontier: list[str] = []
        for current in sorted(frontier):
            for other, edge in _neighborhood_neighbors(
                current, direction, by_src, by_tgt
            ):
                collected[str(edge.get("id", id(edge)))] = edge
                if other in reached:
                    continue
                reached.add(other)
                paths[other] = [*paths[current], other]
                next_frontier.append(other)
        if not next_frontier:
            break
        frontier = sorted(set(next_frontier))

    node_list = [by_id[node_id] for node_id in sorted(reached) if node_id in by_id]
    edge_list = sorted(
        collected.values(),
        key=lambda item: (
            str(item.get("src")), str(item.get("tgt")), str(item.get("id"))),
    )
    return {
        "reference_id": reference_id,
        "root": root,
        "direction": direction,
        "depth": depth,
        "nodes": node_list,
        "edges": edge_list,
        "paths": [
            {"target": node_id, "nodes": paths[node_id], "length": len(paths[node_id]) - 1}
            for node_id in sorted(paths)
        ],
        "counts": {"nodes": len(node_list), "edges": len(edge_list)},
        "truth_assessed": False,
    }


def _path_missing(src_id: str, tgt_id: str, max_depth: int, reason: str) -> dict[str, Any]:
    return {
        "src": src_id,
        "tgt": tgt_id,
        "found": False,
        "path": [],
        "length": 0,
        "nodes": [],
        "edges": [],
        "max_depth": max_depth,
        "reason": reason,
        "truth_assessed": False,
    }


def citation_path(
    state: Mapping[str, Any],
    src_id: str,
    tgt_id: str,
    *,
    max_depth: int = 4,
) -> dict[str, Any]:
    """Return the shortest directed ``cites_work`` path, or ``found=False``."""
    max_depth = _bounded_int(
        max_depth, name="max_depth", minimum=1, maximum=MAX_PATH_DEPTH)
    nodes = _state_nodes(state)
    by_id = {node["id"]: node for node in nodes if isinstance(node.get("id"), str)}
    if not isinstance(src_id, str) or src_id not in by_id:
        return _path_missing(str(src_id), str(tgt_id), max_depth, "source node not found")
    if by_id[src_id].get("type") != "Reference":
        raise CitationGraphError(f"{src_id!r} is not a Reference node")
    if not isinstance(tgt_id, str) or tgt_id not in by_id:
        return _path_missing(src_id, str(tgt_id), max_depth, "target node not found")
    if by_id[tgt_id].get("type") != "Reference":
        return _path_missing(src_id, tgt_id, max_depth, "target is not a Reference")

    if src_id == tgt_id:
        return {
            "src": src_id,
            "tgt": tgt_id,
            "found": True,
            "path": [src_id],
            "length": 0,
            "nodes": [by_id[src_id]],
            "edges": [],
            "max_depth": max_depth,
            "truth_assessed": False,
        }

    references = {node_id for node_id, node in by_id.items() if node.get("type") == "Reference"}
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for edge in sorted(
        _cites_work_edges(state),
        key=lambda item: (
            str(item.get("src")), str(item.get("tgt")), str(item.get("id"))),
    ):
        if edge.get("src") not in references or edge.get("tgt") not in references:
            continue
        adjacency.setdefault(edge["src"], []).append((edge["tgt"], edge))

    parents: dict[str, str | None] = {src_id: None}
    parent_edge: dict[str, dict[str, Any]] = {}
    queue: list[tuple[str, int]] = [(src_id, 0)]
    cursor = 0
    while cursor < len(queue):
        current, distance = queue[cursor]
        cursor += 1
        if distance >= max_depth:
            continue
        for other, edge in adjacency.get(current, []):
            if other in parents:
                continue
            parents[other] = current
            parent_edge[other] = edge
            if other == tgt_id:
                chain = [tgt_id]
                while chain[-1] != src_id:
                    parent = parents[chain[-1]]
                    if parent is None:
                        break
                    chain.append(parent)
                chain.reverse()
                edge_chain = [parent_edge[node_id] for node_id in chain[1:]]
                return {
                    "src": src_id,
                    "tgt": tgt_id,
                    "found": True,
                    "path": chain,
                    "length": len(chain) - 1,
                    "nodes": [by_id[node_id] for node_id in chain],
                    "edges": edge_chain,
                    "max_depth": max_depth,
                    "truth_assessed": False,
                }
            queue.append((other, distance + 1))
    return _path_missing(
        src_id, tgt_id, max_depth,
        "no directed cites_work path within max_depth")
