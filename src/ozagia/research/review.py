"""Evidence-table projection and review screening over the research graph."""
from __future__ import annotations

import csv
import io
from typing import Any, Mapping

from ozagia.research.graph import ResearchError


def _locator(node: Mapping[str, Any]) -> Mapping[str, Any]:
    value = node.get("source_locator") or node.get("locator")
    return value if isinstance(value, Mapping) else {}


def _source_label(node: Mapping[str, Any]) -> str:
    locator = _locator(node)
    path = locator.get("path")
    if not path:
        return ""
    lines = ""
    if locator.get("start_line"):
        lines = f":{locator['start_line']}"
        if locator.get("end_line") and locator["end_line"] != locator["start_line"]:
            lines += f"-{locator['end_line']}"
    return f"{path}{lines}"


def _citation_keys(node: Mapping[str, Any]) -> list[str]:
    value = node.get("citations") or node.get("citation_keys") or []
    return [str(item) for item in value] if isinstance(value, list) else []


def evidence_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project claims/evidence into a stable, source-located table."""
    references = {
        node["id"]: node for node in state.get("nodes", [])
        if node.get("type") == "Reference"
    }
    cited_by: dict[str, list[str]] = {}
    for edge in state.get("edges", []):
        if edge.get("type") == "cites" and edge.get("tgt") in references:
            cited_by.setdefault(edge.get("src"), []).append(
                str(references[edge["tgt"]].get("citation_key") or ""))
    rows = []
    for node in state.get("nodes", []):
        if node.get("type") not in {"Claim", "Evidence", "Observation"}:
            continue
        keys = _citation_keys(node)
        linked = [key for key in cited_by.get(node["id"], []) if key]
        rows.append({
            "record_id": node["id"],
            "kind": node["type"],
            "wording": node.get("wording") or node.get("text") or node.get("title") or "",
            "status": node.get("status", ""),
            "claim_type": node.get("claim_type", ""),
            "source": _source_label(node),
            "citation_keys": "; ".join(dict.fromkeys(keys)),
            "references": "; ".join(dict.fromkeys(linked)),
            "truth_assessed": False,
        })
    return rows


def _escape_markdown(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def evidence_table(state: Mapping[str, Any], *, format: str = "markdown") -> dict[str, Any]:
    rows = evidence_rows(state)
    columns = [
        "record_id", "kind", "wording", "status", "claim_type",
        "source", "citation_keys", "references",
    ]
    normalized = format.casefold()
    if normalized in {"csv", "text/csv"}:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        content = output.getvalue()
    elif normalized in {"markdown", "md"}:
        lines = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for row in rows:
            lines.append("| " + " | ".join(
                _escape_markdown(row[column]) for column in columns) + " |")
        content = "\n".join(lines) + "\n"
    else:
        raise ResearchError("evidence table format must be markdown or csv")
    return {
        "format": "csv" if normalized in {"csv", "text/csv"} else "markdown",
        "content": content,
        "rows": rows,
        "count": len(rows),
        "truth_assessed": False,
    }


def screen_reference(
    graph: Any,
    reference_id: str,
    *,
    stage: str,
    decision: str,
    reason: str,
    reviewer: str,
    criteria: Any = None,
) -> dict[str, Any]:
    """Record a reviewable screening decision; it is not an automatic filter."""
    state = graph.state()
    reference = next(
        (node for node in state.get("nodes", [])
         if node.get("id") == reference_id and node.get("type") == "Reference"),
        None,
    )
    if reference is None:
        raise ResearchError("screening needs an existing Reference id")
    if stage not in {"title_abstract", "full_text"}:
        raise ResearchError("screening stage must be title_abstract or full_text")
    if decision not in {"include", "exclude", "maybe"}:
        raise ResearchError("screening decision must be include, exclude or maybe")
    if not str(reason).strip():
        raise ResearchError("screening decision needs a reason")
    if not str(reviewer).strip():
        raise ResearchError("screening decision needs a reviewer")
    record = graph.record(
        "Decision",
        screening=True,
        reference_id=reference_id,
        stage=stage,
        decision=decision,
        reason=str(reason),
        reviewer=str(reviewer),
        criteria=criteria,
        truth_assessed=False,
    )
    graph.link(
        record["id"], "screens", reference_id,
        provenance={"kind": "screening_decision", "reviewer": reviewer, "stage": stage},
        rationale="A reviewer made this screening decision; it is not an evidence or support relation.",
    )
    prior = [
        node for node in state.get("nodes", [])
        if node.get("type") == "Decision" and node.get("screening") is True
        and node.get("reference_id") == reference_id and node.get("stage") == stage
    ]
    for previous in prior:
        graph.link(
            record["id"], "supersedes", previous["id"],
            provenance={"kind": "screening_revision", "stage": stage},
            rationale="A later screening decision supersedes this earlier decision.",
        )
    return {
        "decision": record,
        "reference": reference,
        "superseded": [node["id"] for node in prior],
        "truth_assessed": False,
    }


def screening_report(state: Mapping[str, Any]) -> dict[str, Any]:
    """PRISMA-style counts from recorded screening decisions, not inferences."""
    references = [node for node in state.get("nodes", []) if node.get("type") == "Reference"]
    decisions = [
        node for node in state.get("nodes", [])
        if node.get("type") == "Decision" and node.get("screening") is True
    ]
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for decision in decisions:
        latest[(str(decision.get("reference_id")), str(decision.get("stage")))] = decision
    def count(stage: str, decision: str) -> int:
        return sum(
            1 for (reference_id, current_stage), item in latest.items()
            if current_stage == stage and item.get("decision") == decision
        )
    reference_ids = {node["id"] for node in references}
    title_screened = {reference_id for (reference_id, stage) in latest if stage == "title_abstract"}
    full_screened = {reference_id for (reference_id, stage) in latest if stage == "full_text"}
    included = {
        reference_id for (reference_id, stage), item in latest.items()
        if stage == "full_text" and item.get("decision") == "include"
    }
    included.update({
        reference_id for (reference_id, stage), item in latest.items()
        if stage == "title_abstract" and item.get("decision") == "include"
        and reference_id not in full_screened
    })
    excluded = {
        reference_id for (reference_id, stage), item in latest.items()
        if item.get("decision") == "exclude"
    }
    unresolved = sorted(reference_ids - title_screened)
    return {
        "counts": {
            "records_identified": len(references),
            "title_abstract_screened": len(title_screened),
            "title_abstract_included": count("title_abstract", "include"),
            "title_abstract_excluded": count("title_abstract", "exclude"),
            "title_abstract_maybe": count("title_abstract", "maybe"),
            "full_text_assessed": len(full_screened),
            "full_text_included": count("full_text", "include"),
            "full_text_excluded": count("full_text", "exclude"),
            "full_text_maybe": count("full_text", "maybe"),
            "included": len(included),
            "excluded": len(excluded),
            "unresolved": len(unresolved),
        },
        "included_references": sorted(included),
        "excluded_references": sorted(excluded),
        "unresolved_references": unresolved,
        "decisions": decisions,
        "truth_assessed": False,
    }
