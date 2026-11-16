#!/usr/bin/env python3
"""Optional Ocelot worker: structural clustering for review findings.

Run inside an Ocelot checkout with its venv:

    PYTHONPATH=/path/to/ocelot /path/to/ocelot/.venv/bin/python \
        examples/ocelot_reconcile_worker.py < payload.json

The worker mirrors findings into one Microtheory per model run, adds structural
edges for the target and issue family, loads Ocelot's shape_vocabulary and
analogy_search seeds, and uses the graph analogy search to find structural
correspondences. The output is a set of clusters plus the analogies that
justified them. It is a candidate reconciliation, not a truth verdict.

`run_payload` is also imported by ozagia's optional in-process bridge when
`substrate_rs` and the Ocelot tree are importable.
"""
from __future__ import annotations

import json
import pathlib
import sys

MAX_FINDINGS = 500
NODE_TYPES = (
    "Agent", "AnalogyProbe", "ConceptShape", "Analogy", "Concept",
    "Microtheory", "FunctionalRole", "ShapePrinciple", "MetaShape",
    "Claim", "Reference", "Evidence", "Argument", "Source", "Target",
    "EvidenceState",
)
FAMILY_EDGES = {
    "duplicate": "duplicate_of",
    "evidence": "evidence_gap_of",
    "falsifiability": "falsifiability_of",
    "provenance": "provenance_of",
    "claim_structure": "claim_structure_of",
    "scope": "scope_of",
    "statistics": "statistics_of",
    "methods": "methods_of",
    "other": "other_shape_of",
}
EDGE_TYPES = (
    "observed_probe", "emitted_analogy", "has_concept", "implements",
    "has_principle", "excites", "inhibits", "causes", "enables",
    "about", "has_evidence_state", *FAMILY_EDGES.values(),
)


class UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        self.parent.setdefault(value, value)
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def _clean_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "_-" else "_" for char in value)


def _target_type(value: str) -> str:
    return value if value in NODE_TYPES else "Target"


def build_substrate(root: pathlib.Path | None = None):
    from substrate_rs import _native
    s = _native.Substrate()
    agent = s.add_node("Agent", {})
    for source in NODE_TYPES:
        for target in NODE_TYPES:
            for edge_type in EDGE_TYPES:
                s.add_production({
                    "src": {"type": source, "var": "src"},
                    "edge_type": edge_type,
                    "tgt": {"type": target, "var": "tgt"},
                    "where": None,
                    "weight": {"type": "Lit", "value": 1.0},
                    "provenance": "ozagia:ocelot_reconcile:open",
                })
    root = root or pathlib.Path.cwd()
    for name in ("shape_vocabulary", "analogy_search"):
        path = root / "seeds" / f"{name}.json"
        summary = s.load_seed_manifest(json.loads(path.read_text()), agent)
        if summary.get("rules_skipped") or summary.get("reflexes_skipped"):
            raise RuntimeError(f"Ocelot seed {name!r} did not install cleanly")
    return s


def run_payload(payload: dict, *, root: pathlib.Path | None = None) -> dict:
    """Run the structural pass on an already-decoded payload."""
    import domains.analogical_thinking as at

    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError("findings must be a list")
    if len(findings) > MAX_FINDINGS:
        raise ValueError(f"too many findings (limit {MAX_FINDINGS})")
    if not findings:
        return {"clusters": [], "analogies": [], "engine": "ocelot:analogy_search"}

    s = build_substrate(root)
    runs: dict[str, object] = {}
    targets: dict[tuple[str, str], object] = {}
    evidence_states: dict[str, object] = {}
    finding_meta: dict[str, dict] = {}

    for item in findings:
        finding_id = _clean_name(str(item.get("finding_id") or ""))
        if not finding_id:
            continue
        run_id = str(item.get("run_id") or "unknown")
        mt = runs.get(run_id)
        if mt is None:
            mt = s.add_node("Microtheory", {"name": _clean_name(run_id)})
            runs[run_id] = mt
        target_type = str(item.get("target_type") or "Target")
        target_id = str(item.get("target_id") or "")
        key = (target_type, target_id)
        target = targets.get(key)
        if target is None:
            target = s.add_node(_target_type(target_type), {"name": target_id or "unknown"})
            targets[key] = target
        family = str(item.get("family") or "other")
        family = family if family in FAMILY_EDGES else "other"
        severity = str(item.get("severity") or "note")
        concept = s.add_node("Concept", {
            "name": finding_id,
            "definition": str(item.get("wording") or ""),
            "category": family,
        })
        s.add_edge(mt, "has_concept", concept)
        s.add_edge(concept, "about", target)
        s.add_edge(concept, FAMILY_EDGES[family], target)
        if family == "evidence":
            state = evidence_states.get(severity)
            if state is None:
                state = s.add_node("EvidenceState", {"name": severity})
                evidence_states[severity] = state
            s.add_edge(concept, "has_evidence_state", state)
        finding_meta[finding_id] = item

    analogies = []
    seen_pairs = set()
    run_names = list(runs)
    for source_run in run_names:
        target_runs = [name for name in run_names if name != source_run]
        if not target_runs:
            continue
        results = at.find_analogies(s, _clean_name(source_run), [
            _clean_name(name) for name in target_runs
        ], order_n=4)
        for result in results:
            left = str(result.get("source_concept") or "").split("::")[-1]
            right = str(result.get("target_concept") or "").split("::")[-1]
            if not left or not right or left == right:
                continue
            left_meta = finding_meta.get(left) or {}
            right_meta = finding_meta.get(right) or {}
            if (left_meta.get("family"), left_meta.get("target_type"),
                    left_meta.get("target_id")) != (
                        right_meta.get("family"), right_meta.get("target_type"),
                        right_meta.get("target_id")):
                continue
            pair = tuple(sorted((left, right)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            analogies.append({
                "source_finding": left,
                "target_finding": right,
                "order_n": result.get("order_n"),
                "struct_shared": result.get("struct_shared"),
                "shared": result.get("shared"),
            })

    union = UnionFind(finding_meta)
    for analogy in analogies:
        union.union(analogy["source_finding"], analogy["target_finding"])
    clusters_by_root: dict[str, list[str]] = {}
    for finding_id in finding_meta:
        clusters_by_root.setdefault(union.find(finding_id), []).append(finding_id)
    clusters = [
        sorted(values)
        for _, values in sorted(clusters_by_root.items())
        if len(values) > 1
    ]
    clustered = {finding_id for cluster in clusters for finding_id in cluster}
    clusters.extend([[finding_id] for finding_id in sorted(finding_meta)
                     if finding_id not in clustered])
    return {
        "clusters": clusters,
        "analogies": analogies,
        "engine": "ocelot:analogy_search",
        "finding_count": len(finding_meta),
        "truth_assessed": False,
    }


def main() -> int:
    payload = json.load(sys.stdin)
    print(json.dumps(run_payload(payload, root=pathlib.Path.cwd())))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - JSON error for the parent
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(1)
