"""Deterministic Markdown views of a deep review packet."""
from collections.abc import Mapping
import math
from typing import Any
MAP = {"security": "security", "performance": "performance", "failure-modes": "failure-modes",
       "data-structures": "data-structures", "scalability": "scalability", "architecture": "architecture",
       "tests-and-coverage": "tests", "dependencies": "dependencies",
       "operations": "ops", "documents-and-api": "docs", "correctness": None}

# Collector/repository-review section -> canonical report subject. Keep this
# direction separate from ``MAP``: ``MAP`` still decides output filenames and
# coverage lookups. repo_review uses section names that are not all subject
# names, and these must not silently fall through to correctness:
#   ops/operations          -> operations
#   docs/documents-and-api  -> documents-and-api
#   health                  -> architecture: size, TODO density and unreferenced
#                              symbols are maintainability/structure signals.
#   churn                   -> tests-and-coverage: untested churn and unstable
#                              edit sessions are answered with regression tests.
SOURCE_SUBJECT = {
    "security": "security",
    "performance": "performance",
    "failure-modes": "failure-modes",
    "data-structures": "data-structures",
    "scalability": "scalability",
    "architecture": "architecture",
    "health": "architecture",
    "tests": "tests-and-coverage",
    "tests-and-coverage": "tests-and-coverage",
    "churn": "tests-and-coverage",
    "dependencies": "dependencies",
    "ops": "operations",
    "operations": "operations",
    "docs": "documents-and-api",
    "documents-and-api": "documents-and-api",
    "correctness": "correctness",
}

_SEVERITY_ORDER = {"blocking": 0, "major": 1, "minor": 2, "note": 3}
_METRIC_LIMIT = 1_000_000_000_000
_METRIC_SPECS = {
    # subject: (section-level numeric fields, nested numeric fields,
    #          nested status fields)
    "security": {
        "section": {"files_checked": "Files checked",
                    "trust_boundary_files_checked": "Trust-boundary files checked",
                    "trust_boundary_findings": "Trust-boundary candidates"},
    },
    "performance": {
        "profile_metrics": {"series": "Profile series",
                            "points": "Profile points",
                            "mapped": "Mapped profile series"},
    },
    "scalability": {
        "profile_metrics": {"series": "Profile series",
                            "points": "Profile points",
                            "mapped": "Mapped profile series"},
    },
    "data-structures": {
        "data_inventory": {"files_seen": "Files seen",
                           "bytes_read": "Bytes read",
                           "duration_s": "Analysis seconds"},
        "data_inventory_counts": {"models": "Models",
                                  "schema_declarations": "Schema declarations",
                                  "documents": "Structured documents"},
    },
    "dependencies": {
        "inventory": {"files_seen": "Files seen", "bytes_read": "Bytes read",
                      "duration_s": "Analysis seconds"},
        "inventory_counts": {"manifests": "Manifests",
                             "lockfiles": "Lockfiles",
                             "version_conflicts": "Version conflicts",
                             "duplicate_declarations": "Duplicate declarations"},
        "states": {"cve_state": ("CVE state", {"unknown", "not_assessed"}),
                   "local_advisories.state": ("Local advisory state", {
                       "unknown", "invalid", "matches_found",
                       "no_exact_match_in_supplied_data"}),
                   "license_policy.state": ("License policy state", {
                       "unknown", "invalid", "evaluated"})},
    },
    "operations": {
        "inventory": {"files_seen": "Files seen", "bytes_read": "Bytes read"},
        "static_validation": {
            "files_checked": "Configuration files checked",
            "findings": "Static configuration candidates",
            "kubernetes_json_files_checked": "Kubernetes JSON files checked",
            "kubernetes_json_files_unknown": "Kubernetes JSON files unknown",
            "selected_yaml_files_checked": "Selected YAML files checked",
            "selected_yaml_files_unknown": "Selected YAML files unknown",
            "terraform_files_parsed": "Terraform files parsed",
            "terraform_files_unknown": "Terraform files unknown",
        },
        "states": {"static_validation.state": ("Static validation state",
                                                  {"checked", "unknown"})},
    },
    "documents-and-api": {
        "doc_coverage": {"docs_checked": "Documentation files checked",
                          "docs_found": "Documentation files found",
                          "local_imports_checked": "Local imports checked",
                          "stale_local_imports": "Stale local imports",
                          "documented_modules": "Documented modules",
                          "documented_symbols_checked": "Documented symbols checked",
                          "removed_or_renamed_symbols": "Removed or renamed symbols"},
        "section_counts": {"api_docs_files_checked": "API docs files checked"},
    },
}


