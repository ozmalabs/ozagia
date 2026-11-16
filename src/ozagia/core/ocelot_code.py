"""Optional Ocelot structural clustering for code-review and repo-scan findings.

The research review path already has an optional Ocelot adapter
(``ozagia.research.ocelot_reconcile``). This module adapts *code* findings into
the same worker payload so coding tasks can use Ocelot's structural analogy
search without making Ocelot a dependency:

- Ocelot is resolved explicitly, through ``ozagia.integrations.ocelot.resolve``
  or a caller-supplied checkout. Nothing is downloaded or installed here.
- Findings keep their original identity. Clusters are always grouped by the
  exact ``(family, target_type, target_id)`` key, so two findings with a
  different issue family or target are never merged.
- When Ocelot is absent or fails, exact clusters are returned as a fallback.
  The result is reported as ``not_installed``/``unavailable``/``error`` instead
  of failing a review or scan.
- A structural analogy is a candidate signal, not a truth verdict. The result
  always has ``truth_assessed: false`` and never adds support edges.

The worker is the script shared with the research adapter
(``examples/ocelot_reconcile_worker.py``). Calling it directly is necessary
because the research adapter derives targets from claim/reference fields,
while code findings target files, symbols and property sets.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.integrations import ocelot as ocelot_installer

SCHEMA = "ozagia.ocelot_code.clusters.v1"
DEFAULT_TIMEOUT = 120
MAX_FINDINGS = 500
MODES = ("auto", "inprocess", "subprocess")

#: Structural families understood by the shared worker. Findings from
#: different families are never merged.
FAMILIES = frozenset({
    "duplicate",
    "evidence",
    "falsifiability",
    "provenance",
    "claim_structure",
    "scope",
    "statistics",
    "methods",
    "other",
})

#: Code-review finding types and repo-scan categories mapped to a structural
#: family. Unknown types fall back to the research adapter's canonical mapping.
FAMILY_BY_TYPE = {
    # code-review finding types
    "breaking_change": "claim_structure",
    "missing_test": "evidence",
    "unhandled_error": "other",
    "stale_reference": "provenance",
    "scope_creep": "scope",
    "duplicate_logic": "duplicate",
    "contract_mismatch": "claim_structure",
    "security_sensitive": "other",
    "performance_risk": "other",
    "unknown": "other",
    # repo-scan categories
    "missing_tests": "evidence",
    "hotspots": "other",
    "duplicate_modules": "duplicate",
    "duplicate_docs": "duplicate",
    "weak_boundaries": "scope",
    "orphaned_files": "provenance",
    "stale_history": "provenance",
    "generated_clutter": "other",
    "unindexed_languages": "other",
    "missing_adapters": "other",
    "test_policy": "evidence",
    "code_review": "other",
    "security": "other",
}

#: Repo-scan categories worth clustering when Ocelot is requested.
SCAN_CATEGORIES = ("duplicate_modules", "duplicate_docs", "weak_boundaries",
                   "security")

RISK_SEVERITY = {"high": "major", "medium": "minor", "low": "note"}


class OcelotCodeError(ValueError):
    """The clustering request itself is invalid."""


# --------------------------------------------------------------------------- #
# finding adaptation
# --------------------------------------------------------------------------- #
def _issue_type(finding: Mapping[str, Any]) -> str:
    for field in ("issue_type", "type", "category"):
        value = finding.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def finding_family(finding: Mapping[str, Any]) -> str:
    """The structural family of a code-review finding or scan opportunity."""
    explicit = finding.get("family")
    if isinstance(explicit, str) and explicit in FAMILIES:
        return explicit
    issue_type = _issue_type(finding).casefold().replace("-", "_")
    mapped = FAMILY_BY_TYPE.get(issue_type)
    if mapped is not None:
        return mapped
    if not issue_type:
        return "other"
    try:
        from ozagia.research.ocelot_reconcile import canonical_issue_family
        family = canonical_issue_family(issue_type)
    except Exception:  # noqa: BLE001 - optional adapter must never break this
        family = "other"
    return family if family in FAMILIES else "other"


def _finding_files(finding: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    file_value = finding.get("file")
    if isinstance(file_value, str) and file_value:
        paths.append(file_value)
    evidence = finding.get("evidence")
    entries: list[Any]
    if isinstance(evidence, Mapping):
        entries = [evidence]
    elif isinstance(evidence, (list, tuple)):
        entries = list(evidence)
    else:
        entries = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("kind") != "file":
            continue
        path = entry.get("path")
        if isinstance(path, str) and path:
            paths.append(path)
    keys = finding.get("graph_keys")
    if isinstance(keys, (list, tuple)):
        for key in keys:
            if isinstance(key, str) and key.startswith("file:"):
                paths.append(key[len("file:"):])
    return sorted({path for path in paths if path})


def finding_target(finding: Mapping[str, Any]) -> tuple[str, str]:
    """``(target_type, target_id)``: the most specific available target.

    A file, file set, symbol or graph node is preferred over a synthetic
    per-finding target. Different targets never merge.
    """
    target_type = finding.get("target_type")
    target_id = finding.get("target_id")
    if (isinstance(target_type, str) and target_type
            and isinstance(target_id, str) and target_id):
        return target_type, target_id

    files = _finding_files(finding)
    if len(files) == 1:
        return "File", files[0]
    if len(files) > 1:
        return "FileSet", "|".join(files)

    proposed = finding.get("proposed_module")
    if isinstance(proposed, str) and proposed.strip() and "/" in proposed:
        return "File", proposed.strip()

    keys = sorted({key for key in finding.get("graph_keys") or []
                   if isinstance(key, str) and key})
    symbols = [key for key in keys if key.startswith("sym:")]
    if len(symbols) == 1:
        return "Symbol", symbols[0]
    if keys:
        return "Node", keys[0]
    return "Finding", str(finding.get("finding_id") or finding.get("id") or "")


def _finding_id(finding: Mapping[str, Any], index: int) -> str:
    for field in ("finding_id", "id"):
        value = finding.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"finding-{index + 1:04d}"


def _wording(finding: Mapping[str, Any]) -> str:
    for field in ("wording", "message", "title", "action"):
        value = finding.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _severity(finding: Mapping[str, Any]) -> str:
    value = finding.get("severity")
    if isinstance(value, str) and value.strip():
        return value.strip().casefold()
    risk = finding.get("risk")
    if isinstance(risk, str):
        return RISK_SEVERITY.get(risk.casefold(), "note")
    return "note"


def _default_run(finding: Mapping[str, Any]) -> str:
    if "category" in finding:
        return "repo_scan"
    return "code_review"


def findings_payload(findings: Iterable[Mapping[str, Any]], *,
                     default_run: str | None = None) -> list[dict[str, Any]]:
    """Adapt findings to the shared Ocelot worker payload.

    Input may be code-review findings, repo-scan opportunities or rows that
    already carry the worker fields. Supplied ``run_id``, ``target_type``,
    ``target_id`` and ``family`` values are preserved. Missing ids are
    synthesized in input order; duplicate ids get a deterministic suffix.
    """
    entries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(findings):
        if not isinstance(raw, Mapping):
            continue
        if len(entries) >= MAX_FINDINGS:
            break
        finding_id = _finding_id(raw, index)
        count = seen.get(finding_id, 0) + 1
        seen[finding_id] = count
        if count > 1:
            finding_id = f"{finding_id}~{count}"
        target_type, target_id = finding_target(raw)
        if target_type == "Finding" and not target_id:
            target_id = finding_id
        run_id = raw.get("run_id") or raw.get("reviewer_id") or raw.get("source")
        if not isinstance(run_id, str) or not run_id:
            run_id = default_run or _default_run(raw)
        entries.append({
            "finding_id": finding_id,
            "run_id": str(run_id),
            "target_type": target_type,
            "target_id": target_id,
            "family": finding_family(raw),
            "severity": _severity(raw),
            "wording": _wording(raw),
        })
    return entries


def _key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(entry.get("family") or "other"),
            str(entry.get("target_type") or "Finding"),
            str(entry.get("target_id") or ""))


def exact_clusters(payload: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Exact fallback clusters keyed by family, target type and target id."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for entry in payload:
        finding_id = str(entry.get("finding_id") or "")
        if not finding_id:
            continue
        groups.setdefault(_key(entry), []).append(finding_id)
    clusters: list[dict[str, Any]] = []
    for key in sorted(groups):
        members = sorted(set(groups[key]))
        clusters.append({
            "key": {
                "family": key[0],
                "target_type": key[1],
                "target_id": key[2],
            },
            "findings": members,
            "size": len(members),
            "origin": "exact",
            "analogy_count": 0,
        })
    return clusters


# --------------------------------------------------------------------------- #
# Ocelot resolution and worker transport
# --------------------------------------------------------------------------- #
def _metadata(resolved: Mapping[str, Any]) -> dict[str, Any] | None:
    path = resolved.get("path")
    if not isinstance(path, str) or not path:
        return None
    return {
        "path": path,
        "version": resolved.get("version"),
        "source": resolved.get("source"),
    }


def _resolve(root: str | Path | None,
             ocelot_root: str | Path | None) -> dict[str, Any]:
    """Resolve a checkout without downloading or installing anything."""
    if ocelot_root is not None:
        try:
            path = Path(ocelot_root).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            return {"state": "error", "path": None, "version": None,
                    "source": "explicit", "error": f"cannot resolve Ocelot root: {exc}"}
        if not path.is_dir():
            return {"state": "unavailable", "path": str(path), "version": None,
                    "source": "explicit",
                    "error": f"Ocelot checkout not found: {path}"}
        return {"state": "ready", "path": str(path), "version": None,
                "source": "explicit", "error": None}
    try:
        resolved = ocelot_installer.resolve(root)
    except ocelot_installer.OcelotError as exc:
        return {"state": "error", "path": None, "version": None, "source": None,
                "error": str(exc)}
    state = str(resolved.get("state") or "unknown")
    if state == "ready" and resolved.get("path"):
        return {
            "state": "ready",
            "path": resolved.get("path"),
            "version": resolved.get("version"),
            "source": resolved.get("source"),
            "error": None,
            "note": resolved.get("note"),
        }
    return {
        "state": "not_installed" if state == "not_installed" else "unavailable",
        "path": resolved.get("path"),
        "version": resolved.get("version"),
        "source": resolved.get("source"),
        "error": None,
        "note": resolved.get("note"),
    }


def _worker_output(payload: Mapping[str, Any], *, ocelot_root: Path,
                   mode: str, timeout: int) -> tuple[dict[str, Any], str]:
    """Run the shared worker in-process or in Ocelot's interpreter."""
    from ozagia.research import ocelot_reconcile as reconcile
    script = reconcile._worker_path()
    if not script.is_file():
        raise reconcile.OcelotReconcileError(f"Ocelot worker not found: {script}")
    executable = Path(
        os.environ.get("OZAGIA_OCELOT_PYTHON")
        or ocelot_root / ".venv/bin/python").expanduser()
    if mode in {"inprocess", "auto"}:
        try:
            output = reconcile._inprocess_output(
                payload, root=ocelot_root, worker=script)
            return output, "inprocess"
        except reconcile.OcelotReconcileError:
            if mode == "inprocess":
                raise
    output = reconcile._subprocess_output(
        payload, root=ocelot_root, executable=executable, script=script,
        timeout=timeout)
    if not isinstance(output, Mapping):
        raise reconcile.OcelotReconcileError(
            "Ocelot worker returned no JSON object")
    return dict(output), "subprocess"


