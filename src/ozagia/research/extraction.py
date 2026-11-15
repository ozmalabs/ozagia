"""Structured extraction records for the research graph.

An extraction records what a source *reports* in a small, comparable schema.
It does not assess truth, infer missing values, convert units or pool effects.
Every field in :data:`EXTRACTION_SCHEMA` is preserved in the recorded node,
using ``None`` when the extraction did not report a value. The canonical field
mapping is stored under ``fields``; each validated value is also mirrored at
the top level so graph projections can read it without unpacking.

The mechanical check requires a target, a study design, at least one outcome,
an effect measure or effect size, and a source locator. Numeric values are
checked for finiteness and interval consistency, but the check is not a
meta-analysis and never decides whether an effect is real.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ozagia.research.graph import ResearchError

__all__ = [
    "EXTRACTION_FIELDS",
    "EXTRACTION_SCHEMA",
    "EXTRACTION_TABLE_COLUMNS",
    "ExtractionError",
    "check_extraction",
    "extraction_report",
    "extraction_table",
    "record_extraction",
]

EXTRACTION_SCHEMA: dict[str, dict[str, str]] = {
    "study_design": {
        "type": "string",
        "check": "required",
        "description": "Design reported by the source, for example randomised trial or cohort.",
    },
    "population": {
        "type": "string",
        "check": "optional",
        "description": "Population or sample described by the source.",
    },
    "intervention": {
        "type": "string",
        "check": "optional",
        "description": "Intervention or exposure as reported.",
    },
    "comparator": {
        "type": "string",
        "check": "optional",
        "description": "Comparator or control as reported.",
    },
    "outcomes": {
        "type": "list[string]",
        "check": "required",
        "description": "Reported outcome names; each item must be a non-empty string.",
    },
    "effect_measure": {
        "type": "string",
        "check": "effect_measure or effect_size",
        "description": "Reported measure, for example risk ratio or mean difference.",
    },
    "effect_size": {
        "type": "number",
        "check": "effect_measure or effect_size",
        "description": "Point estimate exactly as reported; no conversion or pooling.",
    },
    "ci_low": {
        "type": "number",
        "check": "optional",
        "description": "Lower confidence-interval bound as reported.",
    },
    "ci_high": {
        "type": "number",
        "check": "optional",
        "description": "Upper confidence-interval bound as reported.",
    },
    "p_value": {
        "type": "number",
        "check": "optional",
        "description": "Reported p-value in the inclusive range 0..1.",
    },
    "sample_size": {
        "type": "positive integer",
        "check": "optional",
        "description": "Positive analysis sample size as reported.",
    },
    "unit": {
        "type": "string",
        "check": "optional",
        "description": "Unit of the effect size, when the measure has one.",
    },
    "direction": {
        "type": "string",
        "check": "optional",
        "description": "Reported direction of effect, for example decrease or no difference.",
    },
    "notes": {
        "type": "string",
        "check": "optional",
        "description": "Extractor note that does not replace a field value.",
    },
}

EXTRACTION_FIELDS = tuple(EXTRACTION_SCHEMA)
EXTRACTION_TABLE_COLUMNS = (
    "extraction_id",
    "target",
    "design",
    "population",
    "intervention",
    "comparator",
    "outcomes",
    "measure",
    "effect",
    "CI",
    "p",
    "n",
    "unit",
    "direction",
    "source",
    "status",
)

_FIELD_SET = frozenset(EXTRACTION_FIELDS)
_STRING_FIELDS = (
    "study_design",
    "population",
    "intervention",
    "comparator",
    "effect_measure",
    "unit",
    "direction",
    "notes",
)
_NUMBER_FIELDS = ("effect_size", "ci_low", "ci_high", "p_value")
_TARGET_TYPES = frozenset({"Reference", "Claim"})


class ExtractionError(ResearchError):
    """A structured extraction is malformed or refers to an unknown target."""


# --------------------------------------------------------------------------- #
# validation and graph helpers
# --------------------------------------------------------------------------- #
def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise ExtractionError("extraction operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and node.get("id")
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _extraction_values(node: Mapping[str, Any]) -> dict[str, Any]:
    """Read canonical fields, falling back to flat legacy/top-level values."""
    raw = node.get("fields")
    values = dict(raw) if isinstance(raw, Mapping) else {}
    for field in EXTRACTION_FIELDS:
        if field not in values and field in node:
            values[field] = node.get(field)
    return values


def _require_extraction(
    state: Mapping[str, Any], extraction_id: str
) -> Mapping[str, Any]:
    node = _node_by_id(state, extraction_id)
    if node is None:
        raise ExtractionError(f"unknown extraction id: {extraction_id}")
    if node.get("type") != "Extraction":
        raise ExtractionError(f"{extraction_id} is not an Extraction node")
    return node


def _require_target(
    state: Mapping[str, Any],
    reference_id: Any,
    claim_id: Any,
) -> Mapping[str, Any]:
    if (reference_id is None) == (claim_id is None):
        raise ExtractionError("exactly one of reference_id or claim_id is required")
    if reference_id is not None:
        target_id, expected = reference_id, "Reference"
    else:
        target_id, expected = claim_id, "Claim"
    if not isinstance(target_id, str) or not target_id.strip():
        raise ExtractionError(f"{expected.lower()}_id must be a non-empty string")
    target = _node_by_id(state, target_id.strip())
    if target is None:
        raise ExtractionError(f"unknown {expected} id: {target_id}")
    if target.get("type") != expected:
        raise ExtractionError(
            f"{target_id} is a {target.get('type')}, not a {expected}"
        )
    return target


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtractionError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _json_safe(value: Any, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ExtractionError(f"{field} must be JSON-serializable: {exc}") from exc


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ExtractionError(f"{field} must be a finite number")
    return number


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExtractionError(f"{field} must be a positive integer")
    return value


def _outcomes(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ExtractionError("outcomes must be a non-empty list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ExtractionError("outcomes must contain non-empty strings")
        item = item.strip()
        if item not in items:
            items.append(item)
    if not items:
        raise ExtractionError("outcomes must be a non-empty list of strings")
    return items


def _source_locator(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ExtractionError("source_locator must be non-empty when supplied")
        return text
    if isinstance(value, Mapping):
        if not value:
            raise ExtractionError("source_locator mapping must be non-empty")
        return _json_safe(dict(value), "source_locator")
    raise ExtractionError("source_locator must be a non-empty string or mapping")


def _validate_fields(fields: Any) -> dict[str, Any]:
    """Return all schema fields, with ``None`` for values not reported."""
    if not isinstance(fields, Mapping):
        raise ExtractionError("fields must be a mapping")
    unknown = sorted(set(fields) - _FIELD_SET, key=str)
    if unknown:
        raise ExtractionError(f"unknown extraction fields: {unknown}")
    values: dict[str, Any] = {field: None for field in EXTRACTION_FIELDS}
    for field in EXTRACTION_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None:
            continue
        if field in _STRING_FIELDS:
            values[field] = _nonempty_string(value, field)
        elif field == "outcomes":
            values[field] = _outcomes(value)
        elif field == "sample_size":
            values[field] = _positive_int(value, field)
        else:
            values[field] = _finite_number(value, field)
    p_value = values["p_value"]
    if p_value is not None and not 0.0 <= p_value <= 1.0:
        raise ExtractionError("p_value must be between 0 and 1")
    ci_low, ci_high = values["ci_low"], values["ci_high"]
    if ci_low is not None and ci_high is not None and ci_low > ci_high:
        raise ExtractionError("ci_low must not exceed ci_high")
    return values


# --------------------------------------------------------------------------- #
# table projection helpers
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number_text(value: Any) -> str:
    return _text(value)


def _ci_text(fields: Mapping[str, Any]) -> str:
    low = _number_text(fields.get("ci_low"))
    high = _number_text(fields.get("ci_high"))
    if low and high:
        return f"{low} to {high}"
    return low or high


def _outcomes_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if item is not None)
    return _text(value)


def _source_label(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return str(value)
    path = value.get("path")
    if isinstance(path, str) and path.strip():
        label = path.strip()
        start = value.get("start_line")
        end = value.get("end_line")
        if start is not None:
            label += f":{start}"
            if end is not None and end != start:
                label += f"-{end}"
        return label
    url = value.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    page = value.get("page")
    if page is None:
        page = value.get("page_start")
    if page is not None:
        label = f"p. {page}"
        end = value.get("page_end")
        if end is not None and end != page:
            label += f"-{end}"
        return label
    try:
        return json.dumps(
            dict(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return str(value)


def _escape_markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _table_row(node: Mapping[str, Any]) -> dict[str, str]:
    fields = _extraction_values(node)
    return {
        "extraction_id": _text(node.get("id")),
        "target": _text(node.get("target_id")),
        "design": _text(fields.get("study_design")),
        "population": _text(fields.get("population")),
        "intervention": _text(fields.get("intervention")),
        "comparator": _text(fields.get("comparator")),
        "outcomes": _outcomes_text(fields.get("outcomes")),
        "measure": _text(fields.get("effect_measure")),
        "effect": _number_text(fields.get("effect_size")),
        "CI": _ci_text(fields),
        "p": _number_text(fields.get("p_value")),
        "n": _number_text(fields.get("sample_size")),
        "unit": _text(fields.get("unit")),
        "direction": _text(fields.get("direction")),
        "source": _source_label(node.get("source_locator")),
        "status": _text(node.get("status")),
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def record_extraction(
    graph: Any,
    *,
    reference_id: str | None = None,
    claim_id: str | None = None,
    fields: Mapping[str, Any],
    source_locator: Any = None,
    extractor: str,
    reviewer: str | None = None,
    status: str = "candidate",
) -> dict[str, Any]:
    """Record one candidate extraction and link it to its target.

    Exactly one of ``reference_id`` or ``claim_id`` is required. The returned
    extraction node preserves every schema field, using ``None`` for values the
    source did not report. No value is inferred, converted or pooled.
    """
    state = _as_state(graph)
    target = _require_target(state, reference_id, claim_id)
    values = _validate_fields(fields)
    extractor = _nonempty_string(extractor, "extractor")
    reviewer = _optional_string(reviewer, "reviewer")
    status = _nonempty_string(status, "status")
    locator = _source_locator(source_locator)
    node = graph.record(
        "Extraction",
        target_id=target["id"],
        reference_id=target["id"] if reference_id is not None else None,
        claim_id=target["id"] if claim_id is not None else None,
        fields=values,
        **values,
        source_locator=locator,
        extractor=extractor,
        reviewer=reviewer,
        status=status,
        truth_assessed=False,
    )
    edge = graph.link(
        node["id"],
        "extracts",
        target["id"],
        provenance={"kind": "structured_extraction", "extractor": extractor},
        rationale=(
            "The extraction records fields from this target; it is not evidence "
            "that the target is true."
        ),
    )
    return {
        "extraction": node,
        "target": dict(target),
        "edge": edge,
        "truth_assessed": False,
    }


def _field_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True


def _has_locator(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    return True


def _valid_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def check_extraction(state: Any, extraction_id: str) -> dict[str, Any]:
    """Check one recorded extraction without assessing its truth."""
    current = _as_state(state)
    node = _require_extraction(current, extraction_id)
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    target_id = node.get("target_id")
    target = None
    if not isinstance(target_id, str) or not target_id.strip():
        issues.append({
            "code": "target_id_missing",
            "reason": "Extraction has no target_id.",
        })
    else:
        target = _node_by_id(current, target_id)
        if target is None:
            issues.append({
                "code": "target_missing",
                "node_id": extraction_id,
                "reason": f"Extraction target {target_id!r} does not exist.",
            })
        elif target.get("type") not in _TARGET_TYPES:
            issues.append({
                "code": "target_type_invalid",
                "node_id": extraction_id,
                "reason": (
                    f"Extraction target {target_id!r} is a {target.get('type')}, "
                    "not a Reference or Claim."
                ),
            })

    raw_fields = node.get("fields")
    if raw_fields is not None and not isinstance(raw_fields, Mapping):
        issues.append({
            "code": "fields_invalid",
            "node_id": extraction_id,
            "reason": "Extraction fields must be a mapping.",
        })
    fields = _extraction_values(node)
    unknown = sorted(set(fields) - _FIELD_SET, key=str)
    if unknown:
        issues.append({
            "code": "unknown_field",
            "node_id": extraction_id,
            "fields": unknown,
            "reason": f"Extraction has unknown fields: {unknown}.",
        })

    for field in _STRING_FIELDS:
        value = fields.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            issues.append({
                "code": "field_type_invalid",
                "node_id": extraction_id,
                "field": field,
                "reason": f"{field} must be a non-empty string.",
            })

    study_design = fields.get("study_design")
    if study_design is None:
        issues.append({
            "code": "required_field_missing",
            "node_id": extraction_id,
            "field": "study_design",
            "reason": "Extraction has no study_design.",
        })

    outcomes = fields.get("outcomes")
    if outcomes is None:
        issues.append({
            "code": "required_field_missing",
            "node_id": extraction_id,
            "field": "outcomes",
            "reason": "Extraction has no outcomes.",
        })
    elif not isinstance(outcomes, list):
        issues.append({
            "code": "field_type_invalid",
            "node_id": extraction_id,
            "field": "outcomes",
            "reason": "outcomes must be a non-empty list of strings.",
        })
    elif not outcomes:
        issues.append({
            "code": "required_field_missing",
            "node_id": extraction_id,
            "field": "outcomes",
            "reason": "Extraction has no outcomes.",
        })
    elif not all(
        isinstance(item, str) and item.strip() for item in outcomes
    ):
        issues.append({
            "code": "field_type_invalid",
            "node_id": extraction_id,
            "field": "outcomes",
            "reason": "outcomes must contain non-empty strings.",
        })

    effect_measure = fields.get("effect_measure")
    effect_size = fields.get("effect_size")
    if not _field_present(effect_measure) and effect_size is None:
        issues.append({
            "code": "effect_measure_or_size_missing",
            "node_id": extraction_id,
            "reason": "Extraction has neither effect_measure nor effect_size.",
        })
    if effect_size is not None and not _field_present(effect_measure):
        warnings.append({
            "code": "effect_measure_missing",
            "node_id": extraction_id,
            "reason": "Effect size is recorded without an effect measure.",
        })

    for field in _NUMBER_FIELDS:
        value = fields.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append({
                "code": "field_type_invalid",
                "node_id": extraction_id,
                "field": field,
                "reason": f"{field} must be a finite number.",
            })
        elif not math.isfinite(float(value)):
            issues.append({
                "code": "field_not_finite",
                "node_id": extraction_id,
                "field": field,
                "reason": f"{field} must be finite.",
            })

    sample_size = fields.get("sample_size")
    if sample_size is not None:
        if (
            isinstance(sample_size, bool)
            or not isinstance(sample_size, int)
            or sample_size <= 0
        ):
            issues.append({
                "code": "sample_size_invalid",
                "node_id": extraction_id,
                "field": "sample_size",
                "reason": "sample_size must be a positive integer.",
            })

    p_value = _valid_number(fields.get("p_value"))
    if p_value is not None and not 0.0 <= p_value <= 1.0:
        issues.append({
            "code": "p_value_out_of_range",
            "node_id": extraction_id,
            "field": "p_value",
            "reason": "p_value must be between 0 and 1.",
        })

    ci_low = _valid_number(fields.get("ci_low"))
    ci_high = _valid_number(fields.get("ci_high"))
    if ci_low is not None and ci_high is not None and ci_low > ci_high:
        issues.append({
            "code": "ci_order_invalid",
            "node_id": extraction_id,
            "reason": "ci_low must not exceed ci_high.",
        })
    if (fields.get("ci_low") is None) != (fields.get("ci_high") is None):
        warnings.append({
            "code": "ci_incomplete",
            "node_id": extraction_id,
            "reason": "Only one confidence-interval bound is recorded.",
        })

    locator = node.get("source_locator")
    if not _has_locator(locator):
        issues.append({
            "code": "source_locator_missing",
            "node_id": extraction_id,
            "reason": "Extraction has no source locator.",
        })
    elif not isinstance(locator, (str, Mapping)):
        issues.append({
            "code": "source_locator_invalid",
            "node_id": extraction_id,
            "reason": "source_locator must be a string or mapping.",
        })

    extractor = node.get("extractor")
    if not isinstance(extractor, str) or not extractor.strip():
        issues.append({
            "code": "extractor_missing",
            "node_id": extraction_id,
            "reason": "Extraction has no extractor.",
        })
    status = node.get("status")
    if not isinstance(status, str) or not status.strip():
        issues.append({
            "code": "status_missing",
            "node_id": extraction_id,
            "reason": "Extraction has no status.",
        })
    reviewer = node.get("reviewer")
    if reviewer is None:
        warnings.append({
            "code": "reviewer_missing",
            "node_id": extraction_id,
            "reason": "Extraction has no reviewer.",
        })
    elif not isinstance(reviewer, str) or not reviewer.strip():
        issues.append({
            "code": "reviewer_invalid",
            "node_id": extraction_id,
            "reason": "reviewer must be a non-empty string when supplied.",
        })
    if node.get("truth_assessed") is not False:
        issues.append({
            "code": "truth_assessed_claimed",
            "node_id": extraction_id,
            "reason": "Extraction must retain truth_assessed=False.",
        })

    links = [
        edge for edge in _edges(current)
        if edge.get("type") == "extracts" and edge.get("src") == extraction_id
    ]
    if isinstance(target_id, str) and target_id:
        if not any(edge.get("tgt") == target_id for edge in links):
            issues.append({
                "code": "extracts_link_missing",
                "node_id": extraction_id,
                "reason": "Extraction has no extracts edge to its target.",
            })
        extras = sorted({
            str(edge.get("tgt")) for edge in links
            if edge.get("tgt") != target_id
        })
        if extras:
            warnings.append({
                "code": "extracts_link_extra",
                "node_id": extraction_id,
                "targets": extras,
                "reason": "Extraction also links to other targets.",
            })

    present_fields = [
        field for field in EXTRACTION_FIELDS
        if _field_present(fields.get(field))
    ]
    counts = {
        "fields_present": len(present_fields),
        "fields_total": len(EXTRACTION_FIELDS),
        "outcomes": len(outcomes) if isinstance(outcomes, list) else 0,
        "extracts_links": len(links),
        "issues": len(issues),
        "warnings": len(warnings),
    }
    return {
        "extraction_id": extraction_id,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": counts,
        "truth_assessed": False,
    }


def extraction_table(state: Any, format: str = "markdown") -> dict[str, Any]:
    """Project all extractions into a deterministic table with source locators."""
    current = _as_state(state)
    nodes = sorted(
        (node for node in _nodes(current) if node.get("type") == "Extraction"),
        key=lambda node: str(node.get("id") or ""),
    )
    rows = [_table_row(node) for node in nodes]
    if not isinstance(format, str):
        raise ExtractionError("extraction table format must be markdown or csv")
    normalized = format.casefold()
    columns = list(EXTRACTION_TABLE_COLUMNS)
    if normalized in {"csv", "text/csv"}:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=columns,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        content = output.getvalue()
        output_format = "csv"
    elif normalized in {"markdown", "md"}:
        lines = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for row in rows:
            lines.append(
                "| " + " | ".join(_escape_markdown(row[column]) for column in columns) + " |"
            )
        content = "\n".join(lines) + "\n"
        output_format = "markdown"
    else:
        raise ExtractionError("extraction table format must be markdown or csv")
    return {
        "format": output_format,
        "content": content,
        "rows": rows,
        "count": len(rows),
        "columns": columns,
        "truth_assessed": False,
    }


def extraction_report(state: Any, extraction_id: str) -> dict[str, Any]:
    """Return one extraction node, its target, links and mechanical checks."""
    current = _as_state(state)
    node = _require_extraction(current, extraction_id)
    target_id = node.get("target_id")
    target = (
        _node_by_id(current, target_id)
        if isinstance(target_id, str) and target_id
        else None
    )
    links = [
        edge for edge in _edges(current)
        if edge.get("type") == "extracts" and edge.get("src") == extraction_id
    ]
    return {
        "extraction_id": extraction_id,
        "node": dict(node),
        "target": dict(target) if target is not None else None,
        "links": [dict(edge) for edge in links],
        "checks": check_extraction(current, extraction_id),
        "truth_assessed": False,
    }