def subject_for_section(section):
    """Return the canonical subject for a raw collector section name.

    Both subject keys and report-section values from ``MAP`` are accepted, so
    `operations`, `documents-and-api`, `tests`, and their output-section
    aliases route without falling through to correctness.
    """
    text = str(section or "").strip()
    if text in SOURCE_SUBJECT:
        return SOURCE_SUBJECT[text]
    if text in MAP:
        return text
    for subject, value in MAP.items():
        if value is not None and value == text:
            return subject
    return "correctness"


def sources_for_subject(subject):
    """Return raw section names whose findings/coverage belong to ``subject``.

    The primary ``MAP`` section comes first so merged coverage keeps the main
    report's scope; aliases are appended in a stable order.
    """
    primary = MAP.get(subject)
    sources = [source for source, target in SOURCE_SUBJECT.items()
               if target == subject]
    sources.sort(key=lambda source: (source != primary, source))
    return tuple(sources)


def _risk_key(finding):
    priority = finding.get("priority") or {}
    score = priority.get("score")
    blast = priority.get("blast_radius") or {}
    reachable = blast.get("reachable_callers")
    direct = blast.get("direct_symbols")
    return (_SEVERITY_ORDER.get(finding.get("severity"), 4),
            -(score if isinstance(score, (int, float)) else 0),
            -(reachable if isinstance(reachable, (int, float)) else 0),
            -(direct if isinstance(direct, (int, float)) else 0),
            str(finding.get("id", "")))


def rank_findings(findings):
    """Return a globally ranked view: severity, priority, blast radius, id."""
    return sorted(findings, key=_risk_key)


def _slug(value):
    return "".join(char.lower() if char.isalnum() else "-"
                   for char in str(value)).strip("-")


def _cell(value):
    """Make untrusted finding values safe and single-line in Markdown tables."""
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("\r", " ").replace("\n", " ")
    # Values also appear in prose paragraphs. Escape Markdown delimiters so
    # caller-controlled scope/coverage text cannot create links or formatting.
    for char in ("`", "*", "_", "[", "]", "(", ")", "~", "!"):
        text = text.replace(char, "\\" + char)
    return text


def _display_location(location):
    if not isinstance(location, Mapping):
        return str(location)
    path = location.get("path", "")
    symbol = location.get("symbol")
    line = location.get("line") or location.get("start")
    result = str(path)
    if symbol:
        result += f"::{symbol}"
    if line:
        result += f":{line}"
    return result


def _bounded_metric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _METRIC_LIMIT else None
    if (not isinstance(value, float) or not math.isfinite(value)
            or not 0 <= value <= _METRIC_LIMIT):
        return None
    return round(value, 3)


_FMEA_RULES = {
    "unbounded_exception_retry": {
        "symptom": "An exception handler may continue an unconditional loop.",
        "detection": "Static AST rule matched: unconditional loop with an exception-handler continue.",
    },
    "silently_suppressed_exception": {
        "symptom": "An exception handler may suppress an exception without reporting it.",
        "detection": "Static AST rule matched: a bare or broad handler contains only pass.",
    },
}


def _fmea_cell(value, *, limit=320):
    """Bound and escape a field in the derived FMEA view."""
    text = str(value or "Unknown")
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return _cell(text)


def _render_failure_fmea(findings):
    """Render an evidence-linked FMEA view without changing source findings."""
    if not findings:
        return []
    rows = ["## Failure-mode analysis", "",
            "Static review candidates only; no runtime criticality or likelihood is assigned.",
            "", "| Finding | Symptom | Trigger | Detection | Mitigation | Evidence |",
            "| --- | --- | --- | --- | --- | --- |"]
    for finding in findings:
        kind = str(finding.get("type", ""))
        rule = _FMEA_RULES.get(kind)
        symptom = rule["symptom"] if rule else "Unknown (no supported symptom mapping)."
        detection = (rule["detection"] if rule
                     else "Unknown (no supported detection mapping).")
        trigger = "Unknown (static source inspection does not establish runtime trigger conditions)."
        mitigation = finding.get("recommendation") or "Unknown (no mitigation recorded)."
        evidence_items = finding.get("evidence")
        has_evidence = (isinstance(evidence_items, list)
                        and any(isinstance(item, Mapping) for item in evidence_items))
        evidence = (f"See evidence details for {_fmea_cell(finding.get('id', 'finding'))}."
                    if has_evidence
                    else "Unknown (source finding has no evidence items).")
        rows.append("| " + " | ".join((
            _fmea_cell(finding.get("id", "finding")),
            _fmea_cell(symptom), _fmea_cell(trigger), _fmea_cell(detection),
            _fmea_cell(mitigation), _fmea_cell(evidence))) + " |")
    return rows


