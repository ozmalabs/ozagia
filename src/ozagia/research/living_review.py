"""Living-review alerts derived from recorded metadata only.

Scans never fetch providers, open cached artifacts, or read source files. They
turn recorded retraction state, citation metadata, provenance timestamps and
source-locator status into explicit observations. An alert is a prompt to review,
not an automatic edit, retraction or truth judgement.
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ozagia.research.graph import ResearchError

ALERT_STATUSES = frozenset({"open", "acknowledged", "dismissed"})
ALERT_KINDS = frozenset(
    {
        "retracted_reference",
        "expression_of_concern",
        "claim_cites_retracted",
        "unresolved_citation",
        "missing_identifier",
        "new_citation_since_plan",
        "source_unavailable",
    }
)
_ALERT_TARGET_TYPES = frozenset({"Reference", "Claim", "Source", "ReviewPlan"})
_CITATION_NODE_TYPES = frozenset({"Claim", "Evidence"})
_UNAVAILABLE_LOCATOR_STATUSES = frozenset({"stale", "missing"})


class LivingReviewError(ResearchError):
    """A living-review scan, alert or acknowledgement is invalid."""


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise LivingReviewError("living-review operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _collapse(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LivingReviewError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LivingReviewError(f"{field} must be a string")
    return value.strip()


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
        raise LivingReviewError(f"value must be JSON-serializable: {exc}") from exc


def _json_safe(value: Any, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise LivingReviewError(f"{field} must be JSON-serializable: {exc}") from exc


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LivingReviewError(f"{field} must be a non-empty ISO timestamp")
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LivingReviewError(f"{field} must be an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value, field)


def _citation_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("citations", "citation_keys"):
        raw = node.get(field)
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for item in values:
            if not isinstance(item, str):
                continue
            key = item.strip().lstrip("@").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _reference_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("citation_key", "citation_keys"):
        raw = node.get(field)
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for item in values:
            if not isinstance(item, str):
                continue
            key = item.strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _reference_key_index(
    nodes: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        if node.get("type") != "Reference":
            continue
        for key in _reference_keys(node):
            index.setdefault(key.casefold(), node)
    return index


def _locators(node: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[Any] = []
    for field in ("locator", "source_locator"):
        value = node.get(field)
        if isinstance(value, Mapping):
            values.append(value)
    for field in ("locators", "source_locators"):
        value = node.get(field)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, Mapping))
    locators: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        clean = {
            str(key): item
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
        if not clean:
            continue
        signature = _canonical(clean)
        if signature in seen:
            continue
        seen.add(signature)
        locators.append(clean)
    return sorted(locators, key=_canonical)


def _references(
    nodes: Sequence[Mapping[str, Any]],
    reference_ids: set[str] | None,
) -> list[Mapping[str, Any]]:
    selected = [node for node in nodes if node.get("type") == "Reference"]
    if reference_ids is not None:
        selected = [node for node in selected if str(node["id"]) in reference_ids]
    return sorted(selected, key=lambda node: str(node["id"]))


def _retraction_status(node: Mapping[str, Any]) -> str:
    retraction = node.get("retraction")
    if not isinstance(retraction, Mapping):
        return ""
    status = retraction.get("status")
    return _collapse(status).casefold() if isinstance(status, str) else ""


def _reference_filter(value: Any) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise LivingReviewError("reference_ids must be a list of Reference ids")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LivingReviewError("reference_ids must contain non-empty node ids")
        result.add(item.strip())
    return result


def _alert(
    kind: str,
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "target_id": str(target["id"]),
        "target_type": str(target.get("type") or ""),
        "payload": dict(payload),
        "truth_assessed": False,
    }


def _alert_sort_key(alert: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(alert.get("kind")),
        str(alert.get("target_id")),
        _canonical(alert.get("payload")),
    )


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #
def scan_alerts(
    state: Any,
    *,
    review_plan_id: str | None = None,
    since: str | None = None,
    reference_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return deterministic living-review alerts without writing or fetching."""
    current = _as_state(state)
    nodes = _nodes(current)
    edges = _edges(current)
    by_id = {str(node["id"]): node for node in nodes}
    selected_refs = _reference_filter(reference_ids)
    since_value = since.strip() if isinstance(since, str) and since.strip() else since
    since_time = _optional_timestamp(since_value, "since")

    plan = None
    if review_plan_id is not None:
        plan_id = _nonempty(review_plan_id, "review_plan_id")
        plan = by_id.get(plan_id)
        if plan is None or plan.get("type") != "ReviewPlan":
            raise LivingReviewError(f"unknown ReviewPlan id: {plan_id}")
    plan_time = (
        _optional_timestamp(plan.get("created_at"), "review plan created_at")
        if plan is not None
        else None
    )
    thresholds = [value for value in (since_time, plan_time) if value is not None]
    threshold = max(thresholds) if thresholds else None

    alerts: list[dict[str, Any]] = []
    reference_index = _reference_key_index(nodes)
    all_retracted = {
        str(node["id"]): node
        for node in _references(nodes, None)
        if _retraction_status(node) == "retracted"
    }

    # Retracted works and expressions of concern.
    for reference in _references(nodes, selected_refs):
        status = _retraction_status(reference)
        if status not in {"retracted", "concern"}:
            continue
        kind = "retracted_reference" if status == "retracted" else "expression_of_concern"
        retraction = reference.get("retraction") or {}
        payload: dict[str, Any] = {
            "retraction_status": status,
            "citation_key": _reference_keys(reference)[0] if _reference_keys(reference) else None,
        }
        if isinstance(retraction, Mapping):
            for field in ("provider", "checked_at"):
                if retraction.get(field):
                    payload[field] = retraction[field]
            if retraction.get("notices"):
                payload["notices"] = retraction["notices"]
        alerts.append(_alert(kind, reference, payload))

    # Claims that cite a retracted work, by edge or by recorded citation key.
    cite_signals: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        if edge.get("type") != "cites":
            continue
        source = by_id.get(str(edge.get("src")))
        target = by_id.get(str(edge.get("tgt")))
        if source is None or target is None or source.get("type") != "Claim":
            continue
        if target.get("type") != "Reference":
            continue
        if str(target["id"]) not in all_retracted:
            continue
        key = (str(source["id"]), str(target["id"]))
        entry = cite_signals.setdefault(
            key,
            {"claim": source, "reference": target, "signals": set(), "edge_ids": []},
        )
        entry["signals"].add("cites_edge")
        if edge.get("id"):
            entry["edge_ids"].append(str(edge["id"]))
    for claim in sorted(
        (node for node in nodes if node.get("type") == "Claim"),
        key=lambda node: str(node["id"]),
    ):
        for citation_key in _citation_keys(claim):
            reference = reference_index.get(citation_key.casefold())
            if reference is None or str(reference["id"]) not in all_retracted:
                continue
            key = (str(claim["id"]), str(reference["id"]))
            entry = cite_signals.setdefault(
                key,
                {"claim": claim, "reference": reference, "signals": set(), "edge_ids": []},
            )
            entry["signals"].add("citation_key")
    for claim_id, reference_id in sorted(cite_signals):
        if selected_refs is not None and reference_id not in selected_refs:
            continue
        entry = cite_signals[(claim_id, reference_id)]
        alerts.append(
            _alert(
                "claim_cites_retracted",
                entry["claim"],
                {
                    "reference_id": reference_id,
                    "citation_key": (
                        _reference_keys(entry["reference"])[0]
                        if _reference_keys(entry["reference"])
                        else None
                    ),
                    "signals": sorted(entry["signals"]),
                    "edge_ids": sorted(entry["edge_ids"]),
                },
            )
        )

    # Citation keys with no Reference record.
    for node in sorted(
        (item for item in nodes if item.get("type") in _CITATION_NODE_TYPES),
        key=lambda item: str(item["id"]),
    ):
        for citation_key in _citation_keys(node):
            if citation_key.casefold() in reference_index:
                continue
            alerts.append(
                _alert(
                    "unresolved_citation",
                    node,
                    {"citation_key": citation_key},
                )
            )

    # References with no stable identifier.
    for reference in _references(nodes, selected_refs):
        if reference.get("identifiers"):
            continue
        alerts.append(
            _alert(
                "missing_identifier",
                reference,
                {
                    "citation_key": (
                        _reference_keys(reference)[0] if _reference_keys(reference) else None
                    ),
                    "title": _collapse(reference.get("title")) or None,
                },
            )
        )

    # New provider-asserted citations recorded after the plan or cutoff.
    if threshold is not None:
        for edge in sorted(edges, key=lambda item: str(item.get("id"))):
            if edge.get("type") != "cites_work":
                continue
            provenance = edge.get("provenance")
            if not isinstance(provenance, Mapping):
                continue
            retrieved_at = provenance.get("retrieved_at")
            if not isinstance(retrieved_at, str) or not retrieved_at.strip():
                continue
            try:
                retrieved_time = _timestamp(retrieved_at, "provenance.retrieved_at")
            except LivingReviewError:
                continue
            if retrieved_time <= threshold:
                continue
            target = by_id.get(str(edge.get("tgt")))
            if target is None or target.get("type") != "Reference":
                continue
            if selected_refs is not None:
                endpoints = {str(edge.get("src")), str(edge.get("tgt"))}
                if not endpoints & selected_refs:
                    continue
            alerts.append(
                _alert(
                    "new_citation_since_plan",
                    target,
                    {
                        "edge_id": str(edge.get("id") or ""),
                        "source_reference_id": str(edge.get("src") or ""),
                        "retrieved_at": retrieved_at.strip(),
                        "threshold": threshold.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "plan_id": str(plan["id"]) if plan is not None else None,
                    },
                )
            )

    # Source locators that already record stale or missing availability.
    for source in sorted(
        (node for node in nodes if node.get("type") == "Source"),
        key=lambda node: str(node["id"]),
    ):
        found = False
        for locator in _locators(source):
            status = _collapse(locator.get("status")).casefold()
            if status not in _UNAVAILABLE_LOCATOR_STATUSES:
                continue
            found = True
            alerts.append(
                _alert(
                    "source_unavailable",
                    source,
                    {"locator": locator, "status": status},
                )
            )
        if found:
            continue
        node_status = _collapse(source.get("status")).casefold()
        availability = source.get("availability")
        if isinstance(availability, Mapping):
            node_status = node_status or _collapse(availability.get("status")).casefold()
        if node_status in _UNAVAILABLE_LOCATOR_STATUSES:
            alerts.append(
                _alert(
                    "source_unavailable",
                    source,
                    {"locator": None, "status": node_status},
                )
            )

    alerts.sort(key=_alert_sort_key)
    counts: dict[str, int] = {}
    for alert in alerts:
        kind = str(alert["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "alerts": alerts,
        "count": len(alerts),
        "kinds": sorted(counts),
        "counts_by_kind": dict(sorted(counts.items())),
        "plan_id": str(plan["id"]) if plan is not None else None,
        "since": since_value,
        "threshold": threshold.strftime("%Y-%m-%dT%H:%M:%SZ") if threshold else None,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# recording and acknowledgement
# --------------------------------------------------------------------------- #
def record_alert(
    graph: Any,
    *,
    kind: str,
    target_id: str,
    payload: Any = None,
    reviewer: str | None = None,
    status: str = "open",
) -> dict[str, Any]:
    """Record one observed alert against an existing graph target."""
    state = graph.state()
    kind = _nonempty(kind, "kind")
    if status not in ALERT_STATUSES:
        raise LivingReviewError(f"status must be one of {sorted(ALERT_STATUSES)}")
    if not isinstance(target_id, str) or not target_id.strip():
        raise LivingReviewError("target_id must be a non-empty node id")
    target_id = target_id.strip()
    target = _node_by_id(state, target_id)
    if target is None:
        raise LivingReviewError(f"unknown alert target id: {target_id}")
    if target.get("type") not in _ALERT_TARGET_TYPES:
        raise LivingReviewError(
            f"alert target must be one of {sorted(_ALERT_TARGET_TYPES)}, "
            f"not {target.get('type')}"
        )
    reviewer = _optional_text(reviewer, "reviewer")
    if payload is not None:
        payload = _json_safe(payload, "payload")

    fields: dict[str, Any] = {
        "kind": kind,
        "target_id": target_id,
        "payload": payload,
        "reviewer": reviewer,
        "status": status,
        "detected_at": _utc(),
        "truth_assessed": False,
    }
    alert = graph.record("Alert", **fields)
    provenance: dict[str, Any] = {"kind": "alert"}
    if reviewer is not None:
        provenance["reviewer"] = reviewer
    link = graph.link(
        alert["id"],
        "alerts",
        target_id,
        provenance=provenance,
        rationale=f"Alert {kind!r} refers to this {target.get('type')}.",
    )
    return {
        "alert": alert,
        "target": dict(target),
        "link": link,
        "truth_assessed": False,
    }


def acknowledge_alert(
    graph: Any,
    alert_id: str,
    *,
    reviewer: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Acknowledge an existing Alert while preserving what was recorded."""
    state = graph.state()
    if not isinstance(alert_id, str) or not alert_id.strip():
        raise LivingReviewError("alert_id must be a non-empty node id")
    alert_id = alert_id.strip()
    alert = _node_by_id(state, alert_id)
    if alert is None or alert.get("type") != "Alert":
        raise LivingReviewError(f"unknown Alert id: {alert_id}")
    reviewer = _nonempty(reviewer, "reviewer")
    note = _optional_text(note, "note")
    acknowledged_at = _utc()
    updated = graph.amend(
        alert_id,
        status="acknowledged",
        reviewer=reviewer,
        note=note,
        acknowledged_at=acknowledged_at,
    )
    return {
        "alert": updated,
        "previous_status": alert.get("status"),
        "acknowledged_at": acknowledged_at,
        "truth_assessed": False,
    }


def alert_report(state: Any, *, status: str | None = None) -> dict[str, Any]:
    """Summarize recorded alerts and the plans they point at."""
    current = _as_state(state)
    if status is not None and status not in ALERT_STATUSES:
        raise LivingReviewError(f"status must be one of {sorted(ALERT_STATUSES)}")
    nodes = _nodes(current)
    by_id = {str(node["id"]): node for node in nodes}
    alerts = sorted(
        (node for node in nodes if node.get("type") == "Alert"),
        key=lambda node: str(node["id"]),
    )
    filtered = [
        alert for alert in alerts
        if status is None or alert.get("status") == status
    ]
    open_alerts = [alert for alert in alerts if alert.get("status") == "open"]
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {item: 0 for item in sorted(ALERT_STATUSES)}
    for alert in alerts:
        kind = str(alert.get("kind") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        alert_status = str(alert.get("status") or "")
        by_status[alert_status] = by_status.get(alert_status, 0) + 1
    plan_ids = sorted(
        {
            str(alert.get("target_id"))
            for alert in alerts
            if (by_id.get(str(alert.get("target_id"))) or {}).get("type") == "ReviewPlan"
        }
    )
    plans = sorted(
        (by_id[plan_id] for plan_id in plan_ids if plan_id in by_id),
        key=lambda node: (str(node.get("created_at") or ""), str(node["id"])),
    )
    plan = dict(plans[-1]) if plans else None
    return {
        "alerts": [dict(alert) for alert in filtered],
        "count": len(filtered),
        "counts": {
            "alerts": len(alerts),
            "filtered": len(filtered),
            "open": len(open_alerts),
            "acknowledged": by_status.get("acknowledged", 0),
            "dismissed": by_status.get("dismissed", 0),
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": by_status,
        },
        "by_kind": dict(sorted(by_kind.items())),
        "by_status": by_status,
        "counts_by_kind": dict(sorted(by_kind.items())),
        "counts_by_status": by_status,
        "open_alerts": [dict(alert) for alert in open_alerts],
        "open_count": len(open_alerts),
        "plan_ids": plan_ids,
        "plan_id": plan_ids[-1] if plan_ids else None,
        "plans": [dict(item) for item in plans],
        "plan": plan,
        "status": status,
        "truth_assessed": False,
    }
