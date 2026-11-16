"""Optional semantic clustering for multi-model review findings.

Exact-string clustering is conservative but treats paraphrases as dissent. This
module adds an embedding-based second view backed by a local service (Ollama by
default). It is optional and injected: the core still works without a model or
network, and the exact consensus remains available.

The semantic view is a proposal. It records its model, threshold and member
findings; it does not turn agreement into truth or delete the minority report.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.request import Request, urlopen

from ozagia.research.graph import ResearchError

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text:latest"
DEFAULT_THRESHOLD = 0.76
_SEVERITY_ORDER = {"blocking": 4, "major": 3, "minor": 2, "note": 1}


class SemanticReconcileError(ResearchError):
    """The optional embedding service or clustering input is invalid."""


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise SemanticReconcileError("embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else list(vector)


def ollama_embedder(
    *,
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    timeout: float = 60.0,
) -> Callable[[list[str]], list[list[float]]]:
    """Return a batch embedder backed by a local Ollama instance."""
    base = base_url.rstrip("/")
    if not model.strip():
        raise SemanticReconcileError("embedding model must be non-empty")

    def embed(texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not texts:
            return []
        body = json.dumps({"model": model, "input": texts}).encode("utf-8")
        request = Request(
            f"{base}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read(20_000_000).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - reported as a typed error
            raise SemanticReconcileError(
                f"Ollama embedding request failed for {model!r}: {exc}") from exc
        vectors = payload.get("embeddings") if isinstance(payload, Mapping) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise SemanticReconcileError("Ollama returned an unexpected embedding batch")
        try:
            return [[float(value) for value in vector] for vector in vectors]
        except (TypeError, ValueError) as exc:
            raise SemanticReconcileError("Ollama returned a malformed vector") from exc

    embed.model = model  # type: ignore[attr-defined]
    embed.base_url = base  # type: ignore[attr-defined]
    return embed


def cluster_texts(
    texts: Sequence[str],
    embedder: Callable[[list[str]], list[list[float]]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[list[int]]:
    """Greedy cosine clustering; deterministic for a stable input order."""
    if not 0.0 <= threshold <= 1.0:
        raise SemanticReconcileError("threshold must be between 0 and 1")
    if not texts:
        return []
    vectors = embedder(list(texts))
    if len(vectors) != len(texts):
        raise SemanticReconcileError("embedder returned the wrong number of vectors")
    vectors = [_normalise(vector) for vector in vectors]
    clusters: list[dict[str, Any]] = []
    for index, vector in enumerate(vectors):
        best = None
        best_score = -1.0
        for cluster in clusters:
            score = _cosine(vector, cluster["centroid"])
            if score > best_score:
                best, best_score = cluster, score
        if best is not None and best_score >= threshold:
            count = len(best["members"])
            best["members"].append(index)
            centroid = [
                (old * count + new) / (count + 1)
                for old, new in zip(best["centroid"], vector)
            ]
            best["centroid"] = _normalise(centroid)
        else:
            clusters.append({"members": [index], "centroid": vector})
    return [cluster["members"] for cluster in clusters]


def _max_severity(values: Sequence[str]) -> str | None:
    available = [value for value in values if value in _SEVERITY_ORDER]
    return max(available, key=lambda value: _SEVERITY_ORDER[value]) if available else None


def _merge_rows(rows: list[Mapping[str, Any]], members: list[int]) -> dict[str, Any]:
    chosen = [rows[index] for index in members]
    run_ids = sorted({str(run) for row in chosen for run in row.get("run_ids", [])})
    reviewers = sorted({str(rev) for row in chosen for rev in row.get("reviewers", [])})
    finding_ids = sorted({str(fid) for row in chosen for fid in row.get("finding_ids", [])})
    severities: dict[str, str] = {}
    for row in chosen:
        for run_id, severity in (row.get("severities") or {}).items():
            if isinstance(severity, str) and severity not in severities:
                severities[run_id] = severity
    issue_types = sorted({
        str(row.get("issue_type")) for row in chosen
        if row.get("issue_type") not in (None, "", "mixed")
    })
    wording = max((str(row.get("wording") or "") for row in chosen), key=len, default="")
    first = chosen[0]
    row: dict[str, Any] = {
        "key": [first.get("target_type", ""), first.get("target_id", ""),
                "semantic", " ".join(wording.casefold().split())],
        "target_type": first.get("target_type", ""),
        "target_id": first.get("target_id", ""),
        "issue_type": issue_types[0] if len(issue_types) == 1 else "mixed",
        "issue_types": issue_types,
        "wording": wording,
        "normalized_wording": " ".join(wording.casefold().split()),
        "finding_ids": finding_ids,
        "run_ids": run_ids,
        "reviewers": reviewers,
        "run_count": len(run_ids),
        "reviewer_count": len(reviewers),
        "severity": _max_severity(list(severities.values())),
        "severities": severities,
        "presence": {
            str(run_id): str(run_id) in run_ids
            for run_id in first.get("_all_runs", [])
        },
        "reviewer_presence": {
            str(reviewer): str(reviewer) in reviewers
            for reviewer in first.get("_all_reviewers", [])
        },
        "member_keys": [row.get("key") for row in chosen],
        "findings": [
            finding
            for row in chosen
            for finding in (row.get("findings") or [])
        ],
        "clustering": "embedding",
    }
    return row


def recluster_agreement(
    agreement: Mapping[str, Any],
    *,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    groups: Sequence[Sequence[int]] | None = None,
    method: str = "embedding",
    model: str | None = None,
) -> dict[str, Any]:
    """Return a copy of an exact agreement with semantic/structural clusters.

    ``groups`` supplies precomputed member-index clusters (for example from an
    Ocelot shape/analogy pass). Otherwise ``embedder`` clusters the row wording.
    """
    rows = list(agreement.get("issues") or [])
    if not rows:
        result = dict(agreement)
        result["clustering"] = {
            "method": method,
            "model": model if model is not None else getattr(embedder, "model", None),
            "threshold": threshold,
            "clusters": 0,
        }
        return result
    all_runs = [str(run.get("id")) for run in agreement.get("runs", [])]
    all_reviewers = [str(item) for item in agreement.get("reviewers", [])]
    min_reviewers = int(agreement.get("min_reviewers") or 2)

    cluster_plan: list[list[int]] = []
    if groups is not None:
        seen: set[int] = set()
        for group in groups:
            members = [int(index) for index in group
                       if 0 <= int(index) < len(rows) and int(index) not in seen]
            if members:
                seen.update(members)
                cluster_plan.append(members)
        cluster_plan.extend([index] for index in range(len(rows)) if index not in seen)
    else:
        if embedder is None:
            raise SemanticReconcileError(
                "semantic reconciliation needs an embedder or precomputed groups")
        grouped: dict[tuple[str, str], list[int]] = {}
        for index, row in enumerate(rows):
            grouped.setdefault(
                (str(row.get("target_type", "")), str(row.get("target_id", ""))), []
            ).append(index)
        for (_, _), indices in sorted(grouped.items()):
            texts = [str(rows[index].get("wording") or rows[index].get("issue_type") or "")
                     for index in indices]
            for members in cluster_texts(texts, embedder, threshold=threshold):
                cluster_plan.append([indices[member] for member in members])

    semantic_rows: list[dict[str, Any]] = []
    for members in cluster_plan:
        row = _merge_rows(rows, members)
        # Rebuild presence now that the row knows the full run/reviewer sets.
        row["presence"] = {run_id: run_id in row["run_ids"] for run_id in all_runs}
        row["reviewer_presence"] = {
            reviewer: reviewer in row["reviewers"] for reviewer in all_reviewers
        }
        row["consensus"] = len(row["reviewers"]) >= min_reviewers
        row["dissent"] = (
            len(all_reviewers) >= 2 and len(row["reviewers"]) == 1
            and not row["consensus"]
        )
        semantic_rows.append(row)

    semantic_rows.sort(key=lambda row: (
        str(row.get("target_type")), str(row.get("target_id")),
        str(row.get("issue_type")), str(row.get("wording"))))
    consensus_rows = [row for row in semantic_rows if row["consensus"]]
    dissent_rows = [row for row in semantic_rows if row["dissent"]]
    single_reviewer_rows = [row for row in semantic_rows if row["reviewer_count"] == 1]
    key_sets = [
        {
            tuple(row["key"])
            for row in semantic_rows
            if str(run_id) in row["run_ids"]
        }
        for run_id in all_runs
    ]
    severity_pairs = 0
    severity_agreements = 0
    for row in semantic_rows:
        values = list((row.get("severities") or {}).values())
        for index, left in enumerate(values):
            for right in values[index + 1:]:
                severity_pairs += 1
                if left == right:
                    severity_agreements += 1
    try:
        from ozagia.research.structured_review import _fleiss_kappa, _mean_jaccard
        kappa = _fleiss_kappa([len(row["run_ids"]) for row in semantic_rows], len(all_runs))
        jaccard = _mean_jaccard(key_sets)
    except Exception as exc:  # noqa: BLE001 - keep the semantic view usable alone
        raise SemanticReconcileError(f"cannot score semantic clusters: {exc}") from exc
    severity_rate = (
        round(severity_agreements / severity_pairs, 6) if severity_pairs else None
    )

    result = dict(agreement)
    result.update({
        "issues": semantic_rows,
        "issue_matrix": {
            "runs": all_runs,
            "reviewers": all_reviewers,
            "rows": semantic_rows,
        },
        "consensus": consensus_rows,
        "dissent": dissent_rows,
        "fleiss_kappa": kappa,
        "kappa": kappa,
        "mean_pairwise_jaccard": jaccard,
        "mean_jaccard": jaccard,
        "severity_agreement": severity_rate,
        "severity_agreement_detail": {
            "rate": severity_rate,
            "agreements": severity_agreements,
            "comparisons": severity_pairs,
        },
        "counts": {
            "runs": len(all_runs),
            "reviewers": len(all_reviewers),
            "findings": len(agreement.get("findings", [])),
            "issues": len(semantic_rows),
            "consensus": len(consensus_rows),
            "dissent": len(dissent_rows),
            "single_reviewer": len(single_reviewer_rows),
        },
        "clustering": {
            "method": method,
            "model": model if model is not None else getattr(embedder, "model", None),
            "threshold": threshold,
            "clusters": len(semantic_rows),
            "exact_clusters": len(rows),
        },
    })
    return result


__all__ = [
    "DEFAULT_EMBED_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_THRESHOLD",
    "SemanticReconcileError",
    "cluster_texts",
    "ollama_embedder",
    "recluster_agreement",
]