def _subject_metrics(subject, findings, source_section, *, status,
                     checks, skipped, unknown, omissions):
    """Build a numeric/status-only metric view from allowlisted fields."""
    metrics = {
        "Status": status if status in {"complete", "partial", "unknown"}
        else "unknown",
        "Findings": len(findings),
        "Blocking findings": sum(item.get("severity") == "blocking" for item in findings),
        "Major findings": sum(item.get("severity") == "major" for item in findings),
        "Minor findings": sum(item.get("severity") == "minor" for item in findings),
        "Note findings": sum(item.get("severity") == "note" for item in findings),
        "Evidence items": sum(len(item.get("evidence") or []) for item in findings),
        "Locations": sum(len(item.get("locations") or []) for item in findings),
        "Checks": len(checks), "Skipped checks": len(skipped),
        "Unknown checks": len(unknown), "Omissions": len(omissions),
    }
    source = source_section if isinstance(source_section, Mapping) else {}
    spec = _METRIC_SPECS.get(subject, {})

    def numbers(container, fields):
        if not isinstance(container, Mapping):
            return
        for key, label in fields.items():
            value = _bounded_metric(container.get(key))
            if value is not None:
                metrics[label] = value

    numbers(source, spec.get("section", {}))
    for key, label in spec.get("section_counts", {}).items():
        value = source.get(key)
        if isinstance(value, list):
            metrics[label] = min(len(value), _METRIC_LIMIT)
    for container_key, fields in spec.items():
        if container_key in {"section", "section_counts", "states"} or container_key.endswith("_counts"):
            continue
        numbers(source.get(container_key), fields)
    for container_key, fields in spec.items():
        if not container_key.endswith("_counts"):
            continue
        container = source.get(container_key.removesuffix("_counts"))
        if not isinstance(container, Mapping):
            continue
        for key, label in fields.items():
            value = container.get(key)
            if isinstance(value, list):
                metrics[label] = min(len(value), _METRIC_LIMIT)
    for dotted, (label, allowed) in spec.get("states", {}).items():
        value: Any = source
        for key in dotted.split("."):
            value = value.get(key) if isinstance(value, Mapping) else None
        if isinstance(value, str) and value in allowed:
            metrics[label] = value
    return metrics


