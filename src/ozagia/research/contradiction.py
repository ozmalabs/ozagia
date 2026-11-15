"""Deterministic contradiction candidates and reviewer-recorded contradictions.

The detector only compares recorded graph fields: explicit ``opposes`` edges,
claims that share a normalized scope but carry incompatible signed-off statuses,
and extraction records with the same outcome/measure/unit whose effect signs
differ or whose confidence intervals do not overlap.  It never calls a model and
never decides which side is true.  A recorded ``Contradiction`` is a candidate
relation that keeps both claims, the supporting records and the reviewer visible.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any

from ozagia.research.graph import ResearchError

CONTRADICTION_KINDS = frozenset(
    {
        "explicit_opposition",
        "status_incompatibility",
        "extraction_disagreement",
        "other",
    }
)
CONTRADICTION_STATUSES = frozenset({"candidate", "confirmed", "dismissed"})
_ENDPOINT_TYPES = frozenset({"Claim", "Evidence", "Observation"})
_STATUS_INCOMPATIBLE = ("supported_within_scope", "contradicted")


class ContradictionError(ResearchError):
    """A contradiction candidate, record or check is mechanically invalid."""


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise ContradictionError("contradiction operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContradictionError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


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
        raise ContradictionError(f"value must be JSON-serializable: {exc}") from exc


def _normalized(value: Any) -> str:
    return " ".join(str(value).split()).casefold() if value is not None else ""


def _normalized_scope(node: Mapping[str, Any]) -> str:
    return _normalized(node.get("scope"))


def _node_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _first_number(node: Mapping[str, Any], fields: Sequence[str]) -> float | None:
    for field in fields:
        number = _number(node.get(field))
        if number is not None:
            return number
    return None


def _normalized_values(node: Mapping[str, Any], fields: Sequence[str]) -> set[str]:
    values: set[str] = set()
    for field in fields:
        raw = node.get(field)
        if isinstance(raw, str):
            value = _normalized(raw)
            if value:
                values.add(value)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            for item in raw:
                if isinstance(item, str):
                    value = _normalized(item)
                    if value:
                        values.add(value)
    return values


def _optional_extraction_module() -> Any:
    """Import the optional extraction layer lazily; absence is not an error."""
    try:
        from ozagia.research import extraction
    except ImportError:
        return None
    return extraction


# --------------------------------------------------------------------------- #
# extraction comparison
# --------------------------------------------------------------------------- #
def _effect_sign(node: Mapping[str, Any]) -> int:
    effect = _first_number(node, ("effect_size", "effect", "estimate", "point_estimate", "value"))
    if effect is not None:
        return 1 if effect > 0 else -1 if effect < 0 else 0
    direction = _normalized(
        node.get("direction") or node.get("effect_direction") or node.get("sign")
    )
    if direction in {"positive", "increase", "increased", "higher", "greater", "up"}:
        return 1
    if direction in {"negative", "decrease", "decreased", "lower", "less", "down"}:
        return -1
    return 0


def _confidence_interval(node: Mapping[str, Any]) -> tuple[float, float] | None:
    for low_field, high_field in (
        ("ci_low", "ci_high"),
        ("ci_lower", "ci_upper"),
        ("confidence_interval_low", "confidence_interval_high"),
        ("lower_ci", "upper_ci"),
    ):
        low, high = _number(node.get(low_field)), _number(node.get(high_field))
        if low is not None and high is not None and low <= high:
            return low, high
    raw = node.get("confidence_interval") or node.get("ci")
    low = high = None
    if isinstance(raw, Mapping):
        for key in ("low", "lower", "ci_low", "min"):
            if (low := _number(raw.get(key))) is not None:
                break
        for key in ("high", "upper", "ci_high", "max"):
            if (high := _number(raw.get(key))) is not None:
                break
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        if len(raw) >= 2:
            low, high = _number(raw[0]), _number(raw[1])
    if low is not None and high is not None and low <= high:
        return low, high
    return None


def _comparable_extractions(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> tuple[set[str], str] | None:
    left_outcomes = _normalized_values(left, ("outcome", "outcomes"))
    right_outcomes = _normalized_values(right, ("outcome", "outcomes"))
    outcomes = left_outcomes & right_outcomes
    measure = _normalized(left.get("effect_measure") or left.get("measure"))
    other_measure = _normalized(right.get("effect_measure") or right.get("measure"))
    unit = _normalized(left.get("unit") or left.get("effect_unit"))
    other_unit = _normalized(right.get("unit") or right.get("effect_unit"))
    if not outcomes or not measure or measure != other_measure:
        return None
    if not unit or unit != other_unit:
        return None
    return outcomes, measure


def _extraction_pair_signal(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> tuple[set[str], list[str]] | None:
    comparable = _comparable_extractions(left, right)
    if comparable is None:
        return None
    outcomes, measure = comparable
    signals: list[str] = []
    left_sign, right_sign = _effect_sign(left), _effect_sign(right)
    if left_sign and right_sign and left_sign != right_sign:
        signals.append("opposite_effect_sign")
    left_ci, right_ci = _confidence_interval(left), _confidence_interval(right)
    if left_ci is not None and right_ci is not None:
        if left_ci[1] < right_ci[0] or right_ci[1] < left_ci[0]:
            signals.append("non_overlapping_ci")
    if not signals:
        return None
    unit = _normalized(left.get("unit") or left.get("effect_unit"))
    return set(signals), [f"outcome={sorted(outcomes)[0]}", f"measure={measure}", f"unit={unit}"]


def _extraction_candidates(
    nodes: list[Mapping[str, Any]],
    by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    extractions = sorted(
        (node for node in nodes if node.get("type") == "Extraction"),
        key=lambda node: str(node.get("id")),
    )
    candidates: list[dict[str, Any]] = []
    examined = 0
    for left, right in combinations(extractions, 2):
        examined += 1
        left_target = left.get("target_id")
        right_target = right.get("target_id")
        if not isinstance(left_target, str) or not isinstance(right_target, str):
            continue
        if not left_target.strip() or not right_target.strip() or left_target == right_target:
            continue
        left_node, right_node = by_id.get(left_target), by_id.get(right_target)
        if left_node is None or right_node is None:
            continue
        if left_node.get("type") not in _ENDPOINT_TYPES:
            continue
        if right_node.get("type") not in _ENDPOINT_TYPES:
            continue
        compared = _extraction_pair_signal(left, right)
        if compared is None:
            continue
        signals, fields = compared
        claim_a, claim_b = _node_pair(left_target, right_target)
        signals_text = "+".join(sorted(signals))
        candidates.append(
            {
                "kind": "extraction_disagreement",
                "claim_a_id": claim_a,
                "claim_b_id": claim_b,
                "evidence_ids": sorted((str(left["id"]), str(right["id"]))),
                "rationale": (
                    f"Extractions {left['id']} and {right['id']} report "
                    f"{', '.join(fields)} with {signals_text.replace('_', ' ')}."
                ),
                "signal": signals_text,
                "confidence": 0.9 if len(signals) > 1 else 0.8,
            }
        )
    return candidates, examined


# --------------------------------------------------------------------------- #
# candidate detection
# --------------------------------------------------------------------------- #
def find_contradictions(state: Any, *, max_pairs: int = 100) -> dict[str, Any]:
    """Return deterministic contradiction candidates from recorded fields."""
    if (
        isinstance(max_pairs, bool)
        or not isinstance(max_pairs, int)
        or max_pairs < 0
    ):
        raise ContradictionError("max_pairs must be a non-negative integer")
    current = _as_state(state)
    nodes = _nodes(current)
    edges = _edges(current)
    by_id = {str(node["id"]): node for node in nodes}
    candidates: list[dict[str, Any]] = []
    truncated = False
    examined = 0

    def add(candidate: dict[str, Any]) -> bool:
        nonlocal truncated
        if len(candidates) >= max_pairs:
            truncated = True
            return False
        candidates.append(candidate)
        return True

    # 1. Explicit opposes edges between evidence-bearing records.
    opposed: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        if edge.get("type") != "opposes":
            continue
        source = by_id.get(str(edge.get("src")))
        target = by_id.get(str(edge.get("tgt")))
        if source is None or target is None:
            continue
        if source.get("type") not in _ENDPOINT_TYPES:
            continue
        if target.get("type") not in _ENDPOINT_TYPES:
            continue
        pair = _node_pair(str(source["id"]), str(target["id"]))
        if pair not in opposed:
            examined += 1
            opposed[pair] = []
        edge_id = str(edge.get("id") or "")
        if edge_id and edge_id not in opposed[pair]:
            opposed[pair].append(edge_id)
    for pair in sorted(opposed):
        edge_ids = sorted(opposed[pair])
        if not add(
            {
                "kind": "explicit_opposition",
                "claim_a_id": pair[0],
                "claim_b_id": pair[1],
                "evidence_ids": [],
                "rationale": (
                    f"Explicit opposes edge(s) {', '.join(edge_ids) or '(unnamed)'} "
                    f"connect {pair[0]} and {pair[1]}."
                ),
                "signal": "explicit_opposes",
                "confidence": 1.0,
            }
        ):
            break

    # 2. Claims with the same normalized scope and incompatible statuses.
    by_scope: dict[str, list[Mapping[str, Any]]] = {}
    for node in sorted(nodes, key=lambda item: str(item.get("id"))):
        if node.get("type") != "Claim":
            continue
        status = node.get("status")
        if status not in _STATUS_INCOMPATIBLE:
            continue
        scope = _normalized_scope(node)
        if not scope:
            continue
        by_scope.setdefault(scope, []).append(node)
    stop = len(candidates) >= max_pairs
    for scope in sorted(by_scope):
        if stop:
            break
        supported = sorted(
            (node for node in by_scope[scope] if node.get("status") == "supported_within_scope"),
            key=lambda node: str(node["id"]),
        )
        contradicted = sorted(
            (node for node in by_scope[scope] if node.get("status") == "contradicted"),
            key=lambda node: str(node["id"]),
        )
        for left in supported:
            if stop:
                break
            for right in contradicted:
                examined += 1
                pair = _node_pair(str(left["id"]), str(right["id"]))
                if pair[0] == pair[1]:
                    continue
                status_by_id = {
                    pair[0]: str(
                        left["status"] if str(left["id"]) == pair[0] else right["status"]
                    ),
                    pair[1]: str(
                        left["status"] if str(left["id"]) == pair[1] else right["status"]
                    ),
                }
                if not add(
                    {
                        "kind": "status_incompatibility",
                        "claim_a_id": pair[0],
                        "claim_b_id": pair[1],
                        "evidence_ids": [],
                        "rationale": (
                            f"Claims {pair[0]} and {pair[1]} share scope {scope!r} "
                            f"but have statuses {status_by_id[pair[0]]} and "
                            f"{status_by_id[pair[1]]}."
                        ),
                        "signal": "claim_status",
                        "confidence": 0.8,
                    }
                ):
                    stop = True
                    break

    # 3. Comparable extractions with opposite signs or disjoint intervals.
    extraction_available = _optional_extraction_module() is not None
    has_extractions = any(node.get("type") == "Extraction" for node in nodes)
    if not stop and (extraction_available or has_extractions):
        extraction_candidates, extraction_examined = _extraction_candidates(nodes, by_id)
        examined += extraction_examined
        for candidate in extraction_candidates:
            if not add(candidate):
                break

    candidates.sort(
        key=lambda item: (
            str(item.get("kind")),
            str(item.get("claim_a_id")),
            str(item.get("claim_b_id")),
            str(item.get("signal")),
            _canonical(item.get("evidence_ids")),
        )
    )
    kinds: dict[str, int] = {}
    for candidate in candidates:
        kind = str(candidate["kind"])
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "candidates": candidates,
        "count": len(candidates),
        "kinds": dict(sorted(kinds.items())),
        "pairs_examined": examined,
        "truncated": truncated,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def _validate_endpoint(
    state: Mapping[str, Any],
    node_id: Any,
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(node_id, str) or not node_id.strip():
        raise ContradictionError(f"{field} must be a non-empty node id")
    node_id = node_id.strip()
    node = _node_by_id(state, node_id)
    if node is None:
        raise ContradictionError(f"{field} refers to unknown node id: {node_id}")
    if node.get("type") not in _ENDPOINT_TYPES:
        raise ContradictionError(
            f"{field} must refer to one of {sorted(_ENDPOINT_TYPES)}, "
            f"not {node.get('type')}"
        )
    return node


def _evidence_ids(state: Mapping[str, Any], value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContradictionError("evidence_ids must be a list of node ids")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ContradictionError("evidence_ids must contain non-empty node ids")
        item = item.strip()
        node = _node_by_id(state, item)
        if node is None:
            raise ContradictionError(f"evidence_ids refers to unknown node id: {item}")
        if node.get("type") not in _ENDPOINT_TYPES:
            raise ContradictionError(
                f"evidence id {item} must be one of {sorted(_ENDPOINT_TYPES)}, "
                f"not {node.get('type')}"
            )
        if item not in ids:
            ids.append(item)
    return ids


def record_contradiction(
    graph: Any,
    *,
    claim_a_id: str,
    claim_b_id: str,
    kind: str,
    rationale: str,
    evidence_ids: Sequence[str] | None = None,
    reviewer: str,
    status: str = "candidate",
) -> dict[str, Any]:
    """Record one reviewer-authored contradiction candidate and its links."""
    state = graph.state()
    if kind not in CONTRADICTION_KINDS:
        raise ContradictionError(
            f"kind must be one of {sorted(CONTRADICTION_KINDS)}"
        )
    if status not in CONTRADICTION_STATUSES:
        raise ContradictionError(
            f"status must be one of {sorted(CONTRADICTION_STATUSES)}"
        )
    reviewer = _nonempty(reviewer, "reviewer")
    rationale = _nonempty(rationale, "rationale")
    claim_a = _validate_endpoint(state, claim_a_id, "claim_a_id")
    claim_b = _validate_endpoint(state, claim_b_id, "claim_b_id")
    if claim_a["id"] == claim_b["id"]:
        raise ContradictionError("claim_a_id and claim_b_id must differ")
    evidence = _evidence_ids(state, evidence_ids)

    record = graph.record(
        "Contradiction",
        claim_a_id=claim_a["id"],
        claim_b_id=claim_b["id"],
        kind=kind,
        rationale=rationale,
        evidence_ids=evidence,
        reviewer=reviewer,
        status=status,
        truth_assessed=False,
    )
    links = []
    linked: list[str] = []
    for node in (claim_a, claim_b, *(_node_by_id(state, item) for item in evidence)):
        if node is None or str(node["id"]) in linked:
            continue
        linked.append(str(node["id"]))
        links.append(
            graph.link(
                record["id"],
                "contradicts",
                node["id"],
                provenance={
                    "kind": "contradiction",
                    "reviewer": reviewer,
                    "status": status,
                },
                rationale=rationale,
            )
        )
    return {
        "contradiction": record,
        "claims": [dict(claim_a), dict(claim_b)],
        "evidence": [dict(_node_by_id(state, item)) for item in evidence],
        "links": links,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# checks and report
# --------------------------------------------------------------------------- #
def check_contradiction(state: Any, contradiction_id: str) -> dict[str, Any]:
    """Check the mechanical integrity of one recorded Contradiction."""
    current = _as_state(state)
    if not isinstance(contradiction_id, str) or not contradiction_id.strip():
        raise ContradictionError("contradiction_id must be a non-empty node id")
    contradiction_id = contradiction_id.strip()
    by_id = {str(node["id"]): node for node in _nodes(current)}
    contradiction = by_id.get(contradiction_id)
    if contradiction is None or contradiction.get("type") != "Contradiction":
        raise ContradictionError(f"unknown Contradiction id: {contradiction_id}")

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if contradiction.get("kind") not in CONTRADICTION_KINDS:
        issues.append(
            {
                "code": "kind_invalid",
                "reason": f"Contradiction kind must be one of {sorted(CONTRADICTION_KINDS)}.",
            }
        )
    if contradiction.get("status") not in CONTRADICTION_STATUSES:
        issues.append(
            {
                "code": "status_invalid",
                "reason": (
                    "Contradiction status must be one of "
                    f"{sorted(CONTRADICTION_STATUSES)}."
                ),
            }
        )
    if not _optional_text(contradiction.get("rationale")):
        issues.append(
            {"code": "rationale_missing", "reason": "Contradiction has no rationale."}
        )
    if not _optional_text(contradiction.get("reviewer")):
        issues.append(
            {"code": "reviewer_missing", "reason": "Contradiction has no reviewer."}
        )

    claim_ids: list[str] = []
    for field in ("claim_a_id", "claim_b_id"):
        value = contradiction.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(
                {
                    "code": "claim_id_invalid",
                    "path": field,
                    "reason": f"Contradiction {field} must be a non-empty node id.",
                }
            )
            continue
        node_id = value.strip()
        node = by_id.get(node_id)
        if node is None:
            issues.append(
                {
                    "code": "unknown_claim",
                    "path": field,
                    "node_id": node_id,
                    "reason": f"Contradiction {field} refers to unknown node id {node_id}.",
                }
            )
            continue
        if node.get("type") not in _ENDPOINT_TYPES:
            issues.append(
                {
                    "code": "claim_type_mismatch",
                    "path": field,
                    "node_id": node_id,
                    "reason": (
                        f"Contradiction {field} must refer to one of "
                        f"{sorted(_ENDPOINT_TYPES)}."
                    ),
                }
            )
        claim_ids.append(node_id)
    if len(claim_ids) == 2 and claim_ids[0] == claim_ids[1]:
        issues.append(
            {
                "code": "claims_not_distinct",
                "reason": "Contradiction claim_a_id and claim_b_id must differ.",
            }
        )

    evidence_ids: list[str] = []
    raw_evidence = contradiction.get("evidence_ids", [])
    if raw_evidence is None:
        raw_evidence = []
    if isinstance(raw_evidence, str) or not isinstance(raw_evidence, Sequence):
        issues.append(
            {
                "code": "evidence_ids_invalid",
                "reason": "Contradiction evidence_ids must be a list of node ids.",
            }
        )
    else:
        for item in raw_evidence:
            if not isinstance(item, str) or not item.strip():
                issues.append(
                    {
                        "code": "evidence_id_invalid",
                        "reason": "Contradiction evidence_ids contains an invalid id.",
                    }
                )
                continue
            node_id = item.strip()
            node = by_id.get(node_id)
            if node is None:
                issues.append(
                    {
                        "code": "unknown_evidence",
                        "node_id": node_id,
                        "reason": f"Contradiction evidence id {node_id} is unknown.",
                    }
                )
                continue
            if node.get("type") not in _ENDPOINT_TYPES:
                issues.append(
                    {
                        "code": "evidence_type_mismatch",
                        "node_id": node_id,
                        "reason": (
                            f"Contradiction evidence id {node_id} must be one of "
                            f"{sorted(_ENDPOINT_TYPES)}."
                        ),
                    }
                )
            evidence_ids.append(node_id)

    contradicts_edges = [
        edge for edge in _edges(current)
        if edge.get("type") == "contradicts" and edge.get("src") == contradiction_id
    ]
    linked = {str(edge.get("tgt")) for edge in contradicts_edges}
    expected = [*claim_ids, *evidence_ids]
    for node_id in dict.fromkeys(expected):
        if node_id not in linked:
            issues.append(
                {
                    "code": "contradicts_edge_missing",
                    "node_id": node_id,
                    "reason": f"Contradiction has no contradicts edge to {node_id}.",
                }
            )
    if contradiction.get("truth_assessed") is not False:
        warnings.append(
            {
                "code": "truth_assessed_not_false",
                "reason": "Contradiction does not explicitly record truth_assessed=false.",
            }
        )

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "nodes": 1 + len(claim_ids) + len(evidence_ids),
            "edges": len(contradicts_edges),
            "claims": len(claim_ids),
            "evidence": len(evidence_ids),
            "contradicts_edges": len(contradicts_edges),
            "issues": len(issues),
            "warnings": len(warnings),
        },
        "truth_assessed": False,
    }


def _count_by(records: Sequence[Mapping[str, Any]], field: str, keys: Sequence[str]) -> dict[str, int]:
    counts = {key: 0 for key in keys}
    for record in records:
        value = record.get(field)
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def contradiction_report(state: Any) -> dict[str, Any]:
    """Summarize current candidates and recorded contradictions."""
    current = _as_state(state)
    scan = find_contradictions(current)
    records = sorted(
        (node for node in _nodes(current) if node.get("type") == "Contradiction"),
        key=lambda node: str(node["id"]),
    )
    resolved_pairs = {
        _node_pair(str(record.get("claim_a_id")), str(record.get("claim_b_id")))
        for record in records
        if record.get("status") in {"confirmed", "dismissed"}
    }
    unresolved: dict[tuple[str, str], set[str]] = {}
    for candidate in scan["candidates"]:
        pair = _node_pair(
            str(candidate.get("claim_a_id")),
            str(candidate.get("claim_b_id")),
        )
        if pair in resolved_pairs:
            continue
        unresolved.setdefault(pair, set()).add(str(candidate.get("kind")))
    unresolved_pairs = [
        {
            "claim_a_id": pair[0],
            "claim_b_id": pair[1],
            "kinds": sorted(kinds),
        }
        for pair, kinds in sorted(unresolved.items())
    ]
    candidate_counts = _count_by(
        scan["candidates"], "kind", sorted(CONTRADICTION_KINDS)
    )
    record_counts = _count_by(records, "kind", sorted(CONTRADICTION_KINDS))
    status_counts = _count_by(records, "status", sorted(CONTRADICTION_STATUSES))
    return {
        "candidates": scan["candidates"],
        "records": [dict(record) for record in records],
        "candidate_count": len(scan["candidates"]),
        "record_count": len(records),
        "candidates_by_kind": candidate_counts,
        "records_by_kind": record_counts,
        "records_by_status": status_counts,
        "unresolved_pairs": unresolved_pairs,
        "unresolved_pair_count": len(unresolved_pairs),
        "pairs_examined": scan["pairs_examined"],
        "truncated": scan["truncated"],
        "counts": {
            "candidates": len(scan["candidates"]),
            "records": len(records),
            "unresolved_pairs": len(unresolved_pairs),
            "pairs_examined": scan["pairs_examined"],
            "candidates_by_kind": candidate_counts,
            "records_by_kind": record_counts,
            "records_by_status": status_counts,
        },
        "truth_assessed": False,
    }
