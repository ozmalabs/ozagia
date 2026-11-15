"""Bridge an imported paper claim to a reviewed, explicit native Term candidate.

This module deliberately does not parse prose into a model. Callers supply the
Term JSON, assumptions, bindings, provenance, and residuals for human/agent
review. A candidate is an executable representation proposal, not an imported
claim's proof or truth assessment.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ozagia.research.graph import ResearchError
from ozagia.research.trace import trace_chain
from ozagia.research.lean import export_lean, LeanExportError


def _nodes(graph: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    state = graph.state() if hasattr(graph, "state") else graph
    if not isinstance(state, Mapping):
        raise ResearchError("expected a ResearchGraph or graph state")
    nodes = {n.get("id"): dict(n) for n in state.get("nodes", []) if isinstance(n, Mapping)}
    return nodes, state.get("edges", [])


def _source_provenance(
    claim_id: str, nodes: Mapping[str, dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return recorded source locators; hashes are not reopened here."""
    found = []
    for edge in edges:
        if edge.get("type") != "cites" or edge.get("src") != claim_id:
            continue
        source = nodes.get(edge.get("tgt"), {})
        locator = source.get("locator")
        if source.get("type") == "Source" and isinstance(locator, Mapping):
            if locator.get("path") and locator.get("sha256"):
                found.append({"source_id": source["id"], "locator": dict(locator)})
    return found


def create_model_candidate(
    graph: Any,
    *,
    claim_id: str,
    term: Mapping[str, Any],
    bindings: Mapping[str, str] | None = None,
    assumption_ids: list[str] | None = None,
    context_ids: list[str] | None = None,
    residuals: list[str] | None = None,
    reviewer: str,
    review_note: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an explicit candidate model and its provenance/dependencies.

    The imported claim must cite at least one Source with a path and SHA-256
    locator. Every bound Term dependency must already carry an explicit Term;
    the candidate's source claim is also linked as a derivation context. The
    returned trace and Lean text are generated from the recorded graph.
    """
    nodes, edges = _nodes(graph)
    claim = nodes.get(claim_id)
    if not claim or not (
        claim.get("type") == "Claim"
        or claim.get("type") == "Evidence"
        and claim.get("evidence_type") == "sourced_formula_candidate"
    ):
        raise ResearchError("claim_id must identify an existing Claim or sourced formula candidate")
    if not isinstance(term, Mapping) or not isinstance(term.get("type"), str):
        raise ResearchError("term must be an explicit native Term JSON object")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ResearchError("reviewer must identify who reviewed the candidate")
    if not isinstance(review_note, str) or not review_note.strip():
        raise ResearchError("review_note must explain the candidate mapping")
    if not isinstance(residuals, list) or any(
        not isinstance(x, str) or not x.strip() for x in residuals
    ):
        raise ResearchError(
            "residuals must be a list of explicit strings (use [] if none are recorded)"
        )
    sources = _source_provenance(claim_id, nodes, edges)
    if not sources:
        raise ResearchError("imported claim needs a cited Source with path and sha256 locator")

    bindings = dict(bindings or {})
    assumptions = list(assumption_ids or [])
    context = list(context_ids or [])
    if any(
        not isinstance(alias, str) or not alias or not isinstance(ref, str)
        for alias, ref in bindings.items()
    ):
        raise ResearchError("bindings must map non-empty aliases to node IDs")
    if any(ref not in nodes for ref in bindings.values()):
        raise ResearchError("a Term binding references an unknown research node")
    if any(
        not isinstance(nodes[ref].get("term"), Mapping)
        or not isinstance(nodes[ref]["term"].get("type"), str)
        for ref in bindings.values()
    ):
        raise ResearchError("every bound dependency must already have an explicit native Term")
    if len(set(assumptions)) != len(assumptions) or any(
        aid not in nodes or nodes[aid].get("type") != "Assumption" for aid in assumptions
    ):
        raise ResearchError("assumption_ids must be distinct existing Assumption nodes")
    if any(not isinstance(cid, str) or cid not in nodes for cid in context) or len(
        set(context)
    ) != len(context):
        raise ResearchError("context_ids must be distinct existing research nodes")

    fields = {
        "wording": f"Executable model candidate for {claim.get('wording', claim_id)}",
        "claim_type": "inference",
        "status": "open",
        "model_candidate": True,
        "representation_of": claim_id,
        "term": dict(term),
        "bindings": bindings,
        "assumptions": assumptions,
        "residuals": list(residuals),
        "review": {"reviewer": reviewer.strip(), "note": review_note.strip()},
        "source_provenance": {"status": "recorded_assertion_not_reverified", "locators": sources},
        "candidate_provenance": dict(provenance or {}),
        "truth_assessed": False,
    }
    candidate = graph.record("Claim", **fields)
    for ref in dict.fromkeys([claim_id, *bindings.values(), *context]):
        is_context = ref == claim_id
        graph.link(
            candidate["id"],
            "derived_from",
            ref,
            provenance=(
                {"kind": "paper_claim_context", "sources": sources}
                if is_context
                else {
                    "kind": "term_binding" if ref in bindings.values() else "interpretation_context"
                }
            ),
            rationale=(
                "Explicit candidate represents this imported claim; it does not establish it."
                if is_context
                else (
                    "Native Term binding dependency supplied for candidate review."
                    if ref in bindings.values()
                    else "Graph context used to select the interpretation."
                )
            ),
        )
    for aid in assumptions:
        graph.link(
            candidate["id"],
            "assumes",
            aid,
            provenance={"kind": "reviewed_candidate_assumption"},
            rationale="Assumption declared for this candidate model.",
        )
    state = graph.state() if hasattr(graph, "state") else graph
    try:
        lean = {
            "status": "available",
            "text": export_lean(state, target=candidate["id"]),
            "kernel_checked": False,
        }
    except LeanExportError as exc:
        lean = {"status": "unsupported", "text": None, "kernel_checked": False, "reason": str(exc)}
    return {
        "candidate": candidate,
        "trace": trace_chain(state, candidate["id"]),
        "lean": lean,
        "residuals": list(residuals),
        "truth_assessed": False,
        "execution_api": {"operation": "execute", "node_id": candidate["id"]},
        "review_required": True,
    }


def execute_model_candidate(
    graph: Any, candidate_id: str, substrate: Any, env: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Execute the already-recorded native Term via the existing evaluator."""
    from ozagia.research.trace import execute

    nodes, _ = _nodes(graph)
    candidate = nodes.get(candidate_id)
    if (
        not candidate
        or candidate.get("type") != "Claim"
        or candidate.get("model_candidate") is not True
    ):
        raise ResearchError("candidate_id must identify a recorded model candidate")
    return execute(
        graph.state() if hasattr(graph, "state") else graph, candidate_id, substrate, env=env or {}
    )
