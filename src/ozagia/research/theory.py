"""Theory competition, compatibility and evidence basis over the research graph.

A theory is a structured candidate: name, statement, scope, mechanism,
assumptions, predictions and status.  This layer records rivalry and
compatibility relations, explicit theory-evidence links, and descriptive
comparisons.  It never chooses a theory, assigns a probability, or decides
which theory is correct.  Every report carries ``truth_assessed=False``.

Shared evidence is computed from explicit ``supported_by``/``challenged_by``
links, never guessed from wording or citations.  "Insufficient to discern" is a
first-class result.  A merely unique evidence item is not discriminating; a
discriminating item must be linked explicitly with ``discriminated_by``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any

from ozagia.research.graph import ResearchError

__all__ = [
    "COMPATIBILITY_DEGREES",
    "EVIDENCE_RELATIONS",
    "THEORY_STATUSES",
    "TheoryError",
    "compatibility_matrix",
    "link_theory_evidence",
    "record_compatibility",
    "record_rivalry",
    "record_theory",
    "theory_compare",
    "theory_report",
    "theory_scan",
]

THEORY_STATUSES = ("proposed", "active", "rival", "superseded")
COMPATIBILITY_DEGREES = (
    "compatible",
    "partially_compatible",
    "incompatible",
    "orthogonal",
)
EVIDENCE_RELATIONS = ("supported_by", "challenged_by", "discriminated_by", "explains")

# These mirror the endpoint rules in graph._EDGE_ENDPOINTS.  Keeping the
# expected types here lets a caller get a clear error before graph.link runs.
_RELATION_TARGET_TYPES = {
    "supported_by": frozenset({"Evidence", "Claim", "Observation", "Extraction"}),
    "challenged_by": frozenset({"Evidence", "Claim", "Observation", "Extraction"}),
    "discriminated_by": frozenset({"Prediction", "Evidence", "Observation"}),
    "explains": frozenset({"Claim", "Evidence", "Observation"}),
}
_DISCRIMINATING_EVIDENCE_TYPES = frozenset({"Evidence", "Observation"})
_CONTRADICTORY_DEGREES = frozenset({"compatible", "partially_compatible"})

_RATING_LABELS = {
    "very low": "very_low",
    "verylow": "very_low",
    "very_low": "very_low",
}
_RISK_LABELS = {
    "some concerns": "some_concerns",
    "someconcerns": "some_concerns",
    "some_concerns": "some_concerns",
    "not applicable": "not_applicable",
    "notapplicable": "not_applicable",
    "not_applicable": "not_applicable",
    "n/a": "not_applicable",
    "na": "not_applicable",
}


class TheoryError(ResearchError):
    """A theory record, relation or comparison is malformed."""


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
    raise TheoryError("theory operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node
        for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TheoryError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _collapse(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _normalized(value: Any) -> str:
    return _collapse(value).casefold()


def _token(value: Any) -> str:
    """Return a stable lower-case token using underscores."""
    if value is None:
        return ""
    text = str(value).strip().casefold()
    for character in ("_", "-", ".", "/"):
        text = text.replace(character, " ")
    return "_".join(text.split())


def _string_items(value: Any, field: str) -> list[str]:
    """Validate an optional sequence of non-empty strings, preserving order."""
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TheoryError(f"{field} must be a list of non-empty strings")
    items: list[str] = []
    for index, item in enumerate(value):
        text = _nonempty_string(item, f"{field}[{index}]")
        if text not in items:
            items.append(text)
    return items


def _json_safe(value: Any, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TheoryError(f"{field} must be JSON-serializable: {exc}") from exc


def _theory_status(value: Any) -> str:
    status = _token(value)
    if status not in THEORY_STATUSES:
        raise TheoryError(f"status must be one of {list(THEORY_STATUSES)}")
    return status


def _compatibility_degree(value: Any) -> str:
    degree = _token(value)
    if degree not in COMPATIBILITY_DEGREES:
        raise TheoryError(f"degree must be one of {list(COMPATIBILITY_DEGREES)}")
    return degree


def _relation(value: Any) -> str:
    relation = _token(value)
    if relation not in EVIDENCE_RELATIONS:
        raise TheoryError(f"relation must be one of {list(EVIDENCE_RELATIONS)}")
    return relation


def _require_node(
    state: Mapping[str, Any],
    node_id: Any,
    label: str,
    expected: frozenset[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(node_id, str) or not node_id.strip():
        raise TheoryError(f"{label} must be a non-empty node id")
    node = _node_by_id(state, node_id.strip())
    if node is None:
        raise TheoryError(f"unknown {label} id: {node_id}")
    if expected is not None and node.get("type") not in expected:
        raise TheoryError(
            f"{label} {node_id} must be one of {sorted(expected)}, "
            f"not {node.get('type')}"
        )
    return node


def _require_theory(state: Mapping[str, Any], node_id: Any, label: str) -> Mapping[str, Any]:
    node = _require_node(state, node_id, label)
    if node.get("type") != "Theory":
        raise TheoryError(f"{label} {node_id} must be a Theory, not {node.get('type')}")
    return node


def _outgoing_ids(state: Mapping[str, Any], node_id: str, relation: str) -> list[str]:
    return sorted(
        {
            str(edge["tgt"])
            for edge in _edges(state)
            if edge.get("type") == relation
            and edge.get("src") == node_id
            and isinstance(edge.get("tgt"), str)
        }
    )


def _pair_edges(
    state: Mapping[str, Any], relation: str, theory_a: str, theory_b: str
) -> list[Mapping[str, Any]]:
    pair = {theory_a, theory_b}
    return sorted(
        (
            edge
            for edge in _edges(state)
            if edge.get("type") == relation
            and {edge.get("src"), edge.get("tgt")} == pair
        ),
        key=lambda edge: str(edge.get("id") or ""),
    )


def _provenance(edge: Mapping[str, Any]) -> Mapping[str, Any]:
    value = edge.get("provenance")
    return value if isinstance(value, Mapping) else {}


def _node_label(node: Mapping[str, Any] | None, node_id: str = "") -> str:
    if node is not None:
        for field in ("wording", "name", "statement", "title", "question", "text", "summary"):
            value = node.get(field)
            if isinstance(value, str) and value.strip():
                return _collapse(value)
    return node_id


# --------------------------------------------------------------------------- #
# recording theories
# --------------------------------------------------------------------------- #
def record_theory(
    graph: Any,
    *,
    name: str,
    statement: str,
    scope: str,
    mechanism: str | None = None,
    assumptions: Sequence[str] | None = None,
    predictions: Sequence[str] | None = None,
    assumption_ids: Sequence[str] | None = None,
    prediction_ids: Sequence[str] | None = None,
    reviewer: str | None = None,
    status: str = "proposed",
    notes: str | None = None,
) -> dict[str, Any]:
    """Record a theory and link its assumptions and predictions.

    ``assumptions`` and ``predictions`` contain wording for new nodes.
    ``assumption_ids`` and ``prediction_ids`` refer to nodes already in the
    graph.  Both forms may be used together.  The theory is a candidate, not a
    truth claim.
    """
    state = _as_state(graph)
    name = _nonempty_string(name, "name")
    statement = _nonempty_string(statement, "statement")
    scope = _nonempty_string(scope, "scope")
    mechanism = _optional_string(mechanism, "mechanism")
    reviewer = _optional_string(reviewer, "reviewer")
    notes = _optional_string(notes, "notes")
    status = _theory_status(status)

    assumption_texts = _string_items(assumptions, "assumptions")
    prediction_texts = _string_items(predictions, "predictions")
    existing_assumption_ids = _string_items(assumption_ids, "assumption_ids")
    existing_prediction_ids = _string_items(prediction_ids, "prediction_ids")

    # Validate all referenced nodes before recording anything.
    for assumption_id in existing_assumption_ids:
        _require_node(state, assumption_id, "assumption", frozenset({"Assumption"}))
    for prediction_id in existing_prediction_ids:
        _require_node(state, prediction_id, "prediction", frozenset({"Prediction"}))

    fields: dict[str, Any] = {
        "name": name,
        "statement": statement,
        "scope": scope,
        "status": status,
        "truth_assessed": False,
    }
    if mechanism is not None:
        fields["mechanism"] = mechanism
    if reviewer is not None:
        fields["reviewer"] = reviewer
    if notes is not None:
        fields["notes"] = notes
    _json_safe(fields, "theory")

    theory = graph.record("Theory", **fields)
    assumption_nodes: list[dict[str, Any]] = []
    prediction_nodes: list[dict[str, Any]] = []

    for wording in assumption_texts:
        assumption = graph.record(
            "Assumption",
            wording=wording,
            theory_id=theory["id"],
            status="assumed",
            truth_assessed=False,
        )
        graph.link(
            theory["id"],
            "assumes",
            assumption["id"],
            provenance={"kind": "theory_assumption"},
            rationale=(
                "Assumption declared for this theory; it is not evidence that "
                "the theory is true."
            ),
        )
        assumption_nodes.append(assumption)

    for assumption_id in existing_assumption_ids:
        assumption = _node_by_id(state, assumption_id)
        if assumption is None:  # already validated, but keep the guard local
            raise TheoryError(f"unknown assumption id: {assumption_id}")
        graph.link(
            theory["id"],
            "assumes",
            assumption_id,
            provenance={"kind": "theory_assumption", "existing": True},
            rationale="Existing assumption linked to this theory; it remains an assumption.",
        )
        assumption_nodes.append(dict(assumption))

    for wording in prediction_texts:
        prediction = graph.record(
            "Prediction",
            wording=wording,
            theory_id=theory["id"],
            status="untested",
            truth_assessed=False,
        )
        graph.link(
            theory["id"],
            "predicts",
            prediction["id"],
            provenance={"kind": "theory_prediction"},
            rationale=(
                "Prediction declared for this theory; it remains untested until "
                "tests or observations are linked."
            ),
        )
        prediction_nodes.append(prediction)

    for prediction_id in existing_prediction_ids:
        prediction = _node_by_id(state, prediction_id)
        if prediction is None:  # already validated, but keep the guard local
            raise TheoryError(f"unknown prediction id: {prediction_id}")
        graph.link(
            theory["id"],
            "predicts",
            prediction_id,
            provenance={"kind": "theory_prediction", "existing": True},
            rationale="Existing prediction linked to this theory; its test status is unchanged.",
        )
        prediction_nodes.append(dict(prediction))

    return {
        "theory": theory,
        "assumptions": assumption_nodes,
        "predictions": prediction_nodes,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# rivalry and compatibility
# --------------------------------------------------------------------------- #
def _same_rivalry(
    edge: Mapping[str, Any],
    theory_a: str,
    theory_b: str,
    scope: str | None,
    rationale: str | None,
    reviewer: str | None,
) -> bool:
    if edge.get("type") != "rival_of":
        return False
    if {edge.get("src"), edge.get("tgt")} != {theory_a, theory_b}:
        return False
    provenance = _provenance(edge)
    return (
        _token(provenance.get("kind")) == "theory_rivalry"
        and _normalized(provenance.get("scope")) == _normalized(scope)
        and _normalized(provenance.get("rationale")) == _normalized(rationale)
        and _normalized(provenance.get("reviewer")) == _normalized(reviewer)
    )


def record_rivalry(
    graph: Any,
    theory_a: str,
    theory_b: str,
    *,
    scope: str | None = None,
    rationale: str | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Record one rival_of edge between distinct theories.

    Rivalry is symmetric in reports.  An identical relation supplied in either
    direction returns the existing edge instead of creating a duplicate.
    """
    state = _as_state(graph)
    first = _require_theory(state, theory_a, "theory_a")
    second = _require_theory(state, theory_b, "theory_b")
    if first["id"] == second["id"]:
        raise TheoryError("a theory cannot be its own rival")
    scope = _optional_string(scope, "scope")
    rationale = _optional_string(rationale, "rationale")
    reviewer = _optional_string(reviewer, "reviewer")

    for edge in _pair_edges(state, "rival_of", first["id"], second["id"]):
        if _same_rivalry(edge, first["id"], second["id"], scope, rationale, reviewer):
            return {
                "edge": dict(edge),
                "created": False,
                "truth_assessed": False,
            }

    provenance = {
        "kind": "theory_rivalry",
        "scope": scope,
        "rationale": rationale,
        "reviewer": reviewer,
    }
    edge = graph.link(
        first["id"],
        "rival_of",
        second["id"],
        provenance=provenance,
        rationale=rationale,
    )
    return {"edge": edge, "created": True, "truth_assessed": False}


