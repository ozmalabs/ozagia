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

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import state_path, state_rel
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


# --------------------------------------------------------------------------- #
# change shapes and test-impact analogies
# --------------------------------------------------------------------------- #
SHAPE_SCHEMA = "ozagia.ocelot_code.change_shape.v1"
IMPACT_SCHEMA = "ozagia.ocelot_code.impact.v1"
PATTERN_SCHEMA = "ozagia.ocelot_code.patterns.v1"

MAX_SHAPE_FILES = 500
MAX_SHAPE_SYMBOLS = 1000
MAX_SHAPES = 200
MAX_PATTERN_SHAPES = 500
MAX_PATTERN_FILES = 2000
DEFAULT_MIN_OVERLAP = 0.5

TEST_EDGE_TYPES = frozenset({
    "has_test", "tests_module", "mentions_file", "tested_by_file",
})
_IMPORT_EDGE = "imports"
_DOC_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt",
})
_CONFIG_SUFFIXES = frozenset({
    ".toml", ".yaml", ".yml", ".json", ".jsonc", ".ini", ".cfg", ".conf",
    ".lock", ".xml", ".properties",
})
_CONFIG_NAMES = frozenset({
    "pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", ".gitignore",
    ".gitattributes", "requirements.txt", "requirements-dev.txt",
    "Makefile", "Dockerfile",
})
_TEST_SUFFIXES = frozenset({
    ".py", ".pyi", ".go", ".rs", ".js", ".jsx", ".mjs", ".cjs", ".ts",
    ".tsx", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".scala",
})
_TEST_DIR_NAMES = frozenset({"tests", "test", "spec", "specs", "__tests__"})
_TEST_NAME = re.compile(
    r"(?:^test_.*|.*_test|.*\.test|.*\.spec)\."
    r"(?:py|pyi|go|rs|js|jsx|mjs|cjs|ts|tsx|java|kt|rb|php|cs|swift|scala)\Z")
