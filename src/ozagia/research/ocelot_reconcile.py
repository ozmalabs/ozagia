"""Optional Ocelot-backed structural reconciliation for review findings.

Ocelot's graph-shape and analogy machinery can compare findings by structure
(target, issue family, severity, evidence relation) instead of wording. This
adapter runs a small worker inside an Ocelot checkout and returns candidate
clusters. It is deliberately external: ozagia core does not import Ocelot, and
the adapter is used only when a caller supplies the checkout and interpreter.

The result is a proposal. It records the Ocelot analogies and leaves the exact
clusters and dissent available.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

from ozagia.research.graph import ResearchError

DEFAULT_TIMEOUT = 120
MAX_FINDINGS = 500
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


class OcelotReconcileError(ResearchError):
    """The optional Ocelot worker is unavailable or returned invalid output."""


def canonical_issue_family(issue_type: Any) -> str:
    """Map a model's free-form issue type to a structural family."""
    text = str(issue_type or "").casefold().replace("-", "_")
    if not text:
        return "other"
    if "claim_scope" in text:
        return "duplicate"
    if any(word in text for word in ("duplicate", "duplication", "overlap", "redund")):
        return "duplicate"
    if any(word in text for word in (
        "evidence", "support", "missing_context", "interpretation", "citation",
        "uncited", "unsupported",
    )):
        return "evidence"
    if any(word in text for word in ("falsif", "operational", "testable", "disconfirm")):
        return "falsifiability"
    if any(word in text for word in (
        "provenance", "metadata", "identifier", "reference", "locator", "doi",
        "arxiv", "citation_key",
    )):
        return "provenance"
    if any(word in text for word in (
        "compound", "conflat", "claim_type", "structure", "split", "granular",
    )):
        return "claim_structure"
    if any(word in text for word in ("scope", "general", "population", "boundary")):
        return "scope"
    if any(word in text for word in ("statistic", "p_value", "confidence", "ci", "power")):
        return "statistics"
    if any(word in text for word in ("method", "design", "baseline", "control", "sampling")):
        return "methods"
    return "other"


def _finding_target(finding: Mapping[str, Any]) -> tuple[str, str]:
    for field, node_type in (
        ("claim_id", "Claim"),
        ("reference_id", "Reference"),
        ("argument_id", "Argument"),
    ):
        value = finding.get(field)
        if isinstance(value, str) and value:
            return node_type, value
    evidence = finding.get("evidence_ids")
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], str):
        return "Evidence", evidence[0]
    return "Target", ""


def _worker_path() -> Path:
    return Path(__file__).resolve().parents[3] / "examples" / "ocelot_reconcile_worker.py"