def _same_compatibility(
    edge: Mapping[str, Any],
    theory_a: str,
    theory_b: str,
    degree: str,
    scope: str | None,
) -> bool:
    if edge.get("type") != "compatible_with":
        return False
    if {edge.get("src"), edge.get("tgt")} != {theory_a, theory_b}:
        return False
    provenance = _provenance(edge)
    return (
        _token(provenance.get("degree")) == degree
        and _normalized(provenance.get("scope")) == _normalized(scope)
    )


def record_compatibility(
    graph: Any,
    theory_a: str,
    theory_b: str,
    *,
    degree: str,
    scope: str | None = None,
    rationale: str | None = None,
    reviewer: str | None = None,
    evidence_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Record a graded, undirected compatibility relation.

    Compatibility is distinct from rivalry: two theories can be partial rivals
    in one scope and partially compatible in another.  The degree, scope,
    rationale and reviewer stay on the edge provenance.  An identical
    pair+degree+scope in either direction returns the existing edge.
    """
    state = _as_state(graph)
    first = _require_theory(state, theory_a, "theory_a")
    second = _require_theory(state, theory_b, "theory_b")
    if first["id"] == second["id"]:
        raise TheoryError("a theory cannot be compared with itself")
    degree = _compatibility_degree(degree)
    scope = _optional_string(scope, "scope")
    rationale = _optional_string(rationale, "rationale")
    reviewer = _optional_string(reviewer, "reviewer")
    evidence = _string_items(evidence_ids, "evidence_ids")
    for evidence_id in evidence:
        _require_node(state, evidence_id, "compatibility evidence")

    for edge in _pair_edges(state, "compatible_with", first["id"], second["id"]):
        if _same_compatibility(edge, first["id"], second["id"], degree, scope):
            return {
                "edge": dict(edge),
                "created": False,
                "truth_assessed": False,
            }

    provenance = {
        "kind": "theory_compatibility",
        "degree": degree,
        "scope": scope,
        "rationale": rationale,
        "reviewer": reviewer,
        "evidence_ids": sorted(evidence),
    }
    _json_safe(provenance, "compatibility provenance")
    edge = graph.link(
        first["id"],
        "compatible_with",
        second["id"],
        provenance=provenance,
        rationale=rationale,
    )
    return {"edge": edge, "created": True, "truth_assessed": False}


def link_theory_evidence(
    graph: Any,
    theory_id: str,
    target_id: str,
    *,
    relation: str = "supported_by",
    rationale: str | None = None,
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Link a theory to evidence, a claim, an observation or a test.

    The target type is checked against the graph's endpoint rules.  Repeating
    the same directed link returns the existing edge with ``created=False``.
    """
    state = _as_state(graph)
    theory = _require_theory(state, theory_id, "theory")
    relation = _relation(relation)
    target = _require_node(
        state,
        target_id,
        "target",
        _RELATION_TARGET_TYPES[relation],
    )
    rationale = _optional_string(rationale, "rationale")
    reviewer = _optional_string(reviewer, "reviewer")

    for edge in _edges(state):
        if (
            edge.get("type") == relation
            and edge.get("src") == theory["id"]
            and edge.get("tgt") == target["id"]
        ):
            return {
                "edge": dict(edge),
                "created": False,
                "truth_assessed": False,
            }

    edge = graph.link(
        theory["id"],
        relation,
        target["id"],
        provenance={
            "kind": "theory_evidence_link",
            "reviewer": reviewer,
            "rationale": rationale,
        },
        rationale=rationale,
    )
    return {"edge": edge, "created": True, "truth_assessed": False}


# --------------------------------------------------------------------------- #
# relation and evidence helpers
# --------------------------------------------------------------------------- #
def _relation_record(edge: Mapping[str, Any], default_kind: str) -> dict[str, Any]:
    provenance = _provenance(edge)
    rationale = provenance.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        fallback = edge.get("rationale")
        rationale = fallback if isinstance(fallback, str) and fallback.strip() else None
    evidence_ids = provenance.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        evidence_ids = []
    return {
        "edge_id": str(edge.get("id") or ""),
        "type": edge.get("type"),
        "kind": provenance.get("kind") or default_kind,
        "src": edge.get("src"),
        "tgt": edge.get("tgt"),
        "degree": provenance.get("degree"),
        "scope": provenance.get("scope"),
        "rationale": rationale,
        "reviewer": provenance.get("reviewer"),
        "evidence_ids": [str(item) for item in evidence_ids],
        "created_at": edge.get("created_at"),
    }


def _theory_evidence(
    state: Mapping[str, Any], theory_id: str
) -> tuple[set[str], set[str]]:
    return (
        set(_outgoing_ids(state, theory_id, "supported_by")),
        set(_outgoing_ids(state, theory_id, "challenged_by")),
    )


def _prediction_ids(state: Mapping[str, Any], theory_id: str) -> list[str]:
    ids = set(_outgoing_ids(state, theory_id, "predicts"))
    ids.update(
        str(node["id"])
        for node in _nodes(state)
        if node.get("type") == "Prediction" and node.get("theory_id") == theory_id
    )
    return sorted(ids)


def _prediction_records(
    state: Mapping[str, Any], theory_id: str
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for prediction_id in _prediction_ids(state, theory_id):
        prediction = _node_by_id(state, prediction_id)
        if prediction is None:
            continue
        tests = [
            dict(node)
            for test_id in _outgoing_ids(state, prediction_id, "tests")
            if (node := _node_by_id(state, test_id)) is not None
        ]
        discriminated = sorted(
            {
                str(edge["tgt"])
                for edge in _edges(state)
                if edge.get("type") == "discriminated_by"
                and edge.get("src") == theory_id
                and edge.get("tgt") == prediction_id
            }
        )
        records.append(
            {
                "prediction": dict(prediction),
                "tested": bool(tests),
                "tests": tests,
                "discriminated_by": discriminated,
            }
        )
    return records


def _appraisal_targets(
    state: Mapping[str, Any], node: Mapping[str, Any], relation: str
) -> set[str]:
    ids: set[str] = set()
    target_id = node.get("target_id")
    if isinstance(target_id, str) and target_id.strip():
        ids.add(target_id.strip())
    ids.update(_outgoing_ids(state, str(node.get("id") or ""), relation))
    return ids


def _rating_label(value: Any) -> str:
    text = _collapse(value)
    if not text:
        return "unknown"
    return _RATING_LABELS.get(text.casefold(), _token(text) or "unknown")


def _risk_label(value: Any) -> str:
    text = _collapse(value)
    if not text:
        return "unknown"
    return _RISK_LABELS.get(text.casefold(), _token(text) or "unknown")


def _study_design(node: Mapping[str, Any]) -> str:
    fields = node.get("fields")
    value = fields.get("study_design") if isinstance(fields, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        value = node.get("study_design")
    return _collapse(value) or "not_recorded"


def _evidence_profile(state: Mapping[str, Any], theory_id: str) -> dict[str, Any]:
    supported, challenged = _theory_evidence(state, theory_id)
    linked = supported | challenged
    by_id = {str(node["id"]): node for node in _nodes(state)}

    def by_type(ids: set[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node_id in sorted(ids):
            node_type = str(by_id.get(node_id, {}).get("type") or "unknown")
            counts[node_type] = counts.get(node_type, 0) + 1
        return dict(sorted(counts.items()))

    certainty_ratings: dict[str, int] = {}
    certainty_total = 0
    risk_judgements: dict[str, int] = {}
    risk_total = 0
    for node in _nodes(state):
        node_type = node.get("type")
        if node_type == "Certainty":
            if linked & _appraisal_targets(state, node, "grades"):
                certainty_total += 1
                rating = _rating_label(node.get("rating"))
                certainty_ratings[rating] = certainty_ratings.get(rating, 0) + 1
        elif node_type == "RiskOfBias":
            if linked & _appraisal_targets(state, node, "assesses"):
                risk_total += 1
                judgement = _risk_label(node.get("overall"))
                risk_judgements[judgement] = risk_judgements.get(judgement, 0) + 1

    extraction_ids: set[str] = set()
    for node in _nodes(state):
        if node.get("type") != "Extraction":
            continue
        node_id = str(node["id"])
        if node_id in linked or (
            _appraisal_targets(state, node, "extracts") & linked
        ):
            extraction_ids.add(node_id)
    designs: dict[str, int] = {}
    for extraction_id in sorted(extraction_ids):
        node = by_id.get(extraction_id)
        if node is None:
            continue
        design = _study_design(node)
        designs[design] = designs.get(design, 0) + 1

    return {
        "evidence_items": len(linked),
        "supported_by_type": by_type(supported),
        "challenged_by_type": by_type(challenged),
        "certainty": {
            "total": certainty_total,
            "by_rating": dict(sorted(certainty_ratings.items())),
        },
        "risk_of_bias": {
            "total": risk_total,
            "by_judgement": dict(sorted(risk_judgements.items())),
        },
        "extraction_study_designs": dict(sorted(designs.items())),
    }


def _discriminating_records(
    state: Mapping[str, Any],
    theory_ids: Sequence[str],
    unique_a: set[str],
    unique_b: set[str],
    shared: set[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for theory_id in theory_ids:
        for target_id in _outgoing_ids(state, theory_id, "discriminated_by"):
            key = (theory_id, target_id)
            if key in seen:
                continue
            seen.add(key)
            target = _node_by_id(state, target_id)
            target_type = str(target.get("type") or "unknown") if target else "missing"
            if target_id in unique_a:
                uniqueness = "unique_a"
            elif target_id in unique_b:
                uniqueness = "unique_b"
            elif target_id in shared:
                uniqueness = "shared"
            else:
                uniqueness = "not_in_evidence_sets"
            records.append(
                {
                    "theory_id": theory_id,
                    "target_id": target_id,
                    "target_type": target_type,
                    "wording": _node_label(target, target_id),
                    "is_evidence": target_type in _DISCRIMINATING_EVIDENCE_TYPES,
                    "uniqueness": uniqueness,
                }
            )
    return sorted(records, key=lambda record: (record["theory_id"], record["target_id"]))


def _discernment(
    shared: set[str],
    unique_a: set[str],
    unique_b: set[str],
    discriminating_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    discriminating_evidence = sorted(
        {
            str(record["target_id"])
            for record in discriminating_records
            if record.get("is_evidence")
        }
    )
    discriminating_predictions = sorted(
        {
            str(record["target_id"])
            for record in discriminating_records
            if record.get("target_type") == "Prediction"
        }
    )
    reasons: list[str] = []
    if discriminating_evidence:
        status = "discriminating_evidence_present"
        reasons.append(
            "Evidence or observations are explicitly linked as discriminating "
            "for at least one theory."
        )
    elif not shared and not unique_a and not unique_b:
        status = "no_evidence"
        reasons.append("Neither theory has evidence linked by supported_by or challenged_by.")
        if discriminating_predictions:
            reasons.append(
                "Discriminating predictions are recorded, but no evidence or "
                "observation distinguishes the theories."
            )
    elif not unique_a and not unique_b:
        status = "shared_only"
        reasons.append(
            "All recorded evidence is shared by both theories; no unique evidence "
            "is recorded."
        )
        if discriminating_predictions:
            reasons.append(
                "Discriminating predictions are recorded, but no discriminating "
                "evidence or observation is linked."
            )
    else:
        status = "unique_but_not_discriminating"
        reasons.append(
            "Unique evidence exists, but no evidence or observation is explicitly "
            "linked as discriminating."
        )
        if discriminating_predictions:
            reasons.append(
                "Discriminating predictions are recorded, but no discriminating "
                "evidence or observation is linked."
            )
    reasons.append("No theory is selected or judged true.")
    return {
        "status": status,
        "reasons": reasons,
        "discriminating_evidence": discriminating_evidence,
        "discriminating_predictions": discriminating_predictions,
    }


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def theory_compare(
    state: Any, theory_a: str, theory_b: str
) -> dict[str, Any]:
    """Compare two theories descriptively, including whether evidence can discern.

    The result reports explicit rivalry and compatibility records, evidence
    sets, predictions and tests, evidence profiles, and a discernment status.
    It never declares a winner or assesses truth.
    """
    current = _as_state(state)
    first = _require_theory(current, theory_a, "theory_a")
    second = _require_theory(current, theory_b, "theory_b")
    if first["id"] == second["id"]:
        raise TheoryError("theory_a and theory_b must be distinct")

    rivalry = [
        _relation_record(edge, "theory_rivalry")
        for edge in _pair_edges(current, "rival_of", first["id"], second["id"])
    ]
    compatibility = [
        _relation_record(edge, "theory_compatibility")
        for edge in _pair_edges(current, "compatible_with", first["id"], second["id"])
    ]

    supported_a, challenged_a = _theory_evidence(current, first["id"])
    supported_b, challenged_b = _theory_evidence(current, second["id"])
    all_a = supported_a | challenged_a
    all_b = supported_b | challenged_b
    shared = all_a & all_b
    unique_a = all_a - all_b
    unique_b = all_b - all_a
    union = all_a | all_b
    overlap_ratio = round(len(shared) / len(union), 6) if union else 0.0

    discriminating = _discriminating_records(
        current,
        (first["id"], second["id"]),
        unique_a,
        unique_b,
        shared,
    )
    discernment = _discernment(shared, unique_a, unique_b, discriminating)

    return {
        "theory_a": dict(first),
        "theory_b": dict(second),
        "rivalry": rivalry,
        "compatibility": compatibility,
        "supported": {
            "a": sorted(supported_a),
            "b": sorted(supported_b),
        },
        "challenged": {
            "a": sorted(challenged_a),
            "b": sorted(challenged_b),
        },
        "shared_evidence": sorted(shared),
        "unique_a": sorted(unique_a),
        "unique_b": sorted(unique_b),
        "overlap_ratio": overlap_ratio,
        "predictions": {
            "a": _prediction_records(current, first["id"]),
            "b": _prediction_records(current, second["id"]),
        },
        "discriminating_targets": discriminating,
        "discriminating_evidence": discernment["discriminating_evidence"],
        "discriminating_predictions": discernment["discriminating_predictions"],
        "evidence_profiles": {
            "a": _evidence_profile(current, first["id"]),
            "b": _evidence_profile(current, second["id"]),
        },
        "discernment": discernment,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def _rival_pairs(state: Mapping[str, Any]) -> dict[tuple[str, str], list[str]]:
    pairs: dict[tuple[str, str], list[str]] = {}
    for edge in _edges(state):
        if edge.get("type") != "rival_of":
            continue
        source, target = edge.get("src"), edge.get("tgt")
        if not isinstance(source, str) or not isinstance(target, str) or source == target:
            continue
        pair = tuple(sorted((source, target)))
        pairs.setdefault(pair, []).append(str(edge.get("id") or ""))
    return {pair: sorted(ids) for pair, ids in sorted(pairs.items())}


def theory_scan(state: Any) -> dict[str, Any]:
    """Report deterministic theory-level gaps without choosing a theory."""
    current = _as_state(state)
    theories = sorted(
        (node for node in _nodes(current) if node.get("type") == "Theory"),
        key=lambda node: str(node["id"]),
    )

    without_predictions: list[dict[str, Any]] = []
    without_evidence: list[dict[str, Any]] = []
    for theory in theories:
        theory_id = str(theory["id"])
        if not _prediction_ids(current, theory_id):
            without_predictions.append({
                "theory_id": theory_id,
                "name": theory.get("name"),
                "reason": "Theory has no predictions.",
            })
        supported, challenged = _theory_evidence(current, theory_id)
        if not supported and not challenged:
            without_evidence.append({
                "theory_id": theory_id,
                "name": theory.get("name"),
                "reason": "Theory has no supported_by or challenged_by evidence.",
            })

    untested: list[dict[str, Any]] = []
    for node in sorted(
        (item for item in _nodes(current) if item.get("type") == "Prediction"),
        key=lambda item: str(item["id"]),
    ):
        prediction_id = str(node["id"])
        if _outgoing_ids(current, prediction_id, "tests"):
            continue
        theory_ids = sorted(
            {
                str(edge["src"])
                for edge in _edges(current)
                if edge.get("type") == "predicts" and edge.get("tgt") == prediction_id
            }
            | {
                str(item["id"])
                for item in theories
                if item.get("id") == node.get("theory_id")
            }
        )
        untested.append({
            "prediction_id": prediction_id,
            "theory_ids": theory_ids,
            "wording": _node_label(node, prediction_id),
            "status": node.get("status"),
            "reason": "Prediction has no tests link to evidence, an observation or a run.",
        })

    rival_pairs = _rival_pairs(current)
    shared_only: list[dict[str, Any]] = []
    without_discriminating: list[dict[str, Any]] = []
    for (theory_a, theory_b), edge_ids in rival_pairs.items():
        supported_a, challenged_a = _theory_evidence(current, theory_a)
        supported_b, challenged_b = _theory_evidence(current, theory_b)
        all_a = supported_a | challenged_a
        all_b = supported_b | challenged_b
        shared = all_a & all_b
        unique_a = all_a - all_b
        unique_b = all_b - all_a
        if shared and not unique_a and not unique_b:
            shared_only.append({
                "theory_a": theory_a,
                "theory_b": theory_b,
                "rivalry_edge_ids": edge_ids,
                "shared_evidence": sorted(shared),
                "reason": "Rival theories have shared evidence but no unique evidence.",
            })
        discriminating = _discriminating_records(
            current,
            (theory_a, theory_b),
            unique_a,
            unique_b,
            shared,
        )
        evidence_discriminators = [
            record for record in discriminating if record.get("is_evidence")
        ]
        if not evidence_discriminators:
            without_discriminating.append({
                "theory_a": theory_a,
                "theory_b": theory_b,
                "rivalry_edge_ids": edge_ids,
                "shared_evidence_count": len(shared),
                "unique_a_count": len(unique_a),
                "unique_b_count": len(unique_b),
                "discriminating_predictions": [
                    record["target_id"]
                    for record in discriminating
                    if record.get("target_type") == "Prediction"
                ],
                "reason": (
                    "No evidence or observation is explicitly linked as "
                    "discriminating for either rival theory."
                ),
            })

    explained_claims = {
        str(edge["tgt"])
        for edge in _edges(current)
        if edge.get("type") == "explains" and isinstance(edge.get("tgt"), str)
    }
    unexplained_claims = [
        {
            "claim_id": str(node["id"]),
            "wording": _node_label(node, str(node["id"])),
            "status": node.get("status"),
        }
        for node in sorted(
            (item for item in _nodes(current) if item.get("type") == "Claim"),
            key=lambda item: str(item["id"]),
        )
        if str(node["id"]) not in explained_claims
    ]

    compatibility_by_pair: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for edge in _edges(current):
        if edge.get("type") != "compatible_with":
            continue
        source, target = edge.get("src"), edge.get("tgt")
        if not isinstance(source, str) or not isinstance(target, str) or source == target:
            continue
        compatibility_by_pair.setdefault(tuple(sorted((source, target))), []).append(edge)

    contradictory: list[dict[str, Any]] = []
    for pair in sorted(set(rival_pairs) & set(compatibility_by_pair)):
        edges = compatibility_by_pair[pair]
        degrees = sorted({
            _token(_provenance(edge).get("degree"))
            for edge in edges
            if _token(_provenance(edge).get("degree")) in COMPATIBILITY_DEGREES
        })
        conflicting = sorted(set(degrees) & _CONTRADICTORY_DEGREES)
        if not conflicting:
            continue
        contradictory.append({
            "theory_a": pair[0],
            "theory_b": pair[1],
            "rivalry_edge_ids": rival_pairs[pair],
            "compatibility_edge_ids": sorted(
                str(edge.get("id") or "") for edge in edges
            ),
            "compatibility_degrees": conflicting,
            "reason": (
                "The pair is recorded as rivals and as compatible or partially "
                "compatible; both records need review."
            ),
        })

    return {
        "theories_without_predictions": without_predictions,
        "untested_predictions": untested,
        "theories_without_evidence": without_evidence,
        "rival_pairs_with_shared_only_evidence": shared_only,
        "rival_pairs_without_discriminating_evidence": without_discriminating,
        "claims_not_explained_by_any_theory": unexplained_claims,
        "contradictory_relations": contradictory,
        "counts": {
            "theories": len(theories),
            "assumptions": sum(
                1 for node in _nodes(current) if node.get("type") == "Assumption"
            ),
            "predictions": sum(
                1 for node in _nodes(current) if node.get("type") == "Prediction"
            ),
            "rival_pairs": len(rival_pairs),
            "compatibility_records": sum(
                len(edges) for edges in compatibility_by_pair.values()
            ),
            "theories_without_predictions": len(without_predictions),
            "untested_predictions": len(untested),
            "theories_without_evidence": len(without_evidence),
            "rival_pairs_with_shared_only_evidence": len(shared_only),
            "rival_pairs_without_discriminating_evidence": len(without_discriminating),
            "claims_not_explained_by_any_theory": len(unexplained_claims),
            "contradictory_relations": len(contradictory),
        },
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# theory report
# --------------------------------------------------------------------------- #
def _theory_checks(
    state: Mapping[str, Any], theory: Mapping[str, Any]
) -> dict[str, Any]:
    theory_id = str(theory["id"])
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    supported, challenged = _theory_evidence(state, theory_id)
    predictions = _prediction_records(state, theory_id)
    untested = [
        str(record["prediction"]["id"])
        for record in predictions
        if not record["tested"]
    ]
    assumptions = sorted(
        set(_outgoing_ids(state, theory_id, "assumes"))
        | {
            str(node["id"])
            for node in _nodes(state)
            if node.get("type") == "Assumption" and node.get("theory_id") == theory_id
        }
    )
    rivals = [
        edge
        for edge in _edges(state)
        if edge.get("type") == "rival_of"
        and theory_id in (edge.get("src"), edge.get("tgt"))
    ]
    rival_ids = {
        str(edge.get("tgt") if edge.get("src") == theory_id else edge.get("src"))
        for edge in rivals
    }
    compatibility = [
        edge
        for edge in _edges(state)
        if edge.get("type") == "compatible_with"
        and theory_id in (edge.get("src"), edge.get("tgt"))
    ]

    if not predictions:
        issues.append({
            "code": "theory_without_predictions",
            "theory_id": theory_id,
            "reason": "Theory has no predictions.",
        })
    elif untested:
        warnings.append({
            "code": "untested_predictions",
            "theory_id": theory_id,
            "prediction_ids": untested,
            "reason": "Some predictions have no tests link.",
        })
    if not supported and not challenged:
        issues.append({
            "code": "theory_without_evidence",
            "theory_id": theory_id,
            "reason": "Theory has no supported_by or challenged_by evidence.",
        })
    if not assumptions:
        warnings.append({
            "code": "theory_without_assumptions",
            "theory_id": theory_id,
            "reason": "Theory has no linked assumptions.",
        })
    if not rivals:
        warnings.append({
            "code": "theory_without_rival",
            "theory_id": theory_id,
            "reason": "No rival theory is recorded.",
        })
    if not compatibility:
        warnings.append({
            "code": "theory_without_compatibility_record",
            "theory_id": theory_id,
            "reason": "No compatibility relation is recorded.",
        })
    if theory.get("status") not in THEORY_STATUSES:
        issues.append({
            "code": "theory_status_invalid",
            "theory_id": theory_id,
            "reason": "Theory status is missing or unrecognized.",
        })
    if theory.get("truth_assessed") is True:
        issues.append({
            "code": "truth_assessed_claimed",
            "theory_id": theory_id,
            "reason": "Theory must retain truth_assessed=False.",
        })

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "assumptions": len(assumptions),
            "predictions": len(predictions),
            "untested_predictions": len(untested),
            "supported": len(supported),
            "challenged": len(challenged),
            "rivals": len(rival_ids),
            "compatibility_records": len(compatibility),
        },
    }


def _theory_report(state: Mapping[str, Any], theory: Mapping[str, Any]) -> dict[str, Any]:
    theory_id = str(theory["id"])
    supported, challenged = _theory_evidence(state, theory_id)

    assumption_ids = sorted(
        set(_outgoing_ids(state, theory_id, "assumes"))
        | {
            str(node["id"])
            for node in _nodes(state)
            if node.get("type") == "Assumption" and node.get("theory_id") == theory_id
        }
    )
    assumptions = [
        dict(node)
        for assumption_id in assumption_ids
        if (node := _node_by_id(state, assumption_id)) is not None
    ]

    explanation_ids = set(_outgoing_ids(state, theory_id, "explains"))
    discriminated_ids = set(_outgoing_ids(state, theory_id, "discriminated_by"))
    claim_ids = {
        node_id
        for node_id in supported | challenged | explanation_ids | discriminated_ids
        if (node := _node_by_id(state, node_id)) is not None
        and node.get("type") == "Claim"
    }
    claims = [
        dict(node)
        for claim_id in sorted(claim_ids)
        if (node := _node_by_id(state, claim_id)) is not None
    ]

    rivals: dict[str, list[dict[str, Any]]] = {}
    for edge in _edges(state):
        if edge.get("type") != "rival_of":
            continue
        if theory_id not in (edge.get("src"), edge.get("tgt")):
            continue
        other_id = str(edge.get("tgt") if edge.get("src") == theory_id else edge.get("src"))
        rivals.setdefault(other_id, []).append(_relation_record(edge, "theory_rivalry"))
    rival_records = []
    for other_id in sorted(rivals):
        other = _node_by_id(state, other_id)
        rival_records.append({
            "theory_id": other_id,
            "name": other.get("name") if other is not None else None,
            "records": sorted(rivals[other_id], key=lambda record: record["edge_id"]),
        })

    compatibility = [
        _relation_record(edge, "theory_compatibility")
        for edge in sorted(
            (
                edge
                for edge in _edges(state)
                if edge.get("type") == "compatible_with"
                and theory_id in (edge.get("src"), edge.get("tgt"))
            ),
            key=lambda edge: str(edge.get("id") or ""),
        )
    ]

    return {
        "theory": dict(theory),
        "assumptions": assumptions,
        "predictions": _prediction_records(state, theory_id),
        "evidence": {
            "supported": [
                dict(node)
                for node_id in sorted(supported)
                if (node := _node_by_id(state, node_id)) is not None
            ],
            "challenged": [
                dict(node)
                for node_id in sorted(challenged)
                if (node := _node_by_id(state, node_id)) is not None
            ],
            "explains": [
                dict(node)
                for node_id in sorted(explanation_ids)
                if (node := _node_by_id(state, node_id)) is not None
            ],
            "discriminated_by": [
                dict(node)
                for node_id in sorted(discriminated_ids)
                if (node := _node_by_id(state, node_id)) is not None
            ],
        },
        "claims": claims,
        "rivals": rival_records,
        "compatibility": compatibility,
        "checks": _theory_checks(state, theory),
        "truth_assessed": False,
    }


def theory_report(state: Any, theory_id: str | None = None) -> dict[str, Any]:
    """Return one theory or all theories with links, relations and checks."""
    current = _as_state(state)
    if theory_id is not None:
        theory = _require_theory(current, theory_id, "theory")
        return _theory_report(current, theory)

    theories = sorted(
        (node for node in _nodes(current) if node.get("type") == "Theory"),
        key=lambda node: str(node["id"]),
    )
    reports = [_theory_report(current, theory) for theory in theories]
    issues = [
        {"theory_id": report["theory"]["id"], **issue}
        for report in reports
        for issue in report["checks"]["issues"]
    ]
    warnings = [
        {"theory_id": report["theory"]["id"], **warning}
        for report in reports
        for warning in report["checks"]["warnings"]
    ]
    return {
        "theories": reports,
        "checks": {
            "ok": not issues,
            "issues": issues,
            "warnings": warnings,
        },
        "counts": {
            "theories": len(reports),
            "assumptions": sum(len(report["assumptions"]) for report in reports),
            "predictions": sum(len(report["predictions"]) for report in reports),
            "supported": sum(len(report["evidence"]["supported"]) for report in reports),
            "challenged": sum(len(report["evidence"]["challenged"]) for report in reports),
            "claims": sum(len(report["claims"]) for report in reports),
            "rivals": sum(len(report["rivals"]) for report in reports),
            "compatibility_records": sum(
                len(report["compatibility"]) for report in reports
            ),
        },
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# compatibility matrix
# --------------------------------------------------------------------------- #
def compatibility_matrix(state: Any) -> dict[str, Any]:
    """Return a deterministic pair matrix with an explicit missing marker."""
    current = _as_state(state)
    theories = sorted(
        (node for node in _nodes(current) if node.get("type") == "Theory"),
        key=lambda node: str(node["id"]),
    )
    by_id = {str(node["id"]): node for node in theories}
    rows: list[dict[str, Any]] = []
    for first, second in combinations([str(node["id"]) for node in theories], 2):
        supported_a, challenged_a = _theory_evidence(current, first)
        supported_b, challenged_b = _theory_evidence(current, second)
        all_a = supported_a | challenged_a
        all_b = supported_b | challenged_b
        shared = all_a & all_b
        unique_a = all_a - all_b
        unique_b = all_b - all_a
        records = _pair_edges(current, "compatible_with", first, second)
        common = {
            "theory_a": first,
            "theory_b": second,
            "theory_a_name": by_id[first].get("name"),
            "theory_b_name": by_id[second].get("name"),
            "shared_evidence_count": len(shared),
            "unique_a_count": len(unique_a),
            "unique_b_count": len(unique_b),
        }
        if not records:
            rows.append({
                **common,
                "relation": "missing",
                "missing": True,
                "edge_id": None,
                "degree": None,
                "scope": None,
                "rationale": None,
                "reviewer": None,
                "evidence_ids": [],
            })
            continue
        for edge in records:
            record = _relation_record(edge, "theory_compatibility")
            rows.append({
                **common,
                "relation": "compatible_with",
                "missing": False,
                "edge_id": record["edge_id"],
                "degree": record["degree"],
                "scope": record["scope"],
                "rationale": record["rationale"],
                "reviewer": record["reviewer"],
                "evidence_ids": record["evidence_ids"],
            })

    rows.sort(key=lambda row: (
        row["theory_a"],
        row["theory_b"],
        str(row["edge_id"] or ""),
    ))
    return {
        "theories": [dict(node) for node in theories],
        "pairs": rows,
        "counts": {
            "theories": len(theories),
            "pairs": len(rows),
            "recorded": sum(1 for row in rows if not row["missing"]),
            "missing": sum(1 for row in rows if row["missing"]),
        },
        "truth_assessed": False,
    }
