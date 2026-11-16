"""Small, source-attributed queries over opt-in infrastructure graph nodes.

The context query is task-aware but deterministic: it ranks declarations by
identifier, kind, namespace, path and family hints from the current request,
then by weaker history hints. It returns compact citations before excerpts and
never reads source files, state, or manifest values.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import re
from typing import Any, Iterable

MAX_QUERY_CHARS = 4000
MAX_TERM_CHARS = 64
MAX_TERMS = 32
_TOKEN = re.compile(r"[A-Za-z0-9_./:@#-]+")
_STOP = frozenset({
    "about", "that", "this", "with", "what", "where", "which", "from", "find",
    "show", "list", "tell", "does", "doesn't", "uses", "using", "used", "into",
    "infrastructure", "resource", "resources", "terraform", "opentofu",
    "kubernetes", "deploy", "deployment", "deployments", "the", "and", "for",
    "are", "was", "were", "how", "why", "when", "please", "check", "look",
})
_FAMILY_HINTS = (
    ("terraform", re.compile(
        r"\b(?:terraform|opentofu|tofu|hcl|tf)\b|\.tf\b|\.tofu\b|\bmodule\.")),
    ("kubernetes", re.compile(
        r"\b(?:kubernetes|k8s|kubectl|helm|kustomize|manifest|namespace|"
        r"pod|serviceaccount|ingress|workload)\b")),
)
_FAMILY_ORDER = {
    "kubernetes": 0, "terraform": 1, "deployment": 2, "cloudformation": 3,
    "arm": 4, "pulumi": 5, "gitops": 6,
}


def _query_terms(text: str, *, weight: float) -> dict[str, float]:
    """Extract bounded identifier-ish terms; dotted/path tokens rank higher."""
    terms: dict[str, float] = {}
    for token in _TOKEN.findall((text or "")[:MAX_QUERY_CHARS]):
        token = token.strip("._/-:@#").lower()
        if len(token) < 3 or token in _STOP or len(token) > MAX_TERM_CHARS:
            continue
        strong = any(char in token for char in "._/:@#") and any(
            char.isalpha() for char in token)
        value = weight * (2.0 if strong else 1.0)
        if value > terms.get(token, 0.0):
            terms[token] = value
        if len(terms) >= MAX_TERMS:
            break
    return terms


def _family_hint(query: str) -> str | None:
    found = [family for family, pattern in _FAMILY_HINTS if pattern.search(query)]
    return found[0] if len(found) == 1 else None


def _node_text(attrs: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(attrs.get("name") or "").lower(),
        "address": str(attrs.get("address") or "").lower(),
        "aliases": str(attrs.get("aliases") or "").lower(),
        "namespace": str(attrs.get("namespace") or "").lower(),
        "kind": str(attrs.get("kind") or "").lower(),
        "family": str(attrs.get("family") or "").lower(),
        "path": str(attrs.get("path") or "").lower(),
    }


@lru_cache(maxsize=8192)
def _field_parts(value: str) -> frozenset[str]:
    """Identifier components, so ``net`` does not match ``subnet`` but
    ``aws_subnet`` matches ``aws_subnet.private``."""
    parts: set[str] = set()
    for segment in re.split(r"[./:@#]+", value):
        if segment:
            parts.add(segment)
            parts.update(part for part in re.split(r"[_-]+", segment) if part)
    return frozenset(parts)


def _camel_tokens(value: str) -> set[str]:
    """Lowercase word parts of an API kind such as ``ServiceAccount``."""
    return {part.lower() for part in re.findall(
        r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", value)}


def _infra_index(memory: Any) -> dict[str, set[int]] | None:
    """Token -> node-id index for the opt-in infrastructure domain.

    Built once per graph mutation and stored on the memory object. It is a
    candidate filter only; scoring still validates every field match. Without
    a maintained type index (test doubles), callers fall back to a full scan.
    """
    type_index = getattr(memory, "nodes_of_type", None)
    if not callable(type_index):
        return None
    version = getattr(memory, "_index_version", None)
    cached = getattr(memory, "_infra_index_cache", None)
    if cached is not None and cached[0] == version:
        return cached[1]
    index: dict[str, set[int]] = {}
    nodes = getattr(memory, "nodes", {})

    def add(token: str, nid: int) -> None:
        if len(token) >= 3:
            index.setdefault(token, set()).add(nid)

    for nid in type_index("InfraResource"):
        attrs = (nodes.get(nid) or {}).get("attrs") or {}
        for field in ("name", "address", "aliases", "namespace"):
            value = str(attrs.get(field) or "").lower()
            if not value:
                continue
            add(value, nid)
            for part in _field_parts(value):
                add(part, nid)
        kind = str(attrs.get("kind") or "")
        if kind:
            add(kind.lower(), nid)
            for part in _field_parts(kind.lower()) | _camel_tokens(kind):
                add(part, nid)
        path = str(attrs.get("path") or "").lower()
        if path:
            for part in _field_parts(path):
                add(part, nid)
    if version is not None:
        memory._infra_index_cache = (version, index)
    return index


def _indexed_candidates(index: dict[str, set[int]],
                        terms: Iterable[str]) -> set[int] | None:
    found: set[int] = set()
    any_hit = False
    for term in terms:
        variants = [term]
        trimmed = term
        while "." in trimmed:
            trimmed = trimmed.rsplit(".", 1)[0]
            variants.append(trimmed)
        for variant in variants:
            hit = index.get(variant)
            if hit:
                found.update(hit)
                any_hit = True
                break
    return found if any_hit else None


def _field_match(term: str, value: str, *, kind: bool = False) -> int:
    """0: no match, 1: component/kind-affix match, 2: exact value."""
    if not term or not value:
        return 0
    if term == value:
        return 2
    if term in _field_parts(value):
        return 1
    # A reference commonly carries an attribute suffix (`...id`) that is not
    # part of the declaration address.
    if term.startswith(value + ".") or value.startswith(term + "."):
        return 1
    if kind and (value.startswith(term) or value.endswith(term)):
        return 1
    return 0


def _score_node(attrs: dict[str, Any], current: dict[str, float],
                historical: dict[str, float], family_hint: str | None
                ) -> tuple[float, list[str], bool, bool]:
    """Return score, matched terms, a strong current match, and a strong
    history match (name/address/kind), not a bare path coincidence."""
    fields = _node_text(attrs)
    score = 0.0
    matched: list[str] = []
    strong = False
    history_strong = False
    for term, weight in current.items():
        name = _field_match(term, fields["name"])
        address = _field_match(term, fields["address"])
        aliases = _field_match(term, fields["aliases"])
        kind = _field_match(term, fields["kind"], kind=True)
        namespace = _field_match(term, fields["namespace"])
        path = term in fields["path"]
        if 2 in (name, address, aliases):
            score += 6.0 * weight
            strong = True
        elif name or address or aliases:
            score += 2.5 * weight
            strong = True
        elif kind:
            score += 1.5 * weight
            strong = True
        elif namespace:
            score += 1.5 * weight
        elif path:
            score += 0.75 * weight
        else:
            continue
        matched.append(term)
    for term, weight in historical.items():
        if term in current:
            continue
        if _field_match(term, fields["name"]) or _field_match(
                term, fields["address"]) or _field_match(term, fields["aliases"]):
            score += 2.0 * weight
            history_strong = True
        elif _field_match(term, fields["kind"], kind=True) or _field_match(
                term, fields["namespace"]):
            score += 1.0 * weight
            history_strong = True
        elif term in fields["path"]:
            score += 0.5 * weight
        else:
            continue
        matched.append(term)
    if family_hint and fields["family"] == family_hint and matched:
        score += 1.0
    return score, matched, strong, history_strong


def _citation(attrs: dict[str, Any]) -> str:
    path = str(attrs.get("path") or "")
    line = attrs.get("line")
    pointer = attrs.get("pointer")
    if isinstance(line, int):
        return f"{path}:{line}"
    if isinstance(pointer, str) and pointer:
        return f"{path}#{pointer}"
    return path


def _label(attrs: dict[str, Any]) -> str:
    label = str(attrs.get("address") or attrs.get("name") or "")
    namespace = str(attrs.get("namespace") or "")
    if namespace and namespace.lower() not in label.lower():
        return f"{namespace}/{label}"
    return label


def _render_edge(relation: Any, target: str, provenance: str, *,
                 inbound: bool = False, citation: str = "") -> str:
    name = str(relation)
    # The adapter prefixes every derived relation with `infra_`; omit that
    # namespace in compact hints but keep the full relation in result metadata.
    if name.startswith("infra_"):
        name = name[len("infra_"):]
    if citation:
        target = f"{target} @{citation}"
    if inbound:
        return f"  <- {name} {target} ({provenance})"
    return f"  {name} -> {target} ({provenance})"


def context(memory: Any, query: str, *, history: Iterable[str] = (),
            max_chars: int = 1600, limit: int = 8) -> dict[str, Any]:
    """Return ranked structural facts only; never read source or state values.

    ``history`` is used as a weaker hint than the current ``query`` so a
    follow-up such as "what does that Service use?" can still find the object
    discussed in the previous turn. The character budget is a hard cap; the
    result also reports a clearly labelled ``chars / 4`` token estimate.
    """
    budget = min(4000, max(0, int(max_chars)))
    cap = min(100, max(0, int(limit)))
    estimated = {"token_estimate_method": "estimated_chars_div_4"}
    empty = {"context": "", "keys": [], "results": [], "related": [],
             "edges": [], "matched_term_count": 0, "truncated": False,
             "ambiguous": False, "budget_chars": budget, **estimated}
    if budget <= 0 or cap <= 0:
        return {**empty, "reason": "empty context budget"}
    current = _query_terms(str(query or ""), weight=1.0)
    historical = _query_terms("\n".join(
        item for item in history if isinstance(item, str)), weight=0.35)
    if not current and not historical:
        return {**empty, "reason": "query has no searchable terms"}
    family_hint = _family_hint(str(query or ""))

    nodes = getattr(memory, "nodes", {})
    type_index = getattr(memory, "nodes_of_type", None)
    index = _infra_index(memory)
    hit_ids = (_indexed_candidates(index, list(current) + list(historical))
               if index is not None else None)
    if hit_ids is not None:
        candidates = ((nid, nodes.get(nid) or {}) for nid in hit_ids)
    elif callable(type_index):
        candidates = ((nid, nodes.get(nid) or {})
                      for nid in type_index("InfraResource"))
    else:
        candidates = ((nid, node) for nid, node in nodes.items()
                      if node.get("type") == "InfraResource")
    scored: list[tuple[float, int, str, str, int, dict[str, Any], list[str], bool, bool]] = []
    for nid, node in candidates:
        attrs = node.get("attrs") or {}
        score, matched, strong, history_strong = _score_node(
            attrs, current, historical, family_hint)
        # A single weak substring match (often a directory name) is not enough
        # evidence to inject context; require an identifier/kind/exact hit or
        # at least two independent term matches. History alone can identify a
        # declaration, but only by name/address/kind, not a bare path.
        if not matched or (not strong and not history_strong
                           and len(set(matched)) < 2):
            continue
        family = str(attrs.get("family") or "")
        scored.append((-score, _FAMILY_ORDER.get(family, 99),
                       str(attrs.get("path") or ""),
                       str(attrs.get("name") or ""), int(nid), node,
                       sorted(set(matched)), strong, history_strong))
    scored.sort(key=lambda row: row[:5])
    if not scored:
        return {**empty, "reason": "no matching infrastructure declarations"}

    parts: list[str] = []
    keys: list[str] = []
    results: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    rendered_ids: set[int] = set()
    used = 0
    truncated = False
    edge_cap = min(16, max(4, cap * 2))
    seen_edges: set[tuple[int, int, str, bool]] = set()

    def append(text: str) -> bool:
        nonlocal used
        prefix = "\n" if parts else ""
        if used + len(prefix) + len(text) > budget:
            return False
        parts.append(prefix + text)
        used += len(prefix) + len(text)
        return True

    def add_edge(edge: dict[str, Any], *, inbound: bool) -> None:
        nonlocal truncated
        if len(edges) >= edge_cap:
            truncated = True
            return
        source = int(edge.get("src", -1))
        target = int(edge.get("tgt", -1))
        # An inbound relation whose source declaration is already rendered is
        # the same fact as its outbound line; do not spend budget twice.
        if inbound and source in rendered_ids:
            return
        side = source if inbound else target
        side_node = nodes.get(side) or {}
        if side_node.get("type") != "InfraResource":
            return
        side_attrs = side_node.get("attrs") or {}
        relation = str(edge.get("type") or "")
        if not relation:
            return
        provenance = str((edge.get("attrs") or {}).get("provenance", "declared"))
        marker = (source, target, relation, inbound)
        if marker in seen_edges:
            return
        kind = str(side_attrs.get("kind") or "")
        bare_label = _label(side_attrs)
        label = " ".join(part for part in (kind, bare_label) if part)
        citation = "" if side in rendered_ids else _citation(side_attrs)
        line = _render_edge(relation, label, provenance, inbound=inbound,
                            citation=citation)
        if not append(line):
            truncated = True
            return
        seen_edges.add(marker)
        source_attrs = (nodes.get(source) or {}).get("attrs") or {}
        target_attrs = (nodes.get(target) or {}).get("attrs") or {}
        source_label = _label(source_attrs)
        target_label = _label(target_attrs)
        edges.append({
            "source": (nodes.get(source) or {}).get("key"),
            "target": (nodes.get(target) or {}).get("key"),
            "source_label": source_label, "target_label": target_label,
            "relation": relation, "kind": relation, "provenance": provenance,
            "evidence": _citation(source_attrs),
            "inbound": inbound, "label": label,
        })
        side_key = side_node.get("key")
        if side not in rendered_ids and len(related) < edge_cap:
            related.append({
                "key": side_key, "path": side_attrs.get("path", ""),
                "line": side_attrs.get("line"),
                "pointer": side_attrs.get("pointer"),
                "label": bare_label, "kind": side_attrs.get("kind", ""),
                "relation": relation, "provenance": provenance,
                "direction": "dependency" if not inbound else "dependent",
            })

    for negative_score, _, _, _, nid, node, _matched, _current_hit, _strong in scored[:cap]:
        attrs = node.get("attrs") or {}
        if nid in rendered_ids:
            continue
        citation = _citation(attrs)
        label = _label(attrs)
        kind = str(attrs.get("kind") or "")
        line = f"[infra] {citation} {kind} {label}".strip()
        if not append(line):
            truncated = True
            continue
        rendered_ids.add(nid)
        key = node.get("key")
        if isinstance(key, str):
            keys.append(key)
        results.append({
            "path": str(attrs.get("path") or ""), "key": key,
            "line": attrs.get("line"), "pointer": attrs.get("pointer"),
            "label": label, "id": nid, "kind": kind,
            "namespace": attrs.get("namespace"), "family": attrs.get("family"),
            "score": round(-float(negative_score), 3),
        })
    # Relationships are rendered only for declarations that fit the budget.
    rendered_order = [int(row["id"]) for row in results]
    for nid in rendered_order:
        for edge in getattr(memory, "_out", {}).get(nid, []):
            add_edge(edge, inbound=False)
    # For the first two matches, also show one hop of dependents so an impact
    # question sees "who uses this" without a separate traversal.
    for nid in rendered_order[:2]:
        for edge in getattr(memory, "_in", {}).get(nid, []):
            add_edge(edge, inbound=True)
    if len(scored) > len(results):
        truncated = True
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        groups[(str(row.get("kind") or ""), str(row.get("label") or ""))].append(row)
    ambiguous = any(len(group) > 1 for group in groups.values())
    rendered = "".join(parts)
    return {
        "context": rendered, "keys": keys, "results": results,
        "related": related, "edges": edges,
        "matched_term_count": len({term for row in scored[:cap]
                                   for term in row[6]}),
        "truncated": truncated, "ambiguous": ambiguous,
        "ambiguity": [{"kind": kind, "label": label, "count": len(group)}
                      for (kind, label), group in sorted(groups.items())
                      if len(group) > 1],
        "budget_chars": budget,
        "estimated_tokens": (len(rendered) + 3) // 4,
        **estimated,
    }


def locate(memory: Any, query: str, *, limit: int = 20) -> dict[str, Any]:
    """Find matching declarations; multiple matches are deliberately retained."""
    query = query.strip().lower()
    if not query:
        return {"results": [], "ambiguous": False, "complete": False}
    found = []
    nodes = getattr(memory, "nodes", {})
    type_index = getattr(memory, "nodes_of_type", None)
    index = _infra_index(memory)
    terms = list(_query_terms(query, weight=1.0))
    hit_ids = (_indexed_candidates(index, terms)
               if index is not None else None)
    if hit_ids is not None:
        candidates = (nodes.get(nid) or {} for nid in hit_ids)
    elif callable(type_index):
        candidates = (nodes.get(nid) or {} for nid in type_index("InfraResource"))
    else:
        candidates = (node for node in nodes.values()
                      if node.get("type") == "InfraResource")
    for node in candidates:
        attrs = node.get("attrs") or {}
        if any(query in str(attrs.get(field, "")).lower()
               for field in ("name", "address", "aliases", "path", "namespace")):
            found.append({"key": node.get("key"), **attrs})
    found.sort(key=lambda n: (str(n.get("path", "")), str(n.get("name", ""))))
    cap = min(100, max(0, limit))
    return {"results": found[:cap], "ambiguous": len(found) > 1,
            "truncated": len(found) > cap, "complete": False}


def traverse(memory: Any, key: str, *, direction: str = "dependencies",
             depth: int = 2, limit: int = 30) -> dict[str, Any]:
    """Traverse indexed edges with bounded depth; graph coverage is partial."""
    if direction not in ("dependencies", "impact"):
        raise ValueError("direction must be dependencies or impact")
    node_map = getattr(memory, "nodes", {})
    by_key = getattr(memory, "node_by_key", None)
    start = by_key(key) if callable(by_key) else None
    if start is None:
        start = next((nid for nid, node in node_map.items()
                      if node.get("type") == "InfraResource"
                      and node.get("key") == key), None)
    if start is None:
        return {"nodes": [], "edges": [], "complete": False, "reason": "key not found"}
    if (node_map.get(start) or {}).get("type") != "InfraResource":
        return {"nodes": [], "edges": [], "complete": False, "reason": "key not found"}
    reverse = direction == "impact"
    graph = memory._in if reverse else memory._out
    seen = {start}
    queue = [(start, 0)]
    nodes = []
    edges = []
    cap = min(100, max(0, int(limit)))
    max_depth = min(5, max(0, int(depth)))
    truncated = False
    while queue:
        nid, level = queue.pop(0)
        if level >= max_depth:
            continue
        for edge in graph.get(nid, []):
            relation = str(edge.get("type", ""))
            if not relation.startswith("infra_"):
                continue
            target = int(edge["src"] if reverse else edge["tgt"])
            target_node = node_map.get(target) or {}
            if target_node.get("type") != "InfraResource":
                continue
            if len(edges) >= cap:
                truncated = True
                break
            edges.append({"source": (node_map.get(int(edge["src"])) or {}).get("key"),
                          "target": (node_map.get(int(edge["tgt"])) or {}).get("key"),
                          "relation": relation, **(edge.get("attrs") or {})})
            if target not in seen:
                seen.add(target)
                nodes.append({"key": target_node.get("key"), **target_node.get("attrs", {})})
                queue.append((target, level + 1))
        if truncated:
            break
    return {"nodes": nodes, "edges": edges, "truncated": truncated,
            "complete": False, "direction": direction}