def _parse_worker_output(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        start, end = stdout.find("{"), stdout.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stdout[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise OcelotReconcileError("Ocelot worker returned invalid JSON")


def _load_worker(script: Path):
    spec = importlib.util.spec_from_file_location("ozagia_ocelot_worker", script)
    if spec is None or spec.loader is None:
        raise OcelotReconcileError(f"cannot load Ocelot worker: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inprocess_output(payload: Mapping[str, Any], *, root: Path,
                      worker: Path | None) -> dict[str, Any]:
    script = worker or _worker_path()
    if not script.is_file():
        raise OcelotReconcileError(f"Ocelot worker not found: {script}")
    paths = [str(root), str(root / "runners" / "dsl" / "python")]
    added = [path for path in paths if path not in sys.path]
    for path in reversed(added):
        sys.path.insert(0, path)
    try:
        module = _load_worker(script)
        output = module.run_payload(dict(payload), root=root)
    except OcelotReconcileError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a typed error
        raise OcelotReconcileError(f"in-process Ocelot pass failed: {exc}") from exc
    finally:
        for path in added:
            try:
                sys.path.remove(path)
            except ValueError:
                pass
    if not isinstance(output, Mapping):
        raise OcelotReconcileError("Ocelot worker returned no object")
    return dict(output)


def _subprocess_output(payload: Mapping[str, Any], *, root: Path,
                       executable: Path, script: Path,
                       timeout: int) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(root), environment.get("PYTHONPATH", "")) if part)
    try:
        completed = subprocess.run(
            [str(executable), str(script)],
            input=json.dumps(payload), text=True, capture_output=True,
            cwd=str(root), env=environment, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcelotReconcileError(f"Ocelot worker could not run: {exc}") from exc
    if completed.returncode != 0:
        raise OcelotReconcileError(
            "Ocelot worker failed: "
            f"{(completed.stderr or completed.stdout)[:500]}")
    output = _parse_worker_output(completed.stdout)
    if output.get("error"):
        raise OcelotReconcileError(str(output["error"]))
    return output


def ocelot_groups(
    agreement: Mapping[str, Any],
    *,
    ocelot_root: str | Path | None = None,
    python: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    worker: str | Path | None = None,
    mode: str = "subprocess",
) -> dict[str, Any]:
    """Cluster exact agreement rows using Ocelot's graph analogy search.

    ``mode`` is ``subprocess`` (isolated venv), ``inprocess`` (requires
    ``substrate_rs`` importable in this environment and the Ocelot tree on
    ``sys.path``), or ``auto`` (try in-process, then subprocess).
    """
    if mode not in {"subprocess", "inprocess", "auto"}:
        raise OcelotReconcileError("mode must be subprocess, inprocess, or auto")
    root_value = ocelot_root or os.environ.get("OZAGIA_OCELOT")
    if not root_value:
        raise OcelotReconcileError(
            "set OZAGIA_OCELOT or pass ocelot_root to use structural reconciliation")
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise OcelotReconcileError(f"Ocelot checkout not found: {root}")

    findings = list(agreement.get("findings") or [])
    if len(findings) > MAX_FINDINGS:
        raise OcelotReconcileError(f"too many findings for Ocelot (limit {MAX_FINDINGS})")
    payload = {
        "findings": [
            {
                "finding_id": str(finding.get("id") or ""),
                "run_id": str(finding.get("run_id") or ""),
                "wording": str(finding.get("wording") or ""),
                "issue_type": str(finding.get("issue_type") or ""),
                "severity": str(finding.get("severity") or "note"),
                "target_type": _finding_target(finding)[0],
                "target_id": _finding_target(finding)[1],
                "family": canonical_issue_family(finding.get("issue_type")),
            }
            for finding in findings
            if isinstance(finding, Mapping) and finding.get("id")
        ]
    }
    script = Path(worker).expanduser() if worker else _worker_path()
    executable = Path(python or os.environ.get("OZAGIA_OCELOT_PYTHON") or
                      root / ".venv/bin/python").expanduser()
    if mode in {"inprocess", "auto"}:
        try:
            output = _inprocess_output(payload, root=root, worker=script)
        except OcelotReconcileError:
            if mode == "inprocess":
                raise
            output = _subprocess_output(
                payload, root=root, executable=executable, script=script,
                timeout=timeout)
    else:
        output = _subprocess_output(
            payload, root=root, executable=executable, script=script,
            timeout=timeout)

    rows = list(agreement.get("issues") or [])
    finding_to_row: dict[str, int] = {}
    for index, row in enumerate(rows):
        for finding_id in row.get("finding_ids") or []:
            finding_to_row[str(finding_id)] = index
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    clusters = [
        [finding for finding in cluster if finding in finding_to_row]
        for cluster in output.get("clusters") or []
    ]
    for cluster in clusters:
        row_indices = sorted({finding_to_row[finding] for finding in cluster})
        for index in row_indices[1:]:
            union(row_indices[0], index)
    groups_by_root: dict[int, list[int]] = {}
    for index in range(len(rows)):
        groups_by_root.setdefault(find(index), []).append(index)
    groups = sorted(
        (sorted(members) for members in groups_by_root.values()),
        key=lambda members: members[0],
    )
    return {
        "groups": groups,
        "analogies": output.get("analogies") or [],
        "clusters": clusters,
        "engine": output.get("engine") or "ocelot:analogy_search",
        "finding_count": len(findings),
        "truth_assessed": False,
    }


def recluster_agreement_ocelot(
    agreement: Mapping[str, Any],
    *,
    ocelot_root: str | Path | None = None,
    python: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    worker: str | Path | None = None,
    mode: str = "subprocess",
) -> dict[str, Any]:
    """Return an agreement copy with Ocelot structural clusters."""
    from ozagia.research.semantic_reconcile import recluster_agreement
    result = ocelot_groups(
        agreement, ocelot_root=ocelot_root, python=python,
        timeout=timeout, worker=worker, mode=mode,
    )
    return recluster_agreement(
        agreement,
        groups=result["groups"],
        method="ocelot_analogy",
        model=result["engine"],
        threshold=None,
    )


__all__ = [
    "FAMILY_EDGES",
    "OcelotReconcileError",
    "canonical_issue_family",
    "ocelot_groups",
    "recluster_agreement_ocelot",
]