def render_subject(subject, findings, *, status, omissions, checks=(), skipped=(), unknown=(), scope=None,
                   metrics=None):
    rows = [f"# {subject.replace('-', ' ').title()}", "",
            "## Scope", "", _cell(scope or f"Review subject: {subject}."), "",
            f"Status: **{status}**", "",
            "## Findings", "", "| ID | Severity | Type | Subject views | Location |",
            "| --- | --- | --- | --- | --- |"]
    for f in findings:
        locations = f.get("locations") or [{}]
        loc = ", ".join(_display_location(item) for item in locations)
        views = ", ".join(f.get("subjects", [f.get("subject", subject)]))
        rows.append("| " + " | ".join(_cell(f.get(key, "")) for key in
                    ("id", "severity", "type")) + " | " + _cell(views) + " | " +
                     _cell(loc) + " |")
    if subject == "failure-modes":
        rows.extend([""] + _render_failure_fmea(findings))
    metric_rows = _subject_metrics(
        subject, findings, metrics, status=status, checks=checks,
        skipped=skipped, unknown=unknown, omissions=omissions)
    rows.extend(["", "## Metrics", "", "| Metric | Value |",
                 "| --- | ---: |"])
    for label, value in metric_rows.items():
        rows.append(f"| {_cell(label)} | {_cell(value)} |")
    rows.extend(["", "## Coverage", "",
                 f"Status: **{_cell(status)}**; {len(checks)} checks, "
                 f"{len(skipped)} skipped, {len(unknown)} unknown, "
                 f"{len(omissions)} omitted.", "", "Checks: " +
                 (_cell(", ".join(map(str, checks))) if checks else "none recorded."), "",
                 "Skipped: " + (_cell("; ".join(map(str, skipped))) if skipped else "none recorded."), "",
                 "Unknown: " + (_cell("; ".join(map(str, unknown))) if unknown else "none recorded."), "",
                 "Omissions: " + (_cell("; ".join(map(str, omissions))) if omissions else "none recorded."), "",
                 "## Evidence and next actions", ""])
    for finding in findings:
        finding_id = finding.get("id", "Finding")
        rows.extend([f'<a id="finding-{_slug(finding_id)}"></a>',
                     f"### {_cell(finding_id)}", "",
                     _cell(finding.get("impact") or finding.get("title") or "No impact recorded."), "",
                     f"Confidence: {_cell(finding.get('confidence', 'unknown'))}; "
                     f"evidence kind: {_cell(finding.get('evidence_kind', 'unknown'))}.", "",
                     "Evidence:", ""])
        observations = finding.get("subject_observations", [])
        if observations:
            rows.extend(["Subject observations:", ""])
            for observation in observations:
                rows.append("- " + _cell(
                    f"{observation.get('subject')}: {observation.get('title') or observation.get('type')}"))
            rows.append("")
        evidence_items = [item for item in finding.get("evidence", [])
                          if isinstance(item, Mapping)]
        if not evidence_items:
            rows.append("- none recorded.")
        for item in evidence_items:
            details = [f"{item.get('source', 'unknown')} ({item.get('method', 'unknown')})"]
            for key in ("subject", "kind", "hash", "excerpt_ref", "rule", "line"):
                if item.get(key) is not None:
                    details.append(f"{key}: {item[key]}")
            rows.append("- " + _cell("; ".join(map(str, details))))
        hashes = [item.get("hash") for item in evidence_items]
        integrity = (f"{sum(bool(value) for value in hashes)}/{len(hashes)} evidence items "
                     "carry a content hash" if hashes else "no evidence hashes recorded")
        rows.extend(["", "Evidence integrity: " + _cell(integrity), "",
                     "Next action: " + _cell(finding.get("recommendation") or "Review the evidence."), ""])
    return "\n".join(rows)


def render_readme(subjects, findings):
    lines = ["# Deep review", "", "Deterministic offline review report.", "",
             "[Method and scope](./00-method-and-scope.md) · "
             "[Findings index](./findings-index.md)", "", "## Subjects", ""]
    lines += [f"- [{name}](./{row.get('report', f'{i:02d}-{name}.md')}): {row['status']} "
              f"({len(row['findings'])} findings)"
              for i, (name, row) in enumerate(subjects.items(), 1)]
    counts = {level: sum(f.get("severity") == level for f in findings)
              for level in ("blocking", "major", "minor", "note")}
    confidence = {level: sum(f.get("confidence", "unknown") == level for f in findings)
                  for level in ("high", "medium", "low", "unknown")}
    lines += ["", "## Scorecard", "", "| Subject | Status | Findings | Blocking | Major | Minor | Note | Confidence H/M/L/U | Checks | Skipped | Unknown | Omitted |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |"]
    by_id = {f.get("id"): f for f in findings}
    for name, row in subjects.items():
        items = [by_id[item] for item in row.get("findings", []) if item in by_id]
        severity_counts = [sum(f.get("severity") == level for f in items)
                           for level in ("blocking", "major", "minor", "note")]
        confidence_counts = [sum(f.get("confidence", "unknown") == level for f in items)
                             for level in ("high", "medium", "low", "unknown")]
        lines.append("| " + " | ".join([_cell(name), _cell(row.get("status", "unknown")),
            str(len(items)), *(str(n) for n in severity_counts),
            "H/M/L/U " + "/".join(map(str, confidence_counts)),
            _cell(", ".join(map(str, row.get("checks", []))) or "none"),
            _cell(", ".join(map(str, row.get("skipped", []))) or "none"),
            _cell(", ".join(map(str, row.get("unknown", []))) or "none"),
            _cell(", ".join(map(str, row.get("omitted", []))) or "none")]) + " |")
    lines += ["", "## Finding counts", "", "Severity: " + ", ".join(
        f"{key} {value}" for key, value in counts.items()), "", "Confidence: " +
        ", ".join(f"{key} {value}" for key, value in confidence.items()), "",
        f"Total findings: {len(findings)}", ""]
    lines += ["## Top risks", ""]
    if findings:
        subject_by_id = {finding.get("id"): (
                             finding.get("subject", ""),
                             subjects.get(finding.get("subject", ""), {}).get(
                                 "report", f"{finding.get('subject', '')}.md"))
                         for finding in findings}
        for finding in rank_findings(findings)[:10]:
            name, report = subject_by_id.get(finding.get("id"), (finding.get("subject", ""), ""))
            anchor = f"#finding-{_slug(finding.get('id', ''))}"
            location = ", ".join(_display_location(item)
                                  for item in finding.get("locations", [])
                                  if isinstance(item, Mapping))
            description = finding.get("title") or finding.get("impact") or finding.get("type", "Finding")
            label = f"[{_cell(finding.get('id', 'finding'))}]({report}{anchor})" if report else _cell(finding.get("id", "finding"))
            lines.append(f"- **{_cell(finding.get('severity', 'unknown'))}** {label} "
                         f"({_cell(name)}): {_cell(description)}" +
                         (f" — {_cell(location)}" if location else ""))
    else:
        lines.append("No findings recorded.")
    lines += ["", f"See the [findings index](./findings-index.md) for all "
              f"{len(findings)} findings.", ""]
    return "\n".join(lines)


