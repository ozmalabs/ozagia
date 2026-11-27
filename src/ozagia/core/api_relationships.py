"""Repository-level API relationships and canonical API references.

OpenAPI operations, explicit ``ApiMapping`` nodes and ``ApiCodeReference``
nodes already live in member snapshots. This module joins them without reading
source files or building an index:

- each API operation gets one canonical reference, chosen from the best
  available source in the snapshot (OpenAPI ``operationId``, OpenAPI
  method/path, an explicit code reference, then a bare operation identity);
- explicit producer, consumer and test mappings are rolled up into repository
  edges;
- matching operation names or paths across repositories never create an edge.

The report is bounded and JSON-serializable. It never fetches a reference,
imports a member, runs a project or writes to a checkout.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

MAX_REFERENCES = 2000
MAX_MAPPINGS = 1000
MAX_EDGES = 1000
MAX_UNPAIRED = 200
MAX_ERRORS = 100
MAX_TEXT = 500

RELATIONS = ("produces_api", "consumes_api", "tests_api")
SOURCE_ORDER = ("openapi_operation", "openapi_path", "code_reference",
                "operation_identity")
_AUTHORITY = {
    "openapi_operation": "contract",
    "openapi_path": "contract_path",
    "code_reference": "code_mapping",
    "operation_identity": "operation_identity",
}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:MAX_TEXT] if text else None


def _attrs(node: Mapping[str, Any]) -> dict[str, Any]:
    value = node.get("attrs")
    return dict(value) if isinstance(value, Mapping) else {}


def _operation_reference(member: str, attrs: Mapping[str, Any]
                         ) -> tuple[dict[str, Any] | None, str | None]:
    repo = _text(attrs.get("repo")) or member
    file = _text(attrs.get("file"))
    operation_id = _text(attrs.get("operation_id"))
    method = _text(attrs.get("method"))
    path = _text(attrs.get("path"))
    pointer = _text(attrs.get("source_pointer"))
    if operation_id and file:
        source_id = f"{repo}:{file}#{operation_id}"
        kind = "openapi_operation"
        alternates = [f"{repo}:{file}#{method} {path}"] if method and path else []
    elif file and method and path:
        source_id = f"{repo}:{file}#{method} {path}"
        kind = "openapi_path"
        alternates = []
    elif method and path:
        source_id = f"{member}::{method} {path}"
        kind = "operation_identity"
        alternates = []
    else:
        return None, "unusable_api_operation"
    return {
        "id": source_id,
        "kind": kind,
        "authority": _AUTHORITY[kind],
        "source_rank": SOURCE_ORDER.index(kind),
        "repo": repo,
        "file": file,
        "operation_id": operation_id,
        "method": method,
        "path": path,
        "source_pointer": pointer,
        "alternates": alternates,
    }, None


def _code_reference(member: str, attrs: Mapping[str, Any]
                    ) -> tuple[dict[str, Any] | None, str | None]:
    repo = _text(attrs.get("repo")) or member
    path = _text(attrs.get("path"))
    symbol = _text(attrs.get("symbol"))
    if not path or not symbol:
        return None, "unusable_code_reference"
    return {
        "id": f"{repo}:{path}#{symbol}",
        "kind": "code_reference",
        "authority": _AUTHORITY["code_reference"],
        "source_rank": SOURCE_ORDER.index("code_reference"),
        "repo": repo,
        "file": path,
        "symbol": symbol,
    }, None


def _outgoing(edges: Iterable[Any]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for edge in edges:
        if not isinstance(edge, Mapping):
            continue
        source, target = edge.get("src"), edge.get("tgt")
        if not isinstance(source, int) or not isinstance(target, int):
            continue
        out.setdefault(source, []).append({
            "src": source, "type": edge.get("type"), "tgt": target,
            "attrs": edge.get("attrs") if isinstance(edge.get("attrs"), Mapping) else {},
        })
    return out


def _merge_reference(store: dict[str, dict[str, Any]],
                     reference: Mapping[str, Any], member: str,
                     errors: list[str]) -> None:
    key = str(reference["id"])
    incoming = dict(reference)
    existing = store.get(key)
    if existing is None:
        incoming["members"] = [member]
        incoming["ambiguous"] = False
        store[key] = incoming
        return
    if member not in existing["members"]:
        existing["members"].append(member)
    comparable = ("kind", "repo", "file", "operation_id", "method", "path",
                  "source_pointer")
    conflicts = [field for field in comparable
                 if existing.get(field) and incoming.get(field)
                 and existing.get(field) != incoming.get(field)]
    if conflicts:
        existing["ambiguous"] = True
        errors.append(f"ambiguous api reference {key}: conflicting "
                      + ", ".join(conflicts))
        return
    previous = existing
    if incoming.get("source_rank", 99) < previous.get("source_rank", 99):
        members = previous["members"]
        ambiguous = bool(previous.get("ambiguous"))
        incoming["members"] = members
        incoming["ambiguous"] = ambiguous
        store[key] = incoming
        existing = incoming
    for field in comparable + ("symbol",):
        if existing.get(field):
            continue
        # A duplicate may be only partially populated, and it may arrive
        # before or after the copy that wins the source-rank comparison.
        # Fill gaps from either side of that comparison; conflicts between two
        # populated values were already reported above.
        if incoming.get(field):
            existing[field] = incoming[field]
        elif previous.get(field):
            existing[field] = previous[field]
    alternates = sorted(set(existing.get("alternates") or [])
                        | set(previous.get("alternates") or [])
                        | set(incoming.get("alternates") or []))
    if alternates:
        existing["alternates"] = alternates


def _member_api(item: Mapping[str, Any], errors: list[str]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                           list[dict[str, Any]]]:
    """Return operation references, code references and mapping nodes for a member."""
    member = str(item.get("alias") or "repo")
    snapshot = item.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return [], [], []
    nodes = snapshot.get("nodes")
    edges = snapshot.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return [], [], []
    by_id: dict[int, dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node, Mapping) and isinstance(node.get("id"), int):
            by_id[int(node["id"])] = dict(node)
    outgoing = _outgoing(edges)
    operations: list[dict[str, Any]] = []
    code_refs: list[dict[str, Any]] = []
    ref_by_node: dict[int, dict[str, Any]] = {}
    code_by_node: dict[int, dict[str, Any]] = {}
    for node_id, node in by_id.items():
        kind = node.get("type")
        attrs = _attrs(node)
        if kind == "ApiOperation":
            reference, error = _operation_reference(member, attrs)
            if error:
                errors.append(f"{member}:api_operation:{node_id}:{error}")
                continue
            operations.append(reference)
            ref_by_node[node_id] = reference
        elif kind == "ApiCodeReference":
            reference, error = _code_reference(member, attrs)
            if error:
                errors.append(f"{member}:api_code_reference:{node_id}:{error}")
                continue
            code_refs.append(reference)
            code_by_node[node_id] = reference
    mappings: list[dict[str, Any]] = []
    for node_id, node in by_id.items():
        if node.get("type") != "ApiMapping":
            continue
        attrs = _attrs(node)
        if attrs.get("inference") is not False:
            errors.append(f"{member}:api_mapping:{node_id}:inference_not_allowed")
            continue
        relation = attrs.get("relation")
        if relation not in RELATIONS:
            errors.append(f"{member}:api_mapping:{node_id}:unsupported_relation")
            continue
        code_id = next((edge["tgt"] for edge in outgoing.get(node_id, [])
                        if edge.get("type") == relation), None)
        operation_id = next((edge["tgt"] for edge in outgoing.get(node_id, [])
                             if edge.get("type") == "refers_to_api_operation"), None)
        code = code_by_node.get(code_id) if code_id is not None else None
        if operation_id is not None:
            reference = ref_by_node.get(operation_id)
        else:
            reference = code
        if reference is None:
            errors.append(f"{member}:api_mapping:{node_id}:unresolved_reference")
            continue
        if code is None:
            errors.append(f"{member}:api_mapping:{node_id}:missing_code_reference")
            continue
        provenance = attrs.get("provenance")
        mappings.append({
            "member": member,
            "members": [member],
            "repo": code.get("repo") or member,
            "relation": relation,
            "reference": reference["id"],
            "reference_kind": reference["kind"],
            "authority": reference["authority"],
            "operation_repo": reference.get("repo"),
            "code": {key: code.get(key) for key in ("repo", "file", "symbol")},
            "provenance": dict(provenance) if isinstance(provenance, Mapping) else {},
        })
    return operations, code_refs, mappings


def _mapping_key(mapping: Mapping[str, Any]) -> tuple[Any, ...]:
    code = mapping.get("code") or {}
    provenance = mapping.get("provenance") or {}
    return (mapping.get("relation"), mapping.get("reference"),
            code.get("repo"), code.get("file"), code.get("symbol"),
            provenance.get("source"), provenance.get("pointer"))


def _merge_provenance(target: dict[str, Any], mapping: Mapping[str, Any]) -> None:
    provenance = mapping.get("provenance")
    if isinstance(provenance, Mapping) and provenance:
        value = dict(provenance)
        if value not in target["provenance"]:
            target["provenance"].append(value)
    code = mapping.get("code")
    if isinstance(code, Mapping):
        value = dict(code)
        if value not in target["code"]:
            target["code"].append(value)


def analyze(members: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build canonical API references and repo-level API relationships."""
    records = list(members)
    aliases = {str(item.get("alias")) for item in records}
    errors: list[str] = []
    references: dict[str, dict[str, Any]] = {}
    mappings: list[dict[str, Any]] = []
    for item in records:
        operations, code_refs, member_mappings = _member_api(item, errors)
        member = str(item.get("alias") or "repo")
        for reference in operations:
            _merge_reference(references, reference, member, errors)
        # A code location is a reference only when the member has no OpenAPI
        # operation to use as the better source.
        if not operations:
            for reference in code_refs:
                _merge_reference(references, reference, member, errors)
        mappings.extend(member_mappings)
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for mapping in mappings:
        key = _mapping_key(mapping)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = mapping
            continue
        for member in mapping.get("members") or [mapping.get("member")]:
            if member not in existing["members"]:
                existing["members"].append(member)
    mappings = sorted(deduped.values(), key=lambda item: (
        str(item.get("reference")), str(item.get("relation")),
        str(item.get("repo")), str((item.get("code") or {}).get("path"))))
    truncated = False
    if len(references) > MAX_REFERENCES:
        truncated = True
        errors.append(f"api references are limited to the first {MAX_REFERENCES}")
        keep = {item["id"] for item in sorted(
            references.values(), key=lambda item: str(item["id"]))[:MAX_REFERENCES]}
        references = {key: value for key, value in references.items() if key in keep}
    if len(mappings) > MAX_MAPPINGS:
        truncated = True
        mappings = mappings[:MAX_MAPPINGS]
        errors.append(f"api mappings are limited to the first {MAX_MAPPINGS}")

    by_reference: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for mapping in mappings:
        group = by_reference.setdefault(str(mapping["reference"]), {
            "produces_api": [], "consumes_api": [], "tests_api": [],
        })
        group[str(mapping["relation"])].append(mapping)

    edges: dict[tuple[Any, ...], dict[str, Any]] = {}
    unpaired: list[dict[str, Any]] = []

    def add_edge(source: str, target: str, relation: str,
                 reference: Mapping[str, Any], producer: Mapping[str, Any],
                 other: Mapping[str, Any], *, ambiguous: bool = False) -> None:
        if source == target or source not in aliases or target not in aliases:
            return
        key = (source, target, relation, reference["id"])
        edge = edges.get(key)
        if edge is None:
            edge = {
                "source": source, "target": target, "relation": relation,
                "kind": "integration", "origin": "api_mapping",
                "reference": reference["id"],
                "reference_kind": (reference.get("kind")
                                   or producer.get("reference_kind")
                                   or "operation_identity"),
                "authority": (reference.get("authority")
                              or producer.get("authority")
                              or "operation_identity"),
                "provenance": [], "code": [], "ambiguous": bool(ambiguous),
            }
            edges[key] = edge
        edge["ambiguous"] = edge["ambiguous"] or bool(ambiguous)
        _merge_provenance(edge, producer)
        _merge_provenance(edge, other)

    for reference_id, group in sorted(by_reference.items()):
        reference = references.get(reference_id) or {
            "id": reference_id, "kind": "operation_identity",
            "authority": "operation_identity", "repo": None,
        }
        producers = group["produces_api"]
        consumers = group["consumes_api"]
        testers = group["tests_api"]
        producer_repos = {str(item["repo"]) for item in producers}
        ambiguous = len(producer_repos) > 1
        if consumers and not producers:
            for mapping in consumers:
                unpaired.append({
                    "member": mapping["repo"], "relation": "consumes_api",
                    "reference": reference_id, "reason": "no_producer_mapping",
                })
        if testers and not producers:
            for mapping in testers:
                unpaired.append({
                    "member": mapping["repo"], "relation": "tests_api",
                    "reference": reference_id, "reason": "no_producer_mapping",
                })
        for consumer in consumers:
            for producer in producers:
                add_edge(str(consumer["repo"]), str(producer["repo"]),
                         "consumes_api", reference, consumer, producer,
                         ambiguous=ambiguous)
        for tester in testers:
            for producer in producers:
                add_edge(str(tester["repo"]), str(producer["repo"]),
                         "tests_api", reference, tester, producer,
                         ambiguous=ambiguous)
        for producer in producers:
            reference_kind = (reference.get("kind")
                              or producer.get("reference_kind") or "")
            contract_repo = (reference.get("repo")
                             or producer.get("operation_repo"))
            if (str(reference_kind).startswith("openapi")
                    and isinstance(contract_repo, str) and contract_repo):
                add_edge(str(producer["repo"]), contract_repo,
                         "implements_api", reference, producer, producer,
                         ambiguous=ambiguous)

    seen_unpaired: set[tuple[Any, ...]] = set()
    unique_unpaired: list[dict[str, Any]] = []
    for item in unpaired:
        key = (item.get("member"), item.get("relation"),
               item.get("reference"), item.get("reason"))
        if key in seen_unpaired:
            continue
        seen_unpaired.add(key)
        unique_unpaired.append(item)
    unpaired = unique_unpaired
    ordered_edges = sorted(edges.values(), key=lambda item: (
        str(item["source"]), str(item["target"]), str(item["relation"]),
        str(item["reference"])))
    if len(ordered_edges) > MAX_EDGES:
        truncated = True
        ordered_edges = ordered_edges[:MAX_EDGES]
        errors.append(f"api repository edges are limited to the first {MAX_EDGES}")
    if len(unpaired) > MAX_UNPAIRED:
        truncated = True
        unpaired = unpaired[:MAX_UNPAIRED]
        errors.append(f"unpaired api mappings are limited to the first {MAX_UNPAIRED}")
    if len(errors) > MAX_ERRORS:
        truncated = True
        errors = errors[:MAX_ERRORS - 1]
        errors.append(f"api errors are limited to the first {MAX_ERRORS}")

    ordered_references = sorted(
        references.values(),
        key=lambda item: (int(item.get("source_rank", 99)), str(item["id"])))
    if errors or truncated:
        state = "partial"
    elif not ordered_references and not mappings:
        state = "absent"
    else:
        state = "ok"
    return {
        "state": state,
        "source_order": list(SOURCE_ORDER),
        "references": ordered_references,
        "mappings": mappings,
        "edges": ordered_edges,
        "unpaired": unpaired,
        "errors": errors,
        "truncated": truncated,
        "counts": {"references": len(ordered_references),
                   "mappings": len(mappings), "edges": len(ordered_edges),
                   "unpaired": len(unpaired), "errors": len(errors)},
        "note": ("API references use an OpenAPI operationId first, then an "
                 "OpenAPI method/path, then an explicit code reference, then a "
                 "bare operation identity. Repository API edges come only from "
                 "explicit ApiMapping nodes; matching operation names or paths "
                 "across snapshots never creates an edge."),
    }


__all__ = ["analyze"]
