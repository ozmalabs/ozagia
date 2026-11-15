"""Risk-of-bias and certainty appraisal over the research graph.

These operations record reviewer judgements.  They do not assess truth, infer a
certainty rating, pool effects, or turn a high risk-of-bias judgement into an
automatic rejection.  A domain without recorded support is reported by the
mechanical check rather than silently accepted.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ozagia.research.graph import ResearchError

RISK_OF_BIAS_JUDGEMENTS = ("low", "some_concerns", "high", "unclear", "not_applicable")
CERTAINTY_RATINGS = ("high", "moderate", "low", "very_low")
RISK_OF_BIAS_TOOLS = ("RoB 2", "ROBINS-I", "custom")

RISK_OF_BIAS_TARGET_TYPES = frozenset({"Reference", "Claim", "Extraction"})
CERTAINTY_TARGET_TYPES = frozenset({"Claim", "Reference", "ReviewPlan"})
_APPRAISAL_TARGET_TYPES = RISK_OF_BIAS_TARGET_TYPES | CERTAINTY_TARGET_TYPES

_HIGH_RISK_JUDGEMENTS = frozenset({"high", "some_concerns"})
_DOMAIN_FIELDS = frozenset({"name", "judgement", "support", "reason", "source_locator"})
_ADJUSTMENT_FIELDS = frozenset({"domain", "level", "reason"})

_JUDGEMENT_ALIASES = {
    "low": "low",
    "some concerns": "some_concerns",
    "someconcerns": "some_concerns",
    "high": "high",
    "unclear": "unclear",
    "not applicable": "not_applicable",
    "notapplicable": "not_applicable",
    "na": "not_applicable",
    "n/a": "not_applicable",
}
_RATING_ALIASES = {
    "high": "high",
    "moderate": "moderate",
    "low": "low",
    "very low": "very_low",
    "verylow": "very_low",
}
_TOOL_ALIASES = {
    "rob 2": "RoB 2",
    "rob2": "RoB 2",
    "rob 2 0": "RoB 2",
    "rob2 0": "RoB 2",
    "rob ii": "RoB 2",
    "rob v2": "RoB 2",
    "risk of bias 2": "RoB 2",
    "riskofbias2": "RoB 2",
    "cochrane rob 2": "RoB 2",
    "robins i": "ROBINS-I",
    "robinsi": "ROBINS-I",
    "robins": "ROBINS-I",
    "robins i tool": "ROBINS-I",
    "robins i framework": "ROBINS-I",
    "risk of bias in non randomised studies of interventions": "ROBINS-I",
    "risk of bias in nonrandomized studies of interventions": "ROBINS-I",
    "custom": "custom",
}
_APPRAISAL_COLUMNS = (
    "record_id",
    "kind",
    "target_id",
    "target_type",
    "target",
    "tool",
    "judgement",
    "reviewer",
    "support_summary",
    "status",
)
_ROW_KIND_ORDER = {"risk_of_bias": 0, "certainty": 1}


class AppraisalError(ResearchError):
    """A risk-of-bias or certainty judgement is invalid."""


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _collapse(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _slug(value: Any) -> str:
    text = str(value).strip().casefold()
    for character in ("_", "-", "."):
        text = text.replace(character, " ")
    return " ".join(text.split())


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppraisalError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _required_items(value: Any, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AppraisalError(f"{field} must be a non-empty list")
    items = list(value)
    if not items:
        raise AppraisalError(f"{field} must be a non-empty list")
    return items


def _optional_items(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AppraisalError(f"{field} must be a list")
    return list(value)


def _json_safe(value: Any, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise AppraisalError(f"{field} must be JSON-serializable: {exc}") from exc


def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise AppraisalError("appraisal operations need a graph state mapping")


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
    if not isinstance(node_id, str) or not node_id.strip():
        raise AppraisalError(f"{label} must be a non-empty node id")
    node = _node_by_id(state, node_id)
    if node is None:
        raise AppraisalError(f"unknown {label} id: {node_id}")
    if node.get("type") not in expected:
        raise AppraisalError(
            f"{label} {node_id} must be one of {sorted(expected)}, not {node.get('type')}"
        )
    return node


def _linked_target_ids(state: Mapping[str, Any], node_id: str, relation: str) -> set[str]:
    return {
        str(edge["tgt"])
        for edge in _edges(state)
        if edge.get("type") == relation and edge.get("src") == node_id and edge.get("tgt")
    }


def _target_ids(
    state: Mapping[str, Any], node: Mapping[str, Any], relation: str
) -> set[str]:
    ids: set[str] = set()
    raw = node.get("target_id")
    if isinstance(raw, str) and raw.strip():
        ids.add(raw.strip())
    ids.update(_linked_target_ids(state, str(node.get("id")), relation))
    return ids


def _target_label(node: Mapping[str, Any] | None, target_id: str) -> str:
    if node is None:
        return f"unknown target {target_id}" if target_id else "unknown target"
    for field in ("wording", "title", "question", "study", "name", "raw", "text", "summary"):
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            return _collapse(value)
    return str(node.get("id") or target_id)


def _canonical_judgement(value: Any, field: str = "judgement") -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppraisalError(
            f"{field} must be one of {list(RISK_OF_BIAS_JUDGEMENTS)}"
        )
    canonical = _JUDGEMENT_ALIASES.get(_slug(value))
    if canonical is None:
        raise AppraisalError(
            f"{field} must be one of {list(RISK_OF_BIAS_JUDGEMENTS)}, not {value!r}"
        )
    return canonical


def _try_judgement(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _JUDGEMENT_ALIASES.get(_slug(value))


def _canonical_rating(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AppraisalError(f"rating must be one of {list(CERTAINTY_RATINGS)}")
    canonical = _RATING_ALIASES.get(_slug(value))
    if canonical is None:
        raise AppraisalError(
            f"rating must be one of {list(CERTAINTY_RATINGS)}, not {value!r}"
        )
    return canonical


def _canonical_framework(value: Any) -> str:
    framework = _nonempty_string(value, "framework")
    if _slug(framework) in {"grade", "grade certainty", "grade approach"}:
        return "GRADE"
    return framework


def _canonical_tool(value: Any) -> tuple[str, str | None]:
    raw = _nonempty_string(value, "tool")
    canonical = _TOOL_ALIASES.get(_slug(raw))
    if canonical is not None:
        return canonical, None
    # Any other non-empty label is a custom checklist.  Keep the label so the
    # original framework remains visible while counts stay groupable.
    return "custom", raw


def _stored_tool(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value.strip():
        return "unknown", None
    return _canonical_tool(value)


def _has_support(domain: Mapping[str, Any]) -> bool:
    for field in ("support", "reason"):
        value = domain.get(field)
        if isinstance(value, str) and value.strip():
            return True
        if value is not None and not isinstance(value, str) and value:
            return True
    locator = domain.get("source_locator")
    return bool(locator)


def _locator_summary(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _collapse(value)
    if isinstance(value, Mapping):
        path = value.get("path") or value.get("url") or value.get("source")
        page = value.get("page") or value.get("start_line")
        end = value.get("end_line")
        if path:
            label = str(path)
            if page:
                label += f":{page}"
                if end and end != page:
                    label += f"-{end}"
            return _collapse(label)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _location(value: Any, field: str) -> Any:
    if isinstance(value, str):
        return _nonempty_string(value, field)
    if isinstance(value, Mapping):
        if not value:
            raise AppraisalError(f"{field} must not be empty")
        return _json_safe(dict(value), field)
    raise AppraisalError(f"{field} must be a string or mapping")


# --------------------------------------------------------------------------- #
# domain validation
# --------------------------------------------------------------------------- #
def _normalise_domains(value: Any) -> list[dict[str, Any]]:
    items = _required_items(value, "domains")
    domains: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise AppraisalError(f"domains[{index}] must be a mapping")
        unknown = set(item) - _DOMAIN_FIELDS
        if unknown:
            raise AppraisalError(
                f"domains[{index}] has unknown fields: {sorted(unknown)}"
            )
        name = _nonempty_string(item.get("name"), f"domains[{index}].name")
        key = _collapse(name).casefold()
        if key in seen:
            raise AppraisalError(f"domains[{index}] repeats the domain name {name!r}")
        seen.add(key)
        judgement = _canonical_judgement(
            item.get("judgement"), f"domains[{index}].judgement"
        )
        domain: dict[str, Any] = {"name": name, "judgement": judgement}
        has_support = "support" in item
        has_reason = "reason" in item
        if has_support and has_reason:
            raise AppraisalError(
                f"domains[{index}] must use support or reason, not both"
            )
        if has_support:
            domain["support"] = _nonempty_string(
                item["support"], f"domains[{index}].support"
            )
        elif has_reason:
            domain["support"] = _nonempty_string(
                item["reason"], f"domains[{index}].reason"
            )
        if "source_locator" in item:
            domain["source_locator"] = _location(
                item["source_locator"], f"domains[{index}].source_locator"
            )
        _json_safe(domain, f"domains[{index}]")
        domains.append(domain)
    return domains


def _normalise_adjustments(value: Any, field: str) -> list[dict[str, Any]]:
    items = _optional_items(value, field)
    adjustments: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise AppraisalError(f"{field}[{index}] must be a mapping")
        unknown = set(item) - _ADJUSTMENT_FIELDS
        if unknown:
            raise AppraisalError(
                f"{field}[{index}] has unknown fields: {sorted(unknown)}"
            )
        domain = _nonempty_string(item.get("domain"), f"{field}[{index}].domain")
        level = item.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or level < 0:
            raise AppraisalError(f"{field}[{index}].level must be a non-negative integer")
        reason = _nonempty_string(item.get("reason"), f"{field}[{index}].reason")
        adjustment = {"domain": domain, "level": level, "reason": reason}
        _json_safe(adjustment, f"{field}[{index}]")
        adjustments.append(adjustment)
    return adjustments


# --------------------------------------------------------------------------- #
# risk of bias
# --------------------------------------------------------------------------- #
def record_risk_of_bias(
    graph: Any,
    *,
    target_id: str,
    tool: str,
    domains: Sequence[Mapping[str, Any]],
    overall: str,
    reviewer: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record one reviewer's risk-of-bias assessment as a candidate."""
    state = graph.state()
    target = _require_node(
        state, target_id, RISK_OF_BIAS_TARGET_TYPES, "risk-of-bias target"
    )
    canonical_tool, tool_label = _canonical_tool(tool)
    normalised_domains = _normalise_domains(domains)
    canonical_overall = _canonical_judgement(overall, "overall")
    reviewer = _nonempty_string(reviewer, "reviewer")
    notes = _optional_string(notes, "notes")

    fields: dict[str, Any] = {
        "target_id": target["id"],
        "tool": canonical_tool,
        "domains": normalised_domains,
        "overall": canonical_overall,
        "reviewer": reviewer,
        "status": "candidate",
        "truth_assessed": False,
    }
    if tool_label is not None:
        fields["tool_label"] = tool_label
    if notes is not None:
        fields["notes"] = notes
    _json_safe(fields, "risk_of_bias")

    assessment = graph.record("RiskOfBias", **fields)
    edge = graph.link(
        assessment["id"],
        "assesses",
        target["id"],
        provenance={
            "kind": "risk_of_bias_assessment",
            "tool": canonical_tool,
            "reviewer": reviewer,
        },
        rationale=(
            "A reviewer judged this record's risk of bias; the assessment is a "
            "candidate judgement, not a truth verdict."
        ),
    )
    return {
        "assessment": assessment,
        "target": dict(target),
        "edge": edge,
        "truth_assessed": False,
    }


