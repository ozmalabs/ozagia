"""Offline adapter from commit-range code review to deep-review findings."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import PurePath
from collections.abc import Mapping
from typing import Any

from ozagia.core import code_review, security

MAX_CHANGED_FILES = 20
MAX_IMPORTS = 20
MAX_CALLER_FILES = 20
MAX_CALLERS = 50
MAX_CALLER_DEPTH = 3
MAX_GRAPH_NODES = 20_000
MAX_GRAPH_EDGES = 100_000
MAX_GRAPH_BYTES = 32 * 1024 * 1024

_SUBJECT_SECTION = {
    "security": "security",
    "performance": "performance",
    "failure-modes": "failure-modes",
    "data-structures": "data-structures",
    "scalability": "scalability",
    "architecture": "architecture",
    "tests-and-coverage": "tests",
    "dependencies": "dependencies",
    "operations": "ops",
    "documents-and-api": "docs",
    "correctness": "correctness",
}
_SECTION_SUBJECT = {value: key for key, value in _SUBJECT_SECTION.items()}
_TYPE_SECTION = {
    "security": "security",
    "security_sensitive": "security",
    "security_risk": "security",
    "performance": "performance",
    "performance_risk": "performance",
    "test": "tests",
    "tests": "tests",
    "missing_test": "tests",
    "test_failure": "tests",
    "failure_modes": "failure-modes",
    "failure-modes": "failure-modes",
    "unhandled_error": "failure-modes",
    "architecture": "architecture",
    "breaking_change": "architecture",
    "contract_mismatch": "architecture",
    "stale_reference": "architecture",
    "duplicate_logic": "architecture",
    "scope_creep": "architecture",
    "dependencies": "dependencies",
    "dependency": "dependencies",
    "operations": "ops",
    "ops": "ops",
    "docs": "docs",
    "documentation": "docs",
    "unknown": "correctness",
}
_SECURITY_HINT = re.compile(
    r"security|secret|credential|auth|permission|tls|crypto|cwe-?\d+|"
    r"injection|xss|csrf|ssrf|vulnerab", re.I)
_PERFORMANCE_HINT = re.compile(
    r"performance|memory|cpu|latency|quadratic|n\+1|hot path", re.I)
_TEST_HINT = re.compile(r"\btest|coverage\b", re.I)
_FAILURE_HINT = re.compile(
    r"error|exception|timeout|retry|failure|resilience", re.I)
_SCOPES = {
    "security": ("Security-category changed-code findings; exploitability and "
                 "runtime exposure were not assessed."),
    "performance": ("Performance-category changed-code findings; no benchmark "
                    "was run."),
    "failure-modes": ("Failure-mode changed-code findings; no runtime failure "
                      "or telemetry evidence was collected."),
    "tests": ("Test-related changed-code findings; no test was executed."),
    "architecture": ("Architecture and contract changed-code findings; no "
                     "external compatibility check was run."),
    "correctness": ("Changed-code review findings from deterministic diff "
                    "signals; they are candidates, not verified defects."),
}


def _redact_path(value: Any) -> str:
    path = security.redact_text(str(value))
    if PurePath(path).is_absolute() or (len(path) > 1 and path[1] == ":"):
        return "<absolute-path-redacted>"
    return path


def _subject_section(value: Any) -> str | None:
    text = str(value or "").strip()
    if text in _SUBJECT_SECTION:
        return _SUBJECT_SECTION[text]
    if text in _SECTION_SUBJECT:
        return text
    return None


def _section_for(raw: Mapping[str, Any]) -> str:
    """Map one range finding to the closest deep-review subject section."""
    explicit = _subject_section(raw.get("section") or raw.get("subject"))
    if explicit and explicit != "correctness":
        return explicit
    hint_text = " ".join(str(raw.get(key) or "") for key in
                         ("category", "area", "rule", "kind", "cwe", "cwe_id"))
    # An explicit security category is never demoted to correctness just
    # because the generic finding type is unknown or test-shaped.
    if _SECURITY_HINT.search(hint_text):
        return "security"
    section = _TYPE_SECTION.get(str(raw.get("type") or "unknown"))
    if section and section != "correctness":
        return section
    if _PERFORMANCE_HINT.search(hint_text):
        return "performance"
    if _TEST_HINT.search(hint_text):
        return "tests"
    if _FAILURE_HINT.search(hint_text):
        return "failure-modes"
    return explicit or section or "correctness"


def _graph_freshness(graph: Mapping[str, Any] | None) -> str:
    value = str((graph or {}).get("freshness") or "unknown").lower()
    if value in {"missing", "none", "absent"} or (graph or {}).get("available") is False:
        return "absent"
    return value


def _changed_files(packet: Mapping[str, Any]) -> list[str]:
    paths: set[str] = set()
    for row in packet.get("files") or []:
        if not isinstance(row, Mapping):
            continue
        path = row.get("path") or row.get("file")
        if isinstance(path, str) and path:
            paths.add(path)
    plan = packet.get("test_plan")
    if isinstance(plan, Mapping):
        for path in plan.get("changed_files") or []:
            if isinstance(path, str) and path:
                paths.add(path)
    return sorted(paths)


def _node_attrs(row: Mapping[str, Any]) -> Mapping[str, Any]:
    attrs = row.get("attrs")
    return attrs if isinstance(attrs, Mapping) else {}


def blast_radius_facts(graph: Mapping[str, Any] | None, changed_files: list[str], *,
                       freshness: str = "unknown",
                       reason: str | None = None) -> dict[str, Any]:
    """Bounded changed-file/import/caller facts from a fresh graph only.

    ``freshness`` must be ``"fresh"`` only after the caller verified the
    snapshot. Stale, absent, oversized, or invalid graph data yields an
    explicit omission instead of inferred impact.
    """
    changed = sorted({str(path) for path in changed_files if path})
    base = {
        "changed_files": changed[:MAX_CHANGED_FILES],
        "changed_file_count": len(changed),
        "changed_files_truncated": len(changed) > MAX_CHANGED_FILES,
        "imports": [],
        "imported_by": [],
        "callers": {"reachable_symbols": 0, "files": [],
                    "depth_limit": MAX_CALLER_DEPTH, "truncated": False},
        "tests": [],
    }
    if freshness != "fresh":
        base.update({
            "status": "omitted",
            "graph_freshness": freshness,
            "omissions": [reason or f"blast radius omitted: graph freshness is {freshness}"],
        })
        return base
    if not isinstance(graph, Mapping):
        base.update({"status": "omitted", "graph_freshness": freshness,
                     "omissions": [reason or "blast radius omitted: graph data is unavailable"]})
        return base
    node_rows = graph.get("nodes")
    edge_rows = graph.get("edges")
    if not isinstance(node_rows, list) or not isinstance(edge_rows, list):
        base.update({"status": "omitted", "graph_freshness": freshness,
                     "omissions": ["blast radius omitted: graph nodes or edges are invalid"]})
        return base
    if len(node_rows) > MAX_GRAPH_NODES or len(edge_rows) > MAX_GRAPH_EDGES:
        base.update({"status": "omitted", "graph_freshness": freshness,
                     "omissions": ["blast radius omitted: graph exceeds node or edge bound"]})
        return base

    nodes: dict[int, dict[str, Any]] = {}
    file_paths: dict[int, str] = {}
    symbols_by_file: dict[str, list[int]] = {}
    for row in node_rows:
        if not isinstance(row, Mapping):
            continue
        try:
            ident = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        attrs = _node_attrs(row)
        node_type = str(row.get("type") or "")
        nodes[ident] = {"type": node_type, "file": attrs.get("file"),
                        "path": attrs.get("path") or (
                            attrs.get("file") if node_type == "File" else None),
                        "name": attrs.get("name")}
        if node_type == "File" and isinstance(nodes[ident]["path"], str):
            file_paths[ident] = nodes[ident]["path"]
        elif node_type == "Symbol" and isinstance(nodes[ident]["file"], str):
            symbols_by_file.setdefault(nodes[ident]["file"], []).append(ident)
    file_ids = {path: ident for ident, path in file_paths.items()}
    imports_out: dict[int, set[int]] = {}
    imports_in: dict[int, set[int]] = {}
    calls_in: dict[int, set[int]] = {}
    tests_in: dict[int, set[int]] = {}
    for edge in edge_rows:
        if not isinstance(edge, Mapping):
            continue
        try:
            source, target = int(edge.get("src")), int(edge.get("tgt"))
        except (TypeError, ValueError):
            continue
        edge_type = str(edge.get("type") or "")
        if source not in nodes or target not in nodes:
            continue
        if edge_type == "imports" and source in file_paths and target in file_paths:
            imports_out.setdefault(source, set()).add(target)
            imports_in.setdefault(target, set()).add(source)
        elif edge_type == "calls":
            calls_in.setdefault(target, set()).add(source)
        elif edge_type in {"tests_module", "mentions_file"}:
            tests_in.setdefault(target, set()).add(source)

    omissions = []
    imports = []
    imported_by = []
    for path in changed[:MAX_CHANGED_FILES]:
        ident = file_ids.get(path)
        if ident is None:
            continue
        outgoing = sorted(file_paths[item] for item in imports_out.get(ident, set())
                          if item in file_paths and file_paths[item] != path)
        incoming = sorted(file_paths[item] for item in imports_in.get(ident, set())
                          if item in file_paths and file_paths[item] != path)
        if outgoing:
            imports.append({"file": path, "imports": outgoing[:MAX_IMPORTS],
                            "truncated": len(outgoing) > MAX_IMPORTS})
        if incoming:
            imported_by.append({"file": path,
                                "imported_by": incoming[:MAX_IMPORTS],
                                "truncated": len(incoming) > MAX_IMPORTS})

    seeds = {item for path in changed for item in symbols_by_file.get(path, [])}
    reached: set[int] = set()
    frontier = set(seeds)
    for _ in range(MAX_CALLER_DEPTH):
        next_frontier: set[int] = set()
        for target in frontier:
            next_frontier.update(calls_in.get(target, ()))
        next_frontier -= seeds
        next_frontier -= reached
        if not next_frontier:
            break
        reached.update(next_frontier)
        frontier = next_frontier
        if len(reached) >= MAX_CALLERS:
            break
    caller_files = sorted({str(nodes[item]["file"]) for item in reached
                           if nodes.get(item, {}).get("file")})
    caller_files = [path for path in caller_files if path not in changed]

    tests = []
    for path in changed[:MAX_CHANGED_FILES]:
        targets = set(symbols_by_file.get(path, []))
        if path in file_ids:
            targets.add(file_ids[path])
        found: set[str] = set()
        for target in targets:
            found.update(file_paths[item] for item in tests_in.get(target, ())
                         if item in file_paths and file_paths[item] != path)
        if found:
            tests.append({"file": path, "tests": sorted(found)[:MAX_IMPORTS],
                          "truncated": len(found) > MAX_IMPORTS})
    if base["changed_files_truncated"]:
        omissions.append(
            f"blast radius limited to {MAX_CHANGED_FILES} changed files")
    if len(reached) >= MAX_CALLERS:
        omissions.append(f"caller list limited to {MAX_CALLERS} symbols")
    base.update({
        "status": "available",
        "graph_freshness": freshness,
        "changed_symbols": len(seeds),
        "imports": imports,
        "imported_by": imported_by,
        "callers": {"reachable_symbols": len(reached),
                    "files": caller_files[:MAX_CALLER_FILES],
                    "depth_limit": MAX_CALLER_DEPTH,
                    "truncated": len(caller_files) > MAX_CALLER_FILES},
        "tests": tests,
        "omissions": omissions,
    })
    return base


def _packet_blast_radius(root: str, packet: Mapping[str, Any], *,
                        changed_files: list[str] | None = None) -> dict[str, Any]:
    """Load and verify the graph inside the bounded review worker."""
    changed = _changed_files(packet) if changed_files is None else sorted(set(changed_files))
    graph_info = packet.get("graph") if isinstance(packet.get("graph"), Mapping) else {}
    freshness = _graph_freshness(graph_info)
    if freshness != "fresh":
        return blast_radius_facts(None, changed, freshness=freshness)
    from pathlib import Path

    from ozagia.core import repo_review
    try:
        path = repo_review._graph_file(Path(root), None)
        payload, _snapshot_stat = repo_review._read_graph_nofollow(
            path, max_bytes=MAX_GRAPH_BYTES)
        graph = json.loads(payload)
        if not isinstance(graph, dict):
            return blast_radius_facts(
                None, changed, freshness="unknown",
                reason="blast radius omitted: graph snapshot root is not an object")
        head = str(packet.get("head_commit") or "")
        info, _dev, _index = repo_review._load_graph(
            Path(root), path, head, snapshot_data=graph,
            allow_project_adapters=False)
        actual = _graph_freshness(info)
        if actual != "fresh":
            return blast_radius_facts(
                None, changed, freshness=actual,
                reason=f"blast radius omitted: graph is {actual}: {info.get('reason')}")
        return blast_radius_facts(graph, changed, freshness="fresh")
    except FileNotFoundError:
        return blast_radius_facts(
            None, changed, freshness="absent",
            reason="blast radius omitted: graph snapshot is missing")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return blast_radius_facts(
            None, changed, freshness="unknown",
            reason=f"blast radius omitted: graph evidence unavailable: {exc}")


def _packet_security_impact(root: str, packet: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize bounded graph links for security-category changed files.

    This reports repository linkage only. It does not establish reachability at
    runtime, exploitability, or whether a control is effective.
    """
    findings = [row for row in packet.get("findings", [])
                if isinstance(row, Mapping) and _section_for(row) == "security"]
    paths = sorted({str(row.get("file")) for row in findings
                    if isinstance(row.get("file"), str) and row.get("file")})
    if not paths:
        return {"status": "not_applicable", "changed_files": [],
                "evidence": [], "unknowns": []}
    blast = _packet_blast_radius(root, packet, changed_files=paths)
    if blast.get("status") != "available":
        return {"status": "omitted", "changed_files": paths[:MAX_CHANGED_FILES],
                "evidence": [], "unknowns": list(blast.get("omissions") or [
                    "security impact omitted: fresh graph evidence unavailable"]),
                "graph_freshness": blast.get("graph_freshness", "unknown")}
    evidence = []
    for row in blast.get("imports", []):
        evidence.append({"kind": "imports", "file": row["file"],
                         "related_files": row["imports"],
                         "truncated": row.get("truncated", False)})
    for row in blast.get("imported_by", []):
        evidence.append({"kind": "imported_by", "file": row["file"],
                         "related_files": row["imported_by"],
                         "truncated": row.get("truncated", False)})
    caller_files = blast.get("callers", {}).get("files", [])
    if caller_files:
        evidence.append({"kind": "callers", "related_files": caller_files,
                         "depth_limit": MAX_CALLER_DEPTH,
                         "reachable_symbols": blast["callers"]["reachable_symbols"],
                         "truncated": blast["callers"].get("truncated", False)})
    for row in blast.get("tests", []):
        evidence.append({"kind": "tests", "file": row["file"],
                         "related_files": row["tests"],
                         "truncated": row.get("truncated", False)})
    unknowns = ["Graph links do not establish runtime reachability, exploitability, or control effectiveness."]
    unknowns.extend(blast.get("omissions") or [])
    if not evidence:
        unknowns.append("No bounded import, caller, or test links were recorded for the changed security files.")
    return {"status": "available", "graph_freshness": "fresh",
            "changed_files": blast["changed_files"], "evidence": evidence,
            "unknowns": unknowns}