# --------------------------------------------------------------------------- #
# safe cluster assembly
# --------------------------------------------------------------------------- #
def _safe_groups(raw: Any, payload: Sequence[Mapping[str, Any]]
                 ) -> list[list[str]]:
    """Worker clusters filtered to known ids and split by exact key."""
    meta = {str(entry.get("finding_id")): entry for entry in payload
            if entry.get("finding_id")}
    groups: set[tuple[str, ...]] = set()
    if not isinstance(raw, (list, tuple)):
        return []
    for cluster in raw:
        if not isinstance(cluster, (list, tuple)):
            continue
        by_key: dict[tuple[str, str, str], list[str]] = {}
        for value in cluster:
            finding_id = str(value)
            entry = meta.get(finding_id)
            if entry is None:
                continue
            by_key.setdefault(_key(entry), []).append(finding_id)
        for members in by_key.values():
            unique = tuple(sorted(set(members)))
            if len(unique) > 1:
                groups.add(unique)
    return [list(group) for group in sorted(groups)]


def _safe_analogies(raw: Any, payload: Sequence[Mapping[str, Any]]
                    ) -> list[dict[str, Any]]:
    """Analogies whose endpoints are known and share family and target."""
    meta = {str(entry.get("finding_id")): entry for entry in payload
            if entry.get("finding_id")}
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        left = str(item.get("source_finding") or "")
        right = str(item.get("target_finding") or "")
        if left == right or left not in meta or right not in meta:
            continue
        if _key(meta[left]) != _key(meta[right]):
            continue
        pair = tuple(sorted((left, right)))
        if pair in seen:
            continue
        seen.add(pair)
        entry = dict(item)
        entry["family"] = _key(meta[left])[0]
        entry["target_type"] = _key(meta[left])[1]
        entry["target_id"] = _key(meta[left])[2]
        out.append(entry)
    out.sort(key=lambda entry: (
        str(entry.get("source_finding") or ""),
        str(entry.get("target_finding") or ""),
        str(entry.get("order_n") or ""),
    ))
    return out