def render_method(*, root, profile, revision, tool_versions, budget_s,
                  graph=None, graph_hash=None, graph_hash_reason=None,
                  priority_graph=None, profile_reports=(), omissions=(),
                  normalization_omitted=0, range_spec=None, index=False,
                  bench_timeout_s=None):
    """Render the deterministic method/scope document.

    Elapsed durations stay in ``manifest.json``; this file records configured
    budgets and points at the manifest so repeated runs remain byte-identical.
    """
    graph = graph if isinstance(graph, Mapping) else {}
    priority_graph = priority_graph if isinstance(priority_graph, Mapping) else {}
    lines = [
        "# Method and scope", "",
        f"Root: `{_cell(root)}`", "",
        f"Profile: `{_cell(profile)}`", "",
        f"Revision: `{_cell(revision or 'unknown')}`", "",
        "This run is offline and deterministic. It does not run tests or import "
        "the target. Static Python AST checks cover selected performance, "
        "failure-mode, data-structure, and scalability patterns. Other patterns "
        "and languages remain unassessed.", "",
        "## Tool versions", "",
    ]
    for name in sorted(tool_versions):
        lines.append(f"- {_cell(name)}: {_cell(tool_versions[name])}")
    lines += [
        "", "## Budgets and timings", "",
        f"- Total budget: {budget_s:g} s",
        f"- Index refresh: {'requested' if index else 'not requested'}",
        f"- Range review: `{_cell(range_spec)}`" if range_spec is not None
        else "- Range review: not requested",
    ]
    if bench_timeout_s is not None:
        lines.append(f"- Benchmark timeout: {bench_timeout_s:g} s per command")
    lines += [
        "- Phase timings (`collect_s`, `bench_s`, `compose_s`) and elapsed "
        "budget: recorded in `manifest.json` (`timings`, `budgets`).", "",
        "## Graph and prioritization", "",
        f"- Graph freshness: `{_cell(graph.get('freshness', 'unknown'))}`",
        f"- Graph content hash: `{_cell(graph_hash or 'unknown')}`" +
        (f" (reason: {_cell(graph_hash_reason)})"
         if graph_hash_reason else ""),
        f"- Priority graph: `{_cell(priority_graph.get('freshness', 'unknown'))}` "
        f"({_cell(priority_graph.get('reason') or 'no reason recorded')}).", "",
        "## Profile reports", "",
    ]
    if profile_reports:
        lines += [f"- `{_cell(path)}`" for path in profile_reports]
    else:
        lines.append("- none supplied.")
    lines += ["", "## Explicit omissions and unknowns", ""]
    omission_rows = list(omissions)
    if priority_graph.get("freshness") not in (None, "fresh"):
        omission_rows.insert(0, "graph: " + str(
            priority_graph.get("reason") or "freshness was not established"))
    if normalization_omitted:
        omission_rows.insert(0, f"normalization: {normalization_omitted} findings "
                                "were omitted because the review budget ran out")
    if omission_rows:
        lines += [f"- {_cell(row)}" for row in omission_rows]
    else:
        lines.append("- none recorded by collectors.")
    lines += ["", "## Report links", "",
              "- [Findings index](./findings-index.md)",
              "- [README](./README.md)", ""]
    return "\n".join(lines)