_CACHE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".git", ".ozagia",
})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalise_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return None
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def _relative_path(root: Path, value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None
    return _normalise_path(value)


def _is_test_path(rel: str) -> bool:
    path = PurePosixPath(rel)
    if path.name == "conftest.py" or path.suffix.lower() not in _TEST_SUFFIXES:
        return False
    if _TEST_NAME.match(path.name):
        return True
    return (any(part in _TEST_DIR_NAMES for part in path.parts[:-1])
            and path.name.startswith("test_"))


def _path_kind(rel: str) -> str:
    path = PurePosixPath(rel)
    if _is_test_path(rel):
        return "test"
    if (path.suffix.lower() in _DOC_SUFFIXES
            or (path.parts and path.parts[0] in {"docs", "doc"})):
        return "docs"
    if (path.name in _CONFIG_NAMES
            or path.suffix.lower() in _CONFIG_SUFFIXES
            or (path.parts and path.parts[0] in {"ozagia", ".github"})):
        return "config"
    return "source"


def _change_family(kinds: Iterable[str]) -> str:
    values = {str(kind) for kind in kinds if str(kind)}
    if not values:
        return "empty"
    if len(values) == 1:
        return values.pop()
    return "mixed"


def _package_dirs(root: Path | None, rels: Iterable[str]) -> list[str]:
    dirs: set[str] = set()
    for rel in rels:
        path = PurePosixPath(rel)
        if path.is_absolute() or ".." in path.parts:
            continue
        parent = path.parent
        if parent == PurePosixPath("."):
            continue
        dirs.add(parent.as_posix())
        parts = parent.parts
        if parts:
            dirs.add(parts[0])
            if parts[0] in {"src", "lib", "app"} and len(parts) >= 2:
                dirs.add("/".join(parts[:2]))
        if root is not None:
            current = parent
            while current != PurePosixPath(".") and current.parts:
                if (root / current / "__init__.py").is_file():
                    dirs.add(current.as_posix())
                current = current.parent
    return sorted(dirs)


def _suffixes(rels: Iterable[str]) -> list[str]:
    return sorted({PurePosixPath(rel).suffix.lower() for rel in rels
                   if PurePosixPath(rel).suffix})


def _parse_diff_spec(spec: Any) -> tuple[str, str]:
    if not isinstance(spec, str) or ".." not in spec:
        raise OcelotCodeError("diff must be a BASE..HEAD range")
    base, _, head = spec.partition("..")
    base = base.strip()
    head = head.strip()
    if not base or not head:
        raise OcelotCodeError("diff must name both BASE and HEAD")
    return base, head


def _git(root: Path, *args: str) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True,
            check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _split_z(out: bytes) -> list[str]:
    return [value.decode("utf-8", "replace") for value in out.split(b"\0")
            if value]


def _repo_files(root: Path) -> list[str] | None:
    out = _git(root, "ls-files", "-z", "-c", "-o", "--exclude-standard")
    if out is None:
        return None
    state = state_rel(root)
    values: list[str] = []
    for raw in _split_z(out):
        rel = _normalise_path(raw)
        if rel and not (state and (rel == state or rel.startswith(state + "/"))):
            values.append(rel)
    return sorted(set(values))


def _walk_files(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _CACHE_DIRS)
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in sorted(filenames):
            found.append(f"{rel_dir}/{name}" if rel_dir else name)
            if len(found) >= MAX_PATTERN_FILES:
                return found
    return found


def _repo_tests(root: Path) -> list[str]:
    files = _repo_files(root)
    if files is None:
        files = _walk_files(root)
    return [rel for rel in files if _is_test_path(rel)]


def _changed_from_diff(root: Path, diff: Any) -> list[str]:
    base, head = _parse_diff_spec(diff)
    try:
        from ozagia.core import testing as test_plans
        return test_plans.changed_files(root, base=base, head=head)
    except (OSError, ValueError, RuntimeError) as exc:
        raise OcelotCodeError(f"cannot read diff {diff!r}: {exc}") from exc


def _normalise_changed(root: Path, changed: Any) -> list[str]:
    if isinstance(changed, str):
        raw_items: Iterable[Any] = [changed]
    elif isinstance(changed, Mapping):
        raw_items = list(changed)
    elif isinstance(changed, Iterable):
        raw_items = list(changed)
    else:
        return []
    out: list[str] = []
    for value in raw_items:
        rel = _relative_path(root, value)
        if rel:
            out.append(rel)
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# read-only graph projection for shapes
# --------------------------------------------------------------------------- #
def _graph_default_path(root: Path, graph_path: str | Path | None) -> Path:
    if graph_path is None:
        return state_path(root, "graph.json")
    path = Path(graph_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _graph_info(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "available": False,
        "freshness": "missing",
        "usable": False,
        "snapshot_head": None,
        "snapshot_root": None,
        "nodes": 0,
        "edges": 0,
        "reason": None,
        "root": str(root),
    }


def _node_path(entry: Mapping[str, Any]) -> str | None:
    attrs = entry.get("attrs")
    if not isinstance(attrs, Mapping):
        return None
    value = attrs.get("path") or attrs.get("file")
    return value if isinstance(value, str) and value else None


def _graph_index(dev: Any) -> dict[str, Any]:
    key_file = None
    try:
        from ozagia.core.keys import key_file as _key_file
        key_file = _key_file
    except Exception:  # noqa: BLE001 - keys is local and optional here
        key_file = None

    nodes: dict[int, dict[str, Any]] = {}
    files_by_path: dict[str, int] = {}
    symbols_by_file: dict[str, list[int]] = {}
    tests_by_file: dict[str, list[int]] = {}
    test_file_by_key: dict[str, str] = {}
    for nid, raw in dev.nodes.items():
        attrs = raw.get("attrs")
        if not isinstance(attrs, Mapping):
            attrs = {}
        entry = {
            "type": raw.get("type"),
            "key": dev.key_of(int(nid)),
            "attrs": dict(attrs),
        }
        nodes[int(nid)] = entry
        node_type = entry["type"]
        path = _node_path(entry)
        if node_type == "File" and path:
            files_by_path[path] = int(nid)
        elif node_type == "Symbol" and path:
            symbols_by_file.setdefault(path, []).append(int(nid))
        elif node_type == "Test":
            if path:
                tests_by_file.setdefault(path, []).append(int(nid))
            key = entry["key"]
            if key and path:
                test_file_by_key[key] = path

    edge_types_by_path: dict[str, set[str]] = {}
    file_tests: dict[str, set[str]] = {}
    file_test_files: dict[str, set[str]] = {}
    imports: dict[str, set[str]] = {}
    imported_by: dict[str, set[str]] = {}

    def test_identity(entry: Mapping[str, Any]) -> tuple[str, str]:
        path = _node_path(entry) or ""
        key = str(entry.get("key") or "")
        if not key and path:
            attrs = entry.get("attrs") or {}
            key = f"test:{path}::{attrs.get('qualname') or ''}"
        return path, key

    def add_test(source_path: str, test_entry: Mapping[str, Any]) -> None:
        test_path, key = test_identity(test_entry)
        if not source_path or not test_path:
            return
        file_test_files.setdefault(source_path, set()).add(test_path)
        if key:
            file_tests.setdefault(source_path, set()).add(key)

    for edge in dev.edges:
        try:
            src, tgt = int(edge["src"]), int(edge["tgt"])
        except (KeyError, TypeError, ValueError):
            continue
        etype = str(edge.get("type") or "")
        source = nodes.get(src)
        target = nodes.get(tgt)
        if source is None or target is None:
            continue
        for entry in (source, target):
            path = _node_path(entry)
            if path:
                edge_types_by_path.setdefault(path, set()).add(etype)
        if etype in TEST_EDGE_TYPES:
            if source["type"] == "Test" and target["type"] == "File":
                add_test(_node_path(target) or "", source)
            elif target["type"] == "Test" and source["type"] == "File":
                add_test(_node_path(source) or "", target)
        elif etype == _IMPORT_EDGE:
            source_path = (_node_path(source)
                           if source["type"] == "File" else None)
            target_path = (_node_path(target)
                           if target["type"] == "File" else None)
            if source_path and target_path:
                imports.setdefault(source_path, set()).add(target_path)
                imported_by.setdefault(target_path, set()).add(source_path)

    for path in symbols_by_file:
        symbols_by_file[path].sort()
    for path in tests_by_file:
        tests_by_file[path].sort()
    return {
        "nodes": nodes,
        "files_by_path": files_by_path,
        "symbols_by_file": symbols_by_file,
        "tests_by_file": tests_by_file,
        "test_file_by_key": test_file_by_key,
        "edge_types_by_path": edge_types_by_path,
        "file_tests": file_tests,
        "file_test_files": file_test_files,
        "imports": imports,
        "imported_by": imported_by,
        "key_file": key_file,
    }


def _load_shape_graph(root: Path,
                      graph_path: str | Path | None) -> tuple[
                          dict[str, Any], dict[str, Any] | None]:
    path = _graph_default_path(root, graph_path)
    info = _graph_info(root, path)
    if not path.is_file():
        info["reason"] = f"no graph snapshot at {path}"
        return info, None
    try:
        from ozagia.core.memory import DevMemory
        dev = DevMemory(dump=path, root=root)
        staleness = dev.staleness()
    except Exception as exc:  # noqa: BLE001 - any load failure is a state
        info["available"] = True
        info["freshness"] = "error"
        info["reason"] = f"cannot read graph snapshot: {exc}"
        return info, None
    info["available"] = True
    repo_attrs: dict[str, Any] = {}
    for raw in dev.nodes.values():
        if raw.get("type") == "Repo":
            attrs = raw.get("attrs")
            repo_attrs = dict(attrs) if isinstance(attrs, Mapping) else {}
            break
    info.update({
        "snapshot_head": repo_attrs.get("head"),
        "snapshot_root": repo_attrs.get("root"),
        "nodes": len(dev.nodes),
        "edges": len(dev.edges),
    })
    reasons: list[str] = []
    snapshot_root = repo_attrs.get("root")
    if snapshot_root:
        try:
            root_matches = Path(str(snapshot_root)).resolve() == root
        except (OSError, RuntimeError, ValueError):
            root_matches = False
        if not root_matches:
            reasons.append(f"snapshot root {snapshot_root} is not {root}")
    freshness = str(staleness.get("freshness") or "unknown")
    if reasons:
        info["freshness"] = "stale"
        info["reason"] = "; ".join(reasons)
    else:
        info["freshness"] = freshness
        if freshness == "fresh":
            info["reason"] = "snapshot root and working-tree digest match"
        elif freshness == "stale":
            detail: list[str] = []
            if staleness.get("root_mismatch"):
                detail.append("snapshot root differs")
            if staleness.get("tree_changed"):
                detail.append("working tree changed since the snapshot")
            if (staleness.get("snapshot_head")
                    and staleness.get("snapshot_head") != staleness.get("head")):
                detail.append("HEAD moved since the snapshot")
            info["reason"] = "; ".join(detail) or "snapshot is stale"
        else:
            errors = staleness.get("verification_errors") or []
            info["reason"] = ("; ".join(str(value) for value in errors)
                              or "graph freshness could not be verified")
    info["usable"] = info["freshness"] == "fresh"
    if not info["usable"]:
        return info, None
    return info, _graph_index(dev)


def _filename_test_links(path: str, repo_tests: Sequence[str]) -> list[str]:
    target = PurePosixPath(path)
    stem, suffix = target.stem, target.suffix
    names = {
        f"test_{stem}{suffix}", f"{stem}_test{suffix}",
        f"{stem}.test{suffix}", f"{stem}.spec{suffix}",
    }
    return sorted(test for test in repo_tests
                  if PurePosixPath(test).name in names)


def _graph_symbol_rows(index: Mapping[str, Any],
                       path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    nodes = index.get("nodes") or {}
    for nid in (index.get("symbols_by_file") or {}).get(path, ()):
        entry = nodes.get(nid) or {}
        attrs = entry.get("attrs") or {}
        qualname = str(attrs.get("qualname") or attrs.get("name") or "")
        if not qualname:
            continue
        rows.append({
            "key": entry.get("key"),
            "file": path,
            "qualname": qualname,
            "name": qualname.rsplit(".", 1)[-1],
            "start_line": attrs.get("start_line"),
            "end_line": attrs.get("end_line"),
        })
    return rows


def _test_links(root: Path, changed: Sequence[str],
                index: Mapping[str, Any] | None,
                repo_tests: Sequence[str]) -> tuple[list[dict[str, Any]],
                                                    list[str]]:
    links: list[dict[str, Any]] = []
    all_tests: set[str] = set()
    for path in changed:
        tests: list[str] = []
        keys: list[str] = []
        source = "filename"
        if _path_kind(path) == "test":
            tests = [path]
            source = "changed_test"
        elif index is not None:
            tests = sorted((index.get("file_test_files") or {}).get(path, ()))
            keys = sorted((index.get("file_tests") or {}).get(path, ()))
            if tests:
                source = "graph"
        if not tests:
            tests = _filename_test_links(path, repo_tests)
        if tests:
            all_tests.update(tests)
            links.append({
                "file": path,
                "tests": tests,
                "node_ids": keys,
                "source": source,
            })
        else:
            links.append({
                "file": path,
                "tests": [],
                "node_ids": [],
                "source": source,
            })
    return links, sorted(all_tests)


def _shape_signature(family: str, changed: Sequence[str],
                     kinds: Mapping[str, str], symbols: Sequence[str],
                     edge_types: Sequence[str], test_files: Sequence[str],
                     package_dirs: Sequence[str],
                     import_dirs: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "family": family,
        "files": sorted(changed),
        "kinds": sorted(set(kinds.values())),
        "symbols": sorted(set(symbols)),
        "edge_types": sorted(set(edge_types)),
        "test_files": sorted(set(test_files)),
        "package_dirs": sorted(set(package_dirs)),
        "import_dirs": sorted(set(import_dirs)),
    }


def _test_plan_for_shape(root: Path, changed: Sequence[str],
                         graph_path: str | Path | None) -> dict[str, Any]:
    try:
        from ozagia.core import testing as test_plans
        plan = test_plans.plan(
            root, changed=sorted(changed), task_class="test_impact",
            graph_path=graph_path)
    except Exception as exc:  # noqa: BLE001 - a shape must still be returned
        return {
            "available": False,
            "selected_files": [],
            "executed": False,
            "truth_assessed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "the shape records a test selection only when it succeeds",
        }
    return {
        "schema": test_plans.SCHEMA,
        "available": True,
        "mode": plan.get("mode"),
        "requested_mode": plan.get("requested_mode"),
        "selected_files": list(plan.get("selected_files") or []),
        "selected_tests": list(plan.get("selected_tests") or []),
        "fallback_used": bool(plan.get("fallback_used")),
        "fallback_reason": plan.get("fallback_reason"),
        "refused": bool(plan.get("refused")),
        "graph_freshness": plan.get("graph_freshness"),
        "executed": False,
        "truth_assessed": False,
        "claim": "test selection only; no test was executed",
    }


def change_shape(root: str | Path, *, diff: str | None = None,
                 changed: Any = None, graph_path: str | Path | None = None,
                 with_test_plan: bool = False) -> dict[str, Any]:
    """A deterministic structural shape for one change.

    Changed files come from an explicit ``changed`` list or from a
    ``BASE..HEAD`` ``diff``. With neither, the working tree is compared with
    ``HEAD`` (untracked files included). Symbols, edge types, test links and
    import directories come from a fresh graph snapshot; a missing or stale
    snapshot is reported and filename-convention test links are used instead.
    The digest covers the structural fields, not the range, root or test plan.
    """
    root_p = Path(root).expanduser()
    try:
        root_p = root_p.resolve()
    except (OSError, RuntimeError) as exc:
        raise OcelotCodeError(f"cannot resolve root {root!r}: {exc}") from exc
    if not root_p.is_dir():
        raise OcelotCodeError(f"root is not a directory: {root_p}")
    if diff is not None and changed is not None:
        raise OcelotCodeError("pass diff or changed, not both")

    range_info: dict[str, Any]
    error: str | None = None
    if diff is not None:
        base, head = _parse_diff_spec(diff)
        try:
            paths = _changed_from_diff(root_p, diff)
        except OcelotCodeError as exc:
            paths = []
            error = str(exc)
        range_info = {"spec": diff, "base": base, "head": head,
                      "source": "diff"}
    elif changed is not None:
        paths = _normalise_changed(root_p, changed)
        range_info = {"spec": None, "base": None, "head": None,
                      "source": "changed"}
    else:
        try:
            from ozagia.core import testing as test_plans
            paths = test_plans.changed_files(root_p)
        except (OSError, ValueError, RuntimeError) as exc:
            paths = []
            error = f"{type(exc).__name__}: {exc}"
        range_info = {"spec": "HEAD", "base": "HEAD", "head": None,
                      "source": "working_tree"}

    omitted_files = max(0, len(paths) - MAX_SHAPE_FILES)
    changed_files = sorted(paths)[:MAX_SHAPE_FILES]
    kinds = {path: _path_kind(path) for path in changed_files}
    family = _change_family(kinds.values())
    graph_info, index = _load_shape_graph(root_p, graph_path)

    symbol_rows: list[dict[str, Any]] = []
    symbols_truncated = False
    if index is not None:
        for path in changed_files:
            symbol_rows.extend(_graph_symbol_rows(index, path))
        symbol_rows.sort(key=lambda row: (
            str(row.get("file") or ""), int(row.get("start_line") or 0),
            str(row.get("qualname") or "")))
        if len(symbol_rows) > MAX_SHAPE_SYMBOLS:
            symbol_rows = symbol_rows[:MAX_SHAPE_SYMBOLS]
            symbols_truncated = True
    symbol_names = sorted({str(row["qualname"]) for row in symbol_rows})

    edge_types: set[str] = set()
    import_dirs: set[str] = set()
    if index is not None:
        for path in changed_files:
            edge_types.update(
                (index.get("edge_types_by_path") or {}).get(path, ()))
            for related in (index.get("imports") or {}).get(path, ()):
                parent = PurePosixPath(related).parent.as_posix()
                if parent not in ("", "."):
                    import_dirs.add(parent)
            for related in (index.get("imported_by") or {}).get(path, ()):
                parent = PurePosixPath(related).parent.as_posix()
                if parent not in ("", "."):
                    import_dirs.add(parent)

    repo_tests = _repo_tests(root_p) if changed_files else []
    test_links, test_files = _test_links(
        root_p, changed_files, index, repo_tests)
    package_dirs = _package_dirs(root_p, changed_files)
    import_dirs_list = sorted(import_dirs)

    signature = _shape_signature(
        family, changed_files, kinds, symbol_names, sorted(edge_types),
        test_files, package_dirs, import_dirs_list)
    digest = _digest(signature)
    shape_id = f"shape-{digest[:16]}"

    test_plan: dict[str, Any] | None = None
    planned_tests: list[str] = []
    if with_test_plan:
        test_plan = _test_plan_for_shape(root_p, changed_files, graph_path)
        planned_tests = list(test_plan.get("selected_files") or [])

    not_assessed = [
        "the shape describes the change's structure, not its correctness",
        "no test was executed and a similar test set is never complete",
        "structural similarity is a candidate signal, not a causal or truth "
        "verdict",
    ]
    if index is None:
        not_assessed.append(
            "symbols, edge types and graph test links were not assessed: "
            f"graph freshness is {graph_info['freshness']}"
            + (f" ({graph_info.get('reason')})" if graph_info.get("reason")
               else ""))
    if omitted_files:
        not_assessed.append(
            f"{omitted_files} changed file(s) are outside the shape limit "
            f"of {MAX_SHAPE_FILES}")
    if symbols_truncated:
        not_assessed.append(
            f"symbols are truncated at {MAX_SHAPE_SYMBOLS} per shape")
    return {
        "schema": SHAPE_SCHEMA,
        "state": "error" if error else ("no_changes" if not changed_files
                                        else "ok"),
        "root": str(root_p),
        "range": range_info,
        "changed_files": changed_files,
        "changed_kinds": kinds,
        "family": family,
        "symbols": symbol_rows,
        "symbol_names": symbol_names,
        "edge_types": sorted(edge_types),
        "test_links": test_links,
        "test_files": test_files,
        "package_dirs": package_dirs,
        "import_dirs": import_dirs_list,
        "suffixes": _suffixes(changed_files),
        "graph": dict(graph_info),
        "digest": digest,
        "shape_id": shape_id,
        "test_plan": test_plan,
        "planned_tests": planned_tests,
        "truncated": bool(omitted_files or symbols_truncated),
        "omitted_files": omitted_files,
        "truth_assessed": False,
        "error": error,
        "not_assessed": not_assessed,
    }


# --------------------------------------------------------------------------- #
# shared shape clustering
# --------------------------------------------------------------------------- #
class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        self.parent.setdefault(value, value)
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            nxt = self.parent[value]
            self.parent[value] = root
            value = nxt
        return root

    def union(self, left: str, right: str) -> bool:
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        self.parent[b] = a
        return True


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        values: Iterable[Any] = value.keys()
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return []
    out: list[str] = []
    for item in values:
        if isinstance(item, str) and item:
            out.append(item)
        elif isinstance(item, Mapping):
            for field in ("file", "path", "key", "name", "qualname"):
                got = item.get(field)
                if isinstance(got, str) and got:
                    out.append(got)
                    break
    return sorted(set(out))


def _shape_test_files(shape: Mapping[str, Any]) -> list[str]:
    values = set(_as_string_list(shape.get("test_files")))
    links = shape.get("test_links")
    if isinstance(links, Mapping):
        for tests in links.values():
            values.update(_as_string_list(tests))
    elif isinstance(links, (list, tuple)):
        for link in links:
            if isinstance(link, Mapping):
                values.update(_as_string_list(link.get("tests")))
    return sorted(values)


def _shape_symbol_names(shape: Mapping[str, Any]) -> list[str]:
    values = set(_as_string_list(shape.get("symbol_names")))
    for item in _as_string_list(shape.get("symbols")):
        values.add(item)
    symbols = shape.get("symbols")
    if isinstance(symbols, (list, tuple)):
        for item in symbols:
            if isinstance(item, Mapping):
                value = item.get("qualname") or item.get("name")
                if isinstance(value, str) and value:
                    values.add(value)
    return sorted(values)


def _shape_kinds(shape: Mapping[str, Any]) -> dict[str, str]:
    raw = shape.get("changed_kinds")
    if isinstance(raw, Mapping):
        return {str(path): str(kind) for path, kind in raw.items()
                if isinstance(path, str) and isinstance(kind, str)}
    return {path: _path_kind(path)
            for path in _as_string_list(shape.get("changed_files"))}


def _shape_family(shape: Mapping[str, Any]) -> str:
    explicit = shape.get("family")
    if isinstance(explicit, str) and explicit:
        return explicit
    return _change_family(_shape_kinds(shape).values())


def _normalise_impact_shapes(shapes: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(shapes):
        if not isinstance(raw, Mapping):
            continue
        if len(out) >= MAX_SHAPES:
            break
        shape = dict(raw)
        changed = _as_string_list(shape.get("changed_files"))
        if not changed:
            changed = _as_string_list(shape.get("files"))
        changed = sorted({path for path in
                          (_normalise_path(value) for value in changed)
                          if path})
        kinds = _shape_kinds({**shape, "changed_files": changed})
        family = _shape_family({**shape, "changed_files": changed,
                                "changed_kinds": kinds})
        symbols = _shape_symbol_names(shape)
        test_files = _shape_test_files(shape)
        package_dirs = _as_string_list(shape.get("package_dirs"))
        if not package_dirs:
            package_dirs = _package_dirs(None, changed)
        import_dirs = _as_string_list(shape.get("import_dirs"))
        edge_types = _as_string_list(shape.get("edge_types"))
        suffixes = _as_string_list(shape.get("suffixes"))
        if not suffixes:
            suffixes = _suffixes(changed)
        signature = _shape_signature(
            family, changed, kinds, symbols, edge_types, test_files,
            package_dirs, import_dirs)
        # Recompute the digest from the normalised structural fields. A stale
        # or caller-supplied digest never merges shapes that differ.
        digest = _digest(signature)
        base_id = str(shape.get("shape_id") or shape.get("id")
                      or f"shape-{digest[:16]}")
        count = seen.get(base_id, 0) + 1
        seen[base_id] = count
        shape_id = base_id if count == 1 else f"{base_id}~{count}"
        planned = _as_string_list(shape.get("planned_tests"))
        plan = shape.get("test_plan")
        if isinstance(plan, Mapping):
            planned = sorted(set(planned)
                             | set(_as_string_list(plan.get("selected_files"))))
        shape.update({
            "shape_id": shape_id,
            "changed_files": changed,
            "changed_kinds": kinds,
            "kinds": sorted(set(kinds.values())),
            "family": family,
            "symbols": shape.get("symbols") or [],
            "symbol_names": symbols,
            "edge_types": edge_types,
            "test_files": test_files,
            "package_dirs": package_dirs,
            "import_dirs": import_dirs,
            "suffixes": suffixes,
            "digest": digest,
            "planned_tests": planned,
            "index": index,
        })
        out.append(shape)
    return out


def _impact_tokens(shape: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for path in _as_string_list(shape.get("changed_files")):
        tokens.add(f"file:{path}")
        parent = PurePosixPath(path).parent.as_posix()
        if parent not in ("", "."):
            tokens.add(f"dir:{parent}")
    for value in _as_string_list(shape.get("symbol_names")):
        tokens.add(f"sym:{value}")
    for value in _as_string_list(shape.get("edge_types")):
        tokens.add(f"edge:{value}")
    for value in _shape_test_files(shape):
        tokens.add(f"test:{value}")
    for value in _as_string_list(shape.get("package_dirs")):
        tokens.add(f"pkg:{value}")
    for value in _as_string_list(shape.get("import_dirs")):
        tokens.add(f"import:{value}")
    for value in _as_string_list(shape.get("suffixes")):
        tokens.add(f"suffix:{value}")
    for value in _shape_kinds(shape).values():
        tokens.add(f"kind:{value}")
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union)


def _shared_attributes(shapes: Sequence[Mapping[str, Any]],
                       fields: Sequence[str]) -> dict[str, list[str]]:
    shared: dict[str, list[str]] = {}
    if not shapes:
        return shared
    for field in fields:
        values = [set(_as_string_list(shape.get(field))) for shape in shapes]
        if any(not value for value in values):
            continue
        common = set.intersection(*values)
        if common:
            shared[field] = sorted(common)
    return shared


def _shape_ids(shapes: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(shape.get("shape_id") or "") for shape in shapes]


def _exact_groups(shapes: Sequence[Mapping[str, Any]],
                  key: str) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for shape in shapes:
        value = shape.get(key)
        if not isinstance(value, str) or not value:
            continue
        groups.setdefault(value, []).append(str(shape["shape_id"]))
    return [sorted(values) for _, values in sorted(groups.items())
            if len(values) > 1]


def _cluster_shapes(shapes: Sequence[Mapping[str, Any]], *,
                    token_fn: Any, family_fn: Any, min_overlap: float,
                    analogies: Sequence[Mapping[str, Any]] = (),
                    extra_groups: Sequence[Sequence[str]] = ()) -> list[
                        dict[str, Any]]:
    ids = _shape_ids(shapes)
    by_id = {str(shape["shape_id"]): shape for shape in shapes}
    uf = _UnionFind(ids)
    reasons: dict[tuple[str, str], set[str]] = {}

    def link(left: str, right: str, reason: str) -> None:
        if left == right or left not in by_id or right not in by_id:
            return
        pair = tuple(sorted((left, right)))
        reasons.setdefault(pair, set()).add(reason)
        uf.union(left, right)

    for group in _exact_groups(shapes, "digest"):
        for index, left in enumerate(group):
            for right in group[index + 1:]:
                link(left, right, "exact")

    tokens = {str(shape["shape_id"]): token_fn(shape) for shape in shapes}
    ordered = sorted(ids)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if family_fn(by_id[left]) != family_fn(by_id[right]):
                continue
            if _jaccard(tokens[left], tokens[right]) >= min_overlap:
                link(left, right, "overlap")

    accepted_analogies: list[dict[str, Any]] = []
    seen_analogies: set[tuple[str, str]] = set()
    for raw in analogies:
        left = str(raw.get("source_shape") or "")
        right = str(raw.get("target_shape") or "")
        if left not in by_id or right not in by_id or left == right:
            continue
        if family_fn(by_id[left]) != family_fn(by_id[right]):
            continue
        if not (tokens[left] & tokens[right]):
            continue
        pair = tuple(sorted((left, right)))
        if pair in seen_analogies:
            continue
        seen_analogies.add(pair)
        entry = dict(raw)
        entry["source_shape"] = left
        entry["target_shape"] = right
        entry["family"] = family_fn(by_id[left])
        accepted_analogies.append(entry)
        link(left, right, "ocelot")
    accepted_analogies.sort(key=lambda item: (
        str(item.get("source_shape") or ""),
        str(item.get("target_shape") or "")))

    for group in extra_groups:
        members = [str(value) for value in group
                   if str(value) in by_id]
        if len({family_fn(by_id[value]) for value in members}) != 1:
            continue
        for value in members[1:]:
            link(members[0], value, "ocelot")

    clusters: list[dict[str, Any]] = []
    roots: dict[str, list[str]] = {}
    for shape_id in ordered:
        roots.setdefault(uf.find(shape_id), []).append(shape_id)
    groups_sorted = sorted(
        (sorted(values) for values in roots.values()),
        key=lambda values: tuple(values))
    for number, members in enumerate(groups_sorted, start=1):
        member_set = set(members)
        cluster_reasons: set[str] = set()
        inside_analogies = []
        for pair, pair_reasons in reasons.items():
            if pair[0] in member_set and pair[1] in member_set:
                cluster_reasons.update(pair_reasons)
        for analogy in accepted_analogies:
            if (str(analogy["source_shape"]) in member_set
                    and str(analogy["target_shape"]) in member_set):
                inside_analogies.append(analogy)
        if "exact" in cluster_reasons:
            origin = "exact"
        elif "overlap" in cluster_reasons:
            origin = "overlap"
        elif "ocelot" in cluster_reasons:
            origin = "ocelot"
        else:
            origin = "single"
        if "ocelot" in cluster_reasons and origin != "ocelot":
            origin += "+ocelot"
        clusters.append({
            "_members": members,
            "_origin": origin,
            "_reasons": sorted(cluster_reasons),
            "_analogies": inside_analogies,
            "_number": number,
        })
    return clusters


def _confidence_for(origin: str, size: int) -> str:
    if size < 2:
        return "low"
    if origin.startswith("exact"):
        return "high"
    return "medium"


def _dedupe_analogies(analogies: Iterable[Mapping[str, Any]]
                      ) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in analogies:
        pair = (str(raw.get("source_shape") or ""),
                str(raw.get("target_shape") or ""))
        if not pair[0] or not pair[1]:
            continue
        out.setdefault(pair, dict(raw))
    return [out[key] for key in sorted(out)]


def _shape_evidence(origin: str, shared: Mapping[str, list[str]],
                    analogies: Sequence[Mapping[str, Any]]) -> list[str]:
    evidence: list[str] = []
    if origin == "single":
        return ["singleton shape; no structural analogy was claimed"]
    if origin.startswith("exact"):
        evidence.append("the shapes share one structural digest")
    elif origin.startswith("overlap"):
        evidence.append("the shapes overlap on structural attributes")
    for field in ("changed_files", "package_dirs", "import_dirs",
                  "symbol_names", "edge_types", "test_files", "dirs",
                  "suffixes", "roles", "categories"):
        values = shared.get(field) or []
        if values and field not in {"changed_files", "symbol_names"}:
            shown = ", ".join(values[:5])
            evidence.append(f"shared {field}: {shown}")
    for analogy in analogies[:10]:
        evidence.append(
            "Ocelot analogy "
            f"{analogy.get('source_shape')} ~ {analogy.get('target_shape')}")
    if not evidence:
        evidence.append("singleton shape; no structural analogy was claimed")
    return evidence


def _impact_section_from_shapes(shapes: Sequence[Mapping[str, Any]], *,
                                origins: Sequence[str],
                                analogies: Sequence[Mapping[str, Any]]
                                ) -> list[dict[str, Any]]:
    by_id = {str(shape["shape_id"]): shape for shape in shapes}
    clusters: list[dict[str, Any]] = []
    for number, raw in enumerate(origins, start=1):
        members = list(raw["_members"])
        cluster_shapes = [by_id[value] for value in members]
        shared = _shared_attributes(
            cluster_shapes,
            ("changed_files", "symbol_names", "edge_types", "test_files",
             "package_dirs", "import_dirs", "suffixes", "kinds", "family"))
        test_links: dict[str, set[str]] = {}
        planned: set[str] = set()
        for shape in cluster_shapes:
            for path in _shape_test_files(shape):
                test_links.setdefault(path, set()).add(
                    str(shape["shape_id"]))
            planned.update(_as_string_list(shape.get("planned_tests")))
        origin = str(raw["_origin"])
        clusters.append({
            "cluster_id": f"impact-{number:04d}",
            "shape_ids": members,
            "size": len(members),
            "family": str(cluster_shapes[0].get("family") or "empty"),
            "origin": origin,
            "digest": (str(cluster_shapes[0].get("digest") or "")
                       if len({str(shape.get("digest") or "")
                               for shape in cluster_shapes}) == 1 else None),
            "shared": shared,
            "test_links": [
                {"file": path, "shape_ids": sorted(ids)}
                for path, ids in sorted(test_links.items())
            ],
            "test_files": sorted(test_links),
            "candidate_tests": sorted(test_links),
            "planned_tests": sorted(planned),
            "confidence": _confidence_for(origin, len(members)),
            "confidence_note": (
                "structural similarity only; this is not a test-coverage "
                "verdict and the test set is not complete"),
            "evidence": _shape_evidence(origin, shared, raw["_analogies"]),
            "truth_assessed": False,
            "test_set_complete": False,
            "tests_executed": False,
            "note": ("candidate test set from similar change shapes; no test "
                     "was executed"),
        })
    return clusters


def _base_impact_result() -> dict[str, Any]:
    return {
        "schema": IMPACT_SCHEMA,
        "requested": True,
        "available": False,
        "state": "no_shapes",
        "mode": "auto",
        "actual_mode": None,
        "engine": "exact",
        "ocelot": None,
        "clusters": [],
        "analogies": [],
        "shape_count": 0,
        "cluster_count": 0,
        "truncated": False,
        "omitted_shapes": 0,
        "truth_assessed": False,
        "test_set_complete": False,
        "tests_executed": False,
        "claim": ("candidate test sets from similar change shapes; no test "
                  "was executed and completeness was not assessed"),
        "error": None,
        "note": None,
        "not_assessed": [],
    }


def _shape_worker_payload(shapes: Sequence[Mapping[str, Any]],
                          *, family_fn: Any
                          ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    payload: list[dict[str, Any]] = []
    payload_ids: dict[str, str] = {}
    for index, shape in enumerate(shapes, start=1):
        payload_id = f"s{index:04d}"
        payload_ids[payload_id] = str(shape["shape_id"])
        family = family_fn(shape)
        files = _as_string_list(shape.get("changed_files"))
        dirs = _as_string_list(shape.get("package_dirs"))
        wording = (f"family={family}; files={','.join(files[:8])}; "
                   f"dirs={','.join(dirs[:8])}")[:500]
        payload.append({
            "finding_id": payload_id,
            "run_id": f"run-{payload_id}",
            "target_type": "ChangeShape",
            "target_id": family,
            "family": "other",
            "severity": "note",
            "wording": wording,
        })
    return payload, payload_ids


def _shape_search(shapes: Sequence[Mapping[str, Any]], *, root: Path | None,
                  ocelot_root: str | Path | None, mode: str, timeout: int,
                  family_fn: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "state": "unavailable",
        "actual_mode": None,
        "engine": "exact",
        "ocelot": None,
        "analogies": [],
        "groups": [],
        "error": None,
        "note": None,
        "payload_ids": {},
    }
    try:
        resolution = _resolve(root, ocelot_root)
    except Exception as exc:  # noqa: BLE001 - never fail a caller
        result.update({"state": "error",
                       "error": f"{type(exc).__name__}: {exc}",
                       "note": "Ocelot resolution failed; exact fallback only"})
        return result
    result["ocelot"] = _metadata(resolution)
    if resolution.get("state") != "ready":
        result.update({
            "state": str(resolution.get("state") or "unavailable"),
            "error": resolution.get("error"),
            "note": ("Ocelot is unavailable; exact digest/overlap fallback "
                     "only. No install or download was attempted."
                     + (f" {resolution['note']}"
                        if resolution.get("note") else "")),
        })
        return result
    payload, payload_ids = _shape_worker_payload(
        shapes, family_fn=family_fn)
    result["payload_ids"] = payload_ids
    try:
        output, actual_mode = _worker_output(
            {"findings": payload},
            ocelot_root=Path(str(resolution["path"])),
            mode=mode, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - report, never fail a caller
        result.update({
            "state": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "note": ("Ocelot failed; exact digest/overlap fallback only and "
                     "the caller is not failed"),
        })
        return result
    result.update({
        "available": True,
        "state": "ready",
        "actual_mode": actual_mode,
        "engine": str(output.get("engine") or "ocelot:analogy_search"),
        "analogies": list(output.get("analogies") or []),
        "groups": list(output.get("clusters") or []),
        "note": ("Ocelot structural analogy is a candidate signal, not a "
                 "verdict; no completeness was assessed"),
    })
    return result


def _safe_shape_analogies(raw: Any, *, by_id: Mapping[str, Any],
                          payload_ids: Mapping[str, str],
                          family_fn: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(raw, (list, tuple)):
        return out
    reverse = {value: key for key, value in payload_ids.items()}

    def resolve(value: Any) -> str | None:
        text = str(value or "")
        if text in by_id:
            return text
        mapped = payload_ids.get(text)
        if mapped:
            return mapped
        return reverse.get(text)

    for item in raw:
        if not isinstance(item, Mapping):
            continue
        left = resolve(item.get("source_shape") or item.get("source_finding"))
        right = resolve(item.get("target_shape") or item.get("target_finding"))
        if not left or not right or left == right:
            continue
        if left not in by_id or right not in by_id:
            continue
        if family_fn(by_id[left]) != family_fn(by_id[right]):
            continue
        pair = tuple(sorted((left, right)))
        if pair in seen:
            continue
        seen.add(pair)
        entry = {
            "source_shape": pair[0],
            "target_shape": pair[1],
            "family": family_fn(by_id[left]),
            "order_n": item.get("order_n"),
            "struct_shared": item.get("struct_shared"),
        }
        out.append(entry)
    out.sort(key=lambda entry: (
        str(entry.get("source_shape") or ""),
        str(entry.get("target_shape") or "")))
    return out


def _safe_shape_groups(raw: Any, *, by_id: Mapping[str, Any],
                       payload_ids: Mapping[str, str],
                       family_fn: Any) -> list[list[str]]:
    reverse = {value: key for key, value in payload_ids.items()}
    groups: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    if not isinstance(raw, (list, tuple)):
        return groups

    def resolve(value: Any) -> str | None:
        text = str(value or "")
        if text in by_id:
            return text
        mapped = payload_ids.get(text)
        if mapped:
            return mapped
        return reverse.get(text)

    for item in raw:
        if isinstance(item, Mapping):
            values = (item.get("shape_ids") or item.get("members")
                      or item.get("shapes") or item.get("findings") or [])
        elif isinstance(item, (list, tuple)):
            values = item
        else:
            continue
        members = sorted({resolve(value) for value in values
                          if resolve(value) is not None})
        if len(members) < 2:
            continue
        if len({family_fn(by_id[value]) for value in members}) != 1:
            continue
        key = tuple(members)
        if key in seen:
            continue
        seen.add(key)
        groups.append(members)
    return groups


def test_impact_clusters(shapes: Iterable[Mapping[str, Any]], *,
                         ocelot_root: str | Path | None = None,
                         mode: str = "auto",
                         timeout: int = DEFAULT_TIMEOUT,
                         clusterer: Any = None,
                         min_overlap: float = DEFAULT_MIN_OVERLAP,
                         root: str | Path | None = None
                         ) -> dict[str, Any]:
    """Group similar change shapes and attach candidate test sets.

    Similarity is exact digest or structural overlap, plus optional Ocelot
    analogy links when a checkout is available or a ``clusterer`` is injected.
    Every cluster reports ``truth_assessed: false``; no test set is described
    as complete and no test is executed.
    """
    if mode not in MODES:
        raise OcelotCodeError(f"mode must be one of {MODES}")
    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError) as exc:
        raise OcelotCodeError("timeout must be a positive integer") from exc
    if timeout_value <= 0:
        raise OcelotCodeError("timeout must be a positive integer")
    try:
        threshold = float(min_overlap)
    except (TypeError, ValueError) as exc:
        raise OcelotCodeError("min_overlap must be between 0 and 1") from exc
    if not 0.0 <= threshold <= 1.0:
        raise OcelotCodeError("min_overlap must be between 0 and 1")

    raw_items = ([shapes] if isinstance(shapes, Mapping)
                 else [item for item in shapes if isinstance(item, Mapping)])
    normalised = _normalise_impact_shapes(raw_items)
    result = _base_impact_result()
    result["mode"] = mode
    result["shape_count"] = len(normalised)
    result["omitted_shapes"] = max(0, len(raw_items) - len(normalised))
    result["truncated"] = result["omitted_shapes"] > 0
    if not normalised:
        result["note"] = "no change shapes to cluster; Ocelot was not called"
        result["not_assessed"] = ["no test set was inferred"]
        return result

    by_id = {str(shape["shape_id"]): shape for shape in normalised}
    search: dict[str, Any] = {
        "available": False, "state": "not_installed", "actual_mode": None,
        "engine": "exact", "ocelot": None, "analogies": [], "groups": [],
        "error": None, "note": None, "payload_ids": {},
    }
    if clusterer is not None:
        try:
            raw = clusterer(normalised, ocelot_root=ocelot_root, mode=mode)
        except Exception as exc:  # noqa: BLE001 - report, never fail
            search.update({
                "state": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "note": "the injected clusterer failed; exact fallback only",
            })
        else:
            if isinstance(raw, Mapping):
                search.update({
                    "available": bool(raw.get("available", True)),
                    "state": str(raw.get("state") or "ready"),
                    "actual_mode": raw.get("actual_mode"),
                    "engine": str(raw.get("engine") or "injected"),
                    "ocelot": raw.get("ocelot"),
                    "analogies": list(raw.get("analogies") or []),
                    "groups": list(raw.get("clusters") or []),
                    "note": raw.get("note"),
                })
            elif isinstance(raw, (list, tuple)):
                search.update({"available": True, "state": "ready",
                               "engine": "injected", "groups": list(raw)})
            else:
                search.update({
                    "state": "error",
                    "error": "the injected clusterer returned no object",
                    "note": "exact fallback only",
                })
    else:
        search = _shape_search(
            normalised, root=Path(root).expanduser() if root is not None
            else None, ocelot_root=ocelot_root, mode=mode,
            timeout=timeout_value, family_fn=_shape_family)

    analogies = _safe_shape_analogies(
        search.get("analogies"), by_id=by_id,
        payload_ids=search.get("payload_ids") or {},
        family_fn=_shape_family)
    groups = _safe_shape_groups(
        search.get("groups"), by_id=by_id,
        payload_ids=search.get("payload_ids") or {},
        family_fn=_shape_family)
    raw_clusters = _cluster_shapes(
        normalised, token_fn=_impact_tokens, family_fn=_shape_family,
        min_overlap=threshold, analogies=analogies, extra_groups=groups)
    used_analogies = _dedupe_analogies(
        analogy for raw in raw_clusters
        for analogy in raw.get("_analogies") or ())
    clusters = _impact_section_from_shapes(
        normalised, origins=raw_clusters, analogies=used_analogies)
    result.update({
        "available": bool(search.get("available")),
        "state": str(search.get("state") or "not_installed"),
        "actual_mode": search.get("actual_mode"),
        "engine": str(search.get("engine") or "exact"),
        "ocelot": search.get("ocelot"),
        "clusters": clusters,
        "analogies": used_analogies,
        "cluster_count": len(clusters),
        "error": search.get("error"),
        "note": search.get("note") or (
            "exact digest/overlap clusters only; Ocelot did not link shapes"),
    })
    not_assessed = [
        "no test was executed; the clusters only suggest candidate test sets",
        "a similar test set is not complete and a passing subset would not "
        "prove correctness",
        "structural similarity is not causal and does not establish that two "
        "changes affect the same behaviour",
    ]
    if result["state"] not in {"ready", "no_shapes"}:
        not_assessed.append(
            f"Ocelot analogy search was {result['state']}; exact "
            "digest/overlap clusters are reported")
    if result["omitted_shapes"]:
        not_assessed.append(
            f"{result['omitted_shapes']} shape(s) were outside the limit "
            f"of {MAX_SHAPES}")
    result["not_assessed"] = not_assessed
    return result


def _impact_failure_section(mode: str, exc: Exception) -> dict[str, Any]:
    result = _base_impact_result()
    result.update({
        "mode": mode,
        "state": "error",
        "error": f"{type(exc).__name__}: {exc}",
        "note": "impact clustering failed; the caller is not failed",
        "not_assessed": [
            "no test set was inferred",
            "no test was executed and completeness was not assessed",
        ],
    })
    return result


def impact_error_section(exc: Exception, *,
                         mode: str = "auto") -> dict[str, Any]:
    """A reported impact error for callers that could not build a shape."""
    return _impact_failure_section(mode, exc)


def impact_section(shapes: Iterable[Mapping[str, Any]], *,
                   ocelot_root: str | Path | None = None,
                   mode: str = "auto",
                   timeout: int = DEFAULT_TIMEOUT,
                   clusterer: Any = None,
                   root: str | Path | None = None) -> dict[str, Any]:
    """A JSON-safe impact section; an injected failure is reported, not raised."""
    if isinstance(shapes, Mapping):
        items = [shapes]
    else:
        items = list(shapes)
    try:
        return test_impact_clusters(
            items, ocelot_root=ocelot_root, mode=mode, timeout=timeout,
            clusterer=clusterer, root=root)
    except Exception as exc:  # noqa: BLE001 - a review must report, not crash
        section = _impact_failure_section(mode, exc)
        section["shape_count"] = len(items)
        return section


# --------------------------------------------------------------------------- #
# architecture patterns
# --------------------------------------------------------------------------- #
def _pattern_from_file(path: str, root: Path | None, *,
                       symbols: Sequence[str] = (),
                       imports: Sequence[str] = (),
                       imported_by: Sequence[str] = (),
                       edge_types: Sequence[str] = (),
                       key: str | None = None) -> dict[str, Any]:
    normalised = _normalise_path(path)
    if normalised:
        path = normalised
    role = _path_kind(path)
    import_paths = set(imports)
    import_paths.update(imported_by)
    import_dirs = sorted({
        PurePosixPath(value).parent.as_posix()
        for value in import_paths
        if PurePosixPath(value).parent.as_posix() not in ("", ".")
    })
    shape_id = key or f"file:{path}"
    return {
        "shape_id": shape_id,
        "kind": "file",
        "family": f"file:{role}",
        "role": role,
        "path": path,
        "files": [path],
        "changed_files": [path],
        "dirs": [PurePosixPath(path).parent.as_posix()]
                if PurePosixPath(path).parent.as_posix() not in ("", ".") else [],
        "package_dirs": _package_dirs(root, [path]),
        "suffixes": _suffixes([path]),
        "symbols": sorted(set(symbols)),
        "symbol_names": sorted(set(symbols)),
        "imports": sorted(set(imports)),
        "imported_by": sorted(set(imported_by)),
        "import_dirs": import_dirs,
        "edge_types": sorted(set(edge_types)),
    }


def _finding_file_evidence(item: Mapping[str, Any]) -> list[str]:
    files = _finding_files(item)
    proposed = item.get("proposed_module")
    if isinstance(proposed, str) and proposed.strip() and "/" in proposed:
        normalised = _normalise_path(proposed.strip())
        if normalised and normalised not in files:
            files.append(normalised)
    return sorted({normalised for normalised in
                   (_normalise_path(value) for value in files)
                   if normalised})


def _pattern_from_opportunity(item: Mapping[str, Any],
                              root: Path | None,
                              index: int) -> dict[str, Any]:
    category = str(item.get("category") or item.get("type")
                   or "opportunity").strip().casefold()
    files = _finding_file_evidence(item)
    parents = sorted({
        PurePosixPath(path).parent.as_posix()
        for path in files
        if PurePosixPath(path).parent.as_posix() not in ("", ".")
    })
    evidence_kinds = sorted({
        str(entry.get("kind"))
        for entry in (item.get("evidence") or [])
        if isinstance(entry, Mapping) and entry.get("kind")
    })
    shape_id = str(item.get("id") or item.get("finding_id")
                   or f"opportunity-{index:04d}")
    return {
        "shape_id": shape_id,
        "kind": "opportunity",
        "family": f"opportunity:{category}",
        "category": category,
        "files": files,
        "changed_files": files,
        "dirs": parents,
        "package_dirs": _package_dirs(root, files),
        "suffixes": _suffixes(files),
        "evidence_kinds": evidence_kinds,
        "symbols": [],
        "symbol_names": [],
        "import_dirs": [],
        "edge_types": [],
    }


def _pattern_from_symbol(item: Mapping[str, Any],
                         root: Path | None) -> dict[str, Any] | None:
    key = str(item.get("key") or item.get("shape_id") or "")
    path = str(item.get("file") or item.get("path") or "")
    if not path and key.startswith(("sym:", "file:")):
        path = key.split(":", 1)[1].split("::", 1)[0]
    if not path:
        return None
    qualname = str(item.get("qualname") or item.get("name") or "")
    if not qualname and key.startswith("sym:"):
        qualname = key.split("::", 1)[1] if "::" in key else ""
    shape = _pattern_from_file(
        path, root, symbols=[qualname] if qualname else [], key=key or None)
    shape["kind"] = "symbol"
    shape["family"] = f"symbol:{shape['role']}"
    return shape


def _pattern_from_item(item: Any, root: Path | None,
                       index: int) -> dict[str, Any] | None:
    if isinstance(item, str):
        path = _normalise_path(item)
        return _pattern_from_file(path, root) if path else None
    if not isinstance(item, Mapping):
        return None
    if item.get("category"):
        return _pattern_from_opportunity(item, root, index)
    key = str(item.get("key") or "")
    if key.startswith("sym:") or item.get("qualname"):
        return _pattern_from_symbol(item, root)
    if item.get("files") or item.get("paths"):
        values = _as_string_list(item.get("files") or item.get("paths"))
        shapes = [_pattern_from_file(path, root) for path in values]
        shapes = [shape for shape in shapes if shape is not None]
        if not shapes:
            return None
        files = sorted({str(path) for shape in shapes
                        for path in shape["files"]})
        roles = sorted({str(shape["role"]) for shape in shapes})
        return {
            "shape_id": str(item.get("id") or f"group-{index:04d}"),
            "kind": "group",
            "family": "group:" + (roles[0] if len(roles) == 1 else "mixed"),
            "files": files,
            "changed_files": files,
            "dirs": sorted({entry for shape in shapes
                            for entry in shape["dirs"]}),
            "package_dirs": _package_dirs(root, files),
            "suffixes": _suffixes(files),
            "symbols": sorted({entry for shape in shapes
                               for entry in shape["symbols"]}),
            "symbol_names": sorted({entry for shape in shapes
                                    for entry in shape["symbol_names"]}),
            "imports": sorted({entry for shape in shapes
                               for entry in shape["imports"]}),
            "imported_by": sorted({entry for shape in shapes
                                   for entry in shape["imported_by"]}),
            "import_dirs": sorted({entry for shape in shapes
                                   for entry in shape["import_dirs"]}),
            "edge_types": sorted({entry for shape in shapes
                                  for entry in shape["edge_types"]}),
        }
    path = _normalise_path(item.get("path") or item.get("file"))
    if not path:
        return None
    return _pattern_from_file(
        path, root,
        symbols=_as_string_list(item.get("symbols")),
        imports=_as_string_list(item.get("imports")),
        imported_by=_as_string_list(item.get("imported_by")),
        edge_types=_as_string_list(item.get("edge_types")),
        key=item.get("key") if isinstance(item.get("key"), str) else None)


def _graph_pattern_shapes(root: Path, graph_path: str | Path | None
                          ) -> tuple[list[dict[str, Any]], dict[str, Any],
                                     str | None]:
    info, index = _load_shape_graph(root, graph_path)
    if index is None:
        files = _repo_files(root)
        if files is None:
            files = _walk_files(root)
        files = sorted(set(files))[:MAX_PATTERN_SHAPES]
        note = (
            "the graph snapshot is " + str(info.get("freshness"))
            + "; file shapes without symbols or imports are reported")
        return ([_pattern_from_file(path, root) for path in files], info, note)
    file_paths = sorted((index.get("files_by_path") or {}).keys())
    omitted = max(0, len(file_paths) - MAX_PATTERN_SHAPES)
    shapes: list[dict[str, Any]] = []
    nodes = index.get("nodes") or {}
    for path in file_paths[:MAX_PATTERN_SHAPES]:
        symbols = [str((nodes.get(nid) or {}).get("attrs", {}).get("qualname")
                       or "") for nid in
                   (index.get("symbols_by_file") or {}).get(path, ())]
        symbols = [value for value in symbols if value]
        imports = sorted((index.get("imports") or {}).get(path, ()))
        imported_by = sorted((index.get("imported_by") or {}).get(path, ()))
        edge_types = sorted(
            (index.get("edge_types_by_path") or {}).get(path, ()))
        shapes.append(_pattern_from_file(
            path, root, symbols=symbols, imports=imports,
            imported_by=imported_by, edge_types=edge_types))
    note = None
    if omitted:
        note = (f"{omitted} file(s) were outside the pattern shape limit of "
                f"{MAX_PATTERN_SHAPES}")
    return shapes, info, note


def _normalise_pattern_shapes(items: Iterable[Any], root: Path | None
                              ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        if len(out) >= MAX_PATTERN_SHAPES:
            break
        shape = _pattern_from_item(item, root, index)
        if shape is None:
            continue
        base_id = str(shape.get("shape_id") or f"pattern-shape-{index:04d}")
        count = seen.get(base_id, 0) + 1
        seen[base_id] = count
        shape["shape_id"] = base_id if count == 1 else f"{base_id}~{count}"
        shape["digest"] = _digest({
            "kind": shape.get("kind"),
            "family": shape.get("family"),
            "files": shape.get("files") or [],
            "dirs": shape.get("dirs") or [],
            "package_dirs": shape.get("package_dirs") or [],
            "suffixes": shape.get("suffixes") or [],
            "symbols": shape.get("symbols") or [],
            "import_dirs": shape.get("import_dirs") or [],
            "edge_types": shape.get("edge_types") or [],
            "category": shape.get("category"),
        })
        category = shape.get("category")
        shape["categories"] = [str(category)] if category else []
        shape["index"] = index
        out.append(shape)
    return out


def _pattern_tokens(shape: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for value in _as_string_list(shape.get("dirs")):
        tokens.add(f"dir:{value}")
    for value in _as_string_list(shape.get("package_dirs")):
        tokens.add(f"pkg:{value}")
    for value in _as_string_list(shape.get("suffixes")):
        tokens.add(f"suffix:{value}")
    for value in _as_string_list(shape.get("import_dirs")):
        tokens.add(f"import:{value}")
    for value in _as_string_list(shape.get("edge_types")):
        tokens.add(f"edge:{value}")
    for value in _as_string_list(shape.get("evidence_kinds")):
        tokens.add(f"evidence:{value}")
    category = shape.get("category")
    if category:
        tokens.add(f"category:{category}")
    symbols = _as_string_list(shape.get("symbols"))
    if not symbols:
        tokens.add("symbols:none")
    elif len(symbols) <= 3:
        tokens.add("symbols:small")
    else:
        tokens.add("symbols:many")
    return tokens


def _pattern_family(shape: Mapping[str, Any]) -> str:
    return str(shape.get("family") or f"kind:{shape.get('kind')}")


def _pattern_evidence(origin: str, shared: Mapping[str, list[str]],
                      analogies: Sequence[Mapping[str, Any]]) -> list[str]:
    evidence: list[str] = []
    if origin == "single":
        return ["singleton shape; no pattern was claimed"]
    if origin.startswith("exact"):
        evidence.append("the shapes share one structural signature")
    elif origin.startswith("overlap"):
        evidence.append("the shapes overlap on structural attributes")
    for field in ("dirs", "package_dirs", "suffixes", "import_dirs",
                  "edge_types", "evidence_kinds", "categories"):
        values = shared.get(field) or []
        if values:
            evidence.append(f"shared {field}: " + ", ".join(values[:5]))
    for analogy in analogies[:10]:
        evidence.append(
            "Ocelot analogy "
            f"{analogy.get('source_shape')} ~ {analogy.get('target_shape')}")
    if not evidence:
        evidence.append("singleton shape; no pattern was claimed")
    return evidence


def _pattern_section_from_shapes(shapes: Sequence[Mapping[str, Any]], *,
                                 raw_clusters: Sequence[Mapping[str, Any]],
                                 analogies: Sequence[Mapping[str, Any]]
                                 ) -> list[dict[str, Any]]:
    by_id = {str(shape["shape_id"]): shape for shape in shapes}
    patterns: list[dict[str, Any]] = []
    for number, raw in enumerate(raw_clusters, start=1):
        members = list(raw["_members"])
        cluster_shapes = [by_id[value] for value in members]
        shared = _shared_attributes(
            cluster_shapes,
            ("dirs", "package_dirs", "suffixes", "import_dirs",
             "edge_types", "evidence_kinds", "symbols", "categories"))
        categories = sorted({str(shape.get("category"))
                             for shape in cluster_shapes
                             if shape.get("category")})
        files = sorted({str(path) for shape in cluster_shapes
                        for path in _as_string_list(shape.get("files"))})
        symbols = sorted({str(name) for shape in cluster_shapes
                          for name in _as_string_list(shape.get("symbols"))})
        origin = str(raw["_origin"])
        kind = str(cluster_shapes[0].get("kind") or "shape")
        patterns.append({
            "pattern_id": f"pattern-{number:04d}",
            "shape_ids": members,
            "size": len(members),
            "kind": kind,
            "family": _pattern_family(cluster_shapes[0]),
            "category": categories[0] if len(categories) == 1 else None,
            "origin": origin,
            "shared": shared,
            "files": files[:200],
            "symbols": symbols[:100],
            "file_count": len(files),
            "confidence": _confidence_for(origin, len(members)),
            "confidence_note": (
                "structural similarity only; this is a candidate pattern, "
                "not an anti-pattern verdict"),
            "evidence": _pattern_evidence(origin, shared, raw["_analogies"]),
            "truth_assessed": False,
            "candidate": True,
            "note": ("candidate architecture pattern; it does not establish "
                     "a defect, intent or completeness"),
        })
    return patterns


def _base_pattern_result() -> dict[str, Any]:
    return {
        "schema": PATTERN_SCHEMA,
        "requested": True,
        "available": False,
        "state": "no_shapes",
        "mode": "auto",
        "actual_mode": None,
        "engine": "exact",
        "ocelot": None,
        "patterns": [],
        "analogies": [],
        "shape_count": 0,
        "pattern_count": 0,
        "truncated": False,
        "omitted_shapes": 0,
        "candidate": True,
        "truth_assessed": False,
        "claim": ("candidate architecture patterns from structural similarity; "
                  "not a verdict and not complete"),
        "error": None,
        "note": None,
        "not_assessed": [],
    }


def architecture_patterns(items: Any = None, *,
                          root: str | Path | None = None,
                          graph_path: str | Path | None = None,
                          ocelot_root: str | Path | None = None,
                          mode: str = "auto",
                          timeout: int = DEFAULT_TIMEOUT,
                          clusterer: Any = None,
                          min_overlap: float = DEFAULT_MIN_OVERLAP
                          ) -> dict[str, Any]:
    """Cluster repo-scan opportunities or graph file/symbol shapes.

    ``items`` may be scan opportunities, paths, file/symbol mappings, or a
    report-like mapping with an ``opportunities``/``items``/``files`` list.
    With no items, graph file shapes are used; a missing graph falls back to
    the repository file list. Every pattern is a candidate, never a verdict.
    """
    if mode not in MODES:
        raise OcelotCodeError(f"mode must be one of {MODES}")
    try:
        timeout_value = int(timeout)
    except (TypeError, ValueError) as exc:
        raise OcelotCodeError("timeout must be a positive integer") from exc
    if timeout_value <= 0:
        raise OcelotCodeError("timeout must be a positive integer")
    try:
        threshold = float(min_overlap)
    except (TypeError, ValueError) as exc:
        raise OcelotCodeError("min_overlap must be between 0 and 1") from exc
    if not 0.0 <= threshold <= 1.0:
        raise OcelotCodeError("min_overlap must be between 0 and 1")

    root_p: Path | None = None
    if root is not None:
        root_p = Path(root).expanduser()
        try:
            root_p = root_p.resolve()
        except (OSError, RuntimeError) as exc:
            raise OcelotCodeError(
                f"cannot resolve root {root!r}: {exc}") from exc
        if not root_p.is_dir():
            raise OcelotCodeError(f"root is not a directory: {root_p}")

    source = items
    if isinstance(source, Mapping):
        for key in ("opportunities", "items", "rows", "files", "shapes"):
            value = source.get(key)
            if isinstance(value, list):
                source = value
                if root_p is None:
                    # A scan report records its root; use it for package dirs.
                    recorded = (items.get("root")
                                if isinstance(items, Mapping) else None)
                    if isinstance(recorded, str):
                        candidate = Path(recorded).expanduser()
                        if candidate.is_dir():
                            root_p = candidate.resolve()
                break
        else:
            source = [source]

    if source is None:
        if root_p is None:
            raise OcelotCodeError(
                "root is required when no opportunities or files are given")
        shapes, graph_info, note = _graph_pattern_shapes(root_p, graph_path)
        omitted = 0
    else:
        if isinstance(source, (str, Mapping)):
            values: Iterable[Any] = [source]
        elif isinstance(source, Iterable):
            values = source
        else:
            values = []
        raw_values = list(values)
        shapes = _normalise_pattern_shapes(raw_values, root_p)
        omitted = max(0, len(raw_values) - len(shapes))
        graph_info = None
        note = None

    result = _base_pattern_result()
    result.update({
        "mode": mode,
        "shape_count": len(shapes),
        "omitted_shapes": omitted,
        "truncated": omitted > 0,
    })
    if graph_info is not None:
        result["graph"] = dict(graph_info)
    if not shapes:
        result["note"] = note or "no architecture shapes to cluster"
        result["not_assessed"] = [
            "no architecture pattern was inferred",
            "no model or network was used",
        ]
        return result

    by_id = {str(shape["shape_id"]): shape for shape in shapes}
    search: dict[str, Any] = {
        "available": False, "state": "not_installed", "actual_mode": None,
        "engine": "exact", "ocelot": None, "analogies": [], "groups": [],
        "error": None, "note": None, "payload_ids": {},
    }
    if clusterer is not None:
        try:
            raw = clusterer(shapes, ocelot_root=ocelot_root, mode=mode)
        except Exception as exc:  # noqa: BLE001 - report, never fail
            search.update({
                "state": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "note": "the injected clusterer failed; exact fallback only",
            })
        else:
            if isinstance(raw, Mapping):
                search.update({
                    "available": bool(raw.get("available", True)),
                    "state": str(raw.get("state") or "ready"),
                    "actual_mode": raw.get("actual_mode"),
                    "engine": str(raw.get("engine") or "injected"),
                    "ocelot": raw.get("ocelot"),
                    "analogies": list(raw.get("analogies") or []),
                    "groups": list(raw.get("clusters") or []),
                    "note": raw.get("note"),
                })
            elif isinstance(raw, (list, tuple)):
                search.update({"available": True, "state": "ready",
                               "engine": "injected", "groups": list(raw)})
            else:
                search.update({
                    "state": "error",
                    "error": "the injected clusterer returned no object",
                    "note": "exact fallback only",
                })
    else:
        search = _shape_search(
            shapes, root=root_p, ocelot_root=ocelot_root, mode=mode,
            timeout=timeout_value, family_fn=_pattern_family)

    analogies = _safe_shape_analogies(
        search.get("analogies"), by_id=by_id,
        payload_ids=search.get("payload_ids") or {},
        family_fn=_pattern_family)
    groups = _safe_shape_groups(
        search.get("groups"), by_id=by_id,
        payload_ids=search.get("payload_ids") or {},
        family_fn=_pattern_family)
    raw_clusters = _cluster_shapes(
        shapes, token_fn=_pattern_tokens, family_fn=_pattern_family,
        min_overlap=threshold, analogies=analogies, extra_groups=groups)
    used_analogies = _dedupe_analogies(
        analogy for raw in raw_clusters
        for analogy in raw.get("_analogies") or ())
    patterns = _pattern_section_from_shapes(
        shapes, raw_clusters=raw_clusters, analogies=used_analogies)
    result.update({
        "available": bool(search.get("available")),
        "state": str(search.get("state") or "not_installed"),
        "actual_mode": search.get("actual_mode"),
        "engine": str(search.get("engine") or "exact"),
        "ocelot": search.get("ocelot"),
        "patterns": patterns,
        "analogies": used_analogies,
        "pattern_count": len(patterns),
        "error": search.get("error"),
        "note": search.get("note") or (
            "exact signature/overlap patterns only; Ocelot did not link "
            "shapes"),
    })
    not_assessed = [
        "architecture patterns are candidate clusters; they do not establish "
        "an anti-pattern, a defect or intent",
        "pattern completeness was not assessed",
        "no model, network or graph build was used by this call",
    ]
    if result["state"] not in {"ready", "no_shapes"}:
        not_assessed.append(
            f"Ocelot analogy search was {result['state']}; exact "
            "signature/overlap patterns are reported")
    if result["omitted_shapes"]:
        not_assessed.append(
            f"{result['omitted_shapes']} shape(s) were outside the limit "
            f"of {MAX_PATTERN_SHAPES}")
    if graph_info is not None and not graph_info.get("usable"):
        not_assessed.append(
            "graph symbols, imports and edge types were not assessed: "
            f"graph freshness is {graph_info.get('freshness')}")
    if note:
        not_assessed.append(note)
    result["not_assessed"] = not_assessed
    return result


def _pattern_failure_section(mode: str, exc: Exception) -> dict[str, Any]:
    result = _base_pattern_result()
    result.update({
        "mode": mode,
        "state": "error",
        "error": f"{type(exc).__name__}: {exc}",
        "note": "pattern clustering failed; the caller is not failed",
        "not_assessed": [
            "no architecture pattern was inferred",
            "no model or network was used",
        ],
    })
    return result


def pattern_error_section(exc: Exception, *,
                          mode: str = "auto") -> dict[str, Any]:
    """A reported pattern error for callers that could not build shapes."""
    return _pattern_failure_section(mode, exc)


def pattern_section(items: Any = None, *, root: str | Path | None = None,
                    graph_path: str | Path | None = None,
                    ocelot_root: str | Path | None = None,
                    mode: str = "auto",
                    timeout: int = DEFAULT_TIMEOUT,
                    clusterer: Any = None) -> dict[str, Any]:
    """A JSON-safe pattern section; an injected failure is reported, not raised."""
    try:
        return architecture_patterns(
            items, root=root, graph_path=graph_path,
            ocelot_root=ocelot_root, mode=mode, timeout=timeout,
            clusterer=clusterer)
    except Exception as exc:  # noqa: BLE001 - a scan must report, not crash
        return _pattern_failure_section(mode, exc)


def empty_cluster_section(*, mode: str = "auto",
                          note: str | None = None) -> dict[str, Any]:
    """A cluster-shaped section for callers that only request impact/patterns.

    Finding clustering was not requested. The container keeps the existing
    ``ocelot`` section shape so consumers can read it without a special case.
    """
    return {
        "schema": SCHEMA,
        "requested": True,
        "available": False,
        "state": "not_requested",
        "mode": mode,
        "actual_mode": None,
        "engine": "exact",
        "ocelot": None,
        "clusters": [],
        "ocelot_clusters": [],
        "analogies": [],
        "finding_count": 0,
        "cluster_count": 0,
        "truncated": False,
        "omitted_findings": 0,
        "truth_assessed": False,
        "error": None,
        "note": note or "finding clustering was not requested",
    }


__all__ = [
    "DEFAULT_MIN_OVERLAP",
    "DEFAULT_TIMEOUT",
    "FAMILIES",
    "FAMILY_BY_TYPE",
    "IMPACT_SCHEMA",
    "MAX_FINDINGS",
    "MAX_PATTERN_SHAPES",
    "MAX_SHAPES",
    "MAX_SHAPE_FILES",
    "MODES",
    "OcelotCodeError",
    "PATTERN_SCHEMA",
    "RISK_SEVERITY",
    "SCAN_CATEGORIES",
    "SCHEMA",
    "SHAPE_SCHEMA",
    "architecture_patterns",
    "change_shape",
    "cluster_findings",
    "cluster_section",
    "empty_cluster_section",
    "exact_clusters",
    "finding_family",
    "finding_target",
    "findings_payload",
    "impact_error_section",
    "impact_section",
    "pattern_error_section",
    "pattern_section",
    "test_impact_clusters",
]