def _check_risk_node(
    state: Mapping[str, Any], node: Mapping[str, Any]
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    assessment_id = str(node.get("id") or "")

    target_id = node.get("target_id")
    target: Mapping[str, Any] | None = None
    if not isinstance(target_id, str) or not target_id.strip():
        issues.append({
            "code": "target_missing",
            "assessment_id": assessment_id,
            "reason": "RiskOfBias has no target_id.",
        })
    else:
        target_id = target_id.strip()
        target = _node_by_id(state, target_id)
        if target is None:
            issues.append({
                "code": "target_missing",
                "assessment_id": assessment_id,
                "target_id": target_id,
                "reason": f"RiskOfBias target {target_id} does not exist.",
            })
        elif target.get("type") not in RISK_OF_BIAS_TARGET_TYPES:
            issues.append({
                "code": "target_type_invalid",
                "assessment_id": assessment_id,
                "target_id": target_id,
                "reason": (
                    f"RiskOfBias target {target_id} is a {target.get('type')}, not "
                    f"one of {sorted(RISK_OF_BIAS_TARGET_TYPES)}."
                ),
            })

    judgement_counts = {judgement: 0 for judgement in RISK_OF_BIAS_JUDGEMENTS}
    supported_domains = 0
    high_risk_domains = 0
    domains = node.get("domains")
    if not isinstance(domains, list) or not domains:
        issues.append({
            "code": "domains_missing",
            "assessment_id": assessment_id,
            "reason": "RiskOfBias has no domain judgements.",
        })
        domains = []
    seen_names: dict[str, int] = {}
    for index, domain in enumerate(domains):
        if not isinstance(domain, Mapping):
            issues.append({
                "code": "domain_malformed",
                "assessment_id": assessment_id,
                "index": index,
                "reason": f"Domain {index} is not a mapping.",
            })
            continue
        name = domain.get("name")
        label = _collapse(name) or f"domain {index}"
        if not isinstance(name, str) or not name.strip():
            issues.append({
                "code": "domain_name_missing",
                "assessment_id": assessment_id,
                "index": index,
                "reason": f"Domain {index} has no name.",
            })
        else:
            key = _collapse(name).casefold()
            if key in seen_names:
                issues.append({
                    "code": "domain_name_duplicate",
                    "assessment_id": assessment_id,
                    "index": index,
                    "domain": name.strip(),
                    "reason": (
                        f"Domain {index} repeats the name from domain "
                        f"{seen_names[key]}."
                    ),
                })
            else:
                seen_names[key] = index
        judgement = _try_judgement(domain.get("judgement"))
        if judgement is None:
            issues.append({
                "code": "domain_judgement_invalid",
                "assessment_id": assessment_id,
                "index": index,
                "domain": label,
                "reason": (
                    f"Domain {index} needs a judgement in "
                    f"{list(RISK_OF_BIAS_JUDGEMENTS)}."
                ),
            })
        else:
            judgement_counts[judgement] += 1
            if judgement in _HIGH_RISK_JUDGEMENTS:
                high_risk_domains += 1
        if _has_support(domain):
            supported_domains += 1
        else:
            warnings.append({
                "code": "domain_support_missing",
                "assessment_id": assessment_id,
                "index": index,
                "domain": label,
                "reason": (
                    f"Domain {label!r} has no support, reason or source_locator."
                ),
            })

    overall = node.get("overall")
    canonical_overall = _try_judgement(overall)
    if not isinstance(overall, str) or not overall.strip():
        issues.append({
            "code": "overall_missing",
            "assessment_id": assessment_id,
            "reason": "RiskOfBias has no overall judgement.",
        })
    elif canonical_overall is None:
        issues.append({
            "code": "overall_invalid",
            "assessment_id": assessment_id,
            "overall": overall,
            "reason": (
                f"Overall judgement must be one of {list(RISK_OF_BIAS_JUDGEMENTS)}."
            ),
        })
    elif canonical_overall in _HIGH_RISK_JUDGEMENTS and high_risk_domains == 0:
        warnings.append({
            "code": "overall_without_domain_signal",
            "assessment_id": assessment_id,
            "overall": canonical_overall,
            "reason": (
                f"Overall judgement is {canonical_overall}, but no domain is "
                "high or some_concerns."
            ),
        })

    return {
        "assessment_id": assessment_id,
        "target_id": target_id if isinstance(target_id, str) else None,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "domains": len(domains),
            "supported_domains": supported_domains,
            "high_risk_domains": high_risk_domains,
            "judgements": judgement_counts,
            "issues": len(issues),
            "warnings": len(warnings),
        },
        "truth_assessed": False,
    }


