"""Structured review plans, independent runs, agreement and reconciliation.

The core never calls a model.  It records the protocol a reviewer followed and
checks the mechanical validity of what came back.  Agreement is reported as a
statistic about reviewers, never as evidence that a claim is true.

Review packets are deterministic projections of the graph.  A packet is built
before any model runs, so each reviewer sees the same graph context, checks and
prompt scaffold.  Findings are candidates: unknown IDs, citation keys and
unsupported quotes are reported rather than quietly accepted.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from itertools import combinations
from typing import Any

from ozagia.research import manuscript as manuscript_mod
from ozagia.research import references as refs
from ozagia.research.graph import ResearchError

REVIEW_KINDS = frozenset({"systematic", "rapid", "narrative", "peer_review", "steelman"})
SEVERITIES = ("blocking", "major", "minor", "note")
_SEVERITY_SET = frozenset(SEVERITIES)
_REVIEW_TARGET_TYPES = frozenset({"Claim", "Reference", "Report", "Argument", "Source"})
_FINDING_TARGET_TYPES = frozenset({"Claim", "Evidence", "Reference", "Argument", "Source"})
_CONTEXT_GROUPS = {
    "supporting": frozenset({"supports"}),
    "opposing": frozenset({"opposes"}),
    "assuming": frozenset({"assumes"}),
    "derived": frozenset({"derived_from"}),
    "citation": frozenset({"cites", "cites_work", "manifestation_of"}),
}
_TEXT_KEYS = frozenset(
    {
        "wording",
        "text",
        "title",
        "abstract",
        "raw",
        "raw_excerpt",
        "proof_text",
        "excerpt",
        "content",
        "summary",
        "question",
        "recommendation",
        "quote",
        "body",
        "notes",
        "name",
        "thesis",
        "criteria",
        "exclusion_criteria",
        "filters",
        "gathered_text",
    }
)
_NODE_TEXT_FIELDS = (
    "question",
    "wording",
    "text",
    "title",
    "abstract",
    "raw",
    "raw_excerpt",
    "proof_text",
    "excerpt",
    "content",
    "summary",
    "thesis",
    "name",
    "notes",
)
_NODE_LIST_TEXT_FIELDS = ("criteria", "exclusion_criteria", "citation_keys")


class ReviewError(ResearchError):
    """A review protocol, packet, output or reconciliation is invalid."""


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _collapse(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ReviewError(f"value must be canonical JSON: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise ReviewError("review operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and node.get("id")
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _require_node(
    state: Mapping[str, Any],
    node_id: str,
    expected: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    node = _node_by_id(state, node_id)
    if node is None:
        raise ReviewError(f"unknown {label} id: {node_id}")
    if node.get("type") not in expected:
        raise ReviewError(
            f"{label} {node_id} must be one of {sorted(expected)}, not {node.get('type')}"
        )
    return node


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _string_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ReviewError(f"{field} must be a non-empty list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ReviewError(f"{field} must contain non-empty strings")
        item = item.strip()
        if item not in items:
            items.append(item)
    if not items:
        raise ReviewError(f"{field} must be a non-empty list of strings")
    return items


def _optional_string_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    return _string_list(value, field)


def _json_safe(value: Any, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ReviewError(f"{field} must be JSON-serializable: {exc}") from exc


def _nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewError(f"{field} must be a non-negative integer")
    return value


def _nonnegative_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ReviewError(f"{field} must be a non-negative number")
    return float(value)


def _confidence(value: Any, field: str, errors: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field} must be a number between 0 and 1")
        return None
    number = float(value)
    if not 0.0 <= number <= 1.0:
        errors.append(f"{field} must be between 0 and 1")
        return None
    return number


def _dedupe_values(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if value not in result:
                result.append(value)
    return result


def _reference_key_index(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for node in _nodes(state):
        if node.get("type") != "Reference":
            continue
        keys: list[str] = []
        single = node.get("citation_key")
        if isinstance(single, str) and single.strip():
            keys.append(single.strip())
        raw = node.get("citation_keys")
        if isinstance(raw, list):
            keys.extend(item.strip() for item in raw if isinstance(item, str) and item.strip())
        elif isinstance(raw, str) and raw.strip():
            keys.append(raw.strip())
        for key in keys:
            index.setdefault(key.casefold(), node)
    return index


def _normalised_wording(value: str) -> str:
    return _collapse(value).casefold()


def _node_text(node: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for field in _NODE_TEXT_FIELDS:
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for field in _NODE_LIST_TEXT_FIELDS:
        value = node.get(field)
        if isinstance(value, list):
            parts.extend(
                item.strip() for item in value
                if isinstance(item, str) and item.strip()
            )
    return "\n".join(parts)


def _collect_text(value: Any, key: str | None, out: list[str]) -> None:
    if isinstance(value, Mapping):
        for child_key in sorted(value, key=str):
            _collect_text(value[child_key], str(child_key), out)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, key, out)
    elif isinstance(value, str) and key in _TEXT_KEYS and value.strip():
        out.append(value.strip())


def _packet_text(packet: Mapping[str, Any]) -> str:
    """Return the text a quoted finding may come from, whitespace-collapsed."""
    out: list[str] = []
    gathered = packet.get("gathered_text")
    if isinstance(gathered, str) and gathered.strip():
        out.append(gathered)
        for field in ("plan", "target", "targets", "context_nodes", "manuscript"):
            _collect_text(packet.get(field), field, out)
    else:
        # Be permissive for hand-built packets: accept any text-bearing field.
        _collect_text(packet, None, out)
    return _collapse("\n".join(out))


# --------------------------------------------------------------------------- #
# review plan and search log
# --------------------------------------------------------------------------- #
def create_review_plan(
    graph: Any,
    *,
    question: str,
    kind: str = "systematic",
    roles: Sequence[str],
    models: Sequence[str],
    criteria: Any = None,
    exclusion_criteria: Any = None,
    sources: Any = None,
    reviewers: Sequence[str] | None = None,
    rounds: Sequence[str] = ("independent",),
    blinding: bool = True,
    min_reviewers: int = 2,
    adjudication: str = "human",
    target_id: str | None = None,
) -> dict[str, Any]:
    """Record a review protocol as an open ReviewPlan."""
    state = graph.state()
    question = _nonempty_string(question, "question")
    kind = _nonempty_string(kind, "kind").casefold()
    if kind not in REVIEW_KINDS:
        raise ReviewError(f"kind must be one of {sorted(REVIEW_KINDS)}")
    role_list = _string_list(roles, "roles")
    model_list = _string_list(models, "models")
    round_list = _string_list(rounds, "rounds")
    if not isinstance(blinding, bool):
        raise ReviewError("blinding must be a boolean")
    if isinstance(min_reviewers, bool) or not isinstance(min_reviewers, int):
        raise ReviewError("min_reviewers must be an integer")
    maximum = max(1, len(model_list))
    if not 1 <= min_reviewers <= maximum:
        raise ReviewError(f"min_reviewers must be between 1 and {maximum}")
    adjudication = _nonempty_string(adjudication, "adjudication")

    fields: dict[str, Any] = {
        "question": question,
        "kind": kind,
        "roles": role_list,
        "models": model_list,
        "rounds": round_list,
        "blinding": blinding,
        "min_reviewers": min_reviewers,
        "adjudication": adjudication,
        "status": "open",
        "truth_assessed": False,
    }
    for field, value in (
        ("criteria", criteria),
        ("exclusion_criteria", exclusion_criteria),
        ("sources", sources),
    ):
        if value is not None:
            fields[field] = _json_safe(value, field)
    reviewer_list = _optional_string_list(reviewers, "reviewers")
    if reviewer_list is not None:
        fields["reviewers"] = reviewer_list

    target = None
    if target_id is not None:
        target = _require_node(state, target_id, _REVIEW_TARGET_TYPES, "review target")
        fields["target_id"] = target["id"]

    plan = graph.record("ReviewPlan", **fields)
    if target is not None:
        graph.link(
            plan["id"],
            "addresses",
            target["id"],
            provenance={"kind": "review_plan_target"},
            rationale="The review plan addresses this graph record.",
        )
    return {"plan": plan, "target": dict(target) if target else None, "truth_assessed": False}


def record_search_run(
    graph: Any,
    plan_id: str,
    *,
    query: str,
    provider: str,
    executed_at: str | None = None,
    filters: Any = None,
    result_count: int | None = None,
    deduped_count: int | None = None,
    export_sha256: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record one literature search belonging to a review plan."""
    state = graph.state()
    plan = _require_node(state, plan_id, frozenset({"ReviewPlan"}), "review plan")
    query = _nonempty_string(query, "query")
    provider = _nonempty_string(provider, "provider")
    executed = _optional_string(executed_at, "executed_at") or _utc()
    result_count = _nonnegative_int(result_count, "result_count")
    deduped_count = _nonnegative_int(deduped_count, "deduped_count")
    export_sha256 = _optional_string(export_sha256, "export_sha256")
    notes = _optional_string(notes, "notes")

    fields: dict[str, Any] = {
        "plan_id": plan["id"],
        "query": query,
        "provider": provider,
        "executed_at": executed,
        "truth_assessed": False,
    }
    if filters is not None:
        fields["filters"] = _json_safe(filters, "filters")
    if result_count is not None:
        fields["result_count"] = result_count
    if deduped_count is not None:
        fields["deduped_count"] = deduped_count
    if export_sha256 is not None:
        fields["export_sha256"] = export_sha256
    if notes is not None:
        fields["notes"] = notes

    search = graph.record("SearchRun", **fields)
    graph.link(
        search["id"],
        "searches",
        plan["id"],
        provenance={"kind": "review_search", "provider": provider},
        rationale="The search was executed for this review plan.",
    )
    return {"search_run": search, "plan": dict(plan), "truth_assessed": False}


