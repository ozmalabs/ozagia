"""Paper-level structured review: packet, report, arguments and bundle.

The paper-review workflow joins the existing research pieces without adding a
model runtime.  A selected paper is ingested, its claims are collected, and a
deterministic packet is built for the harness to send to reviewers.  The report
projects recorded runs, findings, reconciliations and arguments back into one
review document.  Every judgement stays a candidate: nothing in this module
assesses truth or turns reviewer agreement into evidence.

The core only reads and records graph state.  Model calls belong to the
harness; this module never imports a provider or invokes a model.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ozagia.research import appraisal
from ozagia.research import argument as argument_mod
from ozagia.research import contradiction
from ozagia.research import extraction
from ozagia.research import living_review
from ozagia.research import manuscript as manuscript_mod
from ozagia.research import paper as paper_mod
from ozagia.research import references as refs
from ozagia.research import review as review_mod
from ozagia.research import structured_review as structured
from ozagia.research import theory
from ozagia.research.graph import ResearchError

PAPER_REVIEW_SCHEMA = "ozagia.paper_review_packet.v1"
PAPER_REVIEW_REPORT_SCHEMA = "ozagia.paper_review_report.v1"
PAPER_REVIEW_BUNDLE_SCHEMA = "ozagia.paper_review_bundle.v1"
PAPER_SUFFIXES = frozenset({
    ".tex", ".md", ".markdown", ".pdf", ".epub", ".html", ".htm", ".txt",
})
BUNDLE_FILES = (
    "review.md",
    "review.json",
    "claims.csv",
    "evidence.csv",
    "references.bib",
    "arguments.json",
    "checks.json",
)
REVIEW_SECTIONS = (
    "summary",
    "strengths",
    "weaknesses",
    "major_issues",
    "minor_issues",
    "questions",
    "methods",
    "statistics",
    "reproducibility",
    "ethics",
    "novelty_related_work",
    "claims_evidence",
    "theory",
    "contradictions",
    "alerts",
    "dissent",
    "audit",
)
SECTION_TITLES = {
    "summary": "Summary",
    "strengths": "Strengths",
    "weaknesses": "Weaknesses",
    "major_issues": "Major issues",
    "minor_issues": "Minor issues",
    "questions": "Questions",
    "methods": "Methods",
    "statistics": "Statistics",
    "reproducibility": "Reproducibility",
    "ethics": "Ethics",
    "novelty_related_work": "Novelty and related work",
    "claims_evidence": "Claims and evidence",
    "theory": "Theory",
    "contradictions": "Contradictions",
    "alerts": "Alerts",
    "dissent": "Dissent",
    "audit": "Audit",
}
EMPTY_MARKERS = {
    "summary": "No review run summaries recorded.",
    "strengths": "No strengths recorded.",
    "weaknesses": "No weaknesses recorded.",
    "major_issues": "No major issues recorded.",
    "minor_issues": "No minor issues recorded.",
    "questions": "No questions recorded.",
    "methods": "No methods findings recorded.",
    "statistics": "No statistics findings recorded.",
    "reproducibility": "No reproducibility findings recorded.",
    "ethics": "No ethics findings recorded.",
    "novelty_related_work": "No novelty or related-work findings recorded.",
    "claims_evidence": "No claims recorded.",
    "theory": "No theory context recorded.",
    "contradictions": "No contradictions recorded.",
    "alerts": "No alerts recorded.",
    "dissent": "No dissent recorded.",
    "audit": "No audit trail recorded.",
}
_SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "note": 3}


class PaperReviewError(ResearchError):
    """A paper review packet, report or bundle is invalid."""


# --------------------------------------------------------------------------- #
# deterministic helpers
# --------------------------------------------------------------------------- #
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
        raise PaperReviewError(f"value must be canonical JSON: {exc}") from exc


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
    raise PaperReviewError("paper review operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node
        for node in state.get("nodes", [])
        if isinstance(node, Mapping) and node.get("id")
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _require_node(
    state: Mapping[str, Any],
    node_id: Any,
    node_type: str,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(node_id, str) or not node_id.strip():
        raise PaperReviewError(f"{label} id must be a non-empty string")
    node = _node_by_id(state, node_id.strip())
    if node is None:
        raise PaperReviewError(f"unknown {label} id: {node_id.strip()}")
    if node.get("type") != node_type:
        raise PaperReviewError(
            f"{label} {node_id.strip()} must be a {node_type}, not {node.get('type')}"
        )
    return node


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PaperReviewError(f"{field} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaperReviewError(f"{field} must be a non-negative integer")
    return value


def _normal_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().replace("\\", "/").lstrip("./")


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
    locators: list[dict[str, Any]] = []
    for value in values:
        clean = {
            key: item
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
        if clean and clean not in locators:
            locators.append(clean)
    return sorted(locators, key=_canonical)


def _locator(node: Mapping[str, Any]) -> dict[str, Any]:
    values = _locators(node)
    return values[0] if values else {}


def _locator_path(node: Mapping[str, Any]) -> str:
    for locator in _locators(node):
        path = _normal_path(locator.get("path"))
        if path:
            return path
    return ""


def _locator_reference_ids(node: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for locator in _locators(node):
        for field in ("reference_id", "reference", "ref_id", "source_id"):
            value = locator.get(field)
            if isinstance(value, str) and value.strip() and value.strip() not in ids:
                ids.append(value.strip())
    for field in ("reference_id", "source_id"):
        value = node.get(field)
        if isinstance(value, str) and value.strip() and value.strip() not in ids:
            ids.append(value.strip())
    return ids


def _citation_keys(node: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("citations", "citation_keys"):
        raw = node.get(field)
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        for item in values:
            if isinstance(item, str) and item.strip():
                key = item.strip().lstrip("@").strip()
                if key and key not in keys:
                    keys.append(key)
    single = node.get("citation_key")
    if isinstance(single, str) and single.strip():
        key = single.strip().lstrip("@").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _node_text(node: Mapping[str, Any]) -> str:
    for field in (
        "wording",
        "text",
        "title",
        "summary",
        "thesis",
        "question",
        "name",
        "statement",
    ):
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            return _collapse(value)
    return ""


def _reference_index(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for node in _nodes(state):
        if node.get("type") != "Reference":
            continue
        for key in _citation_keys(node):
            index.setdefault(key.casefold(), node)
    return index


# --------------------------------------------------------------------------- #
# source and claim collection
# --------------------------------------------------------------------------- #
def _paper_suffix(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise PaperReviewError("path must be a non-empty string")
    suffix = Path(path.strip()).suffix.casefold()
    if suffix not in PAPER_SUFFIXES:
        raise PaperReviewError(
            "paper review source must be .tex, .md, .markdown, .pdf, .epub, "
            f".html, .htm, or .txt, not {suffix or path!r}"
        )
    return suffix


def _find_source(
    state: Mapping[str, Any],
    *,
    root_id: str,
    path: str,
    sha256: str,
) -> Mapping[str, Any] | None:
    normal_path = _normal_path(path)
    candidates = []
    for node in _nodes(state):
        if node.get("type") != "Source":
            continue
        for locator in _locators(node):
            if (
                locator.get("root_id") == root_id
                and _normal_path(locator.get("path")) == normal_path
                and locator.get("sha256") == sha256
            ):
                candidates.append(node)
                break
    return sorted(candidates, key=lambda node: str(node.get("id")))[0] if candidates else None


def _claim_linked_to_source(
    state: Mapping[str, Any],
    claim: Mapping[str, Any],
    source: Mapping[str, Any],
) -> bool:
    source_id = str(source.get("id"))
    claim_id = str(claim.get("id"))
    for edge in _edges(state):
        if (
            edge.get("type") == "cites"
            and edge.get("src") == claim_id
            and edge.get("tgt") == source_id
        ):
            return True

    source_path = _locator_path(source)
    if source_path and _locator_path(claim) == source_path:
        source_roots = {
            locator.get("root_id")
            for locator in _locators(source)
            if locator.get("root_id")
        }
        claim_roots = {
            locator.get("root_id")
            for locator in _locators(claim)
            if locator.get("root_id")
        }
        if not source_roots or not claim_roots or source_roots & claim_roots:
            return True

    source_reference_ids = set(_locator_reference_ids(source))
    source_reference_ids.add(source_id)
    for edge in _edges(state):
        if (
            edge.get("type") == "manifestation_of"
            and edge.get("src") == source_id
            and isinstance(edge.get("tgt"), str)
        ):
            source_reference_ids.add(edge["tgt"])
    claim_reference_ids = set(_locator_reference_ids(claim))
    if source_reference_ids & claim_reference_ids:
        return True
    return False


def _sorted_claims(claims: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    unique = {str(claim.get("id")): claim for claim in claims}
    return [unique[node_id] for node_id in sorted(unique)]


def _source_claims(
    state: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    return _sorted_claims(
        [
            claim
            for claim in _nodes(state)
            if claim.get("type") == "Claim"
            and _claim_linked_to_source(state, claim, source)
        ]
    )


def _claim_ids_for_plan(
    state: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    findings: Sequence[Mapping[str, Any]] = (),
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    target_ids: list[str] = []
    target_id = plan.get("target_id")
    if isinstance(target_id, str) and target_id:
        target_ids.append(target_id)
    for edge in _edges(state):
        if edge.get("type") == "addresses" and edge.get("src") == plan.get("id"):
            value = edge.get("tgt")
            if isinstance(value, str) and value:
                target_ids.append(value)

    source = None
    claims: list[Mapping[str, Any]] = []
    for node_id in dict.fromkeys(target_ids):
        node = _node_by_id(state, node_id)
        if node is None:
            continue
        if node.get("type") == "Source":
            source = node
            claims.extend(_source_claims(state, node))
        elif node.get("type") == "Claim":
            claims.append(node)
        elif node.get("type") == "Reference":
            for claim in _nodes(state):
                if claim.get("type") != "Claim":
                    continue
                if any(
                    edge.get("type") == "cites"
                    and edge.get("src") == claim.get("id")
                    and edge.get("tgt") == node.get("id")
                    for edge in _edges(state)
                ):
                    claims.append(claim)

    for finding in findings:
        claim_id = finding.get("claim_id")
        if isinstance(claim_id, str):
            claim = _node_by_id(state, claim_id)
            if claim is not None and claim.get("type") == "Claim":
                claims.append(claim)

    if source is None:
        for claim in claims:
            path = _locator_path(claim)
            if not path:
                continue
            source = next(
                (
                    node
                    for node in _nodes(state)
                    if node.get("type") == "Source"
                    and _locator_path(node) == path
                ),
                None,
            )
            if source is not None:
                claims.extend(_source_claims(state, source))
                break
    return source, _sorted_claims(claims)


def _claim_context(
    state: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    *,
    reference_index: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_id = {str(node.get("id")): node for node in _nodes(state)}
    edges = _edges(state)
    context: dict[str, dict[str, Any]] = {}
    for claim in claims:
        claim_id = str(claim.get("id"))
        evidence: list[dict[str, Any]] = []
        assumptions: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        reference_ids: list[str] = []
        keys: list[str] = list(_citation_keys(claim))

        for edge in edges:
            if claim_id not in (edge.get("src"), edge.get("tgt")):
                continue
            relation = edge.get("type")
            other_id = edge.get("tgt") if edge.get("src") == claim_id else edge.get("src")
            other = by_id.get(str(other_id))
            if other is None:
                continue
            if relation in {"supports", "opposes", "tests", "derived_from"}:
                evidence.append(
                    {
                        "node_id": other["id"],
                        "node_type": other.get("type"),
                        "relation": relation,
                        "direction": "out" if edge.get("src") == claim_id else "in",
                        "wording": _node_text(other),
                        "citation_keys": _citation_keys(other),
                        "source_locator": _locator(other) or None,
                        "edge_id": edge.get("id"),
                    }
                )
            elif relation == "assumes":
                assumptions.append(
                    {
                        "node_id": other["id"],
                        "node_type": other.get("type"),
                        "relation": relation,
                        "direction": "out" if edge.get("src") == claim_id else "in",
                        "wording": _node_text(other),
                        "edge_id": edge.get("id"),
                    }
                )
            elif relation == "cites" and other.get("type") == "Reference":
                reference_id = str(other.get("id"))
                if reference_id not in reference_ids:
                    reference_ids.append(reference_id)
                for key in _citation_keys(other):
                    if key not in keys:
                        keys.append(key)
                citations.append(
                    {
                        "reference_id": reference_id,
                        "citation_key": (
                            _citation_keys(other)[0] if _citation_keys(other) else None
                        ),
                        "title": _collapse(other.get("title")) or None,
                        "wording": _node_text(other),
                        "edge_id": edge.get("id"),
                    }
                )

        for key in _citation_keys(claim):
            reference = reference_index.get(key.casefold())
            if reference is None:
                continue
            reference_id = str(reference.get("id"))
            if reference_id not in reference_ids:
                reference_ids.append(reference_id)
            if not any(item.get("reference_id") == reference_id for item in citations):
                citations.append(
                    {
                        "reference_id": reference_id,
                        "citation_key": (
                            _citation_keys(reference)[0] if _citation_keys(reference) else key
                        ),
                        "title": _collapse(reference.get("title")) or None,
                        "wording": _node_text(reference),
                        "edge_id": None,
                    }
                )

        context[claim_id] = {
            "claim_id": claim_id,
            "evidence": sorted(
                evidence,
                key=lambda item: (
                    str(item["relation"]),
                    str(item["node_id"]),
                    str(item["edge_id"]),
                ),
            ),
            "assumptions": sorted(
                assumptions,
                key=lambda item: (str(item["node_id"]), str(item["edge_id"])),
            ),
            "citations": sorted(
                citations,
                key=lambda item: (str(item["reference_id"]), str(item["edge_id"])),
            ),
            "citation_keys": sorted(keys, key=lambda key: (key.casefold(), key)),
            "reference_ids": sorted(reference_ids),
        }
    return context


def _enriched_claims(
    claims: Sequence[Mapping[str, Any]],
    context: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    enriched = []
    for claim in claims:
        item = dict(claim)
        details = context.get(str(claim.get("id")), {})
        item["evidence"] = [dict(value) for value in details.get("evidence", [])]
        item["assumptions"] = [dict(value) for value in details.get("assumptions", [])]
        item["citations"] = [dict(value) for value in details.get("citations", [])]
        item["citation_keys"] = list(details.get("citation_keys", []))
        item["reference_ids"] = list(details.get("reference_ids", []))
        enriched.append(item)
    return enriched


# --------------------------------------------------------------------------- #
# review prompt and output schema
# --------------------------------------------------------------------------- #
def _finding_schema() -> dict[str, Any]:
    return {
        "wording": "string",
        "severity": "blocking|major|minor|note",
        "issue_type": "string",
        "claim_id": "optional Claim id",
        "reference_id": "optional Reference id",
        "evidence_ids": ["optional Evidence or Claim ids"],
        "citation_keys": ["optional citation keys present in the packet"],
        "quote": "optional text present in the packet",
        "recommendation": "optional string",
        "confidence": 0.0,
    }


def _output_schema() -> dict[str, Any]:
    finding = _finding_schema()
    return {
        "summary": "string",
        "strengths": ["string"],
        "weaknesses": ["string"],
        "major_issues": [finding],
        "minor_issues": [finding],
        "questions": ["string"],
        "methods": ["string"],
        "statistics": ["string"],
        "reproducibility": ["string"],
        "ethics": ["string"],
        "novelty_related_work": ["string"],
        "claims_evidence_alignment": [
            {
                "claim_id": "Claim id",
                "assessment": "string",
                "evidence_ids": ["Evidence or Claim ids"],
                "citation_keys": ["citation keys"],
                "quote": "optional packet text",
            }
        ],
        "theory_fit": ["string"],
        "findings": [finding],
        "recommendation": "optional string",
        "confidence": 0.0,
    }


def _review_prompt(
    *,
    plan: Mapping[str, Any] | None,
    source: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
) -> str:
    locator = _locator(source)
    lines = [
        "Paper review packet.",
        "Review the selected paper and the recorded claims independently. "
        "Do not use another reviewer's output.",
        "The packet contains graph records and candidate claims, not established truth.",
    ]
    if plan is None:
        lines.append("Plan: none supplied.")
    else:
        lines.extend(
            [
                f"Review question: {_collapse(plan.get('question'))}",
                f"Review kind: {_collapse(plan.get('kind'))}",
                f"Roles: {', '.join(str(item) for item in plan.get('roles') or [])}",
                f"Models: {', '.join(str(item) for item in plan.get('models') or [])}",
                f"Minimum reviewers: {plan.get('min_reviewers', '')}",
                f"Blinding: {bool(plan.get('blinding'))}",
                f"Adjudication: {_collapse(plan.get('adjudication'))}",
            ]
        )
        if plan.get("criteria"):
            lines.append(f"Review criteria: {_canonical(plan['criteria'])}")
    lines.extend(
        [
            "Source:",
            f"- id: {source.get('id')}",
            f"- name: {_collapse(source.get('name'))}",
            f"- path: {_collapse(locator.get('path'))}",
            f"- sha256: {_collapse(locator.get('sha256'))}",
            "Claims:",
        ]
    )
    if not claims:
        lines.append("- No claims are linked to this source.")
    for claim in claims:
        keys = _citation_keys(claim)
        key_text = f" [citations: {', '.join(keys)}]" if keys else ""
        lines.append(
            f"- {claim.get('id')} ({claim.get('claim_type') or 'untyped'}): "
            f"{_node_text(claim)}{key_text}"
        )
    lines.extend(
        [
            "Return one JSON object with this shape:",
            _canonical(_output_schema()),
            "Rules: only cite node ids and citation keys present in this packet; "
            "quote only text present in this packet; state uncertainty; do not "
            "decide truth; do not turn reviewer agreement into support.",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# start and packet
# --------------------------------------------------------------------------- #
def _find_existing_source(
    state: Mapping[str, Any],
    *,
    root_id: str,
    path: str,
    sha256: str,
) -> Mapping[str, Any]:
    source = _find_source(state, root_id=root_id, path=path, sha256=sha256)
    if source is None:
        raise PaperReviewError(
            "duplicate paper import was reported but no Source matches "
            f"root_id={root_id!r}, path={path!r}, sha256={sha256!r}"
        )
    return source


def start_paper_review(
    graph: Any,
    *,
    root_id: str,
    path: str,
    question: str,
    roles: Sequence[str],
    models: Sequence[str],
    kind: str = "peer_review",
    bibliography_path: str | None = None,
    criteria: Any = None,
    min_reviewers: int = 2,
    reviewer: str | None = None,
    pdf_extractor: Any = None,
) -> dict[str, Any]:
    """Ingest a selected paper, create its review plan and build the packet.

    Repeated identical imports reuse the existing Source; no second import is
    created.  The returned packet is deterministic and read-only.
    """
    suffix = _paper_suffix(path)
    if not hasattr(graph, "state") or not hasattr(graph, "record"):
        raise PaperReviewError("start_paper_review needs a ResearchGraph")
    if not isinstance(root_id, str) or not root_id.strip():
        raise PaperReviewError("root_id must be a non-empty string")
    root_id = root_id.strip()
    path = path.strip()
    reviewer_text = _optional_string(reviewer, "reviewer")

    if suffix == ".pdf":
        from ozagia.research import pdf_claims
        ingest = pdf_claims.ingest_pdf_claims(
            graph,
            root_id=root_id,
            path=path,
            pdf_extractor=pdf_extractor,
            reviewer=reviewer_text,
        )
        preview = ingest.get("extraction") or {}
        source = ingest.get("source")
    elif suffix in {".epub", ".html", ".htm", ".txt"}:
        from ozagia.research import document_ingest
        ingest = document_ingest.ingest_document_claims(
            graph,
            root_id=root_id,
            path=path,
            reviewer=reviewer_text,
        )
        preview = ingest.get("document") or {}
        source = ingest.get("source")
    else:
        ingest = paper_mod.ingest_paper(
            graph,
            root_id=root_id,
            path=path,
            bibliography_path=bibliography_path,
        )
        preview = ingest.get("preview") or {}
        source = ingest.get("source")
        if source is None:
            source = _find_existing_source(
                graph.state(),
                root_id=root_id,
                path=path,
                sha256=str(preview.get("sha256") or ""),
            )
    if not isinstance(source, Mapping) or source.get("type") != "Source":
        raise PaperReviewError("paper ingestion did not yield a Source")

    plan_result = structured.create_review_plan(
        graph,
        question=question,
        kind=kind,
        roles=roles,
        models=models,
        criteria=criteria,
        reviewers=[reviewer_text] if reviewer_text else None,
        min_reviewers=min_reviewers,
        target_id=str(source["id"]),
    )
    plan = plan_result["plan"]
    packet = build_paper_review_packet(
        graph.state(),
        source_id=str(source["id"]),
        plan_id=str(plan["id"]),
    )
    return {
        "ingest": ingest,
        "plan": plan,
        "source": dict(source),
        "packet": packet,
        "truth_assessed": False,
    }


def build_paper_review_packet(
    state: Any,
    *,
    source_id: str,
    plan_id: str | None = None,
    manuscript_body: str | None = None,
    max_claims: int = 50,
) -> dict[str, Any]:
    """Build a deterministic, read-only packet for reviewing one paper Source."""
    current = _as_state(state)
    source = _require_node(current, source_id, "Source", "source")
    plan = (
        _require_node(current, plan_id, "ReviewPlan", "review plan")
        if plan_id is not None
        else None
    )
    max_claims = _nonnegative_int(max_claims, "max_claims")
    if manuscript_body is not None:
        if not isinstance(manuscript_body, str):
            raise PaperReviewError("manuscript_body must be a string")
        if not manuscript_body.strip():
            raise PaperReviewError("manuscript_body must be non-empty")

    reference_index = _reference_index(current)
    all_claims = _source_claims(current, source)
    total_claims = len(all_claims)
    claims = all_claims[:max_claims]
    context = _claim_context(current, claims, reference_index=reference_index)
    enriched_claims = _enriched_claims(claims, context)
    claim_ids = [str(claim["id"]) for claim in claims]
    reference_ids = sorted(
        {
            reference_id
            for details in context.values()
            for reference_id in details.get("reference_ids", [])
        }
    )
    references = [
        dict(node)
        for node in (
            _node_by_id(current, reference_id) for reference_id in reference_ids
        )
        if node is not None
    ]

    claim_text = "\n".join(_node_text(claim) for claim in claims)
    citation_contexts = []
    if claim_text:
        for item in refs.citation_contexts(claim_text):
            citation_contexts.append({**dict(item), "truth_assessed": False})
    citation_contexts.sort(
        key=lambda item: (
            str(item.get("citation_keys") or []),
            str(item.get("quote") or ""),
        )
    )

    extraction_table = extraction.extraction_table(current)
    appraisal_table = appraisal.appraisal_table(current)
    target_ids = set(claim_ids) | set(reference_ids)
    extraction_rows = [
        row for row in extraction_table["rows"] if row.get("target") in target_ids
    ]
    appraisal_rows = [
        row for row in appraisal_table["rows"] if row.get("target_id") in target_ids
    ]
    extraction_context = {
        **extraction_table,
        "rows": extraction_rows,
        "count": len(extraction_rows),
        "filtered": True,
        "target_ids": sorted(target_ids),
    }
    appraisal_context = {
        **appraisal_table,
        "rows": appraisal_rows,
        "count": len(appraisal_rows),
        "filtered": True,
        "target_ids": sorted(target_ids),
    }

    reference_checks = refs.check_references(current)
    contradiction_scan = contradiction.find_contradictions(current)
    alert_scan = living_review.scan_alerts(
        current,
        review_plan_id=str(plan["id"]) if plan is not None else None,
    )
    theory_ledger = theory.theory_report(current)
    compatibility_matrix = theory.compatibility_matrix(current)

    manuscript = None
    manuscript_check = None
    if manuscript_body is not None:
        manuscript = {
            "body": manuscript_body,
            "sha256": _text_digest(manuscript_body),
            "length": len(manuscript_body),
        }
        manuscript_check = manuscript_mod.check_manuscript(current, manuscript_body)

    prompt = _review_prompt(plan=plan, source=source, claims=claims)
    schema = _output_schema()
    checks: dict[str, Any] = {
        "references": reference_checks,
        "extraction": {"count": extraction_context["count"]},
        "appraisal": {"count": appraisal_context["count"]},
        "contradictions": {
            "count": contradiction_scan["count"],
            "kinds": contradiction_scan["kinds"],
        },
        "alerts": {"count": alert_scan["count"], "kinds": alert_scan["kinds"]},
        "theory": {
            "count": theory_ledger["counts"]["theories"],
            "compatibility_pairs": compatibility_matrix["counts"]["pairs"],
        },
    }
    if manuscript_check is not None:
        checks["manuscript"] = manuscript_check

    packet: dict[str, Any] = {
        "schema": PAPER_REVIEW_SCHEMA,
        "plan": dict(plan) if plan is not None else None,
        "plan_id": plan["id"] if plan is not None else None,
        "source": dict(source),
        "source_node": dict(source),
        "source_id": source["id"],
        "source_locator": _locator(source) or None,
        "source_path": _locator_path(source) or None,
        "source_sha256": _locator(source).get("sha256"),
        "claims": enriched_claims,
        "claim_context": context,
        "claim_ids": claim_ids,
        "claim_total": total_claims,
        "claims_truncated": total_claims > len(claims),
        "max_claims": max_claims,
        "references": references,
        "reference_ids": reference_ids,
        "citation_contexts": citation_contexts,
        "reference_checks": reference_checks,
        "extraction_table": extraction_context,
        "appraisal_table": appraisal_context,
        "contradiction_scan": contradiction_scan,
        "alert_scan": alert_scan,
        "theory_ledger": theory_ledger,
        "compatibility_matrix": compatibility_matrix,
        "manuscript": manuscript,
        "prompt": prompt,
        "review_prompt": prompt,
        "output_schema": schema,
        "review_schema": schema,
        "checks": checks,
        "truth_assessed": False,
    }
    packet["packet_sha256"] = _digest(packet)
    return packet


# --------------------------------------------------------------------------- #
# report aggregation
# --------------------------------------------------------------------------- #
def _is_strength(finding: Mapping[str, Any]) -> bool:
    issue_type = str(finding.get("issue_type") or "").casefold()
    return any(token in issue_type for token in ("strength", "merit", "positive"))


def _matches(finding: Mapping[str, Any], *tokens: str) -> bool:
    text = f"{finding.get('issue_type', '')} {finding.get('wording', '')}".casefold()
    return any(token in text for token in tokens)


def _finding_item(
    finding: Mapping[str, Any],
    runs_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    run = runs_by_id.get(str(finding.get("run_id")))
    item = {
        "finding_id": finding.get("id"),
        "wording": finding.get("wording"),
        "severity": finding.get("severity"),
        "issue_type": finding.get("issue_type"),
        "claim_id": finding.get("claim_id"),
        "reference_id": finding.get("reference_id"),
        "evidence_ids": list(finding.get("evidence_ids") or []),
        "citation_keys": list(finding.get("citation_keys") or []),
        "quote": finding.get("quote"),
        "recommendation": finding.get("recommendation"),
        "confidence": finding.get("confidence"),
        "run_id": finding.get("run_id"),
        "reviewer_id": run.get("reviewer_id") if run is not None else None,
        "model": run.get("model") if run is not None else None,
        "role": run.get("role") if run is not None else None,
        "truth_assessed": False,
    }
    return item


def _argument_item(argument: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(argument)
    item["premises"] = [dict(value) if isinstance(value, Mapping) else value
                        for value in argument.get("premises") or []]
    item["assumptions"] = list(argument.get("assumptions") or [])
    item["objections"] = [
        dict(value) if isinstance(value, Mapping) else value
        for value in argument.get("objections") or []
    ]
    item["falsifiers"] = list(argument.get("falsifiers") or [])
    item["truth_assessed"] = False
    return item


def _contradiction_item(candidate: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(candidate)
    if not _collapse(item.get("wording")):
        item["wording"] = (
            f"{_collapse(item.get('kind')) or 'contradiction'}: "
            f"{item.get('claim_a_id')} vs {item.get('claim_b_id')}"
        )
    return item


def _alert_item(alert: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(alert)
    if not _collapse(item.get("wording")):
        item["wording"] = (
            f"{_collapse(item.get('kind')) or 'alert'} on {item.get('target_id')}"
        )
    return item


def _collect_arguments(
    state: Mapping[str, Any],
    claim_ids: set[str],
) -> list[dict[str, Any]]:
    arguments = []
    for node in _nodes(state):
        if node.get("type") != "Argument":
            continue
        claim_id = node.get("claim_id")
        linked = isinstance(claim_id, str) and claim_id in claim_ids
        if not linked:
            linked = any(
                edge.get("type") == "argues_for"
                and edge.get("src") == node.get("id")
                and edge.get("tgt") in claim_ids
                for edge in _edges(state)
            )
        if linked:
            arguments.append(_argument_item(node))
    return sorted(
        arguments,
        key=lambda item: (
            str(item.get("claim_id")),
            str(item.get("mode")),
            str(item.get("id")),
        ),
    )


def _argument_groups(
    arguments: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in arguments:
        claim_id = str(item.get("claim_id"))
        mode = str(item.get("mode"))
        groups.setdefault(claim_id, {}).setdefault(mode, []).append(dict(item))
    return {
        claim_id: {
            mode: sorted(values, key=lambda value: str(value.get("id")))
            for mode, values in sorted(modes.items())
        }
        for claim_id, modes in sorted(groups.items())
    }


def _recorded_recommendation(record: Mapping[str, Any] | None) -> tuple[Any, Any]:
    if record is None:
        return None, None
    recommendation = record.get("recommendation")
    confidence = record.get("confidence")
    adjudicator = record.get("adjudicator")
    if isinstance(adjudicator, Mapping):
        if recommendation is None:
            recommendation = adjudicator.get("recommendation")
        if confidence is None:
            confidence = adjudicator.get("confidence")
    return recommendation, confidence


def _argument_markdown(argument: Mapping[str, Any]) -> str:
    mode = {
        "steelman": "Steelman",
        "red_team": "Red-team",
        "synthesis": "Synthesis",
    }.get(str(argument.get("mode")), str(argument.get("mode") or "argument").title())
    lines = [
        f"#### {mode}: {argument.get('id')}",
        f"Thesis: {_collapse(argument.get('thesis'))}",
    ]
    if argument.get("confidence") is not None:
        lines.append(f"Confidence: {argument.get('confidence')}")
    if argument.get("reviewer"):
        lines.append(f"Reviewer: {_collapse(argument.get('reviewer'))}")
    premises = argument.get("premises") or []
    if premises:
        lines.append("Premises:")
        for premise in premises:
            if isinstance(premise, Mapping):
                inference = _collapse(premise.get("inference"))
                wording = _collapse(premise.get("wording"))
                lines.append(f"- ({inference or 'unspecified'}) {wording}")
            else:
                lines.append(f"- {_collapse(premise)}")
    assumptions = argument.get("assumptions") or []
    if assumptions:
        lines.append("Assumptions:")
        for assumption in assumptions:
            if isinstance(assumption, Mapping):
                lines.append(f"- {_collapse(assumption.get('wording'))}")
            else:
                lines.append(f"- {_collapse(assumption)}")
    objections = argument.get("objections") or []
    if objections:
        lines.append("Objections:")
        for objection in objections:
            if isinstance(objection, Mapping):
                status = _collapse(objection.get("status"))
                response = _collapse(objection.get("response"))
                line = f"- {_collapse(objection.get('wording'))}"
                if status:
                    line += f" (status: {status})"
                if response:
                    line += f" — reply: {response}"
                lines.append(line)
            else:
                lines.append(f"- {_collapse(objection)}")
    falsifiers = argument.get("falsifiers") or []
    if falsifiers:
        lines.append("Falsifiers:")
        for falsifier in falsifiers:
            if isinstance(falsifier, Mapping):
                wording = (
                    falsifier.get("wording")
                    or falsifier.get("description")
                    or falsifier.get("text")
                )
                lines.append(f"- {_collapse(wording)}")
            else:
                lines.append(f"- {_collapse(falsifier)}")
    return "\n".join(lines)


def _render_items(items: Sequence[Any]) -> str:
    lines: list[str] = []
    for item in items:
        if isinstance(item, str):
            lines.append(f"- {_collapse(item)}")
        elif isinstance(item, Mapping):
            wording = (
                item.get("wording")
                or item.get("thesis")
                or item.get("summary")
                or item.get("kind")
                or item.get("theory_id")
                or item.get("alert_id")
                or item.get("id")
            )
            details = []
            for field in ("severity", "issue_type", "claim_id", "reference_id"):
                value = item.get(field)
                if value not in (None, "", []):
                    details.append(f"{field}={value}")
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"- {_collapse(wording)}{suffix}")
        else:
            lines.append(f"- {_collapse(item)}")
    return "\n".join(lines)


def _section(
    name: str,
    items: Sequence[Any],
    *,
    text: str | None = None,
) -> dict[str, Any]:
    rendered = text if text is not None else _render_items(items)
    empty = not rendered.strip()
    if empty:
        rendered = EMPTY_MARKERS[name]
    return {
        "title": SECTION_TITLES[name],
        "items": [dict(item) if isinstance(item, Mapping) else item for item in items],
        "count": len(items),
        "empty": empty,
        "text": rendered,
    }


def paper_review_report(state: Any, *, plan_id: str) -> dict[str, Any]:
    """Aggregate the complete recorded review trail for one paper plan."""
    current = _as_state(state)
    plan = _require_node(current, plan_id, "ReviewPlan", "review plan")
    history = structured.review_report(current, plan_id)
    agreement = structured.review_agreement(current, plan_id)
    runs = history["runs"]
    findings = history["findings"]
    runs_by_id = {str(run["id"]): run for run in runs}
    latest = history["latest_reconciliation"]

    source, claims = _claim_ids_for_plan(current, plan, findings=findings)
    reference_index = _reference_index(current)
    context = _claim_context(current, claims, reference_index=reference_index)
    enriched_claims = _enriched_claims(claims, context)
    claim_ids = {str(claim["id"]) for claim in claims}
    reference_ids = sorted(
        {
            reference_id
            for details in context.values()
            for reference_id in details.get("reference_ids", [])
        }
    )
    references = [
        dict(node)
        for node in (
            _node_by_id(current, reference_id) for reference_id in reference_ids
        )
        if node is not None
    ]
    target_ids = claim_ids | set(reference_ids)

    arguments = _collect_arguments(current, claim_ids)
    argument_groups = _argument_groups(arguments)
    argument_checks = []
    argument_issues = []
    for item in arguments:
        try:
            check = argument_mod.check_argument(current, str(item["id"]))
        except ResearchError as exc:
            argument_checks.append({"argument_id": item["id"], "error": str(exc)})
            continue
        argument_checks.append(check)
        argument_issues.extend(
            {"argument_id": item["id"], **issue}
            for issue in check.get("issues", [])
        )

    extraction_table = extraction.extraction_table(current)
    appraisal_table = appraisal.appraisal_table(current)
    extraction_rows = [
        row for row in extraction_table["rows"] if row.get("target") in target_ids
    ]
    appraisal_rows = [
        row for row in appraisal_table["rows"] if row.get("target_id") in target_ids
    ]
    extraction_context = {
        **extraction_table,
        "rows": extraction_rows,
        "count": len(extraction_rows),
        "filtered": True,
        "target_ids": sorted(target_ids),
    }
    appraisal_context = {
        **appraisal_table,
        "rows": appraisal_rows,
        "count": len(appraisal_rows),
        "filtered": True,
        "target_ids": sorted(target_ids),
    }

    contradiction_scan = contradiction.find_contradictions(current)
    relevant_contradictions = [
        candidate
        for candidate in contradiction_scan["candidates"]
        if candidate.get("claim_a_id") in claim_ids
        or candidate.get("claim_b_id") in claim_ids
    ]
    recorded_contradictions = sorted(
        (
            dict(node)
            for node in _nodes(current)
            if node.get("type") == "Contradiction"
            and (
                node.get("claim_a_id") in claim_ids
                or node.get("claim_b_id") in claim_ids
            )
        ),
        key=lambda node: str(node.get("id")),
    )
    alert_scan = living_review.scan_alerts(current, review_plan_id=plan_id)
    relevant_alerts = [
        alert
        for alert in alert_scan["alerts"]
        if alert.get("target_id") in claim_ids
        or alert.get("target_id") == (source.get("id") if source is not None else None)
        or alert.get("target_id") in reference_ids
    ]
    recorded_alerts = sorted(
        (
            dict(node)
            for node in _nodes(current)
            if node.get("type") == "Alert"
            and (
                node.get("target_id") in claim_ids
                or node.get("target_id") in reference_ids
                or node.get("target_id") == (source.get("id") if source is not None else None)
            )
        ),
        key=lambda node: str(node.get("id")),
    )
    theory_ledger = theory.theory_report(current)
    compatibility_matrix = theory.compatibility_matrix(current)
    reference_checks = refs.check_references(current)

    finding_items = [_finding_item(finding, runs_by_id) for finding in findings]
    strength_items = [item for item in finding_items if _is_strength(item)]
    weakness_items = [item for item in finding_items if not _is_strength(item)]
    major_items = [
        item for item in finding_items if item.get("severity") in {"blocking", "major"}
    ]
    minor_items = [
        item for item in finding_items if item.get("severity") in {"minor", "note"}
    ]
    question_items = [item for item in finding_items if _matches(item, "question", "clarif")]
    method_items = [item for item in finding_items if _matches(item, "method", "design", "sampl")]
    statistics_items = [
        item
        for item in finding_items
        if _matches(item, "statistic", "statistical", "power", "p_value", "analysis")
    ]
    reproducibility_items = [
        item
        for item in finding_items
        if _matches(item, "reproduc", "replicat", "data_avail", "code_avail")
    ]
    ethics_items = [
        item
        for item in finding_items
        if _matches(item, "ethic", "consent", "conflict", "coi", "privacy")
    ]
    novelty_items = [
        item
        for item in finding_items
        if _matches(item, "novel", "related_work", "related work", "prior_work", "contribution")
    ]
    theory_items = [item for item in finding_items if _matches(item, "theor")]

    claims_evidence_items: list[dict[str, Any]] = []
    for claim in enriched_claims:
        alignment = [
            item
            for item in finding_items
            if item.get("claim_id") == claim.get("id")
            or _matches(item, "claim", "evidence", "citation")
        ]
        claims_evidence_items.append(
            {
                "claim_id": claim.get("id"),
                "wording": _node_text(claim),
                "claim_type": claim.get("claim_type"),
                "status": claim.get("status"),
                "evidence": claim.get("evidence", []),
                "assumptions": claim.get("assumptions", []),
                "citation_keys": claim.get("citation_keys", []),
                "reference_ids": claim.get("reference_ids", []),
                "alignment_findings": alignment,
                "arguments": argument_groups.get(str(claim.get("id")), {}),
            }
        )

    claims_evidence_text = []
    if claims_evidence_items:
        for claim in claims_evidence_items:
            claims_evidence_text.append(
                f"### {claim['claim_id']}: {_collapse(claim['wording'])}"
            )
            claims_evidence_text.append(
                "Status: "
                f"{claim.get('status') or 'unrecorded'}; type: "
                f"{claim.get('claim_type') or 'unrecorded'}"
            )
            if claim["evidence"]:
                for evidence in claim["evidence"]:
                    claims_evidence_text.append(
                        f"- Evidence {evidence.get('node_id')} "
                        f"({evidence.get('relation')}, {evidence.get('node_type')}): "
                        f"{_collapse(evidence.get('wording'))}"
                    )
            else:
                claims_evidence_text.append("- No evidence linked to this claim.")
            if claim["assumptions"]:
                for assumption in claim["assumptions"]:
                    claims_evidence_text.append(
                        f"- Assumption {assumption.get('node_id')}: "
                        f"{_collapse(assumption.get('wording'))}"
                    )
            if claim["citation_keys"]:
                claims_evidence_text.append(
                    f"- Citation keys: {', '.join(claim['citation_keys'])}"
                )
            if claim["alignment_findings"]:
                for item in claim["alignment_findings"]:
                    claims_evidence_text.append(
                        f"- Finding {item.get('finding_id')} "
                        f"({item.get('severity')}, {item.get('issue_type')}): "
                        f"{_collapse(item.get('wording'))}"
                    )
        for argument in arguments:
            claims_evidence_text.append(_argument_markdown(argument))
    reference_problems = [
        *reference_checks.get("issues", []),
        *reference_checks.get("warnings", []),
    ]
    if reference_problems:
        claims_evidence_text.append("#### Citation and reference checks")
        for problem in reference_problems:
            claims_evidence_text.append(
                f"- {problem.get('code')}: {_collapse(problem.get('reason'))}"
            )
    claims_evidence_rendered = "\n\n".join(claims_evidence_text)

    theory_text = []
    if theory_ledger["counts"]["theories"]:
        theory_text.append(
            "Recorded theories: "
            + ", ".join(
                f"{report['theory'].get('id')} ({_collapse(report['theory'].get('name'))})"
                for report in theory_ledger["theories"]
            )
        )
        theory_text.append(
            f"Compatibility pairs: {compatibility_matrix['counts']['pairs']} "
            f"({compatibility_matrix['counts']['missing']} missing)."
        )
    for item in theory_items:
        theory_text.append(
            f"- Finding {item.get('finding_id')} ({item.get('issue_type')}): "
            f"{_collapse(item.get('wording'))}"
        )
    theory_rendered = "\n".join(theory_text)

    contradiction_items: list[Any] = [
        _contradiction_item(item) for item in relevant_contradictions
    ]
    contradiction_items.extend(
        _contradiction_item(item) for item in recorded_contradictions
    )
    contradiction_items.extend(
        item for item in finding_items if _matches(item, "contradict")
    )
    alert_items: list[Any] = [_alert_item(item) for item in relevant_alerts]
    alert_items.extend(_alert_item(item) for item in recorded_alerts)
    alert_items.extend(item for item in finding_items if _matches(item, "alert", "retract"))

    dissent_rows = list(agreement.get("dissent") or [])
    reconciliation_dissent = (
        list(latest.get("dissent") or []) if isinstance(latest, Mapping) else []
    )
    dissent_keys = {
        _canonical(row.get("key") if isinstance(row, Mapping) else row)
        for row in dissent_rows
    }
    for row in reconciliation_dissent:
        key = _canonical(row.get("key") if isinstance(row, Mapping) else row)
        if key not in dissent_keys:
            dissent_rows.append(row)
            dissent_keys.add(key)

    recorded_recommendations = []
    for run in runs:
        if run.get("recommendation") is not None or run.get("confidence") is not None:
            recorded_recommendations.append(
                {
                    "source": "run",
                    "run_id": run.get("id"),
                    "reviewer_id": run.get("reviewer_id"),
                    "model": run.get("model"),
                    "role": run.get("role"),
                    "recommendation": run.get("recommendation"),
                    "confidence": run.get("confidence"),
                }
            )
    reconciliation_recommendation, reconciliation_confidence = (
        _recorded_recommendation(latest)
    )
    if latest is not None and (
        reconciliation_recommendation is not None
        or reconciliation_confidence is not None
    ):
        recorded_recommendations.append(
            {
                "source": "reconciliation",
                "reconciliation_id": latest.get("id"),
                "recommendation": reconciliation_recommendation,
                "confidence": reconciliation_confidence,
            }
        )

    recommendation = None
    confidence = None
    recommendation_source = None
    if reconciliation_recommendation is not None or reconciliation_confidence is not None:
        recommendation = reconciliation_recommendation
        confidence = reconciliation_confidence
        recommendation_source = "reconciliation"
    else:
        run_recommendations = {
            run.get("recommendation")
            for run in runs
            if run.get("recommendation") is not None
        }
        run_confidences = {
            run.get("confidence")
            for run in runs
            if run.get("confidence") is not None
        }
        if len(run_recommendations) == 1:
            recommendation = next(iter(run_recommendations))
            recommendation_source = "run"
        if len(run_confidences) == 1:
            confidence = next(iter(run_confidences))
            if recommendation_source is None:
                recommendation_source = "run"

    run_summary_items = [
        {
            "run_id": run.get("id"),
            "reviewer_id": run.get("reviewer_id"),
            "role": run.get("role"),
            "model": run.get("model"),
            "provider": run.get("provider"),
            "summary": run.get("summary"),
            "recommendation": run.get("recommendation"),
            "confidence": run.get("confidence"),
            "status": run.get("status"),
            "finding_count": run.get("finding_count"),
            "packet_sha256": run.get("packet_sha256"),
            "output_sha256": run.get("output_sha256"),
            "warnings": list(run.get("warnings") or []),
        }
        for run in runs
    ]
    summary_text = "\n".join(
        f"- {item['run_id']} ({item['reviewer_id']}, {item['model']}): "
        f"{_collapse(item.get('summary'))}"
        for item in run_summary_items
        if _collapse(item.get("summary"))
    )

    audit_items = [
        {
            "record_type": "ReviewPlan",
            "record_id": plan.get("id"),
            "question": plan.get("question"),
            "kind": plan.get("kind"),
            "roles": list(plan.get("roles") or []),
            "models": list(plan.get("models") or []),
            "min_reviewers": plan.get("min_reviewers"),
            "target_id": plan.get("target_id"),
            "created_at": plan.get("created_at"),
        },
        *[
            {
                "record_type": "ReviewRun",
                "record_id": run.get("id"),
                "reviewer_id": run.get("reviewer_id"),
                "role": run.get("role"),
                "model": run.get("model"),
                "provider": run.get("provider"),
                "round": run.get("round"),
                "status": run.get("status"),
                "packet_sha256": run.get("packet_sha256"),
                "prompt_sha256": run.get("prompt_sha256"),
                "context_sha256": run.get("context_sha256"),
                "output_sha256": run.get("output_sha256"),
                "finding_count": run.get("finding_count"),
                "warnings": list(run.get("warnings") or []),
            }
            for run in runs
        ],
        *[
            {
                "record_type": "ReviewFinding",
                "record_id": finding.get("id"),
                "run_id": finding.get("run_id"),
                "severity": finding.get("severity"),
                "issue_type": finding.get("issue_type"),
                "claim_id": finding.get("claim_id"),
                "reference_id": finding.get("reference_id"),
            }
            for finding in findings
        ],
        *[
            {
                "record_type": "Argument",
                "record_id": item.get("id"),
                "claim_id": item.get("claim_id"),
                "mode": item.get("mode"),
                "reviewer": item.get("reviewer"),
                "confidence": item.get("confidence"),
                "premises": len(item.get("premises") or []),
                "assumptions": len(item.get("assumptions") or []),
                "objections": len(item.get("objections") or []),
                "falsifiers": len(item.get("falsifiers") or []),
            }
            for item in arguments
        ],
    ]
    for reconciliation in history.get("reconciliations", []):
        audit_items.append(
            {
                "record_type": "Reconciliation",
                "record_id": reconciliation.get("id"),
                "method": reconciliation.get("method"),
                "min_reviewers": reconciliation.get("min_reviewers"),
                "run_ids": list(reconciliation.get("run_ids") or []),
                "metrics": reconciliation.get("metrics"),
                "status": reconciliation.get("status"),
                "adjudicator": reconciliation.get("adjudicator"),
                "rationale": reconciliation.get("rationale"),
            }
        )
    audit_text = "\n".join(
        f"- {item['record_type']} {item['record_id']}: "
        + ", ".join(
            f"{key}={value}"
            for key, value in item.items()
            if key not in {"record_type", "record_id"} and value not in (None, "", [], {})
        )
        for item in audit_items
    )
    if recommendation is not None or confidence is not None:
        audit_text += (
            "\n- Recorded recommendation: "
            f"{recommendation if recommendation is not None else 'not recorded'}; "
            f"recorded confidence: {confidence if confidence is not None else 'not recorded'}"
        )
    else:
        audit_text += "\n- No reconciled recommendation or confidence recorded."

    section_items: dict[str, list[Any]] = {
        "summary": run_summary_items,
        "strengths": strength_items,
        "weaknesses": weakness_items,
        "major_issues": major_items,
        "minor_issues": minor_items,
        "questions": question_items,
        "methods": method_items,
        "statistics": statistics_items,
        "reproducibility": reproducibility_items,
        "ethics": ethics_items,
        "novelty_related_work": novelty_items,
        "claims_evidence": claims_evidence_items + [*arguments],
        "theory": (
            ([{"theory_ledger": theory_ledger,
               "compatibility_matrix": compatibility_matrix}]
             if theory_ledger["counts"]["theories"] else [])
            + theory_items
        ),
        "contradictions": contradiction_items,
        "alerts": alert_items,
        "dissent": dissent_rows,
        "audit": audit_items,
    }
    section_text = {
        "summary": summary_text,
        "strengths": _render_items(strength_items),
        "weaknesses": _render_items(weakness_items),
        "major_issues": _render_items(major_items),
        "minor_issues": _render_items(minor_items),
        "questions": _render_items(question_items),
        "methods": _render_items(method_items),
        "statistics": _render_items(statistics_items),
        "reproducibility": _render_items(reproducibility_items),
        "ethics": _render_items(ethics_items),
        "novelty_related_work": _render_items(novelty_items),
        "claims_evidence": claims_evidence_rendered,
        "theory": theory_rendered,
        "contradictions": _render_items(contradiction_items),
        "alerts": _render_items(alert_items),
        "dissent": _render_items(dissent_rows),
        "audit": audit_text,
    }
    sections = {
        name: _section(name, section_items[name], text=section_text[name])
        for name in REVIEW_SECTIONS
    }

    checks: dict[str, Any] = {
        **history.get("checks", {}),
        "references": reference_checks,
        "extraction": {
            "count": extraction_context["count"],
            "target_ids": sorted(target_ids),
        },
        "appraisal": {
            "count": appraisal_context["count"],
            "target_ids": sorted(target_ids),
        },
        "contradictions": {
            "count": contradiction_scan["count"],
            "relevant": len(relevant_contradictions),
            "recorded": len(recorded_contradictions),
        },
        "alerts": {
            "count": alert_scan["count"],
            "relevant": len(relevant_alerts),
            "recorded": len(recorded_alerts),
        },
        "arguments": {
            "ok": not argument_issues,
            "issues": argument_issues,
            "count": len(arguments),
            "checks": argument_checks,
        },
        "truth_assessed": False,
    }

    report: dict[str, Any] = {
        "schema": PAPER_REVIEW_REPORT_SCHEMA,
        "plan": dict(plan),
        "plan_id": plan["id"],
        "source": dict(source) if source is not None else None,
        "source_id": source.get("id") if source is not None else None,
        "claims": enriched_claims,
        "claim_ids": sorted(claim_ids),
        "claim_context": context,
        "references": references,
        "reference_ids": reference_ids,
        "runs": runs,
        "run_ids": [run.get("id") for run in runs],
        "findings": findings,
        "findings_by_run": history.get("findings_by_run", {}),
        "agreement": agreement,
        "reconciliation": latest,
        "latest_reconciliation": latest,
        "reconciliations": history.get("reconciliations", []),
        "arguments": arguments,
        "arguments_by_claim": argument_groups,
        "argument_checks": argument_checks,
        "extraction_table": extraction_context,
        "appraisal_table": appraisal_context,
        "contradiction_scan": contradiction_scan,
        "relevant_contradictions": relevant_contradictions,
        "recorded_contradictions": recorded_contradictions,
        "alert_scan": alert_scan,
        "relevant_alerts": relevant_alerts,
        "recorded_alerts": recorded_alerts,
        "theory_ledger": theory_ledger,
        "compatibility_matrix": compatibility_matrix,
        "reference_checks": reference_checks,
        "recommendation": recommendation,
        "confidence": confidence,
        "recommendation_source": recommendation_source,
        "recorded_recommendations": recorded_recommendations,
        "sections": {name: sections[name]["text"] for name in REVIEW_SECTIONS},
        "section_details": {name: sections[name] for name in REVIEW_SECTIONS},
        "section_items": section_items,
        "section_meta": {
            name: {
                "title": sections[name]["title"],
                "count": sections[name]["count"],
                "empty": sections[name]["empty"],
            }
            for name in REVIEW_SECTIONS
        },
        "checks": checks,
        "counts": {
            **history.get("counts", {}),
            "claims": len(claims),
            "references": len(references),
            "arguments": len(arguments),
            "contradictions": contradiction_scan["count"],
            "alerts": alert_scan["count"],
            "theories": theory_ledger["counts"]["theories"],
            "extractions": extraction_context["count"],
            "appraisals": appraisal_context["count"],
        },
        "truth_assessed": False,
    }
    report["summary"] = sections["summary"]["text"]
    report["summary_items"] = section_items["summary"]
    report["claims_evidence_alignment"] = section_items["claims_evidence"]
    report["theory_fit"] = section_items["theory"]
    for name in REVIEW_SECTIONS:
        if name != "summary":
            report[name] = section_items[name]
    return report


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def review_markdown(report: Mapping[str, Any]) -> str:
    """Render a paper review report as deterministic Markdown."""
    if not isinstance(report, Mapping):
        raise PaperReviewError("review_markdown needs a report mapping")
    sections = report.get("sections")
    if not isinstance(sections, Mapping):
        raise PaperReviewError("report has no sections mapping")
    plan = report.get("plan") if isinstance(report.get("plan"), Mapping) else {}
    source = report.get("source") if isinstance(report.get("source"), Mapping) else {}
    lines = [
        "# Paper review report",
        "",
        f"Plan: `{report.get('plan_id') or ''}`",
        f"Source: `{source.get('id') or report.get('source_id') or ''}`"
        + (f" ({_collapse(source.get('name'))})" if source.get("name") else ""),
        f"Question: {_collapse(plan.get('question'))}",
        "Truth assessed: **no** — this report records candidates and reviewer "
        "judgements, not truth.",
        "",
    ]
    for name in REVIEW_SECTIONS:
        lines.extend(
            [
                f"## {SECTION_TITLES[name]}",
                "",
                str(sections.get(name) or EMPTY_MARKERS[name]),
                "",
            ]
        )

    recommendation = report.get("recommendation")
    confidence = report.get("confidence")
    if recommendation is None and confidence is None:
        lines.extend(
            [
                "## Recorded judgement",
                "",
                "No reconciled recommendation or confidence recorded.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## Recorded judgement",
                "",
                f"Recommendation: {recommendation if recommendation is not None else 'not recorded'}",
                f"Confidence: {confidence if confidence is not None else 'not recorded'}",
                f"Source: {report.get('recommendation_source') or 'recorded'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #
def _resolve_bundle_dir(graph: Any, plan_id: str, output_dir: str | Path | None) -> Path:
    if output_dir is None:
        graph_path = getattr(graph, "path", None)
        if graph_path is None:
            raise PaperReviewError("graph has no path; pass output_dir explicitly")
        return Path(graph_path).parent / "exports" / f"review-{plan_id}"
    if not str(output_dir).strip():
        raise PaperReviewError("output_dir must not be empty")
    return Path(output_dir).expanduser()


def _json_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise PaperReviewError(f"bundle value must be JSON-serializable: {exc}") from exc


def _csv_text(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=list(columns),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _write_text(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise PaperReviewError(f"cannot write paper review bundle file {path}: {exc}") from exc


def write_paper_review_bundle(
    graph: Any,
    *,
    plan_id: str,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write the deterministic review bundle for one plan.

    Only the seven documented bundle files are written; no other file in the
    output directory is created, deleted or modified.
    """
    if not hasattr(graph, "state"):
        raise PaperReviewError("write_paper_review_bundle needs a ResearchGraph")
    state = graph.state()
    plan = _require_node(state, plan_id, "ReviewPlan", "review plan")
    directory = _resolve_bundle_dir(graph, str(plan["id"]), output_dir)
    report = paper_review_report(state, plan_id=str(plan["id"]))
    evidence = review_mod.evidence_table(state, format="csv")
    rows = evidence["rows"]
    columns = (
        "record_id",
        "kind",
        "wording",
        "status",
        "claim_type",
        "source",
        "citation_keys",
        "references",
    )
    paper_claim_ids = set(report["claim_ids"])
    paper_evidence_ids = {
        item.get("node_id")
        for claim in report["claims"]
        for item in claim.get("evidence", [])
        if item.get("node_type") in {"Evidence", "Observation"}
    }
    claim_rows = [
        row
        for row in rows
        if row.get("kind") == "Claim" and row.get("record_id") in paper_claim_ids
    ]
    evidence_rows = [
        row
        for row in rows
        if row.get("record_id") in paper_evidence_ids
    ]
    extraction_table = extraction.extraction_table(state, format="csv")
    appraisal_table = appraisal.appraisal_table(state, format="csv")
    checks = {
        "schema": PAPER_REVIEW_BUNDLE_SCHEMA,
        "plan_id": plan["id"],
        "checks": report["checks"],
        "reference_checks": report["reference_checks"],
        "extraction_table": {
            "count": extraction_table["count"],
            "rows": extraction_table["rows"],
        },
        "appraisal_table": {
            "count": appraisal_table["count"],
            "rows": appraisal_table["rows"],
        },
        "counts": report["counts"],
        "truth_assessed": False,
    }

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PaperReviewError(f"cannot create paper review bundle directory: {exc}") from exc
    if not directory.is_dir():
        raise PaperReviewError(f"paper review bundle path is not a directory: {directory}")

    contents = {
        "review.md": review_markdown(report),
        "review.json": _json_text(report),
        "claims.csv": _csv_text(claim_rows, columns),
        "evidence.csv": _csv_text(evidence_rows, columns),
        "references.bib": refs.export_bibtex(state),
        "arguments.json": _json_text(
            {
                "plan_id": plan["id"],
                "arguments": report["arguments"],
                "by_claim": report["arguments_by_claim"],
                "truth_assessed": False,
            }
        ),
        "checks.json": _json_text(checks),
    }
    paths: dict[str, str] = {}
    for name in BUNDLE_FILES:
        path = directory / name
        _write_text(path, contents[name])
        paths[name] = str(path)
    return {
        "schema": PAPER_REVIEW_BUNDLE_SCHEMA,
        "plan_id": plan["id"],
        "output_dir": str(directory),
        "files": list(BUNDLE_FILES),
        "paths": paths,
        "count": len(paths),
        "checks": report["checks"],
        "truth_assessed": False,
    }


__all__ = [
    "BUNDLE_FILES",
    "PAPER_REVIEW_REPORT_SCHEMA",
    "PAPER_REVIEW_SCHEMA",
    "PaperReviewError",
    "REVIEW_SECTIONS",
    "build_paper_review_packet",
    "paper_review_report",
    "review_markdown",
    "start_paper_review",
    "write_paper_review_bundle",
]