def _assemble(payload: Sequence[Mapping[str, Any]],
              groups: Sequence[Sequence[str]],
              analogies: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    clusters = exact_clusters(payload)
    safe_sets = [frozenset(group) for group in groups]
    for cluster in clusters:
        members = frozenset(cluster["findings"])
        linked = any(
            group and group.issubset(members) for group in safe_sets)
        analogy_count = sum(
            1 for analogy in analogies
            if str(analogy.get("source_finding")) in members
            and str(analogy.get("target_finding")) in members)
        if linked or analogy_count:
            cluster["origin"] = "exact+ocelot"
        cluster["analogy_count"] = analogy_count
    return clusters


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def cluster_findings(findings: Iterable[Mapping[str, Any]], *,
                     root: str | Path | None = None,
                     ocelot_root: str | Path | None = None,
                     mode: str = "auto",
                     timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Cluster code findings, using Ocelot when a checkout is available.

    Never raises because Ocelot is missing or fails: the result records
    ``state``, ``available``, ``engine`` and an exact fallback. Invalid
    arguments (an unknown mode or a non-positive timeout) raise
    :class:`OcelotCodeError`.
    """
    if mode not in MODES:
        raise OcelotCodeError(f"mode must be one of {MODES}")
    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError) as exc:
        raise OcelotCodeError("timeout must be a positive integer") from exc
    if timeout_value <= 0:
        raise OcelotCodeError("timeout must be a positive integer")

    items = [item for item in findings if isinstance(item, Mapping)]
    payload = findings_payload(items)
    omitted = max(0, len(items) - len(payload))
    exact = exact_clusters(payload)
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "requested": True,
        "available": False,
        "state": "no_findings",
        "mode": mode,
        "actual_mode": None,
        "engine": "exact",
        "ocelot": None,
        "clusters": exact,
        "ocelot_clusters": [],
        "analogies": [],
        "finding_count": len(payload),
        "cluster_count": len(exact),
        "truncated": False,
        "omitted_findings": 0,
        "truth_assessed": False,
        "error": None,
        "note": "no findings to cluster; Ocelot was not called",
    }
    if not payload:
        if omitted:
            base["truncated"] = True
            base["omitted_findings"] = omitted
        return base
    base["truncated"] = omitted > 0
    base["omitted_findings"] = omitted
    if omitted:
        base["note"] = (
            f"{omitted} finding(s) were not clustered because the worker "
            f"limit is {MAX_FINDINGS}.")

    try:
        resolution = _resolve(root, ocelot_root)
    except Exception as exc:  # noqa: BLE001 - resolution must not fail a review
        base["state"] = "error"
        base["error"] = f"{type(exc).__name__}: {exc}"
        base["note"] = (
            "Ocelot resolution failed; exact clusters by issue family and "
            "target are reported. No install or download was attempted.")
        return base
    base["ocelot"] = _metadata(resolution)
    if resolution.get("state") != "ready":
        base["state"] = str(resolution.get("state") or "unavailable")
        base["error"] = resolution.get("error")
        base["note"] = (
            "Ocelot is unavailable; exact clusters by issue family and "
            "target are reported. No install or download was attempted."
            + (f" {resolution['note']}" if resolution.get("note") else ""))
        return base

    try:
        output, actual_mode = _worker_output(
            {"findings": payload},
            ocelot_root=Path(str(resolution["path"])),
            mode=mode, timeout=timeout_value)
    except Exception as exc:  # noqa: BLE001 - report, never fail the caller
        base["state"] = "error"
        base["error"] = f"{type(exc).__name__}: {exc}"
        base["note"] = (
            "Ocelot failed; exact clusters by issue family and target are "
            "reported. The review or scan is not failed by this error.")
        return base

    groups = _safe_groups(output.get("clusters"), payload)
    analogies = _safe_analogies(output.get("analogies"), payload)
    clusters = _assemble(payload, groups, analogies)
    base.update({
        "available": True,
        "state": "ready",
        "actual_mode": actual_mode,
        "engine": str(output.get("engine") or "ocelot:analogy_search"),
        "clusters": clusters,
        "ocelot_clusters": groups,
        "analogies": analogies,
        "cluster_count": len(clusters),
        "note": (
            "Ocelot structural analogy is a candidate signal, not a truth "
            "verdict. Exact findings and dissent remain; different families "
            "or targets are never merged."),
    })
    return base


def _failure_section(findings: Iterable[Mapping[str, Any]], mode: str,
                     exc: Exception) -> dict[str, Any]:
    payload = findings_payload(findings)
    clusters = exact_clusters(payload)
    return {
        "schema": SCHEMA,
        "requested": True,
        "available": False,
        "state": "error",
        "mode": mode,
        "actual_mode": None,
        "engine": "exact",
        "ocelot": None,
        "clusters": clusters,
        "ocelot_clusters": [],
        "analogies": [],
        "finding_count": len(payload),
        "cluster_count": len(clusters),
        "truncated": False,
        "omitted_findings": 0,
        "truth_assessed": False,
        "error": f"{type(exc).__name__}: {exc}",
        "note": (
            "Ocelot clustering failed; exact clusters by issue family and "
            "target are reported and the caller is not failed."),
    }


def cluster_section(findings: Iterable[Mapping[str, Any]], *,
                    root: str | Path | None = None,
                    ocelot_root: str | Path | None = None,
                    mode: str = "auto",
                    timeout: int = DEFAULT_TIMEOUT,
                    clusterer: Any = None) -> dict[str, Any]:
    """A JSON-safe Ocelot section for a review packet or scan report.

    ``clusterer`` is an injection seam with the same signature as
    :func:`cluster_findings`; an error in it becomes an ``error`` section with
    exact fallback clusters rather than an exception.
    """
    runner = clusterer or cluster_findings
    items = list(findings)
    try:
        result = runner(
            items, root=root, ocelot_root=ocelot_root,
            mode=mode, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a review must report, not crash
        return _failure_section(items, mode, exc)
    if not isinstance(result, Mapping):
        return _failure_section(
            items, mode,
            TypeError("the Ocelot clusterer returned no object"))
    section = dict(result)
    section["requested"] = True
    section["truth_assessed"] = False
    return section


#: A later seam for test-impact analogies (similar change shapes -> similar
#: test sets) and architecture-pattern analogies. Both need change-shape and
#: import-shape projections that this slice does not build.


__all__ = [
    "DEFAULT_TIMEOUT",
    "FAMILIES",
    "FAMILY_BY_TYPE",
    "MAX_FINDINGS",
    "MODES",
    "OcelotCodeError",
    "RISK_SEVERITY",
    "SCAN_CATEGORIES",
    "SCHEMA",
    "cluster_findings",
    "cluster_section",
    "exact_clusters",
    "finding_family",
    "finding_target",
    "findings_payload",
]