# --------------------------------------------------------------------------- #
# packet construction
# --------------------------------------------------------------------------- #
def _plan_addresses(state: Mapping[str, Any], plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    ids: list[str] = []
    target_id = plan.get("target_id")
    if isinstance(target_id, str) and target_id:
        ids.append(target_id)
    for edge in _edges(state):
        if edge.get("type") == "addresses" and edge.get("src") == plan.get("id"):
            target = edge.get("tgt")
            if isinstance(target, str):
                ids.append(target)
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for node_id in ids:
        node = _node_by_id(state, node_id)
        if node is not None and node_id not in seen:
            seen.add(node_id)
            result.append(node)
    return sorted(result, key=lambda node: (str(node.get("type")), str(node.get("id"))))


def _node_citation_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    single = node.get("citation_key")
    if isinstance(single, str) and single.strip():
        key = single.strip()
        if key not in keys:
            keys.append(key)
    for field in ("citations", "citation_keys"):
        raw = node.get(field)
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for item in values:
            if isinstance(item, str) and item.strip():
                key = item.strip().lstrip("@").strip()
                if key and key not in keys:
                    keys.append(key)
    return keys


def _text_citation_contexts(
    nodes: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for node in sorted(nodes, key=lambda item: str(item.get("id"))):
        for field in _NODE_TEXT_FIELDS:
            text = node.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            for context in refs.citation_contexts(text):
                item = dict(context)
                item["source_node_id"] = node.get("id")
                item["source_field"] = field
                item["truth_assessed"] = False
                signature = (
                    str(node.get("id")),
                    field,
                    tuple(item.get("citation_keys") or []),
                    item.get("quote"),
                )
                if signature in seen:
                    continue
                seen.add(signature)
                contexts.append(item)
    return sorted(
        contexts,
        key=lambda item: (
            str(item.get("source_node_id")),
            str(item.get("source_field")),
            str(item.get("quote")),
        ),
    )


def _locators(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[Any] = []
    for field in ("source_locator", "locator"):
        value = node.get(field)
        if isinstance(value, Mapping):
            values.append(value)
    for field in ("source_locators", "locators"):
        value = node.get(field)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, Mapping))
    locators = []
    for value in values:
        clean = {key: item for key, item in value.items() if item not in (None, "", [], {})}
        if clean:
            locators.append(clean)
    return sorted(locators, key=_canonical)


def _packet_prompt(
    *,
    plan: Mapping[str, Any] | None,
    targets: list[Mapping[str, Any]],
    context_counts: Mapping[str, int],
    manuscript: Mapping[str, Any] | None,
) -> str:
    lines = [
        "Structured review packet.",
        "Review the supplied records independently. Do not use another reviewer's output.",
    ]
    if plan is None:
        lines.append("Plan: none supplied.")
    else:
        lines.extend(
            [
                f"Review question: {plan.get('question', '')}",
                f"Review kind: {plan.get('kind', '')}",
                f"Roles: {', '.join(str(item) for item in plan.get('roles') or [])}",
                f"Models: {', '.join(str(item) for item in plan.get('models') or [])}",
                f"Minimum reviewers: {plan.get('min_reviewers', '')}",
                f"Blinding: {bool(plan.get('blinding'))}",
                f"Adjudication: {plan.get('adjudication', '')}",
            ]
        )
        criteria = plan.get("criteria")
        if criteria:
            lines.append(f"Inclusion criteria: {_canonical(criteria)}")
        exclusion = plan.get("exclusion_criteria")
        if exclusion:
            lines.append(f"Exclusion criteria: {_canonical(exclusion)}")
    lines.append("Targets:")
    if not targets:
        lines.append("- Manuscript body supplied without a graph target.")
    for node in targets:
        text = _collapse(_node_text(node))
        lines.append(f"- {node.get('id')} ({node.get('type')}): {text}")
    lines.append("One-hop context counts:")
    for group in _CONTEXT_GROUPS:
        lines.append(f"- {group}: {context_counts.get(group, 0)}")
    if manuscript is not None:
        lines.append(f"Manuscript body sha256: {manuscript['sha256']}")
    lines.extend(
        [
            "Return one JSON object with this shape:",
            '{"summary": "string", "findings": ['
            '{"wording": "string", "severity": "blocking|major|minor|note", '
            '"issue_type": "string", "claim_id": "optional", '
            '"reference_id": "optional", "evidence_ids": ["optional"], '
            '"citation_keys": ["optional"], "quote": "optional", '
            '"recommendation": "optional", "confidence": 0.0}], '
            '"recommendation": "optional", "confidence": 0.0}',
            "Rules: only cite IDs and citation keys present in this packet; "
            "quote only text present in this packet; report uncertainty; "
            "do not decide truth and do not turn agreement into support.",
        ]
    )
    return "\n".join(lines)


def build_review_packet(
    state: Any,
    *,
    plan_id: str | None = None,
    claim_id: str | None = None,
    reference_id: str | None = None,
    manuscript_body: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only review packet.

    At least one of a plan, claim, reference or manuscript body is required.
    A plan's addressed target is used as the packet target when no explicit
    claim or reference is supplied.
    """
    current = _as_state(state)
    if plan_id is None and claim_id is None and reference_id is None and manuscript_body is None:
        raise ReviewError("a review packet needs a plan, claim, reference, or manuscript body")
    if manuscript_body is not None:
        if not isinstance(manuscript_body, str):
            raise ReviewError("manuscript_body must be a string")
        if not manuscript_body.strip():
            raise ReviewError("manuscript_body must be non-empty")

    plan = None
    if plan_id is not None:
        plan = _require_node(current, plan_id, frozenset({"ReviewPlan"}), "review plan")
    claim = None
    if claim_id is not None:
        claim = _require_node(current, claim_id, frozenset({"Claim"}), "claim")
    reference = None
    if reference_id is not None:
        reference = _require_node(current, reference_id, frozenset({"Reference"}), "reference")

    addressed = _plan_addresses(current, plan) if plan is not None else []
    target_nodes: list[Mapping[str, Any]] = []
    for node in [claim, reference, *addressed]:
        if node is not None and node["id"] not in {item["id"] for item in target_nodes}:
            target_nodes.append(node)
    target_nodes.sort(key=lambda node: (str(node.get("type")), str(node.get("id"))))
    if not target_nodes and plan is not None:
        target_nodes.append(plan)
    primary = claim or reference or (addressed[0] if addressed else plan)

    context: dict[str, list[dict[str, Any]]] = {group: [] for group in _CONTEXT_GROUPS}
    context_nodes: dict[str, Mapping[str, Any]] = {}
    seen_context_edges: set[tuple[Any, ...]] = set()
    for target in target_nodes:
        for edge in _edges(current):
            if target["id"] not in (edge.get("src"), edge.get("tgt")):
                continue
            edge_key = (
                str(edge.get("id") or ""),
                str(edge.get("type") or ""),
                str(edge.get("src") or ""),
                str(edge.get("tgt") or ""),
            )
            if edge_key in seen_context_edges:
                continue
            for group, relations in _CONTEXT_GROUPS.items():
                if edge.get("type") not in relations:
                    continue
                other_id = edge.get("tgt") if edge.get("src") == target["id"] else edge.get("src")
                other = _node_by_id(current, str(other_id))
                if other is None:
                    continue
                seen_context_edges.add(edge_key)
                locators = _locators(other)
                context[group].append(
                    {
                        "node_id": other["id"],
                        "node_type": other.get("type"),
                        "wording": _collapse(_node_text(other)),
                        "citation_keys": _node_citation_keys(other),
                        "source_locator": locators[0] if locators else None,
                        "edge_id": edge.get("id"),
                        "relation": edge.get("type"),
                        "direction": "out" if edge.get("src") == target["id"] else "in",
                        "node": dict(other),
                        "edge": dict(edge),
                    }
                )
                context_nodes[str(other["id"])] = other
                break
    for group in context:
        context[group].sort(
            key=lambda item: (
                str(item["edge"].get("type")),
                str(item["edge"].get("src")),
                str(item["edge"].get("tgt")),
                str(item["edge"].get("id")),
            )
        )
    ordered_context_nodes = [context_nodes[node_id] for node_id in sorted(context_nodes)]

    locator_nodes: dict[str, Mapping[str, Any]] = {}
    if plan is not None:
        locator_nodes[str(plan["id"])] = plan
    for node in [*target_nodes, *ordered_context_nodes]:
        locator_nodes[str(node["id"])] = node
    source_locators = []
    for node_id in sorted(locator_nodes):
        node = locator_nodes[node_id]
        for locator in _locators(node):
            source_locators.append(
                {
                    **locator,
                    "node_id": node["id"],
                    "node_type": node.get("type"),
                    "locator": locator,
                }
            )

    checks: dict[str, Any] = {"references": refs.check_references(current)}
    manuscript = None
    if manuscript_body is not None:
        manuscript = {
            "body": manuscript_body,
            "sha256": _text_digest(manuscript_body),
            "length": len(manuscript_body),
        }
        checks["manuscript"] = manuscript_mod.check_manuscript(current, manuscript_body)
    citation_contexts = _text_citation_contexts(target_nodes)

    text_nodes: dict[str, Mapping[str, Any]] = {}
    if plan is not None:
        text_nodes[str(plan["id"])] = plan
    for node in [*target_nodes, *ordered_context_nodes]:
        text_nodes[str(node["id"])] = node
    gathered_parts: list[str] = []
    for node_id in sorted(text_nodes):
        text = _node_text(text_nodes[node_id])
        if text:
            gathered_parts.append(text)
    if manuscript is not None:
        gathered_parts.append(manuscript_body)
    for group in context:
        for item in context[group]:
            rationale = item["edge"].get("rationale")
            if isinstance(rationale, str) and rationale.strip():
                gathered_parts.append(rationale.strip())
    gathered_text = "\n".join(gathered_parts)

    context_counts = {group: len(items) for group, items in context.items()}
    prompt = _packet_prompt(
        plan=plan,
        targets=target_nodes,
        context_counts=context_counts,
        manuscript=manuscript,
    )
    packet: dict[str, Any] = {
        "schema": "ozagia.review_packet.v1",
        "plan": dict(plan) if plan is not None else None,
        "target": dict(primary) if primary is not None else None,
        "target_node": dict(primary) if primary is not None else None,
        "target_id": primary.get("id") if primary is not None else None,
        "target_type": primary.get("type") if primary is not None else (
            "Manuscript" if manuscript is not None else None
        ),
        "targets": [dict(node) for node in target_nodes],
        "addressed": [dict(node) for node in addressed],
        "context": context,
        "supports": context["supporting"],
        "opposes": context["opposing"],
        "assumptions": context["assuming"],
        "assuming": context["assuming"],
        "derived": context["derived"],
        "derived_from": context["derived"],
        "citations": context["citation"],
        "citation_edges": context["citation"],
        "citation_contexts": citation_contexts,
        "context_nodes": [dict(node) for node in ordered_context_nodes],
        "source_locators": source_locators,
        "checks": checks,
        "manuscript": manuscript,
        "gathered_text": gathered_text,
        "prompt": prompt,
        "truth_assessed": False,
    }
    if primary is not None and primary.get("type") == "Claim":
        packet["claim"] = dict(primary)
    if primary is not None and primary.get("type") == "Reference":
        packet["reference"] = dict(primary)
    packet["packet_sha256"] = _digest(packet)
    return packet


# --------------------------------------------------------------------------- #
# output validation
# --------------------------------------------------------------------------- #
def _check_id(
    value: Any,
    field: str,
    expected: frozenset[str],
    state: Mapping[str, Any] | None,
    errors: list[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be a non-empty string")
        return None
    value = value.strip()
    if state is not None:
        node = _node_by_id(state, value)
        if node is None:
            errors.append(f"{field} references unknown id {value!r}")
            return value
        if node.get("type") not in expected:
            errors.append(
                f"{field} {value!r} must be one of {sorted(expected)}, not {node.get('type')}"
            )
    return value


def _normalise_finding(
    raw: Any,
    index: int,
    *,
    state: Mapping[str, Any] | None,
    reference_keys: Mapping[str, Mapping[str, Any]] | None,
    packet_text: str | None,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    prefix = f"findings[{index}]"
    if not isinstance(raw, Mapping):
        errors.append(f"{prefix} must be an object")
        return {}
    known = {
        "wording",
        "severity",
        "issue_type",
        "claim_id",
        "reference_id",
        "argument_id",
        "evidence_ids",
        "citation_keys",
        "quote",
        "recommendation",
        "confidence",
    }
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        warnings.append(f"{prefix} has unknown fields: {', '.join(unknown)}")

    normal: dict[str, Any] = {}
    wording = raw.get("wording")
    if not isinstance(wording, str) or not wording.strip():
        errors.append(f"{prefix}.wording must be a non-empty string")
    else:
        normal["wording"] = wording.strip()

    severity = raw.get("severity")
    if not isinstance(severity, str) or severity.strip().casefold() not in _SEVERITY_SET:
        errors.append(
            f"{prefix}.severity must be one of {list(SEVERITIES)}"
        )
    else:
        normal["severity"] = severity.strip().casefold()

    issue_type = raw.get("issue_type")
    if not isinstance(issue_type, str) or not issue_type.strip():
        errors.append(f"{prefix}.issue_type must be a non-empty string")
    else:
        normal["issue_type"] = issue_type.strip()

    for field, expected in (
        ("claim_id", frozenset({"Claim"})),
        ("reference_id", frozenset({"Reference"})),
        ("argument_id", frozenset({"Argument"})),
    ):
        value = _check_id(raw.get(field), f"{prefix}.{field}", expected, state, errors)
        if value is not None:
            normal[field] = value

    evidence = raw.get("evidence_ids")
    if evidence is not None:
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item.strip() for item in evidence
        ):
            errors.append(f"{prefix}.evidence_ids must be a list of non-empty strings")
        else:
            cleaned = _dedupe_values(evidence)
            for value in cleaned:
                _check_id(
                    value,
                    f"{prefix}.evidence_ids",
                    _FINDING_TARGET_TYPES,
                    state,
                    errors,
                )
            if cleaned:
                normal["evidence_ids"] = cleaned

    citations = raw.get("citation_keys")
    if citations is not None:
        if not isinstance(citations, list) or any(
            not isinstance(item, str) or not item.strip() for item in citations
        ):
            errors.append(f"{prefix}.citation_keys must be a list of non-empty strings")
        else:
            cleaned = _dedupe_values(citations)
            if reference_keys is not None:
                for key in cleaned:
                    if key.casefold() not in reference_keys:
                        errors.append(
                            f"{prefix}.citation_keys references unknown citation key {key!r}"
                        )
            if cleaned:
                normal["citation_keys"] = cleaned

    quote = raw.get("quote")
    if quote is not None:
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"{prefix}.quote must be a non-empty string")
        else:
            normal["quote"] = quote.strip()
            if packet_text is not None and _collapse(quote) not in packet_text:
                errors.append(f"{prefix}.quote is not present in the review packet text")

    recommendation = raw.get("recommendation")
    if recommendation is not None:
        if not isinstance(recommendation, str):
            errors.append(f"{prefix}.recommendation must be a string")
        else:
            normal["recommendation"] = recommendation

    confidence = _confidence(raw.get("confidence"), f"{prefix}.confidence", errors)
    if confidence is not None:
        normal["confidence"] = confidence
    return normal


def validate_review_output(
    output: Any,
    *,
    state: Any = None,
    packet: Any = None,
    drop_invalid_findings: bool = False,
) -> dict[str, Any]:
    """Validate one model-shaped review output without recording anything.

    With ``drop_invalid_findings=True``, a malformed or unsupported finding is
    reported as a warning and omitted instead of invalidating the whole run.
    This is for model drivers: one unsupported quote should not discard every
    valid finding in the same review.
    """
    errors: list[str] = []
    warnings: list[str] = []
    findings: list[dict[str, Any]] = []
    current: Mapping[str, Any] | None = None
    reference_keys: Mapping[str, Mapping[str, Any]] | None = None
    if state is not None:
        try:
            current = _as_state(state)
        except ReviewError as exc:
            errors.append(str(exc))
        else:
            reference_keys = _reference_key_index(current)

    packet_text: str | None = None
    if packet is not None:
        if not isinstance(packet, Mapping):
            errors.append("packet must be an object")
        else:
            packet_text = _packet_text(packet)

    if not isinstance(output, Mapping):
        errors.append("output must be an object")
        return {"valid": False, "errors": errors, "warnings": warnings, "findings": findings}

    known = {"summary", "strengths", "findings", "recommendation", "confidence"}
    unknown = sorted(str(key) for key in output if key not in known)
    if unknown:
        warnings.append(f"output has unknown fields: {', '.join(unknown)}")

    summary = output.get("summary")
    if not isinstance(summary, str):
        errors.append("summary must be a string")
    elif not summary.strip():
        warnings.append("summary is empty")

    raw_strengths = output.get("strengths")
    strengths: list[str] = []
    if raw_strengths is not None:
        if not isinstance(raw_strengths, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_strengths
        ):
            errors.append("strengths must be a list of non-empty strings")
        else:
            strengths = list(dict.fromkeys(item.strip() for item in raw_strengths))

    raw_findings = output.get("findings")
    if not isinstance(raw_findings, list):
        errors.append("findings must be a list")
    elif not raw_findings:
        warnings.append("findings is empty")
    else:
        for index, raw in enumerate(raw_findings):
            finding_errors: list[str] = []
            finding = _normalise_finding(
                raw,
                index,
                state=current,
                reference_keys=reference_keys,
                packet_text=packet_text,
                errors=finding_errors,
                warnings=warnings,
            )
            if finding_errors and drop_invalid_findings:
                warnings.append(
                    f"findings[{index}] dropped: " + "; ".join(finding_errors))
                continue
            errors.extend(finding_errors)
            findings.append(finding)

    recommendation = output.get("recommendation")
    if recommendation is not None and not isinstance(recommendation, str):
        errors.append("recommendation must be a string")
    _confidence(output.get("confidence"), "confidence", errors)

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "strengths": strengths,
        "findings": findings,
    }


# --------------------------------------------------------------------------- #
# recording runs and findings
# --------------------------------------------------------------------------- #
def _finding_target_ids(finding: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for field in ("claim_id", "reference_id", "argument_id"):
        value = finding.get(field)
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    evidence = finding.get("evidence_ids")
    if isinstance(evidence, list):
        for value in evidence:
            if isinstance(value, str) and value and value not in ids:
                ids.append(value)
    return ids


def _run_targets(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
    target_id: str | None,
) -> list[Mapping[str, Any]]:
    ids: list[str] = []
    if target_id is not None:
        target = _require_node(state, target_id, _REVIEW_TARGET_TYPES, "review target")
        ids.append(target["id"])
    else:
        for node in _plan_addresses(state, plan):
            if node["id"] not in ids:
                ids.append(node["id"])
    return sorted(
        (
            node for node in (_node_by_id(state, node_id) for node_id in ids)
            if node is not None
        ),
        key=lambda node: str(node["id"]),
    )


def record_review_run(
    graph: Any,
    *,
    plan_id: str,
    reviewer_id: str,
    role: str,
    model: str,
    packet_sha256: str,
    output: Any,
    provider: str | None = None,
    round: str = "independent",
    prompt_sha256: str | None = None,
    context_sha256: str | None = None,
    params: Any = None,
    tokens: Any = None,
    cost: Any = None,
    status: str = "recorded",
    target_id: str | None = None,
) -> dict[str, Any]:
    """Validate and record one reviewer's independent run and findings."""
    state = graph.state()
    plan = _require_node(state, plan_id, frozenset({"ReviewPlan"}), "review plan")
    validation = validate_review_output(output, state=state)
    if not validation["valid"]:
        raise ReviewError("invalid review output: " + "; ".join(validation["errors"]))

    reviewer_id = _nonempty_string(reviewer_id, "reviewer_id")
    role = _nonempty_string(role, "role")
    model = _nonempty_string(model, "model")
    packet_sha256 = _nonempty_string(packet_sha256, "packet_sha256")
    round_name = _nonempty_string(round, "round")
    status = _nonempty_string(status, "status")
    provider = _optional_string(provider, "provider")
    prompt_sha256 = _optional_string(prompt_sha256, "prompt_sha256")
    context_sha256 = _optional_string(context_sha256, "context_sha256")
    if params is not None and not isinstance(params, Mapping):
        raise ReviewError("params must be an object")
    if params is not None:
        params = _json_safe(params, "params")
    if tokens is not None and not isinstance(tokens, Mapping):
        tokens = _nonnegative_int(tokens, "tokens")
    if tokens is not None and isinstance(tokens, Mapping):
        tokens = _json_safe(tokens, "tokens")
    cost = _nonnegative_number(cost, "cost")

    targets = _run_targets(state, plan, target_id)
    output_sha256 = _digest(output)
    summary = output.get("summary")
    recommendation = output.get("recommendation")
    confidence = output.get("confidence")
    run_fields: dict[str, Any] = {
        "plan_id": plan["id"],
        "reviewer_id": reviewer_id,
        "role": role,
        "model": model,
        "provider": provider,
        "round": round_name,
        "packet_sha256": packet_sha256,
        "prompt_sha256": prompt_sha256,
        "context_sha256": context_sha256,
        "params": params,
        "tokens": tokens,
        "cost": cost,
        "status": status,
        "output_sha256": output_sha256,
        "summary": summary,
        "strengths": list(validation.get("strengths") or []),
        "recommendation": recommendation,
        "confidence": confidence,
        "finding_count": len(validation["findings"]),
        "target_ids": [node["id"] for node in targets],
        "warnings": list(validation["warnings"]),
        "truth_assessed": False,
    }
    if target_id is not None:
        run_fields["target_id"] = target_id
    elif len(targets) == 1:
        run_fields["target_id"] = targets[0]["id"]
    run = graph.record("ReviewRun", **run_fields)
    graph.link(
        run["id"],
        "implements",
        plan["id"],
        provenance={"kind": "review_run", "reviewer_id": reviewer_id},
        rationale="The run implements the recorded review plan.",
    )
    for target in targets:
        graph.link(
            run["id"],
            "reviews",
            target["id"],
            provenance={"kind": "review_run", "reviewer_id": reviewer_id},
            rationale="The run reviewed this target; it does not assert support.",
        )

    recorded_findings: list[dict[str, Any]] = []
    for finding in validation["findings"]:
        fields: dict[str, Any] = {
            "plan_id": plan["id"],
            "run_id": run["id"],
            "truth_assessed": False,
        }
        for field in (
            "wording",
            "severity",
            "issue_type",
            "quote",
            "recommendation",
            "confidence",
            "citation_keys",
            "claim_id",
            "reference_id",
            "evidence_ids",
            "argument_id",
        ):
            if finding.get(field) is not None:
                fields[field] = finding[field]
        node = graph.record("ReviewFinding", **fields)
        graph.link(
            run["id"],
            "finds",
            node["id"],
            provenance={"kind": "review_finding", "reviewer_id": reviewer_id},
            rationale="The finding was reported by this run.",
        )
        for target in _finding_target_ids(finding):
            graph.link(
                node["id"],
                "about",
                target,
                provenance={"kind": "review_finding_target", "reviewer_id": reviewer_id},
                rationale="The finding refers to this record; it is not a support relation.",
            )
        recorded_findings.append(node)

    return {
        "run": run,
        "findings": recorded_findings,
        "validation": validation,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# agreement
# --------------------------------------------------------------------------- #
def _independent_runs(
    state: Mapping[str, Any],
    plan_id: str,
    run_ids: Sequence[str] | None = None,
) -> list[Mapping[str, Any]]:
    if run_ids is not None:
        if isinstance(run_ids, str) or not isinstance(run_ids, Sequence):
            raise ReviewError("run_ids must be a list of ReviewRun ids")
        selected: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for run_id in run_ids:
            run = _require_node(state, str(run_id), frozenset({"ReviewRun"}), "review run")
            if run.get("plan_id") != plan_id:
                raise ReviewError(f"review run {run_id} does not belong to plan {plan_id}")
            if run["id"] not in seen:
                seen.add(run["id"])
                selected.append(run)
        return sorted(selected, key=lambda node: str(node["id"]))
    runs = []
    for node in _nodes(state):
        if node.get("type") != "ReviewRun" or node.get("plan_id") != plan_id:
            continue
        if (node.get("round") or "independent") != "independent":
            continue
        if node.get("status") in {"failed", "error", "skipped"}:
            continue
        runs.append(node)
    return sorted(runs, key=lambda node: str(node["id"]))


def _findings_for_runs(
    state: Mapping[str, Any],
    runs: list[Mapping[str, Any]],
    plan_id: str | None = None,
) -> tuple[dict[str, list[Mapping[str, Any]]], list[str]]:
    run_ids = {str(run["id"]) for run in runs}
    finds: dict[str, set[str]] = {run_id: set() for run_id in run_ids}
    for edge in _edges(state):
        if edge.get("type") == "finds" and edge.get("src") in run_ids:
            finds[str(edge["src"])].add(str(edge.get("tgt")))
    by_id = {
        str(node["id"]): node for node in _nodes(state)
        if node.get("type") == "ReviewFinding"
    }
    result: dict[str, list[Mapping[str, Any]]] = {}
    for run in runs:
        run_id = str(run["id"])
        ids = set(finds[run_id])
        ids.update(
            node_id for node_id, node in by_id.items()
            if node.get("run_id") == run_id
        )
        result[run_id] = sorted(
            (by_id[node_id] for node_id in ids if node_id in by_id),
            key=lambda node: str(node["id"]),
        )
    linked = {
        str(finding["id"])
        for findings in result.values()
        for finding in findings
    }
    orphan = sorted(
        node_id for node_id, node in by_id.items()
        if node.get("plan_id") == plan_id and node_id not in linked
    ) if plan_id is not None else []
    return result, orphan


def _finding_targets(
    state: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for edge in _edges(state):
        if edge.get("type") != "about" or edge.get("src") != finding.get("id"):
            continue
        target_id = str(edge.get("tgt"))
        node = _node_by_id(state, target_id)
        target_type = str(node.get("type")) if node is not None else "Unknown"
        item = (target_type, target_id)
        if item not in targets:
            targets.append(item)
    if targets:
        return sorted(targets)
    fallbacks = (
        ("Claim", "claim_id"),
        ("Reference", "reference_id"),
        ("Argument", "argument_id"),
    )
    for target_type, field in fallbacks:
        value = finding.get(field)
        if isinstance(value, str) and value:
            targets.append((target_type, value))
    evidence = finding.get("evidence_ids")
    if isinstance(evidence, list):
        for value in evidence:
            if isinstance(value, str) and value:
                targets.append(("Evidence", value))
    return sorted(dict.fromkeys(targets))


def _crowded_severity(severities: list[str]) -> str | None:
    counts: dict[str, int] = {}
    for severity in severities:
        counts[severity] = counts.get(severity, 0) + 1
    if not counts:
        return None
    return min(
        counts,
        key=lambda severity: (-counts[severity], SEVERITIES.index(severity)),
    )


def _fleiss_kappa(presence: list[int], n_runs: int) -> float | None:
    if n_runs < 2 or not presence:
        return None
    p_bar = sum(
        (count * count + (n_runs - count) * (n_runs - count) - n_runs)
        / (n_runs * (n_runs - 1))
        for count in presence
    ) / len(presence)
    total_present = sum(presence)
    total_ratings = n_runs * len(presence)
    present_share = total_present / total_ratings
    expected = present_share * present_share + (1 - present_share) * (1 - present_share)
    if expected >= 1.0:
        # No marginal variation: perfect agreement by convention (every
        # recorded issue is present in every run).
        return 1.0 if p_bar >= 1.0 else None
    return round((p_bar - expected) / (1 - expected), 6)


def _mean_jaccard(key_sets: list[set[tuple[str, str, str, str]]]) -> float | None:
    if len(key_sets) < 2:
        return None
    values = []
    for left, right in combinations(key_sets, 2):
        union = left | right
        values.append(1.0 if not union else len(left & right) / len(union))
    return round(sum(values) / len(values), 6)


def _agreement(
    state: Mapping[str, Any],
    plan_id: str,
    *,
    run_ids: Sequence[str] | None = None,
    min_reviewers: int | None = None,
    embedder: Callable[..., Any] | None = None,
    cluster_threshold: float = 0.76,
    cluster_groups: Sequence[Sequence[int]] | None = None,
    cluster_method: str = "embedding",
    cluster_model: str | None = None,
) -> dict[str, Any]:
    plan = _require_node(state, plan_id, frozenset({"ReviewPlan"}), "review plan")
    runs = _independent_runs(state, plan_id, run_ids)
    findings_by_run, orphans = _findings_for_runs(state, runs, plan_id)
    run_ids_list = [str(run["id"]) for run in runs]
    reviewers = sorted(
        {str(run.get("reviewer_id") or run["id"]) for run in runs}
    )
    if min_reviewers is None:
        min_reviewers = plan.get("min_reviewers")
    if isinstance(min_reviewers, bool) or not isinstance(min_reviewers, int):
        min_reviewers = 2

    issues: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for run in runs:
        run_id = str(run["id"])
        reviewer_id = str(run.get("reviewer_id") or run_id)
        for finding in findings_by_run[run_id]:
            wording = finding.get("wording")
            if not isinstance(wording, str):
                continue
            issue_type = _collapse(finding.get("issue_type"))
            severity = finding.get("severity")
            severity = severity if severity in _SEVERITY_SET else None
            targets = _finding_targets(state, finding)
            if not targets:
                targets = [("", "")]
            for target_type, target_id in targets:
                key = (
                    target_type,
                    target_id,
                    issue_type,
                    _normalised_wording(wording),
                )
                entry = issues.setdefault(
                    key,
                    {
                        "target_type": target_type,
                        "target_id": target_id,
                        "issue_type": issue_type,
                        "wording": _collapse(wording),
                        "normalized_wording": key[3],
                        "finding_ids": set(),
                        "present_runs": set(),
                        "present_reviewers": set(),
                        "severity_by_run": {},
                    },
                )
                entry["finding_ids"].add(str(finding["id"]))
                entry["present_runs"].add(run_id)
                entry["present_reviewers"].add(reviewer_id)
                if severity is not None:
                    entry["severity_by_run"].setdefault(run_id, severity)

    rows: list[dict[str, Any]] = []
    severity_pairs = 0
    severity_agreements = 0
    comparable_issues = 0
    unanimous_issues = 0
    for key in sorted(issues):
        entry = issues[key]
        present_runs = entry["present_runs"]
        present_reviewers = entry["present_reviewers"]
        severities = [
            entry["severity_by_run"][run_id]
            for run_id in run_ids_list
            if run_id in entry["severity_by_run"]
        ]
        if len(severities) >= 2:
            comparable_issues += 1
            if len(set(severities)) == 1:
                unanimous_issues += 1
            for left, right in combinations(severities, 2):
                severity_pairs += 1
                if left == right:
                    severity_agreements += 1
        consensus = len(present_reviewers) >= min_reviewers
        dissent = (
            len(reviewers) >= 2
            and len(present_reviewers) == 1
            and not consensus
        )
        row = {
            "key": list(key),
            "target_type": entry["target_type"],
            "target_id": entry["target_id"],
            "issue_type": entry["issue_type"],
            "wording": entry["wording"],
            "normalized_wording": entry["normalized_wording"],
            "finding_ids": sorted(entry["finding_ids"]),
            "run_ids": sorted(present_runs),
            "reviewers": sorted(present_reviewers),
            "run_count": len(present_runs),
            "reviewer_count": len(present_reviewers),
            "severity": _crowded_severity(severities),
            "severities": {
                run_id: entry["severity_by_run"].get(run_id)
                for run_id in run_ids_list
            },
            "presence": {run_id: run_id in present_runs for run_id in run_ids_list},
            "reviewer_presence": {
                reviewer_id: reviewer_id in present_reviewers
                for reviewer_id in reviewers
            },
            "consensus": consensus,
            "dissent": dissent,
        }
        rows.append(row)

    consensus_rows = [row for row in rows if row["consensus"]]
    dissent_rows = [row for row in rows if row["dissent"]]
    single_reviewer_rows = [
        row for row in rows if row["reviewer_count"] == 1
    ]
    presence_counts = [row["run_count"] for row in rows]
    key_sets = [
        {
            tuple(row["key"])
            for row in rows
            if row["presence"][run_id]
        }
        for run_id in run_ids_list
    ]
    severity_rate = (
        round(severity_agreements / severity_pairs, 6) if severity_pairs else None
    )
    kappa = _fleiss_kappa(presence_counts, len(runs))
    jaccard = _mean_jaccard(key_sets)
    warnings: list[str] = []
    if not runs:
        warnings.append("no independent review runs for this plan")
    elif not any(findings_by_run[run_id] for run_id in run_ids_list):
        warnings.append("independent review runs contain no findings")
    if orphans:
        warnings.append(
            "findings are not linked to a selected run: " + ", ".join(orphans)
        )

    issue_matrix = {
        "runs": run_ids_list,
        "reviewers": reviewers,
        "rows": rows,
    }
    all_findings = sorted(
        (
            finding
            for run_id in run_ids_list
            for finding in findings_by_run[run_id]
        ),
        key=lambda node: str(node["id"]),
    )
    finding_nodes = {str(finding["id"]): dict(finding) for finding in all_findings}
    for row in rows:
        row["findings"] = [
            finding_nodes[finding_id]
            for finding_id in row["finding_ids"]
            if finding_id in finding_nodes
        ]
    result = {
        "plan_id": plan["id"],
        "plan": dict(plan),
        "min_reviewers": min_reviewers,
        "runs": [dict(run) for run in runs],
        "run_ids": run_ids_list,
        "reviewers": reviewers,
        "findings": [dict(finding) for finding in all_findings],
        "findings_by_run": {
            run_id: [dict(finding) for finding in findings_by_run[run_id]]
            for run_id in run_ids_list
        },
        "issues": rows,
        "issue_matrix": issue_matrix,
        "fleiss_kappa": kappa,
        "kappa": kappa,
        "mean_pairwise_jaccard": jaccard,
        "mean_jaccard": jaccard,
        "severity_agreement": severity_rate,
        "severity_agreement_detail": {
            "rate": severity_rate,
            "agreements": severity_agreements,
            "comparisons": severity_pairs,
            "comparable_issues": comparable_issues,
            "unanimous_issues": unanimous_issues,
        },
        "consensus": consensus_rows,
        "dissent": dissent_rows,
        "counts": {
            "runs": len(runs),
            "reviewers": len(reviewers),
            "findings": len(all_findings),
            "issues": len(rows),
            "consensus": len(consensus_rows),
            "dissent": len(dissent_rows),
            "single_reviewer": len(single_reviewer_rows),
        },
        "warnings": warnings,
        "truth_assessed": False,
    }
    if cluster_groups is not None:
        from ozagia.research.semantic_reconcile import recluster_agreement
        return recluster_agreement(
            result, groups=cluster_groups, method=cluster_method,
            model=cluster_model, threshold=cluster_threshold)
    if embedder is not None:
        from ozagia.research.semantic_reconcile import recluster_agreement
        return recluster_agreement(
            result, embedder=embedder, threshold=cluster_threshold)
    return result


def review_agreement(
    state: Any,
    plan_id: str,
    *,
    embedder: Callable[..., Any] | None = None,
    cluster_threshold: float = 0.76,
    cluster_groups: Sequence[Sequence[int]] | None = None,
    cluster_method: str = "embedding",
    cluster_model: str | None = None,
) -> dict[str, Any]:
    """Report finding agreement across independent runs; not accuracy."""
    current = _as_state(state)
    return _agreement(
        current, plan_id, embedder=embedder, cluster_threshold=cluster_threshold,
        cluster_groups=cluster_groups, cluster_method=cluster_method,
        cluster_model=cluster_model)


# --------------------------------------------------------------------------- #
# reconciliation and report
# --------------------------------------------------------------------------- #
def record_reconciliation(
    graph: Any,
    *,
    plan_id: str,
    method: str = "consensus",
    min_reviewers: int | None = None,
    adjudicator: Any = None,
    rationale: str | None = None,
    run_ids: Sequence[str] | None = None,
    embedder: Callable[..., Any] | None = None,
    cluster_threshold: float = 0.76,
    cluster_groups: Sequence[Sequence[int]] | None = None,
    cluster_method: str = "embedding",
    cluster_model: str | None = None,
) -> dict[str, Any]:
    """Record a reconciliation over runs while preserving every dissent."""
    state = graph.state()
    plan = _require_node(state, plan_id, frozenset({"ReviewPlan"}), "review plan")
    method = _nonempty_string(method, "method")
    if min_reviewers is None:
        min_reviewers = plan.get("min_reviewers", 2)
    if isinstance(min_reviewers, bool) or not isinstance(min_reviewers, int) or min_reviewers < 1:
        raise ReviewError("min_reviewers must be a positive integer")
    if adjudicator is not None:
        adjudicator = _json_safe(adjudicator, "adjudicator")
    rationale = _optional_string(rationale, "rationale")

    agreement = _agreement(
        state, plan_id, run_ids=run_ids, min_reviewers=min_reviewers,
        embedder=embedder, cluster_threshold=cluster_threshold,
        cluster_groups=cluster_groups, cluster_method=cluster_method,
        cluster_model=cluster_model)
    selected_run_ids = agreement["run_ids"]
    metrics = {
        "fleiss_kappa": agreement["fleiss_kappa"],
        "mean_pairwise_jaccard": agreement["mean_pairwise_jaccard"],
        "severity_agreement": agreement["severity_agreement"],
        "severity_agreement_detail": agreement["severity_agreement_detail"],
        "counts": agreement["counts"],
    }
    fields: dict[str, Any] = {
        "plan_id": plan["id"],
        "method": method,
        "min_reviewers": min_reviewers,
        "run_ids": selected_run_ids,
        "metrics": metrics,
        "clustering": agreement.get("clustering"),
        "clusters": agreement["issues"],
        "consensus": agreement["consensus"],
        "dissent": agreement["dissent"],
        "status": "recorded",
        "truth_assessed": False,
    }
    if adjudicator is not None:
        fields["adjudicator"] = adjudicator
    if rationale is not None:
        fields["rationale"] = rationale
    reconciliation = graph.record("Reconciliation", **fields)
    for run_id in selected_run_ids:
        graph.link(
            reconciliation["id"],
            "reconciles",
            run_id,
            provenance={"kind": "review_reconciliation", "method": method},
            rationale=rationale or "The reconciliation compares this run with the others.",
        )
    return {
        "reconciliation": reconciliation,
        "consensus": agreement["consensus"],
        "dissent": agreement["dissent"],
        "agreement": agreement,
        "truth_assessed": False,
    }


def review_report(state: Any, plan_id: str) -> dict[str, Any]:
    """Return the complete recorded review trail for one plan."""
    current = _as_state(state)
    plan = _require_node(current, plan_id, frozenset({"ReviewPlan"}), "review plan")
    runs = sorted(
        (
            node for node in _nodes(current)
            if node.get("type") == "ReviewRun" and node.get("plan_id") == plan_id
        ),
        key=lambda node: str(node["id"]),
    )
    per_run, _orphans = _findings_for_runs(current, runs, plan_id)
    finding_map = {
        str(finding["id"]): finding
        for items in per_run.values()
        for finding in items
    }
    for node in _nodes(current):
        if node.get("type") == "ReviewFinding" and node.get("plan_id") == plan_id:
            finding_map.setdefault(str(node["id"]), node)
    findings = sorted(finding_map.values(), key=lambda node: str(node["id"]))
    findings_by_run: dict[str, list[dict[str, Any]]] = {
        str(run["id"]): [dict(finding) for finding in per_run[str(run["id"])]]
        for run in runs
    }

    reconciliations = sorted(
        (
            node for node in _nodes(current)
            if node.get("type") == "Reconciliation" and node.get("plan_id") == plan_id
        ),
        key=lambda node: (str(node.get("created_at") or ""), str(node["id"])),
    )
    latest = dict(reconciliations[-1]) if reconciliations else None
    reference_check = refs.check_references(current)
    run_warnings = [
        {"run_id": run["id"], "warnings": list(run.get("warnings") or [])}
        for run in runs
        if run.get("warnings")
    ]
    severity_counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        severity = finding.get("severity")
        if severity in severity_counts:
            severity_counts[severity] += 1
    checks = {
        "ok": reference_check["ok"] and not run_warnings,
        "references": reference_check,
        "run_warnings": run_warnings,
        "severity_counts": severity_counts,
    }
    return {
        "plan": dict(plan),
        "plan_id": plan["id"],
        "runs": [dict(run) for run in runs],
        "findings": [dict(finding) for finding in findings],
        "findings_by_run": findings_by_run,
        "reconciliation": latest,
        "latest_reconciliation": latest,
        "reconciliations": [dict(node) for node in reconciliations],
        "checks": checks,
        "counts": {
            "runs": len(runs),
            "findings": len(findings),
            "reconciliations": len(reconciliations),
        },
        "truth_assessed": False,
    }
