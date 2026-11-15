"""Steelman, red-team and synthesis arguments over the research graph.

An argument is an authored candidate with explicit obligations: premises and
their inference kinds, assumptions, objections, replies or explicit unresolved
objections, and falsifiers. This module builds a deterministic brief, records a
validated ``Argument`` node and checks it mechanically. It never calls a model,
creates ``supports``/``opposes`` evidence, or claims that a thesis is true.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError

ARGUMENT_MODES = frozenset({"steelman", "red_team", "synthesis"})
INFERENCE_TYPES = frozenset(
    {"deductive", "inductive", "abductive", "analogy", "assumption"}
)
UNANSWERED_OBJECTION_STATUSES = frozenset(
    {"unanswered", "open", "unresolved", "pending"}
)
_USES_TARGET_TYPES = frozenset({"Claim", "Evidence", "Reference", "ReviewFinding"})
_ID_FIELD_TYPES = {
    "claim_ids": frozenset({"Claim"}),
    "evidence_ids": _USES_TARGET_TYPES,
    "reference_ids": frozenset({"Reference"}),
    "finding_ids": frozenset({"ReviewFinding"}),
}
_TEXT_FIELDS = ("raw_excerpt", "proof_text", "text", "wording")
_ROLE_INSTRUCTIONS = {
    "steelman": (
        "Construct the strongest good-faith case for the claim. State premises "
        "and their inference kinds, expose every assumption, answer the strongest "
        "objections, and give falsifiers that would defeat the case."
    ),
    "red_team": (
        "Construct the strongest good-faith case against the claim. Attack its "
        "premises, identify contrary evidence and alternative explanations, answer "
        "the likely replies, and give falsifiers that would defeat the objection."
    ),
    "synthesis": (
        "Reconcile the strongest case for and against the claim. Separate what the "
        "evidence establishes from what remains assumed, preserve unresolved "
        "dissent, and state what evidence would change the balance."
    ),
}


class ArgumentError(ResearchError):
    """An argument brief or record is mechanically invalid."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _state_of(state: Any) -> Mapping[str, Any]:
    if hasattr(state, "state") and callable(state.state):
        state = state.state()
    if not isinstance(state, Mapping):
        raise ArgumentError("argument operations need a graph state mapping")
    return state