def check_risk_of_bias(state: Any, assessment_id: str) -> dict[str, Any]:
    """Check one recorded assessment without changing its judgement."""
    current = _as_state(state)
    node = _node_by_id(current, assessment_id)
    if node is None:
        raise AppraisalError(f"unknown risk-of-bias assessment id: {assessment_id}")
    if node.get("type") != "RiskOfBias":
        raise AppraisalError(
            f"{assessment_id} is a {node.get('type')}, not a RiskOfBias assessment"
        )
    return _check_risk_node(current, node)


# --------------------------------------------------------------------------- #
# certainty
# --------------------------------------------------------------------------- #
def record_certainty(
    graph: Any,
    *,
    target_id: str,
    rating: str,
    reasons: Sequence[str],
    reviewer: str,
    framework: str = "GRADE",
    downgrades: Sequence[Mapping[str, Any]] | None = None,
    upgrades: Sequence[Mapping[str, Any]] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record one reviewer's certainty rating and its reasons as a candidate."""
    state = graph.state()
    target = _require_node(
        state, target_id, CERTAINTY_TARGET_TYPES, "certainty target"
    )
    canonical_rating = _canonical_rating(rating)
    canonical_framework = _canonical_framework(framework)
    reason_list: list[str] = []
    for index, item in enumerate(_required_items(reasons, "reasons")):
        reason = _nonempty_string(item, f"reasons[{index}]")
        if reason not in reason_list:
            reason_list.append(reason)
    reviewer = _nonempty_string(reviewer, "reviewer")
    downgrade_list = _normalise_adjustments(downgrades, "downgrades")
    upgrade_list = _normalise_adjustments(upgrades, "upgrades")
    notes = _optional_string(notes, "notes")

    fields: dict[str, Any] = {
        "target_id": target["id"],
        "framework": canonical_framework,
        "rating": canonical_rating,
        "reasons": reason_list,
        "downgrades": downgrade_list,
        "upgrades": upgrade_list,
        "reviewer": reviewer,
        "status": "candidate",
        "truth_assessed": False,
    }
    if notes is not None:
        fields["notes"] = notes
    _json_safe(fields, "certainty")

    certainty = graph.record("Certainty", **fields)
    edge = graph.link(
        certainty["id"],
        "grades",
        target["id"],
        provenance={
            "kind": "certainty_rating",
            "framework": canonical_framework,
            "reviewer": reviewer,
        },
        rationale=(
            "A reviewer assigned this certainty rating; it records judgement, "
            "not an automatic inference about truth."
        ),
    )
    return {
        "certainty": certainty,
        "target": dict(target),
        "edge": edge,
        "truth_assessed": False,
    }


def _risk_summary(state: Mapping[str, Any], node: Mapping[str, Any]) -> dict[str, Any]:
    check = _check_risk_node(state, node)
    target_id = node.get("target_id")
    target_id = target_id.strip() if isinstance(target_id, str) else ""
    target = _node_by_id(state, target_id) if target_id else None
    tool, tool_label = _stored_tool(node.get("tool"))
    stored_label = node.get("tool_label")
    if isinstance(stored_label, str) and stored_label.strip():
        tool_label = stored_label.strip()
    overall = node.get("overall")
    return {
        "id": node.get("id"),
        "target_id": target_id,
        "target_type": target.get("type") if target is not None else None,
        "target_label": _target_label(target, target_id),
        "tool": tool,
        "tool_label": tool_label,
        "overall": overall,
        "reviewer": node.get("reviewer"),
        "domain_count": check["counts"]["domains"],
        "judgements": check["counts"]["judgements"],
        "status": node.get("status"),
        "check": {
            "ok": check["ok"],
            "issues": check["issues"],
            "warnings": check["warnings"],
        },
    }


def certainty_report(state: Any, target_id: str | None = None) -> dict[str, Any]:
    """Report recorded certainty and risk-of-bias judgements; never infer either."""
    current = _as_state(state)
    selected_target: Mapping[str, Any] | None = None
    if target_id is not None:
        target_id = _nonempty_string(target_id, "target_id")
        selected_target = _node_by_id(current, target_id)
        if selected_target is None:
            raise AppraisalError(f"unknown target id: {target_id}")
        if selected_target.get("type") not in _APPRAISAL_TARGET_TYPES:
            raise AppraisalError(
                f"target {target_id} must be one of "
                f"{sorted(_APPRAISAL_TARGET_TYPES)}, not {selected_target.get('type')}"
            )

    certainty_nodes = sorted(
        (node for node in _nodes(current) if node.get("type") == "Certainty"),
        key=lambda node: str(node.get("id")),
    )
    risk_nodes = sorted(
        (node for node in _nodes(current) if node.get("type") == "RiskOfBias"),
        key=lambda node: str(node.get("id")),
    )
    if target_id is not None:
        certainty_nodes = [
            node for node in certainty_nodes
            if target_id in _target_ids(current, node, "grades")
        ]
        risk_nodes = [
            node for node in risk_nodes
            if target_id in _target_ids(current, node, "assesses")
        ]

    certainty_records = [dict(node) for node in certainty_nodes]
    risk_summaries = [_risk_summary(current, node) for node in risk_nodes]

    by_rating = {rating: 0 for rating in CERTAINTY_RATINGS}
    by_framework: dict[str, int] = {}
    for node in certainty_nodes:
        rating = _RATING_ALIASES.get(_slug(node.get("rating")))
        if rating is not None:
            by_rating[rating] += 1
        framework = _collapse(node.get("framework")) or "unknown"
        by_framework[framework] = by_framework.get(framework, 0) + 1
    by_framework = dict(sorted(by_framework.items()))

    by_tool = {tool: 0 for tool in RISK_OF_BIAS_TOOLS}
    for summary in risk_summaries:
        tool = str(summary["tool"])
        by_tool[tool] = by_tool.get(tool, 0) + 1

    risky_targets: dict[str, list[dict[str, Any]]] = {}
    for summary in risk_summaries:
        overall = _try_judgement(summary.get("overall"))
        target = str(summary.get("target_id") or "")
        if overall in _HIGH_RISK_JUDGEMENTS and target:
            risky_targets.setdefault(target, []).append(summary)

    warnings: list[dict[str, Any]] = []
    for node in certainty_nodes:
        rating = _RATING_ALIASES.get(_slug(node.get("rating")))
        if rating != "high":
            continue
        for target in sorted(_target_ids(current, node, "grades")):
            related = risky_targets.get(target, [])
            if not related:
                continue
            warnings.append({
                "code": "high_certainty_with_risk_of_bias",
                "certainty_id": node.get("id"),
                "target_id": target,
                "rating": "high",
                "risk_of_bias_ids": [summary["id"] for summary in related],
                "overall": sorted({
                    str(_try_judgement(summary.get("overall")))
                    for summary in related
                }),
                "reason": (
                    "High certainty is recorded while risk of bias is high or "
                    "some_concerns; review the judgements together."
                ),
            })

    return {
        "target_id": target_id,
        "target": dict(selected_target) if selected_target is not None else None,
        "certainty": certainty_records,
        "risk_of_bias": risk_summaries,
        "warnings": warnings,
        "counts": {
            "certainty": len(certainty_records),
            "risk_of_bias": len(risk_summaries),
            "by_rating": by_rating,
            "by_tool": by_tool,
            "by_framework": by_framework,
            "warnings": len(warnings),
        },
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# table projection
# --------------------------------------------------------------------------- #
def _risk_support_summary(node: Mapping[str, Any]) -> str:
    domains = node.get("domains")
    if not isinstance(domains, list) or not domains:
        return "no domain judgements recorded"
    parts: list[str] = []
    for domain in domains:
        if not isinstance(domain, Mapping):
            parts.append("malformed domain")
            continue
        name = _collapse(domain.get("name")) or "unnamed domain"
        judgement = _collapse(domain.get("judgement")) or "unknown judgement"
        basis = _collapse(domain.get("support") or domain.get("reason"))
        locator = _locator_summary(domain.get("source_locator"))
        detail = "; ".join(value for value in (basis, locator) if value)
        parts.append(f"{name}: {judgement} ({detail or 'no support'})")
    return "; ".join(parts)


def _certainty_support_summary(node: Mapping[str, Any]) -> str:
    parts = [
        _collapse(reason)
        for reason in (node.get("reasons") or [])
        if _collapse(reason)
    ]
    for field, label in (("downgrades", "downgrade"), ("upgrades", "upgrade")):
        items = node.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            domain = _collapse(item.get("domain")) or "unknown domain"
            level = item.get("level")
            reason = _collapse(item.get("reason"))
            text = f"{label} {domain} {level}"
            if reason:
                text += f": {reason}"
            parts.append(text)
    return "; ".join(parts) or "no reasons recorded"


def _row(
    state: Mapping[str, Any],
    node: Mapping[str, Any],
    kind: str,
) -> dict[str, str]:
    target_id = node.get("target_id")
    target_id = target_id.strip() if isinstance(target_id, str) else ""
    target = _node_by_id(state, target_id) if target_id else None
    if kind == "risk_of_bias":
        tool, tool_label = _stored_tool(node.get("tool"))
        stored_label = node.get("tool_label")
        if isinstance(stored_label, str) and stored_label.strip():
            tool_label = stored_label.strip()
        tool_text = tool_label or tool
        judgement = node.get("overall")
        support = _risk_support_summary(node)
    else:
        tool_text = _collapse(node.get("framework")) or "unknown framework"
        judgement = node.get("rating")
        support = _certainty_support_summary(node)
    return {
        "record_id": str(node.get("id") or ""),
        "kind": kind,
        "target_id": target_id,
        "target_type": str(target.get("type") or "") if target else "",
        "target": _target_label(target, target_id),
        "tool": tool_text,
        "judgement": _collapse(judgement),
        "reviewer": _collapse(node.get("reviewer")),
        "support_summary": _collapse(support),
        "status": _collapse(node.get("status")),
    }


def _escape_markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def appraisal_table(state: Any, format: str = "markdown") -> dict[str, Any]:
    """Project recorded appraisals into a deterministic evidence-style table."""
    current = _as_state(state)
    if not isinstance(format, str):
        raise AppraisalError("appraisal table format must be markdown or csv")
    rows = [
        _row(current, node, "risk_of_bias")
        for node in _nodes(current)
        if node.get("type") == "RiskOfBias"
    ]
    rows.extend(
        _row(current, node, "certainty")
        for node in _nodes(current)
        if node.get("type") == "Certainty"
    )
    rows.sort(key=lambda row: (
        _ROW_KIND_ORDER[row["kind"]],
        row["record_id"],
    ))

    normalized = format.casefold()
    if normalized in {"csv", "text/csv"}:
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=_APPRAISAL_COLUMNS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        content = output.getvalue()
        resolved = "csv"
    elif normalized in {"markdown", "md"}:
        lines = [
            "| " + " | ".join(_APPRAISAL_COLUMNS) + " |",
            "| " + " | ".join("---" for _ in _APPRAISAL_COLUMNS) + " |",
        ]
        for row in rows:
            lines.append(
                "| " + " | ".join(
                    _escape_markdown(row[column]) for column in _APPRAISAL_COLUMNS
                ) + " |"
            )
        content = "\n".join(lines) + "\n"
        resolved = "markdown"
    else:
        raise AppraisalError("appraisal table format must be markdown or csv")
    return {
        "format": resolved,
        "content": content,
        "rows": rows,
        "count": len(rows),
        "truth_assessed": False,
    }
