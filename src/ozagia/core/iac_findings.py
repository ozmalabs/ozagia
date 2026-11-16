"""Bounded, allowlist-only SARIF import for source findings.

This module reads supplied JSON only. It never invokes scanners and never
copies SARIF messages, properties, snippets, fingerprints, or arbitrary values
to returned facts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import PurePosixPath

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_JSON_NODES = 100_000
MAX_DEPTH = 64
MAX_RUNS = 100
MAX_RESULTS = 10_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SAFE_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-]{0,63}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LEVELS = frozenset({"error", "warning", "note", "none"})


@dataclass(frozen=True)
class IacFinding:
    rule_id: str
    severity: str
    path: str
    line: int
    tool_name: str
    tool_version: str
    provenance: str = "imported artifact"
    artifact_format: str = "sarif-2.1.0"


@dataclass(frozen=True)
class IacFindings:
    findings: tuple[IacFinding, ...]
    diagnostics: tuple[str, ...]


def import_sarif(text: str) -> IacFindings:
    """Import allowlisted SARIF 2.1.0 location and rule metadata."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        return IacFindings((), ("SARIF artifact exceeds 5 MiB limit",))
    duplicate = False

    def unique_object(pairs):
        nonlocal duplicate
        result = {}
        for key, value in pairs:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    try:
        document = json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return IacFindings((), ("invalid SARIF JSON artifact",))
    if duplicate:
        return IacFindings((), ("duplicate JSON keys are unsupported",))
    if not isinstance(document, dict) or document.get("version") != "2.1.0":
        return IacFindings((), ("unsupported SARIF schema version",))
    if not _bounded(document):
        return IacFindings((), ("SARIF structure exceeds depth or node limit",))
    runs = document.get("runs")
    if not isinstance(runs, list) or len(runs) > MAX_RUNS:
        return IacFindings((), ("SARIF runs must be a bounded array",))

    findings: list[IacFinding] = []
    diagnostics: list[str] = []
    total_results = 0
    for run in runs:
        if not isinstance(run, dict):
            diagnostics.append("invalid SARIF run skipped")
            continue
        tool = run.get("tool")
        driver = tool.get("driver") if isinstance(tool, dict) else None
        if not isinstance(driver, dict):
            diagnostics.append("SARIF run missing tool driver")
            continue
        tool_name = driver.get("name")
        tool_version = driver.get("version", "unknown")
        if not isinstance(tool_name, str) or not _SAFE_TOOL.fullmatch(tool_name):
            diagnostics.append("SARIF tool name is invalid")
            continue
        if not isinstance(tool_version, str) or not _SAFE_VERSION.fullmatch(tool_version):
            diagnostics.append("SARIF tool version is invalid")
            continue
        rules = driver.get("rules", [])
        if not isinstance(rules, list) or len(rules) > MAX_RESULTS:
            diagnostics.append("SARIF rule table is invalid")
            continue
        rule_ids = [rule.get("id") if isinstance(rule, dict) else None for rule in rules]
        results = run.get("results", [])
        if not isinstance(results, list):
            diagnostics.append("SARIF results must be an array")
            continue
        total_results += len(results)
        if total_results > MAX_RESULTS:
            return IacFindings((), ("SARIF result count exceeds limit",))
        for result in results:
            if not isinstance(result, dict):
                diagnostics.append("invalid SARIF result skipped")
                continue
            rule_id = result.get("ruleId")
            if rule_id is None:
                index = result.get("ruleIndex")
                if (
                    isinstance(index, int)
                    and not isinstance(index, bool)
                    and 0 <= index < len(rule_ids)
                ):
                    rule_id = rule_ids[index]
            if not isinstance(rule_id, str) or not _SAFE_ID.fullmatch(rule_id):
                diagnostics.append("SARIF result has invalid rule identity")
                continue
            severity = result.get("level", "warning")
            if not isinstance(severity, str) or severity not in _LEVELS:
                diagnostics.append("SARIF result has invalid severity")
                continue
            locations = result.get("locations", [])
            if not isinstance(locations, list):
                diagnostics.append("SARIF result locations are invalid")
                continue
            for location in locations:
                parsed = _location(location)
                if parsed is None:
                    diagnostics.append("SARIF source location is unsupported")
                    continue
                path, line = parsed
                if len(findings) >= MAX_RESULTS:
                    return IacFindings(
                        tuple(findings),
                        tuple(dict.fromkeys((*diagnostics, "SARIF finding count exceeds limit"))),
                    )
                findings.append(IacFinding(rule_id, severity, path, line, tool_name, tool_version))
    return IacFindings(tuple(findings), tuple(dict.fromkeys(diagnostics)))


def _location(location) -> tuple[str, int] | None:
    if not isinstance(location, dict):
        return None
    physical = location.get("physicalLocation")
    if not isinstance(physical, dict):
        return None
    artifact = physical.get("artifactLocation")
    region = physical.get("region")
    if not isinstance(artifact, dict) or not isinstance(region, dict):
        return None
    uri = artifact.get("uri")
    line = region.get("startLine")
    if not isinstance(uri, str) or not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    if line > 2**31 - 1 or artifact.get("uriBaseId") is not None:
        return None
    if uri.startswith(("/", "\\")) or "\\" in uri or _SCHEME.match(uri):
        return None
    if any(char in uri for char in ("%", "?", "#", "\x00")):
        return None
    pieces = uri.split("/")
    if any(piece in ("..", "") for piece in pieces):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9_.@+-]+", piece) for piece in pieces):
        return None
    normalized = PurePosixPath(uri)
    if normalized.is_absolute() or normalized.as_posix() != uri:
        return None
    return uri, line


def _bounded(root) -> bool:
    count = 0
    stack = [(root, 0)]
    while stack:
        value, depth = stack.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_DEPTH:
            return False
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for pair in value.items() for child in pair)
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return True