def _mode(value: Any) -> str:
    if value not in ARGUMENT_MODES:
        raise ArgumentError(
            "argument mode must be steelman, red_team, or synthesis"
        )
    return str(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _has_items(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(_as_list(value))


def _require_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ArgumentError(f"{label} must be a list")
    return list(value)


def _node_wording(node: Mapping[str, Any]) -> str:
    for field in ("wording", "text", "title", "name", "summary", "question", "thesis"):
        wording = _text(node.get(field))
        if wording:
            return wording
    return ""


def _locator(node: Mapping[str, Any]) -> dict[str, Any]:
    for field in ("source_locator", "locator"):
        value = node.get(field)
        if not isinstance(value, Mapping):
            continue
        cleaned = {
            str(key): item
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
        if cleaned:
            return cleaned
    return {}


def _reference_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    single = _text(node.get("citation_key"))
    if single:
        keys.append(single)
    raw = node.get("citation_keys")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = []
    for item in values:
        key = _text(item)
        if key:
            keys.append(key)
    return list(dict.fromkeys(keys))


def _citation_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("citations", "citation_keys"):
        raw = node.get(field)
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            values = []
        for item in values:
            key = _text(item).lstrip("@").strip()
            if key:
                keys.append(key)
    return list(dict.fromkeys(keys))


def _reference_index(nodes: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        if node.get("type") != "Reference":
            continue
        for key in _reference_keys(node):
            index.setdefault(key.casefold(), node)
    return index


def _node_summary(node: Mapping[str, Any]) -> dict[str, Any]:
    locator = _locator(node)
    return {
        "node_id": str(node.get("id") or ""),
        "node_type": str(node.get("type") or ""),
        "wording": _node_wording(node),
        "citation_keys": _citation_keys(node),
        "source_locator": locator or None,
    }


def _edge_summary(
    edge: Mapping[str, Any], peer: Mapping[str, Any], *, direction: str
) -> dict[str, Any]:
    summary = _node_summary(peer)
    summary.update({
        "edge_id": str(edge.get("id") or ""),
        "relation": str(edge.get("type") or ""),
        "direction": direction,
    })
    return summary


def _jsonable(value: Any, *, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ArgumentError(f"{label} must be JSON-serializable: {exc}") from exc


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prompt(mode: str, claim: Mapping[str, Any], gaps: list[dict[str, Any]]) -> str:
    codes = ", ".join(sorted({str(gap["code"]) for gap in gaps})) or "none"
    return (
        f"Role: {mode} argument builder. {_ROLE_INSTRUCTIONS[mode]}\n\n"
        f"Claim {claim.get('id')}: {_node_wording(claim) or '(no wording)'}\n"
        f"Mechanical gaps to address: {codes}.\n\n"
        "Return a candidate argument in the record_argument schema: thesis; "
        "premises with wording, an inference kind, and at least one claim id, "
        "evidence id, or citation key unless inference is 'assumption'; "
        "assumptions; objections with a response or an explicit unanswered "
        "status; falsifiers; confidence between 0 and 1; and a reviewer. "
        "Do not assert truth; every argument is a candidate."
    )


def _contexts_from_claim(claim: Mapping[str, Any]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for field in _TEXT_FIELDS:
        text = claim.get(field)
        if not isinstance(text, str) or not text.strip():
            continue
        for context in refs.citation_contexts(text):
            item = dict(context)
            item["source_field"] = field
            item["text_available"] = True
            item["truth_assessed"] = False
            signature = (
                tuple(item.get("citation_keys") or []),
                item.get("start_line"),
                item.get("quote"),
                field,
            )
            if signature in seen:
                continue
            seen.add(signature)
            contexts.append(item)
    return contexts


def _reference_payload(reference: Mapping[str, Any]) -> dict[str, Any]:
    keys = _reference_keys(reference)
    retraction = reference.get("retraction")
    if not isinstance(retraction, Mapping):
        retraction = {}
    identifiers = reference.get("identifiers")
    if not isinstance(identifiers, Mapping):
        identifiers = {}
    year = reference.get("year")
    year_text = (
        str(year)
        if isinstance(year, (int, float)) and not isinstance(year, bool)
        else _text(year)
    )
    return {
        "reference_id": str(reference.get("id") or ""),
        "citation_key": _text(reference.get("citation_key")) or (keys[0] if keys else ""),
        "citation_keys": keys,
        "title": _text(reference.get("title")),
        "year": year_text,
        "identifiers": dict(identifiers),
        "retraction_status": _text(retraction.get("status")),
        "resolved": True,
        "matched_keys": [],
        "mentioned_by": [],
    }


# --------------------------------------------------------------------------- #
# brief
# --------------------------------------------------------------------------- #
def build_argument_brief(
    state: Any, *, claim_id: str, mode: str = "steelman"
) -> dict[str, Any]:
    """Return a deterministic argument-building packet; no model is called.

    The brief gathers direct graph relations, evidence summaries, reference
    keys and records, citation contexts where claim text is available, stored
    citation keys otherwise, and mechanical gaps. The prompt is a fixed role
    instruction over that packet.
    """
    mode = _mode(mode)
    state = _state_of(state)
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ArgumentError("claim_id must be a non-empty node id")
    claim_id = claim_id.strip()
    nodes = [node for node in state.get("nodes", []) if isinstance(node, Mapping)]
    by_id = {str(node.get("id")): node for node in nodes}
    claim = by_id.get(claim_id)
    if claim is None or claim.get("type") != "Claim":
        raise ArgumentError(f"unknown Claim id: {claim_id}")

    edges = [
        edge for edge in state.get("edges", []) if isinstance(edge, Mapping)
    ]
    supports: list[dict[str, Any]] = []
    opposes: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    cited_references: dict[str, Mapping[str, Any]] = {}
    peer_nodes: list[Mapping[str, Any]] = []

    def add_evidence(
        peer: Mapping[str, Any],
        relation: str,
        *,
        edge: Mapping[str, Any] | None = None,
        direction: str | None = None,
    ) -> None:
        node_id = str(peer.get("id") or "")
        summary = evidence.get(node_id)
        if summary is None:
            summary = _node_summary(peer)
            summary["relations"] = []
            summary["edges"] = []
            evidence[node_id] = summary
        if relation not in summary["relations"]:
            summary["relations"].append(relation)
        if edge is not None:
            edge_id = str(edge.get("id") or "")
            if not any(item["edge_id"] == edge_id for item in summary["edges"]):
                summary["edges"].append({
                    "edge_id": edge_id,
                    "relation": relation,
                    "direction": direction,
                })

    for edge in sorted(edges, key=lambda item: str(item.get("id") or "")):
        src, tgt = edge.get("src"), edge.get("tgt")
        if claim_id not in (src, tgt):
            continue
        peer_id = tgt if src == claim_id else src
        peer = by_id.get(str(peer_id))
        if peer is None:
            continue
        direction = "outgoing" if src == claim_id else "incoming"
        relation = str(edge.get("type") or "")
        if relation in {"supports", "opposes"}:
            summary = _edge_summary(edge, peer, direction=direction)
            if relation == "supports":
                supports.append(summary)
            else:
                opposes.append(summary)
            add_evidence(
                peer, relation, edge=edge, direction=direction)
            peer_nodes.append(peer)
        elif relation == "assumes":
            summary = _edge_summary(edge, peer, direction=direction)
            assumptions.append(summary)
            peer_nodes.append(peer)
        elif relation == "derived_from":
            add_evidence(
                peer, relation, edge=edge, direction=direction)
            peer_nodes.append(peer)
            if peer.get("type") == "Reference":
                cited_references[str(peer.get("id"))] = peer
        elif relation == "cites":
            if peer.get("type") == "Reference":
                cited_references[str(peer.get("id"))] = peer
            else:
                add_evidence(
                    peer, relation, edge=edge, direction=direction)
            peer_nodes.append(peer)

    for values in (supports, opposes, assumptions):
        values.sort(key=lambda item: (item["node_id"], item["edge_id"]))
    for summary in evidence.values():
        summary["relations"] = sorted(summary["relations"])
        summary["edges"] = sorted(
            summary["edges"], key=lambda item: item["edge_id"])
    evidence_items = sorted(evidence.values(), key=lambda item: item["node_id"])

    citation_contexts = _contexts_from_claim(claim)
    keys_by_fold: dict[str, str] = {}
    mention_map: dict[str, set[str]] = {}
    for node in [claim, *peer_nodes]:
        node_id = str(node.get("id") or "")
        for key in _citation_keys(node):
            folded = key.casefold()
            keys_by_fold.setdefault(folded, key)
            if node.get("type") != "Reference":
                mention_map.setdefault(folded, set()).add(node_id)
    for context in citation_contexts:
        for key in _as_list(context.get("citation_keys")):
            item = _text(key)
            if item:
                folded = item.casefold()
                keys_by_fold.setdefault(folded, item)
                mention_map.setdefault(folded, set()).add(claim_id)

    reference_index = _reference_index(nodes)
    reference_payloads: dict[str, dict[str, Any]] = {}
    unresolved_payloads: dict[str, dict[str, Any]] = {}
    for folded, key in sorted(keys_by_fold.items()):
        reference = reference_index.get(folded)
        if reference is None:
            unresolved_payloads[folded] = {
                "reference_id": None,
                "citation_key": key,
                "citation_keys": [key],
                "title": "",
                "year": "",
                "identifiers": {},
                "retraction_status": "",
                "resolved": False,
                "matched_keys": [key],
                "mentioned_by": sorted(mention_map.get(folded, set())),
            }
            continue
        reference_id = str(reference.get("id"))
        payload = reference_payloads.get(reference_id)
        if payload is None:
            payload = _reference_payload(reference)
            reference_payloads[reference_id] = payload
        if key not in payload["matched_keys"]:
            payload["matched_keys"].append(key)
        payload["mentioned_by"] = sorted(
            set(payload["mentioned_by"]) | mention_map.get(folded, set())
        )
    for reference_id, reference in cited_references.items():
        payload = reference_payloads.get(reference_id)
        if payload is None:
            payload = _reference_payload(reference)
            reference_payloads[reference_id] = payload
        payload["mentioned_by"] = sorted(
            set(payload["mentioned_by"]) | {claim_id}
        )

    references = list(reference_payloads.values()) + list(unresolved_payloads.values())
    for payload in references:
        payload["matched_keys"] = sorted(
            set(payload["matched_keys"]), key=lambda item: (item.casefold(), item)
        )
        payload["mentioned_by"] = sorted(
            set(payload["mentioned_by"]), key=lambda item: (item.casefold(), item)
        )
    references.sort(key=lambda item: (
        not item["resolved"],
        item["citation_key"].casefold(), item["citation_key"],
        item["reference_id"] or "",
    ))

    covered = {
        _text(key).casefold()
        for context in citation_contexts
        for key in _as_list(context.get("citation_keys"))
        if _text(key)
    }
    for folded, key in sorted(keys_by_fold.items()):
        if folded in covered:
            continue
        citation_contexts.append({
            "citation_keys": [key],
            "quote": "",
            "start_line": None,
            "end_line": None,
            "syntax": "stored_key",
            "relation_candidate": "unclassified",
            "text_available": False,
            "source_field": None,
            "truth_assessed": False,
        })
    for context in citation_contexts:
        context_keys = [key for key in _as_list(context.get("citation_keys")) if _text(key)]
        context["reference_ids"] = sorted({
            str(reference_index[key.casefold()].get("id"))
            for key in context_keys
            if key.casefold() in reference_index
        })
        context["unresolved_keys"] = sorted(
            {
                key for key in context_keys
                if key.casefold() not in reference_index
            },
            key=lambda item: (item.casefold(), item),
        )

    gaps: list[dict[str, Any]] = []
    if not supports:
        gaps.append({
            "code": "no_supporting_evidence",
            "reason": "No supports edge touches the claim.",
        })
    if not opposes:
        gaps.append({
            "code": "no_opposing_evidence",
            "reason": "No opposes edge touches the claim.",
        })
    if not assumptions:
        gaps.append({
            "code": "no_assumptions",
            "reason": "No assumes edge touches the claim.",
        })
    if not _locator(claim):
        gaps.append({
            "code": "no_source_locator",
            "reason": "The claim has no source_locator or locator.",
        })
    if not keys_by_fold:
        gaps.append({
            "code": "no_citation_keys",
            "reason": "The claim and its direct relations carry no citation keys.",
        })
    for folded, payload in sorted(unresolved_payloads.items()):
        gaps.append({
            "code": "unresolved_citation",
            "citation_key": payload["citation_key"],
            "reason": (
                f"Citation key {payload['citation_key']!r} has no Reference record."
            ),
        })
    gaps.sort(key=lambda item: (
        str(item["code"]),
        str(item.get("citation_key", "")).casefold(),
        str(item.get("citation_key", "")),
    ))

    prompt = _prompt(mode, claim, gaps)
    packet = _jsonable({
        "mode": mode,
        "claim": dict(claim),
        "supports": supports,
        "opposes": opposes,
        "assumptions": assumptions,
        "evidence": evidence_items,
        "references": references,
        "citation_contexts": citation_contexts,
        "gaps": gaps,
        "prompt": prompt,
    }, label="argument brief packet")
    return {
        **packet,
        "packet_sha256": _digest(packet),
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
def _id_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ArgumentError(f"{label} must be a list of node ids")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ArgumentError(f"{label} must contain non-empty node ids")
        result.append(item.strip())
    return list(dict.fromkeys(result))


def _key_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ArgumentError(f"{label} must be a list of citation keys")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ArgumentError(f"{label} must contain non-empty citation keys")
        result.append(item.strip().lstrip("@").strip())
    return list(dict.fromkeys(result))


def _validate_ids(
    record: dict[str, Any],
    *,
    label: str,
    by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    for field, allowed in _ID_FIELD_TYPES.items():
        if field not in record:
            continue
        ids = _id_list(record[field], f"{label}.{field}")
        for node_id in ids:
            node = by_id.get(node_id)
            if node is None:
                raise ArgumentError(
                    f"{label}.{field} refers to unknown node id: {node_id}"
                )
            if node.get("type") not in allowed:
                raise ArgumentError(
                    f"{label}.{field} refers to {node.get('type')}, expected "
                    f"one of {sorted(allowed)}: {node_id}"
                )
        record[field] = ids


def _validate_citation_keys(
    record: dict[str, Any],
    *,
    label: str,
    reference_index: Mapping[str, Mapping[str, Any]],
) -> None:
    if "citation_keys" not in record:
        return
    keys = _key_list(record["citation_keys"], f"{label}.citation_keys")
    for key in keys:
        if key.casefold() not in reference_index:
            raise ArgumentError(
                f"{label}.citation_keys has unresolved citation key: {key}"
            )
    record["citation_keys"] = keys


def _normalize_premises(
    value: Any,
    *,
    by_id: Mapping[str, Mapping[str, Any]],
    reference_index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items = _require_list(value, "premises")
    if not items:
        raise ArgumentError("premises must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(items, start=1):
        label = f"premise {index}"
        if not isinstance(raw, Mapping):
            raise ArgumentError(f"{label} must be an object")
        premise = dict(raw)
        wording = _text(premise.get("wording"))
        if not wording:
            raise ArgumentError(f"{label} needs non-empty wording")
        premise["wording"] = wording
        inference = premise.get("inference")
        if inference not in INFERENCE_TYPES:
            raise ArgumentError(
                f"{label}.inference must be one of {sorted(INFERENCE_TYPES)}"
            )
        _validate_ids(premise, label=label, by_id=by_id)
        _validate_citation_keys(
            premise, label=label, reference_index=reference_index
        )
        if inference != "assumption" and not any(
            premise.get(field)
            for field in ("claim_ids", "evidence_ids", "reference_ids",
                          "finding_ids", "citation_keys")
        ):
            raise ArgumentError(
                f"{label} needs a claim id, evidence id, reference id, finding "
                "id, or citation key unless inference is 'assumption'"
            )
        if "strength" in premise:
            strength = premise["strength"]
            if (
                isinstance(strength, bool)
                or not isinstance(strength, (int, float))
                or not 0 <= strength <= 1
            ):
                raise ArgumentError(f"{label}.strength must be between 0 and 1")
        normalized.append(_jsonable(premise, label=label))
    return normalized


def _normalize_assumptions(
    value: Any,
    *,
    by_id: Mapping[str, Mapping[str, Any]],
    reference_index: Mapping[str, Mapping[str, Any]],
) -> list[Any]:
    items = _require_list(value, "assumptions")
    normalized: list[Any] = []
    for index, raw in enumerate(items, start=1):
        label = f"assumption {index}"
        if isinstance(raw, str):
            wording = _text(raw)
            if not wording:
                raise ArgumentError(f"{label} must be non-empty")
            normalized.append(wording)
            continue
        if not isinstance(raw, Mapping):
            raise ArgumentError(f"{label} must be a string or object")
        assumption = dict(raw)
        wording = _text(assumption.get("wording")) or _text(assumption.get("text"))
        if not wording:
            raise ArgumentError(f"{label} needs non-empty wording")
        assumption["wording"] = wording
        _validate_ids(assumption, label=label, by_id=by_id)
        _validate_citation_keys(
            assumption, label=label, reference_index=reference_index
        )
        if "status" in assumption and assumption["status"] is not None:
            if not isinstance(assumption["status"], str):
                raise ArgumentError(f"{label}.status must be a string")
            assumption["status"] = _text(assumption["status"])
        normalized.append(_jsonable(assumption, label=label))
    return normalized


def _normalize_objections(
    value: Any,
    *,
    by_id: Mapping[str, Mapping[str, Any]],
    reference_index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items = _require_list(value, "objections")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(items, start=1):
        label = f"objection {index}"
        if not isinstance(raw, Mapping):
            raise ArgumentError(f"{label} must be an object")
        objection = dict(raw)
        wording = _text(objection.get("wording"))
        if not wording:
            raise ArgumentError(f"{label} needs non-empty wording")
        objection["wording"] = wording
        _validate_ids(objection, label=label, by_id=by_id)
        _validate_citation_keys(
            objection, label=label, reference_index=reference_index
        )
        if "response" in objection and objection["response"] is not None:
            if not isinstance(objection["response"], str):
                raise ArgumentError(f"{label}.response must be a string")
            objection["response"] = _text(objection["response"])
        if "status" in objection and objection["status"] is not None:
            if not isinstance(objection["status"], str):
                raise ArgumentError(f"{label}.status must be a string")
            objection["status"] = _text(objection["status"])
        normalized.append(_jsonable(objection, label=label))
    return normalized


def _normalize_falsifiers(value: Any) -> list[Any]:
    items = _require_list(value, "falsifiers")
    normalized: list[Any] = []
    for index, raw in enumerate(items, start=1):
        label = f"falsifier {index}"
        if isinstance(raw, str):
            wording = _text(raw)
            if not wording:
                raise ArgumentError(f"{label} must be non-empty")
            normalized.append(wording)
            continue
        if not isinstance(raw, Mapping):
            raise ArgumentError(f"{label} must be a string or object")
        wording = (
            _text(raw.get("wording")) or _text(raw.get("description"))
            or _text(raw.get("text"))
        )
        if not wording:
            raise ArgumentError(f"{label} needs wording, description, or text")
        normalized.append(_jsonable(dict(raw), label=label))
    return normalized


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArgumentError("confidence must be a number between 0 and 1")
    number = float(value)
    if not 0 <= number <= 1:
        raise ArgumentError("confidence must be between 0 and 1")
    return number


def record_argument(
    graph: Any,
    *,
    claim_id: str,
    mode: str,
    thesis: Any,
    premises: Any,
    assumptions: Any = None,
    objections: Any = None,
    falsifiers: Any = None,
    confidence: Any = None,
    reviewer: Any = None,
    model_runs: Any = None,
    notes: Any = None,
) -> dict[str, Any]:
    """Record a validated candidate argument and its ``argues_for``/``uses`` links.

    Invalid records raise :class:`ArgumentError` before any graph write. The
    record itself is a candidate: ``truth_assessed`` stays false and no
    ``supports`` or ``opposes`` edge is created.
    """
    if not hasattr(graph, "record") or not hasattr(graph, "link"):
        raise ArgumentError("record_argument needs a ResearchGraph")
    mode = _mode(mode)
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise ArgumentError("claim_id must be a non-empty node id")
    claim_id = claim_id.strip()
    state = _state_of(graph)
    nodes = [node for node in state.get("nodes", []) if isinstance(node, Mapping)]
    by_id = {str(node.get("id")): node for node in nodes}
    claim = by_id.get(claim_id)
    if claim is None or claim.get("type") != "Claim":
        raise ArgumentError(f"unknown Claim id: {claim_id}")
    thesis_text = _text(thesis)
    if not thesis_text:
        raise ArgumentError("thesis must be non-empty")
    reviewer_text = _text(reviewer)
    if not reviewer_text:
        raise ArgumentError("reviewer must be non-empty")
    confidence_value = _confidence(confidence)
    reference_index = _reference_index(nodes)
    normalized_premises = _normalize_premises(
        premises, by_id=by_id, reference_index=reference_index
    )
    normalized_assumptions = _normalize_assumptions(
        assumptions, by_id=by_id, reference_index=reference_index
    )
    normalized_objections = _normalize_objections(
        objections, by_id=by_id, reference_index=reference_index
    )
    normalized_falsifiers = _normalize_falsifiers(falsifiers)
    if model_runs is None:
        normalized_runs: Any = []
    else:
        normalized_runs = _jsonable(_require_list(model_runs, "model_runs"),
                                    label="model_runs")
    normalized_notes = _jsonable(notes, label="notes")

    mentioned: dict[str, Mapping[str, Any]] = {}
    for record in (*normalized_premises, *normalized_assumptions,
                   *normalized_objections):
        if not isinstance(record, Mapping):
            continue
        for field in _ID_FIELD_TYPES:
            for node_id in _as_list(record.get(field)):
                node = by_id.get(str(node_id))
                if node is not None and node.get("type") in _USES_TARGET_TYPES:
                    mentioned[str(node_id)] = node
        for key in _as_list(record.get("citation_keys")):
            reference = reference_index.get(_text(key).casefold())
            if reference is not None:
                mentioned[str(reference.get("id"))] = reference

    argument = graph.record(
        "Argument",
        claim_id=claim_id,
        mode=mode,
        thesis=thesis_text,
        premises=normalized_premises,
        assumptions=normalized_assumptions,
        objections=normalized_objections,
        falsifiers=normalized_falsifiers,
        confidence=confidence_value,
        reviewer=reviewer_text,
        model_runs=normalized_runs,
        notes=normalized_notes,
        truth_assessed=False,
        status="candidate",
    )
    provenance = {"kind": "argument_record", "mode": mode, "reviewer": reviewer_text}
    links = [graph.link(
        argument["id"], "argues_for", claim_id,
        provenance=provenance,
        rationale=(
            "The argument is offered for this claim; it is a candidate and not "
            "support evidence."
        ),
    )]
    for node_id in sorted(mentioned, key=lambda item: (item.casefold(), item)):
        links.append(graph.link(
            argument["id"], "uses", node_id,
            provenance=provenance,
            rationale=(
                "The argument mentions this record; the link does not create "
                "support or opposition."
            ),
        ))
    return {"argument": argument, "links": links, "truth_assessed": False}


# --------------------------------------------------------------------------- #
# checks and report
# --------------------------------------------------------------------------- #
def _check_ids(
    record: Mapping[str, Any],
    *,
    path: str,
    by_id: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    for field, allowed in _ID_FIELD_TYPES.items():
        if field not in record or record[field] is None:
            continue
        value = record[field]
        if not isinstance(value, (list, tuple)):
            issues.append({
                "code": "id_field_invalid",
                "path": f"{path}.{field}",
                "reason": f"{path}.{field} must be a list of node ids.",
            })
            continue
        for item in value:
            if not isinstance(item, str) or not item.strip():
                issues.append({
                    "code": "id_invalid",
                    "path": f"{path}.{field}",
                    "reason": f"{path}.{field} contains an invalid node id.",
                })
                continue
            node_id = item.strip()
            node = by_id.get(node_id)
            if node is None:
                issues.append({
                    "code": "unknown_id",
                    "path": f"{path}.{field}",
                    "node_id": node_id,
                    "reason": f"{path}.{field} refers to unknown node id {node_id}.",
                })
                continue
            if node.get("type") not in allowed:
                issues.append({
                    "code": "id_type_mismatch",
                    "path": f"{path}.{field}",
                    "node_id": node_id,
                    "reason": (
                        f"{path}.{field} refers to {node.get('type')}, expected "
                        f"one of {sorted(allowed)}."
                    ),
                })


def _check_citation_keys(
    record: Mapping[str, Any],
    *,
    path: str,
    reference_index: Mapping[str, Mapping[str, Any]],
    issues: list[dict[str, Any]],
    seen_keys: set[str],
) -> None:
    for field in ("citation_keys", "citations"):
        if field not in record or record[field] is None:
            continue
        raw = record[field]
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            issues.append({
                "code": "citation_keys_invalid",
                "path": f"{path}.{field}",
                "reason": f"{path}.{field} must be a string or list.",
            })
            continue
        for item in values:
            if not isinstance(item, str) or not item.strip():
                issues.append({
                    "code": "citation_key_invalid",
                    "path": f"{path}.{field}",
                    "reason": f"{path}.{field} contains an invalid citation key.",
                })
                continue
            key = item.strip().lstrip("@").strip()
            folded = key.casefold()
            seen_keys.add(folded)
            if folded not in reference_index:
                issues.append({
                    "code": "unresolved_citation",
                    "path": f"{path}.{field}",
                    "citation_key": key,
                    "reason": f"Citation key {key!r} has no Reference record.",
                })


def _mentioned_ids(records: list[Any]) -> set[str]:
    result: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for field in _ID_FIELD_TYPES:
            for item in _as_list(record.get(field)):
                if isinstance(item, str) and item.strip():
                    result.add(item.strip())
    return result


def check_argument(state: Any, argument_id: str) -> dict[str, Any]:
    """Return mechanical completeness checks for a recorded Argument.

    ``ok`` means the argument's own obligations are mechanically present. It
    does not mean the thesis is true, the evidence is adequate, or the argument
    is persuasive.
    """
    state = _state_of(state)
    if not isinstance(argument_id, str) or not argument_id.strip():
        raise ArgumentError("argument_id must be a non-empty node id")
    argument_id = argument_id.strip()
    nodes = [node for node in state.get("nodes", []) if isinstance(node, Mapping)]
    by_id = {str(node.get("id")): node for node in nodes}
    argument = by_id.get(argument_id)
    if argument is None or argument.get("type") != "Argument":
        raise ArgumentError(f"unknown Argument id: {argument_id}")
    edges = [
        edge for edge in state.get("edges", []) if isinstance(edge, Mapping)
    ]
    reference_index = _reference_index(nodes)
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    mode = argument.get("mode")
    if mode not in ARGUMENT_MODES:
        issues.append({
            "code": "mode_invalid",
            "reason": f"Argument mode must be one of {sorted(ARGUMENT_MODES)}.",
        })
    if not _text(argument.get("thesis")):
        issues.append({
            "code": "thesis_missing",
            "reason": "Argument thesis is missing or empty.",
        })
    if not _text(argument.get("reviewer")):
        issues.append({
            "code": "reviewer_missing",
            "reason": "Argument reviewer is missing or empty.",
        })
    raw_confidence = argument.get("confidence")
    if (
        isinstance(raw_confidence, bool)
        or not isinstance(raw_confidence, (int, float))
        or not 0 <= raw_confidence <= 1
    ):
        issues.append({
            "code": "confidence_invalid",
            "reason": "Argument confidence must be a number between 0 and 1.",
        })

    raw_premises = argument.get("premises")
    premises = _as_list(raw_premises)
    if not isinstance(raw_premises, list):
        issues.append({
            "code": "premises_invalid",
            "reason": "Argument premises must be a list.",
        })
    elif not premises:
        issues.append({
            "code": "premises_missing",
            "reason": "Argument has no premises.",
        })
    non_assumption = 0
    evidenced_premises = 0
    for index, raw in enumerate(premises, start=1):
        path = f"premises[{index - 1}]"
        if not isinstance(raw, Mapping):
            issues.append({
                "code": "premise_invalid",
                "path": path,
                "reason": f"{path} must be an object.",
            })
            continue
        if not _text(raw.get("wording")):
            issues.append({
                "code": "premise_wording_missing",
                "path": path,
                "reason": f"{path} has no wording.",
            })
        inference = raw.get("inference")
        if inference not in INFERENCE_TYPES:
            issues.append({
                "code": "premise_inference_invalid",
                "path": path,
                "reason": (
                    f"{path}.inference must be one of {sorted(INFERENCE_TYPES)}."
                ),
            })
        elif inference != "assumption":
            non_assumption += 1
            has_support = any(
                _has_items(raw.get(field))
                for field in ("claim_ids", "evidence_ids", "reference_ids",
                              "finding_ids", "citation_keys", "citations")
            )
            if has_support:
                evidenced_premises += 1
            else:
                issues.append({
                    "code": "premise_unevidenced",
                    "path": path,
                    "reason": (
                        f"{path} is not an assumption but names no claim, "
                        "evidence, reference, finding, or citation key."
                    ),
                })
        if "strength" in raw and raw["strength"] is not None:
            strength = raw["strength"]
            if (
                isinstance(strength, bool)
                or not isinstance(strength, (int, float))
                or not 0 <= strength <= 1
            ):
                issues.append({
                    "code": "premise_strength_invalid",
                    "path": path,
                    "reason": f"{path}.strength must be between 0 and 1.",
                })
        _check_ids(raw, path=path, by_id=by_id, issues=issues)
        _check_citation_keys(
            raw, path=path, reference_index=reference_index,
            issues=issues, seen_keys=seen_keys,
        )

    raw_assumptions = argument.get("assumptions")
    assumptions = _as_list(raw_assumptions)
    if not isinstance(raw_assumptions, list):
        issues.append({
            "code": "assumptions_invalid",
            "reason": "Argument assumptions must be a list.",
        })
    for index, raw in enumerate(assumptions):
        path = f"assumptions[{index}]"
        if isinstance(raw, str):
            if not _text(raw):
                issues.append({
                    "code": "assumption_invalid",
                    "path": path,
                    "reason": f"{path} is empty.",
                })
            continue
        if not isinstance(raw, Mapping):
            issues.append({
                "code": "assumption_invalid",
                "path": path,
                "reason": f"{path} must be a string or object.",
            })
            continue
        if not (_text(raw.get("wording")) or _text(raw.get("text"))):
            issues.append({
                "code": "assumption_wording_missing",
                "path": path,
                "reason": f"{path} has no wording.",
            })
        _check_ids(raw, path=path, by_id=by_id, issues=issues)
        _check_citation_keys(
            raw, path=path, reference_index=reference_index,
            issues=issues, seen_keys=seen_keys,
        )

    raw_objections = argument.get("objections")
    objections = _as_list(raw_objections)
    if not isinstance(raw_objections, list):
        issues.append({
            "code": "objections_invalid",
            "reason": "Argument objections must be a list.",
        })
    answered = 0
    explicitly_unanswered = 0
    for index, raw in enumerate(objections):
        path = f"objections[{index}]"
        if not isinstance(raw, Mapping):
            issues.append({
                "code": "objection_invalid",
                "path": path,
                "reason": f"{path} must be an object.",
            })
            continue
        if not _text(raw.get("wording")):
            issues.append({
                "code": "objection_wording_missing",
                "path": path,
                "reason": f"{path} has no wording.",
            })
        response = _text(raw.get("response"))
        status = _text(raw.get("status")).casefold()
        if response:
            answered += 1
        elif status in UNANSWERED_OBJECTION_STATUSES:
            explicitly_unanswered += 1
        else:
            issues.append({
                "code": "objection_unanswered",
                "path": path,
                "reason": (
                    f"{path} has no response and no explicit unanswered status."
                ),
            })
        _check_ids(raw, path=path, by_id=by_id, issues=issues)
        _check_citation_keys(
            raw, path=path, reference_index=reference_index,
            issues=issues, seen_keys=seen_keys,
        )

    raw_falsifiers = argument.get("falsifiers")
    falsifiers = _as_list(raw_falsifiers)
    if not isinstance(raw_falsifiers, list):
        issues.append({
            "code": "falsifiers_invalid",
            "reason": "Argument falsifiers must be a list.",
        })
    elif not falsifiers:
        warnings.append({
            "code": "falsifiers_missing",
            "reason": "Argument has no falsifiers.",
        })
    for index, raw in enumerate(falsifiers):
        path = f"falsifiers[{index}]"
        if isinstance(raw, str):
            if not _text(raw):
                issues.append({
                    "code": "falsifier_invalid",
                    "path": path,
                    "reason": f"{path} is empty.",
                })
        elif isinstance(raw, Mapping):
            wording = (
                _text(raw.get("wording")) or _text(raw.get("description"))
                or _text(raw.get("text"))
            )
            if not wording:
                issues.append({
                    "code": "falsifier_invalid",
                    "path": path,
                    "reason": f"{path} needs wording, description, or text.",
                })
        else:
            issues.append({
                "code": "falsifier_invalid",
                "path": path,
                "reason": f"{path} must be a string or object.",
            })

    if argument.get("truth_assessed") is not False:
        warnings.append({
            "code": "truth_assessed_not_false",
            "reason": "Argument does not explicitly record truth_assessed=false.",
        })

    support_edges = [
        edge for edge in edges
        if edge.get("src") == argument_id
        and edge.get("type") in {"supports", "opposes"}
    ]
    for edge in support_edges:
        relation = str(edge.get("type"))
        issues.append({
            "code": (
                "argument_support_edge" if relation == "supports"
                else "argument_opposes_edge"
            ),
            "edge_id": str(edge.get("id") or ""),
            "relation": relation,
            "reason": (
                f"An Argument must not originate a {relation} edge; record the "
                "argument's own premises and objections instead."
            ),
        })

    argues_for = [
        edge for edge in edges
        if edge.get("src") == argument_id and edge.get("type") == "argues_for"
    ]
    if not argues_for:
        warnings.append({
            "code": "argues_for_missing",
            "reason": "Argument has no argues_for edge.",
        })
    claim_id = argument.get("claim_id")
    if claim_id is not None:
        if not isinstance(claim_id, str) or not claim_id.strip():
            issues.append({
                "code": "claim_id_invalid",
                "reason": "Argument claim_id must be a non-empty node id.",
            })
        else:
            claim = by_id.get(claim_id.strip())
            if claim is None:
                issues.append({
                    "code": "unknown_id",
                    "path": "claim_id",
                    "node_id": claim_id.strip(),
                    "reason": f"Argument claim_id refers to unknown node id {claim_id}.",
                })
            elif claim.get("type") != "Claim":
                issues.append({
                    "code": "claim_id_type_mismatch",
                    "path": "claim_id",
                    "node_id": claim_id.strip(),
                    "reason": "Argument claim_id must refer to a Claim.",
                })

    mentioned = _mentioned_ids([*premises, *assumptions, *objections])
    for folded in seen_keys:
        reference = reference_index.get(folded)
        if reference is not None:
            mentioned.add(str(reference.get("id")))
    counts = {
        "premises": len(premises),
        "non_assumption_premises": non_assumption,
        "evidenced_premises": evidenced_premises,
        "assumptions": len(assumptions),
        "objections": len(objections),
        "answered_objections": answered,
        "explicitly_unanswered_objections": explicitly_unanswered,
        "unanswered_objections": len(objections) - answered,
        "falsifiers": len(falsifiers),
        "citation_keys": len(seen_keys),
        "linked_ids": len(mentioned),
        "supports_or_opposes_edges": len(support_edges),
        "issues": len(issues),
        "warnings": len(warnings),
    }
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": counts,
        "truth_assessed": False,
    }


def argument_report(state: Any, argument_id: str) -> dict[str, Any]:
    """Return an Argument with its linked records and mechanical checks."""
    state = _state_of(state)
    if not isinstance(argument_id, str) or not argument_id.strip():
        raise ArgumentError("argument_id must be a non-empty node id")
    argument_id = argument_id.strip()
    nodes = [node for node in state.get("nodes", []) if isinstance(node, Mapping)]
    by_id = {str(node.get("id")): node for node in nodes}
    argument = by_id.get(argument_id)
    if argument is None or argument.get("type") != "Argument":
        raise ArgumentError(f"unknown Argument id: {argument_id}")
    edges = [
        edge for edge in state.get("edges", []) if isinstance(edge, Mapping)
    ]
    links = sorted(
        (
            edge for edge in edges
            if edge.get("src") == argument_id
            and edge.get("type") in {"argues_for", "uses"}
        ),
        key=lambda edge: (
            edge.get("type") != "argues_for", str(edge.get("id") or "")
        ),
    )
    targets: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    argued_claim_id = None
    for edge in links:
        target_id = str(edge.get("tgt") or "")
        if edge.get("type") == "argues_for" and argued_claim_id is None:
            argued_claim_id = target_id
        if target_id and target_id not in seen:
            seen.add(target_id)
            target = by_id.get(target_id)
            if target is not None:
                targets.append(target)
    claims = sorted(
        (node for node in targets if node.get("type") == "Claim"),
        key=lambda node: str(node.get("id")),
    )
    evidence = sorted(
        (node for node in targets if node.get("type") == "Evidence"),
        key=lambda node: str(node.get("id")),
    )
    references = sorted(
        (node for node in targets if node.get("type") == "Reference"),
        key=lambda node: str(node.get("id")),
    )
    findings = sorted(
        (node for node in targets if node.get("type") == "ReviewFinding"),
        key=lambda node: str(node.get("id")),
    )
    primary_claim_id = argued_claim_id or _text(argument.get("claim_id"))
    claim = by_id.get(primary_claim_id) if primary_claim_id else None
    if claim is not None and claim.get("type") != "Claim":
        claim = None
    return {
        "argument": argument,
        "claim": claim,
        "claims": claims,
        "evidence": evidence,
        "references": references,
        "findings": findings,
        "links": links,
        "checks": check_argument(state, argument_id),
        "truth_assessed": False,
    }