def _redact_blast(blast: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(blast)
    result["changed_files"] = [_redact_path(item)
                               for item in blast.get("changed_files") or []]
    for key, field in (("imports", "imports"), ("imported_by", "imported_by")):
        rows = []
        for row in blast.get(key) or []:
            if not isinstance(row, Mapping):
                continue
            copy = dict(row)
            copy["file"] = _redact_path(row.get("file"))
            copy[field] = [_redact_path(item) for item in row.get(field) or []]
            rows.append(copy)
        result[key] = rows
    callers = blast.get("callers")
    if isinstance(callers, Mapping):
        result["callers"] = {**callers,
                             "files": [_redact_path(item)
                                       for item in callers.get("files") or []]}
    tests = []
    for row in blast.get("tests") or []:
        if not isinstance(row, Mapping):
            continue
        tests.append({**row, "file": _redact_path(row.get("file")),
                      "tests": [_redact_path(item)
                                for item in row.get("tests") or []]})
    result["tests"] = tests
    result["omissions"] = [security.redact_text(str(item))
                           for item in blast.get("omissions") or []]
    return result


def _redact_security_impact(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["changed_files"] = [_redact_path(item)
                               for item in value.get("changed_files", [])]
    result["evidence"] = [
        {**row, **({"file": _redact_path(row["file"])} if row.get("file") else {}),
         **({"related_files": [_redact_path(item) for item in row.get("related_files", [])]}
            if "related_files" in row else {})}
        for row in value.get("evidence", []) if isinstance(row, Mapping)]
    result["unknowns"] = [security.redact_text(str(item))
                           for item in value.get("unknowns", [])]
    return result


def collect_range(root: str, range_spec: str, *, budget_s: float = 60.0) -> dict[str, Any]:
    """Return normalized range findings, subject sections, and graph evidence.

    The existing code-review engine reads git diff and an already-present,
    fresh graph. It does not refresh the graph or run tests. Optional Ocelot
    and library-hint work is disabled here; no model, network, or project
    execution is enabled.
    """
    if not isinstance(range_spec, str) or not range_spec.strip():
        raise ValueError("range_spec must be a non-empty BASE..HEAD range")
    try:
        budget = float(budget_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_s must be finite and non-negative") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_s must be finite and non-negative")
    if budget == 0:
        return {
            "findings": [],
            "sections": {"correctness": {
                "checked": [],
                "not_checked": [{"reason": "no review budget remained for range review"}],
                "range": range_spec,
            }},
            "graph": {"freshness": "unknown", "available": False,
                      "usable": False},
            "blast_radius": blast_radius_facts(
                None, [], freshness="unknown",
                reason="blast radius omitted: no review budget remained"),
            "range": range_spec,
        }
    timeout = min(budget, 60.0)
    try:
        envelope = _bounded_review(root, range_spec, timeout=timeout)
    except code_review.CodeReviewError as exc:
        if "time budget exhausted" in str(exc).lower():
            return {
                "findings": [],
                "sections": {"correctness": {
                    "checked": [],
                    "not_checked": [{"reason": "range review time budget exhausted"}],
                    "range": range_spec,
                }},
                "graph": {"freshness": "unknown", "available": False,
                          "usable": False},
                "blast_radius": blast_radius_facts(
                    None, [], freshness="unknown",
                    reason="blast radius omitted: range review time budget exhausted"),
                "range": range_spec,
            }
        raise ValueError(str(exc)) from exc
    if "error" in envelope:
        raise ValueError(str(envelope["error"]))
    packet = envelope.get("packet")
    if not isinstance(packet, Mapping):
        raise ValueError("bounded range review returned an invalid packet")
    graph = packet.get("graph")
    graph = dict(graph) if isinstance(graph, dict) else {
        "freshness": "unknown", "available": False, "usable": False}
    blast = packet.get("blast_radius")
    if isinstance(blast, Mapping):
        blast = _redact_blast(blast)
    else:
        blast = blast_radius_facts(None, _changed_files(packet),
                                   freshness=_graph_freshness(graph))
    security_impact = packet.get("security_impact")
    if not isinstance(security_impact, Mapping):
        security_impact = _packet_security_impact(root, packet)
    security_impact = _redact_security_impact(security_impact)

    findings = []
    by_section: dict[str, list[dict[str, Any]]] = {}
    for raw in packet.get("findings", []):
        if not isinstance(raw, Mapping):
            continue
        path = raw.get("file")
        if isinstance(path, str):
            path = _redact_path(path)
        keys = ([f"file:{path}"] if isinstance(path, str) and path else [])
        graph_keys = raw.get("graph_keys") or []
        graph_keys = sorted({str(key) for key in graph_keys if key}) \
            if isinstance(graph_keys, list) else []
        summary = security.redact_text(
            str(raw.get("message") or raw.get("type") or "Range review finding")
        )
        severity = raw.get("severity", "note")
        # Deterministic diff signals are candidates, not verified impact.
        if severity == "blocking":
            severity = "major"
        section = _section_for(raw)
        normalized = {
            "section": section,
            "range_subject": _SECTION_SUBJECT.get(section, "correctness"),
            "type": str(raw.get("type") or "unknown"),
            "severity": severity,
            "confidence": raw.get("confidence", "unknown"),
            "keys": keys,
            "summary": summary,
            "recommendation": "Review the changed code and its recorded evidence.",
            "range_finding": True,
            "scope": (None if keys else
                      "Range review finding has no repository file location."),
        }
        if path and path != "<absolute-path-redacted>":
            normalized["file"] = str(path)
        if isinstance(raw.get("line"), int) and not isinstance(raw.get("line"), bool):
            normalized["line"] = raw["line"]
        # Stable per-rule component separates same-type findings in one file,
        # while remaining independent of line movement and CR-### ordering.
        normalized["rule_key"] = (
            f"range:{section}:{normalized['type']}:{summary}:graph={','.join(graph_keys)}")
        findings.append(normalized)
        by_section.setdefault(section, []).append(normalized)

    not_checked = list(packet.get("not_assessed") or [])
    not_checked.extend({"reason": str(item)} for item in packet.get("omissions") or [])
    if blast.get("status") == "omitted":
        for reason in blast.get("omissions") or []:
            not_checked.append({"area": "graph", "reason": str(reason)})
    range_value = packet.get("range")
    if isinstance(range_value, Mapping):
        range_value = {str(key): security.redact_text(str(value))
                       for key, value in range_value.items()}
    elif range_value is not None:
        range_value = security.redact_text(str(range_value))
    coverage = packet.get("test_coverage")
    if isinstance(coverage, Mapping):
        # Keep the useful test-link facts while removing machine-specific
        # absolute paths before this packet is persisted or shared.
        safe_coverage = dict(coverage)
        safe_coverage.pop("root", None)
        graph_info = safe_coverage.get("graph")
        if isinstance(graph_info, Mapping):
            safe_coverage["graph"] = {
                key: value for key, value in graph_info.items()
                if key != "path"
            }
        coverage = safe_coverage
        not_checked.append({
            "area": "tests",
            "reason": ("test-link coverage and selection only; no test was "
                       "executed and line coverage was not assessed"),
        })
    sections = {}
    for section in sorted({"correctness", *by_section}):
        sections[section] = {
            "checked": ["git_range_review"],
            "not_checked": list(not_checked),
            "range": range_value,
            "scope": _SCOPES.get(section, "Changed-code review findings only."),
        }
        if section == "correctness":
            sections[section]["diff"] = packet.get("diff")
    result = {
        "findings": findings,
        "sections": sections,
        "graph": graph,
        "blast_radius": blast,
        "security_impact": security_impact,
        "range": range_value,
    }
    if isinstance(coverage, Mapping):
        result["test_coverage"] = coverage
    return result


def _bounded_review(root: str, range_spec: str, *, timeout: float) -> dict[str, Any]:
    """Run code review in a killable subprocess so its caller's budget holds."""
    code = """\
import json, sys
from ozagia.core import code_review as review, test_coverage
from ozagia.core.deep_review import range_review as dr_range
data = json.load(sys.stdin)
try:
    packet = review.review_range(data['root'], data['range'],
                                 ocelot=False, library_hints=False)
    plan = packet.get('test_plan')
    if isinstance(plan, dict) and 'changed_files' in plan:
        try:
            packet['test_coverage'] = test_coverage.report(data['root'], plan)
        except Exception as exc:
            packet['test_coverage'] = {
                'schema': test_coverage.SCHEMA,
                'available': False,
                'reason': type(exc).__name__,
                'executed': False,
                'truth_assessed': False,
            }
    try:
        packet['blast_radius'] = dr_range._packet_blast_radius(data['root'], packet)
        packet['security_impact'] = dr_range._packet_security_impact(data['root'], packet)
    except Exception as exc:
        packet['blast_radius'] = dr_range.blast_radius_facts(
            None, [], freshness='unknown',
            reason=f'blast radius omitted: {type(exc).__name__}')
except Exception as exc:
    print(json.dumps({'error': str(exc)}))
else:
    print(json.dumps({'packet': packet}, sort_keys=True))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            input=json.dumps({"root": str(root), "range": range_spec}),
            capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise code_review.CodeReviewError("range review time budget exhausted") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise code_review.CodeReviewError(f"bounded range review failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise code_review.CodeReviewError("bounded range review subprocess failed")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise code_review.CodeReviewError("bounded range review returned invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise code_review.CodeReviewError("bounded range review returned invalid data")
    return envelope


__all__ = ["blast_radius_facts", "collect_range"]
