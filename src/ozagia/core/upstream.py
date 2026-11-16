"""Consent-based, redacted local field-improvement bundles.

This module prepares a diagnostic bundle from evidence a person explicitly
names. It is a local export, not a reporting service:

- a **preview** reads only the named evidence and writes nothing;
- a **bundle** is written only when the caller passes explicit consent, and it
  is written only to the destination the caller names;
- nothing is uploaded, sent, or turned into an issue or pull request;
- no model is called and no network connection is opened.

Evidence is limited to five explicit shapes: a ``log_inspect`` report, a
code-review packet, a bounded task run, a repo-scan report and a status
snapshot. Raw logs, prompts, file contents, diffs, argv and environment
variables are never copied. Summaries pass through
:class:`ozagia.core.log_inspect.Redactor`, which replaces secrets, emails,
credential-bearing URLs, prompt/file-content fields and home-directory
prefixes with counted placeholders.

A bundle is a directory or a zip archive:

```text
manifest.json    deterministic metadata, hashes and the consent record
README.md        what was included, what was excluded and how to review it
issue.md         a Markdown issue draft; nothing posts it
evidence/        one redacted JSON summary per included input
```
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ozagia.core.log_inspect import Redactor

TOOL_NAME = "upstream"
SCHEMA = 1
PREVIEW_SCHEMA = "ozagia.upstream.preview.v1"
BUNDLE_SCHEMA = "ozagia.upstream.bundle.v1"
EVIDENCE_SCHEMA = "ozagia.upstream.evidence.v1"
ISSUE_SCHEMA = "ozagia.upstream.issue.v1"

MAX_EVIDENCE = 16
MAX_EVIDENCE_FILE_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
BUNDLE_OVERHEAD_BYTES = 256 * 1024
MAX_FINDINGS = 20
MAX_FINDINGS_PER_EVIDENCE = 10
MAX_CONFIG_CHANGES = 100
MAX_TITLE = 200
MAX_SUMMARY = 4000
MAX_TEXT = 240
MAX_MESSAGE = 400
MAX_LIST = 20
MAX_SECTION_ITEMS = 10

MANIFEST_FILE = "manifest.json"
README_FILE = "README.md"
ISSUE_FILE = "issue.md"
EVIDENCE_DIR = "evidence"

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_SAFE_RANGE = re.compile(r"[A-Za-z0-9_./~^-]{1,160}\Z")

EXCLUDED_CATEGORIES = (
    "raw log lines and unclustered records",
    "prompts, messages and conversational payloads",
    "file contents, diffs, patches, transcripts, stdout, stderr and output "
    "bodies",
    "secrets, API keys, tokens, passwords, authorization headers and "
    "credential-bearing URLs",
    "email addresses",
    "home-directory prefixes",
    "raw process argv and environment variables",
    "unrecognised evidence files and documents",
)

NOT_ASSESSED = (
    "No network call was made; nothing was uploaded and no issue or pull "
    "request was created.",
    "Only the explicitly named evidence paths were read; no directory, home "
    "directory or state-directory scan was performed.",
    "Evidence was summarised and redacted; raw logs, prompts, file contents "
    "and raw argv are not part of the bundle.",
    "Redaction is practical, not a proof of anonymity. Paths, model names, "
    "tool names and error classes may still identify a system.",
    "Findings are observed clusters, not root-cause verdicts, and "
    "truth_assessed is always false.",
    "A person must review the bundle and open an issue manually.",
)


class UpstreamError(ValueError):
    """The preview or export request is invalid or cannot be completed."""


class ConsentRequired(UpstreamError):
    """A bundle write was requested without explicit consent."""


class BundleError(UpstreamError):
    """The requested destination or bundle content is unsafe."""


@dataclass(frozen=True)
class _Evidence:
    kind: str
    source: str
    source_bytes: int
    source_sha256: str | None
    content: dict[str, Any]
    findings: tuple[dict[str, Any], ...]
    signatures: tuple[str, ...]
    task_classes: tuple[str, ...]
    reproduction: tuple[dict[str, Any], ...]
    excluded_fields: tuple[str, ...]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    return ()


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _home_text() -> str:
    try:
        home = str(Path.home())
    except (KeyError, OSError, RuntimeError):
        return ""
    return home if home not in ("", os.sep) else ""


def _home_display(path: Path) -> str:
    text = str(path)
    home = _home_text()
    if home and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def _path_display(redactor: Redactor, root: Path, path: Path) -> str:
    resolved = path if path.is_absolute() else (root / path)
    try:
        relative = resolved.resolve().relative_to(root)
    except (OSError, ValueError):
        text = _home_display(resolved)
    else:
        text = relative.as_posix() or "."
    return redactor.text(text, limit=MAX_TEXT)


def _redact(redactor: Redactor, value: Any, *,
            limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    return redactor.text(value, limit=limit)


def _redact_optional(redactor: Redactor, value: Any, *,
                     limit: int = MAX_TEXT) -> str | None:
    text = _redact(redactor, value, limit=limit)
    return text or None


def _redact_list(redactor: Redactor, value: Any, *,
                 max_items: int = MAX_SECTION_ITEMS,
                 limit: int = MAX_MESSAGE) -> list[str]:
    result: list[str] = []
    for item in _sequence(value)[:max_items]:
        text = _redact(redactor, item, limit=limit)
        if text:
            result.append(text)
    return result


def _counts(value: Any, *, max_items: int = 20) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key in sorted(value)[:max_items]:
        count = _integer(value[key])
        if count is not None and count > 0:
            result[str(key)] = count
    return result


def _dimensions(redactor: Redactor, value: Any) -> dict[str, str | None]:
    source = _mapping(value)
    result: dict[str, str | None] = {}
    for key in ("tool", "intent", "harness", "model", "task_class"):
        result[key] = _redact_optional(redactor, source.get(key), limit=80)
    return result


def _safe_token(value: Any, fallback: str) -> str:
    text = value if isinstance(value, str) else ""
    return text if _SAFE_TOKEN.fullmatch(text) else fallback


def _safe_range(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if _SAFE_RANGE.fullmatch(text) and " " not in text:
        return text
    return "<base>..<head>"


def _read_bounded(path: Path, limit: int = MAX_EVIDENCE_FILE_BYTES) -> bytes:
    try:
        with path.open("rb") as handle:
            head = handle.read(2)
            handle.seek(0)
            if head == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=handle) as stream:
                    data = stream.read(limit + 1)
            else:
                data = handle.read(limit + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise UpstreamError(
            f"cannot read evidence {_home_display(path)}: {exc}") from exc
    if len(data) > limit:
        raise UpstreamError(
            f"evidence exceeds {limit} bytes: {_home_display(path)}")
    return data


# --------------------------------------------------------------------------- #
# environment and config
# --------------------------------------------------------------------------- #
def _environment(redactor: Redactor) -> dict[str, Any]:
    version: str | None = None
    try:
        from importlib import metadata

        version = metadata.version("ozagia")
    except Exception:  # noqa: BLE001 - a missing package is not an error
        version = None
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "system": platform.system(),
        "release": _redact_optional(redactor, platform.release(), limit=80),
        "machine": platform.machine() or None,
        "ozagia_version": version,
        "included": [
            "python version and implementation",
            "operating-system name, release and machine type",
            "ozagia package version, when installed",
        ],
        "excluded": [
            "hostname",
            "username",
            "working directory",
            "environment variables",
            "raw command lines",
        ],
    }


def _json_scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False,
                          default=str, allow_nan=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text


def _config_diff(root: Path, redactor: Redactor) -> dict[str, Any]:
    try:
        from ozagia.core import settings as settings_registry

        values = settings_registry.values(root)
        locations = settings_registry.locations(root)
    except Exception as exc:  # noqa: BLE001 - config is one optional section
        return {
            "included": True,
            "state": "unavailable",
            "error": _redact(redactor, exc, limit=300),
            "changed": [],
            "changed_count": 0,
            "omitted": 0,
            "repo_config": None,
            "user_config": None,
            "excluded_fields": [
                "raw configuration text",
                "values equal to built-in defaults",
            ],
        }

    changed: list[tuple[str, Mapping[str, Any]]] = []
    for key in sorted(values):
        record = values[key]
        if not isinstance(record, Mapping):
            continue
        if record.get("source") == "default":
            continue
        changed.append((key, record))

    entries: list[dict[str, Any]] = []
    for key, record in changed[:MAX_CONFIG_CHANGES]:
        entries.append({
            "key": _redact(redactor, key, limit=120),
            "value": _redact(redactor, _json_scalar(record.get("value")),
                             limit=MAX_MESSAGE) or "null",
            "default": _redact(redactor, _json_scalar(record.get("default")),
                               limit=MAX_MESSAGE) or "null",
            "source": _redact(redactor, record.get("source"), limit=40)
            or "unknown",
        })

    repo_text = locations.get("repo_config")
    user_text = locations.get("user_config")
    repo = Path(str(repo_text)) if repo_text else None
    user = Path(str(user_text)) if user_text else None
    return {
        "included": True,
        "state": "changed" if entries else "defaults",
        "repo_config": {
            "path": (_path_display(redactor, root, repo) if repo else None),
            "present": bool(locations.get("repo_config_exists")),
        },
        "user_config": {
            "path": (_redact(redactor, _home_display(user), limit=MAX_TEXT)
                     if user else None),
            "present": bool(locations.get("user_config_exists")),
        },
        "changed": entries,
        "changed_count": len(changed),
        "omitted": max(0, len(changed) - len(entries)),
        "excluded_fields": [
            "raw configuration text",
            "values equal to built-in defaults",
            "user-specific home prefixes (replaced with ~)",
        ],
    }


# --------------------------------------------------------------------------- #
# evidence detection and summaries
# --------------------------------------------------------------------------- #
def _detect(doc: Mapping[str, Any]) -> tuple[str | None, Mapping[str, Any]]:
    if doc.get("tool") == "log_inspect" and isinstance(
            doc.get("findings"), list):
        return "log_report", doc
    if doc.get("schema") == "ozagia.code_review.packet.v1":
        return "code_review", doc
    if (isinstance(doc.get("opportunities"), list)
            and isinstance(doc.get("not_assessed"), list)):
        return "repo_scan", doc
    output = doc.get("output")
    if isinstance(output, Mapping):
        if (isinstance(output.get("opportunities"), list)
                and isinstance(output.get("not_assessed"), list)):
            return "repo_scan", output
        if output.get("output") == "repo_scan.v1":
            return "repo_scan", output
    if all(key in doc for key in (
            "workspace", "resources", "files", "runtime", "daemon",
            "doctor", "config")):
        return "status", doc
    if (isinstance(doc.get("run_id"), str)
            and isinstance(doc.get("spec"), Mapping)
            and isinstance(doc.get("status"), str)):
        return "task_run", doc
    if (doc.get("output") == "repo_scan.v1"
            or doc.get("schema") == "ozagia.repo_scan.report.v1"):
        return "repo_scan", doc
    return None, doc


def _test_plan_summary(redactor: Redactor, value: Any) -> dict[str, Any] | None:
    plan = _mapping(value)
    if not plan:
        return None
    selected = [
        _redact(redactor, item, limit=MAX_TEXT)
        for item in _sequence(plan.get("selected_files"))[:MAX_LIST]
    ]
    return {
        "available": _boolean(plan.get("available")),
        "mode": _redact_optional(redactor, plan.get("mode"), limit=40),
        "executed": False,
        "selected_files": [item for item in selected if item],
        "selected_count": len(_sequence(plan.get("selected_files"))),
        "fallback": _redact_optional(redactor, plan.get("fallback"), limit=40),
        "fallback_used": _boolean(plan.get("fallback_used")),
        "fallback_reason": _redact_optional(
            redactor, plan.get("fallback_reason"), limit=MAX_MESSAGE),
        "not_run_reason": _redact_optional(
            redactor, plan.get("not_run_reason"), limit=MAX_MESSAGE),
        "truth_assessed": False,
    }


def _log_summary(root: Path, doc: Mapping[str, Any],
                 redactor: Redactor) -> _Evidence:
    raw_findings = [
        item for item in _sequence(doc.get("findings"))
        if isinstance(item, Mapping)
    ]
    selected = raw_findings[:MAX_FINDINGS_PER_EVIDENCE]
    findings: list[dict[str, Any]] = []
    top_findings: list[dict[str, Any]] = []
    signatures: list[str] = []
    task_classes: set[str] = set()
    for item in selected:
        error_type = _redact(redactor, item.get("error_type"), limit=120)
        signature = _redact(redactor, item.get("signature"),
                            limit=MAX_MESSAGE)
        dimensions = _dimensions(redactor, item.get("dimensions"))
        if dimensions.get("task_class"):
            task_classes.add(dimensions["task_class"] or "")
        action = _mapping(item.get("proposed_action"))
        finding = {
            "id": _redact(redactor, item.get("id"), limit=40) or None,
            "severity": _redact(redactor, item.get("severity"), limit=20)
            or "info",
            "count": _integer(item.get("count")) or 0,
            "error_type": error_type or "Unknown",
            "signature": signature or "(no message)",
            "likely_component": _redact(
                redactor, item.get("likely_component"), limit=80) or None,
            "dimensions": dimensions,
            "source_kinds": _redact_list(
                redactor, item.get("source_kinds")),
            "proposed_action": {
                "action": _redact_optional(redactor, action.get("action"),
                                           limit=40),
                "summary": _redact_optional(redactor, action.get("summary"),
                                            limit=MAX_MESSAGE),
                "confidence": _redact_optional(
                    redactor, action.get("confidence"), limit=20),
                "network": False,
                "requires_approval": True,
            },
        }
        findings.append(finding)
        summary_text = ": ".join(
            part for part in (error_type, signature) if part) or "(no message)"
        top_findings.append({
            "kind": "log_report",
            "severity": finding["severity"],
            "summary": summary_text,
            "location": finding["likely_component"],
            "count": finding["count"],
        })
        signatures.append(summary_text)

    inputs: list[dict[str, Any]] = []
    for item in _sequence(doc.get("inputs"))[:MAX_LIST]:
        entry = _mapping(item)
        path_text = _redact(redactor, entry.get("path"), limit=MAX_TEXT)
        inputs.append({
            "name": _redact(redactor, entry.get("name"), limit=40) or None,
            "path": path_text or None,
            "kind": _redact(redactor, entry.get("kind"), limit=20) or None,
            "bytes_read": _integer(entry.get("bytes_read")),
            "lines_read": _integer(entry.get("lines_read")),
            "records": _integer(entry.get("records")),
            "truncated_bytes": _boolean(entry.get("truncated_bytes")),
            "truncated_lines": _boolean(entry.get("truncated_lines")),
            "redactions": _counts(entry.get("redactions")),
        })

    report_summary = _mapping(doc.get("summary"))
    limits = _mapping(doc.get("limits"))
    content = {
        "kind": "log_report",
        "tool": "log_inspect",
        "schema": _integer(doc.get("schema")),
        "summary": {
            "inputs": _integer(report_summary.get("inputs")),
            "inputs_read": _integer(report_summary.get("inputs_read")),
            "inputs_unreadable": _integer(
                report_summary.get("inputs_unreadable")),
            "records": _integer(report_summary.get("records")),
            "failures": _integer(report_summary.get("failures")),
            "clusters": _integer(report_summary.get("clusters")),
            "findings_shown": _integer(
                report_summary.get("findings_shown")),
            "malformed_lines": _integer(
                report_summary.get("malformed_lines")),
            "truncated_inputs": _integer(
                report_summary.get("truncated_inputs")),
        },
        "limits": {
            key: _integer(limits.get(key))
            for key in ("max_files", "max_bytes", "max_lines", "findings")
        },
        "redactions": _counts(doc.get("redactions")),
        "inputs": inputs,
        "findings": findings,
        "findings_total": len(raw_findings),
        "findings_shown": len(findings),
        "findings_truncated": len(raw_findings) > len(findings),
        "errors": _redact_list(redactor, doc.get("errors")),
        "not_assessed": _redact_list(redactor, doc.get("not_assessed")),
        "local_only": True,
        "truth_assessed": False,
        "excluded_fields": [
            "raw log lines and records",
            "conversation and prompt payloads",
            "unbounded finder evidence counts",
            "source paths outside the repository (home-redacted)",
        ],
    }
    reproduction = ({
        "command": "python -m ozagia.log_inspect_cli --log <log-path> "
                   "--json",
        "description": "re-run the redacted log inspection on the same "
                       "explicit inputs",
        "executed": False,
    },)
    return _Evidence(
        kind="log_report",
        source="",
        source_bytes=0,
        source_sha256=None,
        content=content,
        findings=tuple(top_findings),
        signatures=tuple(signatures),
        task_classes=tuple(sorted(task_classes)),
        reproduction=reproduction,
        excluded_fields=tuple(content["excluded_fields"]),
    )


def _review_summary(root: Path, doc: Mapping[str, Any],
                    redactor: Redactor) -> _Evidence:
    raw_findings = [
        item for item in _sequence(doc.get("findings"))
        if isinstance(item, Mapping)
    ]
    selected = raw_findings[:MAX_FINDINGS_PER_EVIDENCE]
    findings: list[dict[str, Any]] = []
    top_findings: list[dict[str, Any]] = []
    signatures: list[str] = []
    for item in selected:
        message = _redact(redactor, item.get("message"), limit=MAX_MESSAGE)
        severity = _redact(redactor, item.get("severity"), limit=20) or "note"
        file_text = _redact_optional(redactor, item.get("file"), limit=MAX_TEXT)
        line = _integer(item.get("line"))
        location = file_text or ""
        if line is not None:
            location = f"{location}:{line}" if location else f"line {line}"
        finding = {
            "id": _redact(redactor, item.get("id"), limit=40) or None,
            "type": _redact(redactor, item.get("type"), limit=40) or "unknown",
            "severity": severity,
            "file": file_text,
            "line": line,
            "message": message,
            "evidence": _redact_list(redactor, item.get("evidence")),
            "graph_keys": _redact_list(redactor, item.get("graph_keys")),
            "confidence": _redact(redactor, item.get("confidence"), limit=20)
            or "unknown",
            "confidence_note": _redact_optional(
                redactor, item.get("confidence_note"), limit=MAX_MESSAGE),
        }
        findings.append(finding)
        summary_text = message or finding["type"]
        top_findings.append({
            "kind": "code_review",
            "severity": severity,
            "summary": f"{finding['type']}: {summary_text}",
            "location": location or None,
            "count": 1,
        })
        if severity in ("blocking", "major"):
            signatures.append(f"{finding['type']}: {summary_text}")

    checks: list[dict[str, Any]] = []
    for item in _sequence(_mapping(doc.get("preflight")).get("checks"))[
            :MAX_SECTION_ITEMS]:
        entry = _mapping(item)
        checks.append({
            "id": _redact(redactor, entry.get("id"), limit=60) or None,
            "level": _redact(redactor, entry.get("level"), limit=20) or None,
            "message": _redact(redactor, entry.get("message"),
                               limit=MAX_MESSAGE) or None,
        })

    range_info = _mapping(doc.get("range"))
    worktree = _mapping(doc.get("worktree"))
    expected = _mapping(worktree.get("expected"))
    graph = _mapping(doc.get("graph"))
    diff = _mapping(doc.get("diff"))
    summary = _mapping(doc.get("summary"))
    content = {
        "kind": "code_review",
        "schema": _redact(redactor, doc.get("schema"), limit=80) or None,
        "range": {
            "spec": _redact(redactor, range_info.get("spec"),
                            limit=160) or None,
            "base": _redact(redactor, range_info.get("base"),
                            limit=120) or None,
            "head": _redact(redactor, range_info.get("head"),
                            limit=120) or None,
            "base_commit": _redact(redactor, range_info.get("base_commit"),
                                   limit=80) or None,
            "head_commit": _redact(redactor, range_info.get("head_commit"),
                                   limit=80) or None,
        },
        "worktree": {
            "branch": _redact_optional(redactor, worktree.get("branch"),
                                       limit=120),
            "head": _redact_optional(redactor, worktree.get("head"),
                                     limit=80),
            "detached": _boolean(worktree.get("detached")),
            "clean": _boolean(worktree.get("clean")),
            "operation": _redact_optional(redactor, worktree.get("operation"),
                                          limit=40),
            "dirty": {
                key: _integer(_mapping(worktree.get("dirty")).get(key))
                for key in ("staged", "unstaged", "untracked", "conflicted")
            },
            "expected_branch": _redact_optional(
                redactor, expected.get("branch"), limit=120),
        },
        "preflight": {
            "ok": _boolean(_mapping(doc.get("preflight")).get("ok")),
            "checks": checks,
        },
        "graph": {
            "freshness": _redact_optional(redactor, graph.get("freshness"),
                                          limit=40),
            "usable": _boolean(graph.get("usable")),
            "reason": _redact_optional(redactor, graph.get("reason"),
                                       limit=MAX_MESSAGE),
        },
        "diff": {
            key: _integer(diff.get(key))
            for key in ("files", "added", "modified", "deleted", "renamed",
                        "binary", "large", "generated", "additions",
                        "deletions")
        },
        "summary": {
            key: _integer(summary.get(key))
            for key in ("findings", "blocking", "major", "minor", "note")
        },
        "findings": findings,
        "findings_total": len(raw_findings),
        "findings_shown": len(findings),
        "findings_truncated": len(raw_findings) > len(findings),
        "omissions": _redact_list(redactor, doc.get("omissions")),
        "not_assessed": _redact_list(redactor, doc.get("not_assessed")),
        "test_plan": _test_plan_summary(redactor, doc.get("test_plan")),
        "tests_executed": False,
        "model_review": {
            "status": _redact_optional(
                redactor, _mapping(doc.get("model_review")).get("status"),
                limit=40),
            "truth_assessed": False,
        },
        "truth_assessed": False,
        "excluded_fields": [
            "file contents, patches and diffs",
            "prompt and model message payloads",
            "absolute worktree and artifact paths (home-redacted)",
            "full graph snapshot and file list",
        ],
    }
    review_range = _safe_range(content["range"].get("spec"))
    reproduction = ({
        "command": "python -m ozagia.code_review_cli --root . --range "
                   f"{review_range} --json",
        "description": "re-run the deterministic review for the same range",
        "executed": False,
    },)
    return _Evidence(
        kind="code_review",
        source="",
        source_bytes=0,
        source_sha256=None,
        content=content,
        findings=tuple(top_findings),
        signatures=tuple(signatures),
        task_classes=(),
        reproduction=reproduction,
        excluded_fields=tuple(content["excluded_fields"]),
    )


def _repo_scan_summary(root: Path, doc: Mapping[str, Any],
                       redactor: Redactor) -> _Evidence:
    raw = [item for item in _sequence(doc.get("opportunities"))
           if isinstance(item, Mapping)]
    selected = raw[:MAX_FINDINGS_PER_EVIDENCE]
    opportunities: list[dict[str, Any]] = []
    top_findings: list[dict[str, Any]] = []
    for item in selected:
        title = _redact(redactor, item.get("title"), limit=MAX_TEXT)
        kind = _redact(redactor, item.get("kind"), limit=40) or "other"
        opportunity = {
            "title": title,
            "kind": kind,
            "expected_effect": _redact_optional(
                redactor, item.get("expected_effect"), limit=MAX_MESSAGE),
            "effort": _redact_optional(redactor, item.get("effort"),
                                       limit=20),
            "risk": _redact_optional(redactor, item.get("risk"), limit=20),
            "evidence": _redact_optional(redactor, item.get("evidence"),
                                         limit=MAX_MESSAGE),
        }
        opportunities.append(opportunity)
        top_findings.append({
            "kind": "repo_scan",
            "severity": "info",
            "summary": f"{kind}: {title or '(untitled)'}",
            "location": "effort=%s risk=%s" % (
                opportunity["effort"] or "?",
                opportunity["risk"] or "?"),
            "count": 1,
        })
    content = {
        "kind": "repo_scan",
        "schema": _redact(redactor, doc.get("schema"), limit=80) or None,
        "output": _redact(redactor, doc.get("output"), limit=80) or None,
        "opportunities": opportunities,
        "opportunity_count": len(raw),
        "opportunities_shown": len(opportunities),
        "opportunities_truncated": len(raw) > len(opportunities),
        "not_assessed": _redact_list(redactor, doc.get("not_assessed")),
        "truth_assessed": False,
        "excluded_fields": [
            "file contents and raw excerpts",
            "unselected opportunities beyond the display cap",
            "absolute paths (home-redacted)",
        ],
    }
    return _Evidence(
        kind="repo_scan",
        source="",
        source_bytes=0,
        source_sha256=None,
        content=content,
        findings=tuple(top_findings),
        signatures=(),
        task_classes=(),
        reproduction=(),
        excluded_fields=tuple(content["excluded_fields"]),
    )


def _status_summary(root: Path, doc: Mapping[str, Any],
                    redactor: Redactor) -> _Evidence:
    workspace = _mapping(doc.get("workspace"))
    workspace_report = _mapping(workspace.get("report"))
    worktrees: list[dict[str, Any]] = []
    for item in _sequence(workspace_report.get("worktrees"))[:MAX_SECTION_ITEMS]:
        entry = _mapping(item)
        expected = _mapping(entry.get("expected"))
        dirty = _mapping(entry.get("dirty"))
        worktrees.append({
            "path": _path_display(
                redactor, root, Path(str(entry.get("path") or "."))),
            "branch": _redact_optional(redactor, entry.get("branch"),
                                       limit=120),
            "head": _redact_optional(redactor, entry.get("head"), limit=80),
            "detached": _boolean(entry.get("detached")),
            "expected_branch": _redact_optional(
                redactor, expected.get("branch"), limit=120),
            "dirty": {
                key: _integer(dirty.get(key))
                for key in ("staged", "unstaged", "untracked", "conflicted")
            },
            "operation": _redact_optional(redactor, entry.get("operation"),
                                          limit=40),
        })
    workspace_findings: list[dict[str, Any]] = []
    top_findings: list[dict[str, Any]] = []
    for item in _sequence(workspace_report.get("findings"))[
            :MAX_SECTION_ITEMS]:
        entry = _mapping(item)
        message = _redact(redactor, entry.get("message"), limit=MAX_MESSAGE)
        level = _redact(redactor, entry.get("level"), limit=20) or "info"
        workspace_findings.append({
            "id": _redact_optional(redactor, entry.get("id"), limit=60),
            "level": level,
            "message": message or None,
        })
        if level in ("error", "warn", "warning"):
            top_findings.append({
                "kind": "status",
                "severity": "error" if level == "error" else "warning",
                "summary": message or (entry.get("id") or "workspace finding"),
                "location": "workspace",
                "count": 1,
            })

    resources = _mapping(doc.get("resources"))
    resources_report = _mapping(resources.get("report"))
    host = _mapping(resources_report.get("host"))
    memory = _mapping(host.get("memory"))
    load = _mapping(host.get("load"))
    disks: list[dict[str, Any]] = []
    for item in _sequence(resources_report.get("disks"))[:5]:
        entry = _mapping(item)
        path_text = entry.get("mount") or entry.get("path")
        disks.append({
            "path": _path_display(redactor, root, Path(str(path_text or "."))),
            "fstype": _redact_optional(redactor, entry.get("fstype"),
                                       limit=40),
            "free_gb": _number(entry.get("free_gb")),
            "free_pct": _number(entry.get("free_pct")),
            "is_tmpfs": _boolean(entry.get("is_tmpfs")),
        })

    files = _mapping(doc.get("files"))
    files_report = _mapping(files.get("report"))
    file_counts = _mapping(files_report.get("counts"))
    runtime = _mapping(doc.get("runtime"))
    runtime_report = _mapping(runtime.get("report"))
    daemon = _mapping(doc.get("daemon"))
    daemon_report = _mapping(daemon.get("report"))
    doctor = _mapping(doc.get("doctor"))
    doctor_report = _mapping(doctor.get("report"))
    snapshot = _mapping(doctor_report.get("snapshot"))
    config = _mapping(doc.get("config"))

    errors: list[dict[str, Any]] = []
    for item in _sequence(doc.get("errors"))[:MAX_SECTION_ITEMS]:
        entry = _mapping(item)
        errors.append({
            "component": _redact_optional(redactor, entry.get("component"),
                                          limit=40),
            "code": _redact_optional(redactor, entry.get("code"), limit=80),
            "message": _redact_optional(redactor, entry.get("message"),
                                        limit=MAX_MESSAGE),
        })
    degraded: list[dict[str, Any]] = []
    for item in _sequence(doc.get("degraded"))[:MAX_SECTION_ITEMS]:
        entry = _mapping(item)
        degraded.append({
            "component": _redact_optional(redactor, entry.get("component"),
                                          limit=40),
            "code": _redact_optional(redactor, entry.get("code"), limit=80),
            "message": _redact_optional(redactor, entry.get("message"),
                                        limit=MAX_MESSAGE),
        })
        message = _redact(redactor, entry.get("message"), limit=MAX_MESSAGE)
        if message:
            top_findings.append({
                "kind": "status",
                "severity": "warning",
                "summary": message,
                "location": _redact(redactor, entry.get("component"),
                                    limit=40) or "status",
                "count": 1,
            })

    content = {
        "kind": "status",
        "checked_at": _redact_optional(redactor, doc.get("checked_at"),
                                       limit=60),
        "ok": _boolean(doc.get("ok")),
        "workspace": {
            "state": _redact_optional(redactor, workspace.get("state"),
                                      limit=40),
            "worktrees": worktrees,
            "findings": workspace_findings,
        },
        "resources": {
            "state": _redact_optional(redactor, resources.get("state"),
                                      limit=40),
            "host": {
                "platform": _redact_optional(redactor, host.get("platform"),
                                             limit=80),
                "cpu_count": _integer(host.get("cpu_count")),
                "memory": {
                    key: _number(memory.get(key))
                    for key in ("total_mb", "available_mb", "used_pct")
                },
                "load": {
                    key: _number(load.get(key))
                    for key in ("1m", "5m", "15m")
                },
            },
            "disks": disks,
        },
        "files": {
            "state": _redact_optional(redactor, files.get("state"), limit=40),
            "counts": {
                key: _integer(file_counts.get(key))
                for key in ("records", "orphans", "new")
            },
        },
        "runtime": {
            "state": _redact_optional(redactor, runtime.get("state"),
                                      limit=40),
            "services": len(_sequence(runtime_report.get("services"))),
            "tunnels": len(_sequence(runtime_report.get("tunnels"))),
            "processes": len(_sequence(runtime_report.get("processes"))),
        },
        "daemon": {
            "state": _redact_optional(redactor, daemon.get("state"),
                                      limit=40),
            "version": _redact_optional(redactor, daemon_report.get("version"),
                                        limit=40),
            "uptime_s": _number(daemon_report.get("uptime_s")),
            "stats_error": _redact_optional(
                redactor, daemon_report.get("stats_error"), limit=MAX_MESSAGE),
        },
        "doctor": {
            "state": _redact_optional(redactor, doctor.get("state"), limit=40),
            "snapshot": {
                "state": _redact_optional(redactor, snapshot.get("state"),
                                          limit=40),
                "path": _path_display(
                    redactor,
                    root,
                    Path(str(snapshot.get("path") or ".")),
                ),
            },
        },
        "config": {
            "state": _redact_optional(redactor, config.get("state"), limit=40),
            "present": _boolean(config.get("present")),
        },
        "errors": errors,
        "degraded": degraded,
        "truth_assessed": False,
        "excluded_fields": [
            "full component reports",
            "process command lines and argv",
            "environment variables",
            "file ledger records and reasons",
            "runtime service and tunnel definitions",
        ],
    }
    reproduction = ({
        "command": "python -m ozagia.status_cli --root . --json",
        "description": "recompose the read-only status snapshot",
        "executed": False,
    },)
    return _Evidence(
        kind="status",
        source="",
        source_bytes=0,
        source_sha256=None,
        content=content,
        findings=tuple(top_findings),
        signatures=(),
        task_classes=(),
        reproduction=reproduction,
        excluded_fields=tuple(content["excluded_fields"]),
    )


def _task_run_summary(root: Path, run_dir: Path, manifest: Any,
                      manifest_bytes: bytes | None, error: str | None,
                      redactor: Redactor) -> _Evidence:
    doc = _mapping(manifest)
    spec = _mapping(doc.get("spec"))
    preflight = _mapping(doc.get("preflight"))
    worktree = _mapping(preflight.get("worktree"))
    expected = _mapping(worktree.get("expected"))
    snapshot = _mapping(preflight.get("snapshot"))
    resources = _mapping(preflight.get("resources"))
    gates = _mapping(doc.get("gates"))
    budget = _mapping(doc.get("budget"))
    declared = _mapping(budget.get("declared"))
    used = _mapping(budget.get("used"))
    output = _mapping(doc.get("output"))
    status = _redact(redactor, doc.get("status"), limit=40) or "unknown"
    stop = _redact_optional(redactor, doc.get("stop"), limit=60)
    stop_detail = _redact_optional(redactor, doc.get("stop_detail"),
                                   limit=MAX_MESSAGE)
    run_id = _redact(redactor, doc.get("run_id"), limit=120) or run_dir.name

    gate_reports: dict[str, list[dict[str, Any]]] = {
        "before": [], "after": []}
    for phase in ("before", "after"):
        for item in _sequence(gates.get(phase))[:MAX_LIST]:
            entry = _mapping(item)
            gate_reports[phase].append({
                "id": _redact(redactor, entry.get("id"), limit=60) or None,
                "phase": _redact(redactor, entry.get("phase"), limit=20)
                or phase,
                "level": _redact(redactor, entry.get("level"), limit=20)
                or None,
                "ok": _boolean(entry.get("ok")),
                "message": _redact(redactor, entry.get("message"),
                                   limit=MAX_MESSAGE) or None,
            })

    steps: list[dict[str, Any]] = []
    for item in _sequence(doc.get("steps"))[:MAX_LIST]:
        entry = _mapping(item)
        usage = _mapping(entry.get("usage"))
        steps.append({
            "id": _redact(redactor, entry.get("id"), limit=60) or None,
            "index": _integer(entry.get("index")),
            "tool": _redact(redactor, entry.get("tool"), limit=40) or None,
            "status": _redact(redactor, entry.get("status"), limit=20)
            or None,
            "summary": _redact(redactor, entry.get("summary"),
                               limit=MAX_MESSAGE) or None,
            "elapsed_s": _number(entry.get("elapsed_s")),
            "usage": {
                key: _number(usage.get(key))
                for key in ("tokens", "model_calls", "cost_usd", "wall_s",
                            "memory_mb", "disk_mb", "pids")
            },
            "artifacts": _integer(entry.get("artifacts")),
            "evidence": _integer(entry.get("evidence")),
        })

    artifacts: list[dict[str, Any]] = []
    for item in _sequence(doc.get("artifacts"))[:MAX_LIST]:
        entry = _mapping(item)
        artifacts.append({
            "path": _redact(redactor, entry.get("path"),
                            limit=MAX_TEXT) or None,
            "sha256": _redact(redactor, entry.get("sha256"), limit=80)
            or None,
            "size": _integer(entry.get("size")),
            "kind": _redact(redactor, entry.get("kind"), limit=40) or None,
            "step": _redact(redactor, entry.get("step"), limit=60) or None,
        })

    inputs = _mapping(doc.get("inputs"))
    input_names = _redact_list(redactor, inputs.get("names"))
    input_digests = {
        _redact(redactor, key, limit=60): _redact(redactor, value, limit=80)
        for key, value in _mapping(inputs.get("digests")).items()
    }

    content = {
        "kind": "task_run",
        "state": (
            "complete" if doc
            else ("incomplete" if error == f"{MANIFEST_FILE} is missing"
                  else "invalid")
        ),
        "error": _redact(redactor, error, limit=300) or None,
        "run_id": run_id,
        "spec": {
            "id": _redact(redactor, spec.get("id"), limit=64) or None,
            "version": _integer(spec.get("version")),
            "title": _redact_optional(redactor, spec.get("title"),
                                      limit=MAX_TITLE),
            "digest": _redact_optional(redactor, spec.get("digest"),
                                       limit=80),
            "output": _redact_optional(redactor, spec.get("output"),
                                       limit=80),
            "tools": _redact_list(redactor, spec.get("tools")),
            "write_scope": _redact_list(redactor, spec.get("write_scope")),
            "requires_graph": _boolean(spec.get("requires_graph")),
            "approval": {
                "mutate": _redact_optional(
                    redactor, _mapping(spec.get("approval")).get("mutate"),
                    limit=20),
                "network": _redact_optional(
                    redactor, _mapping(spec.get("approval")).get("network"),
                    limit=20),
            },
        },
        "status": status,
        "stop": stop,
        "stop_detail": stop_detail,
        "started_at": _redact_optional(redactor, doc.get("started_at"),
                                       limit=60),
        "finished_at": _redact_optional(redactor, doc.get("finished_at"),
                                        limit=60),
        "duration_s": _number(doc.get("duration_s")),
        "inputs": {
            "names": input_names,
            "digests": input_digests,
            "digest": _redact_optional(redactor, inputs.get("digest"),
                                       limit=80),
        },
        "preflight": {
            "ok": _boolean(preflight.get("ok")),
            "failures": _redact_list(redactor, preflight.get("failures")),
            "warnings": _redact_list(redactor, preflight.get("warnings")),
            "worktree": {
                "branch": _redact_optional(redactor, worktree.get("branch"),
                                           limit=120),
                "head": _redact_optional(redactor, worktree.get("head"),
                                         limit=80),
                "detached": _boolean(worktree.get("detached")),
                "operation": _redact_optional(
                    redactor, worktree.get("operation"), limit=40),
                "dirty": {
                    key: _integer(_mapping(worktree.get("dirty")).get(key))
                    for key in ("staged", "unstaged", "untracked",
                                "conflicted")
                },
                "expected_branch": _redact_optional(
                    redactor, expected.get("branch"), limit=120),
                "expected_source": _redact_optional(
                    redactor, expected.get("source"), limit=40),
            },
            "paths": {
                "ok": _boolean(_mapping(preflight.get("paths")).get("ok")),
                "write_scope": _redact_list(
                    redactor,
                    _mapping(preflight.get("paths")).get("write_scope")),
                "violations": _redact_list(
                    redactor,
                    _mapping(preflight.get("paths")).get("violations")),
            },
            "resources": {
                "ok": _boolean(resources.get("ok")),
                "reason": _redact_optional(redactor, resources.get("reason"),
                                           limit=60),
                "requested": _mapping(resources.get("requested")),
                "available": _mapping(resources.get("available")),
                "shortfall": _mapping(resources.get("shortfall")),
                "suggestions": _redact_list(redactor,
                                            resources.get("suggestions")),
            },
            "snapshot": {
                "required": _boolean(snapshot.get("required")),
                "state": _redact_optional(redactor, snapshot.get("state"),
                                          limit=40),
                "error": _redact_optional(redactor, snapshot.get("error"),
                                          limit=MAX_MESSAGE),
            },
            "test_plan": _test_plan_summary(redactor,
                                            preflight.get("test_plan")),
        },
        "gates": {
            **gate_reports,
            "after_skipped": _redact_optional(
                redactor, gates.get("after_skipped"), limit=MAX_MESSAGE),
        },
        "steps": steps,
        "step_count": len(_sequence(doc.get("steps"))),
        "steps_shown": len(steps),
        "artifacts": artifacts,
        "artifact_count": len(_sequence(doc.get("artifacts"))),
        "artifacts_shown": len(artifacts),
        "evidence_count": _integer(doc.get("evidence_count")),
        "budget": {
            "declared": {
                key: _number(declared.get(key))
                for key in ("tokens", "model_calls", "cost_usd", "wall_s")
            },
            "used": {
                key: _number(used.get(key))
                for key in ("tokens", "model_calls", "cost_usd", "wall_s",
                            "memory_mb", "disk_mb", "pids")
            },
        },
        "output": {
            "name": _redact_optional(redactor, output.get("name"), limit=80),
            "present": _boolean(output.get("present")),
            "digest": _redact_optional(redactor, output.get("digest"),
                                       limit=80),
            "schema_valid": _boolean(output.get("schema_valid")),
        },
        "what_was_not_assessed": _redact_list(
            redactor, doc.get("what_was_not_assessed"), max_items=MAX_LIST),
        "warnings": _redact_list(redactor, doc.get("warnings")),
        "truth_assessed": False,
        "excluded_fields": [
            "task objective and prompt text",
            "input values",
            "output value (only its name and digest are kept)",
            "events.jsonl and artifact contents",
            "absolute run, worktree and artifact paths (home-redacted)",
        ],
    }

    top_findings: list[dict[str, Any]] = []
    signatures: list[str] = []
    task_classes: list[str] = []
    spec_id = _redact(redactor, spec.get("id"), limit=64)
    if spec_id:
        task_classes.append(spec_id)
        top_findings.append({
            "kind": "task_run",
            "severity": "info" if status == "ok" else "error",
            "summary": f"{status}: {stop}" if stop else f"{status}: task run",
            "location": spec_id,
            "count": 1,
        })
        if status != "ok":
            signatures.append(
                f"{spec_id}: {status}: {stop_detail or stop or 'no detail'}")
    if stop:
        top_findings.append({
            "kind": "task_run",
            "severity": "error" if status in ("failed", "stopped") else "warning",
            "summary": f"stop: {stop}" + (f" ({stop_detail})"
                                          if stop_detail else ""),
            "location": run_id,
            "count": 1,
        })
    safe_run = _safe_token(run_id, "<run-id>")
    reproduction = ({
        "command": f"python -m ozagia.tasks_cli show {safe_run} --root . "
                   "--json",
        "description": "read the bounded task run evidence",
        "executed": False,
    },)
    return _Evidence(
        kind="task_run",
        source="",
        source_bytes=len(manifest_bytes or b""),
        source_sha256=_sha256(manifest_bytes) if manifest_bytes else None,
        content=content,
        findings=tuple(top_findings),
        signatures=tuple(signatures),
        task_classes=tuple(task_classes),
        reproduction=reproduction,
        excluded_fields=tuple(content["excluded_fields"]),
    )


# --------------------------------------------------------------------------- #
# evidence inspection
# --------------------------------------------------------------------------- #
def _task_run_dir(path: Path) -> Path | None:
    for name in (MANIFEST_FILE, "events.jsonl", "artifacts.json", "output.json"):
        if (path / name).is_file():
            return path
    return None


def _inspect_target(root: Path, target: Path,
                    redactor: Redactor) -> tuple[_Evidence | None, str | None]:
    if target.is_dir():
        run_dir = _task_run_dir(target)
        if run_dir is None:
            return None, (
                "directory is not a task run (expected manifest.json, "
                "events.jsonl, artifacts.json or output.json)")
        manifest_path = run_dir / MANIFEST_FILE
        manifest_bytes: bytes | None = None
        manifest: Any = None
        error: str | None = None
        if manifest_path.is_file():
            try:
                manifest_bytes = _read_bounded(manifest_path)
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                if not isinstance(manifest, Mapping):
                    error = "manifest is not a JSON object"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                    UpstreamError) as exc:
                error = str(exc)
        else:
            error = f"{MANIFEST_FILE} is missing"
        evidence = _task_run_summary(
            root, run_dir, manifest, manifest_bytes, error, redactor)
        evidence = _Evidence(
            kind=evidence.kind,
            source=_path_display(redactor, root, target),
            source_bytes=evidence.source_bytes,
            source_sha256=evidence.source_sha256,
            content=evidence.content,
            findings=evidence.findings,
            signatures=evidence.signatures,
            task_classes=evidence.task_classes,
            reproduction=evidence.reproduction,
            excluded_fields=evidence.excluded_fields,
        )
        return evidence, None

    if not target.is_file():
        return None, "not a regular file"
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise UpstreamError(f"cannot stat evidence: {exc}") from exc
    if size > MAX_EVIDENCE_FILE_BYTES:
        return None, (
            f"too large (limit {MAX_EVIDENCE_FILE_BYTES} bytes)")
    try:
        data = _read_bounded(target)
    except UpstreamError as exc:
        return None, str(exc)
    except OSError as exc:
        raise UpstreamError(f"cannot read evidence: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8 text; only JSON reports are accepted"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None, "not a JSON document; raw logs are not accepted"
    if not isinstance(doc, Mapping):
        return None, "unsupported JSON document (expected an object)"
    kind, payload = _detect(doc)
    if kind is None:
        return None, (
            "unsupported evidence type (expected a log_inspect report, a "
            "code-review packet, a task run, a repo-scan report or a status "
            "snapshot)")
    if kind == "log_report":
        evidence = _log_summary(root, payload, redactor)
    elif kind == "code_review":
        evidence = _review_summary(root, payload, redactor)
    elif kind == "repo_scan":
        evidence = _repo_scan_summary(root, payload, redactor)
    elif kind == "status":
        evidence = _status_summary(root, payload, redactor)
    else:
        evidence = _task_run_summary(
            root, target.parent, payload, data, None, redactor)
    return _Evidence(
        kind=evidence.kind,
        source=_path_display(redactor, root, target),
        source_bytes=len(data),
        source_sha256=_sha256(data),
        content=evidence.content,
        findings=evidence.findings,
        signatures=evidence.signatures,
        task_classes=evidence.task_classes,
        reproduction=evidence.reproduction,
        excluded_fields=evidence.excluded_fields,
    ), None


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #
def _root_path(root: str | Path) -> Path:
    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise UpstreamError(f"repository root is not a directory: {path}")
    return path


def _title_text(value: Any, redactor: Redactor) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpstreamError("title must be a non-empty string")
    text = redactor.text(value, limit=MAX_TITLE)
    if not text:
        raise UpstreamError("title is empty after redaction")
    return text


def _summary_text(value: Any, redactor: Redactor) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UpstreamError("summary must be a string or null")
    if not value.strip():
        return None
    return redactor.text(value, limit=MAX_SUMMARY) or None


def _evidence_entries(evidence: Any) -> list[str | Path]:
    if evidence is None:
        return []
    if isinstance(evidence, (str, bytes, os.PathLike)):
        entries: list[Any] = [evidence]
    elif isinstance(evidence, Mapping):
        raise UpstreamError("evidence must be a path or a sequence of paths")
    elif isinstance(evidence, Iterable):
        entries = list(evidence)
    else:
        raise UpstreamError("evidence must be a path or a sequence of paths")
    if len(entries) > MAX_EVIDENCE:
        raise UpstreamError(
            f"at most {MAX_EVIDENCE} evidence paths are allowed")
    result: list[str | Path] = []
    for entry in entries:
        if isinstance(entry, bytes) or not isinstance(
                entry, (str, os.PathLike)):
            raise UpstreamError(
                "each evidence entry must be a path string or Path")
        result.append(entry)
    return result


def _resolve_target(root: Path, entry: str | Path) -> Path:
    raw = Path(entry).expanduser()
    if not raw.is_absolute():
        raw = root / raw
    try:
        return raw.resolve()
    except OSError as exc:
        raise UpstreamError(f"cannot resolve evidence path: {exc}") from exc


def _evidence_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": SCHEMA,
        "kind": _safe_kind(item.get("kind")),
        "source": item.get("source"),
        "source_sha256": item.get("source_sha256"),
        "source_bytes": item.get("source_bytes"),
        "content": item.get("content") or {},
    }


def preview(root: str | Path, *, title: str, summary: str | None = None,
            evidence: Iterable[str | Path] = (),
            include_environment: bool = True,
            include_config: bool = True) -> dict[str, Any]:
    """Build a redacted, read-only preview of a field-improvement bundle.

    Only the explicitly listed evidence paths are read. Every supported input
    is summarised and redacted; unsupported or oversized inputs are listed in
    ``excluded`` with a reason. Nothing is written and no network call is made.
    """
    root_path = _root_path(root)
    redactor = Redactor()
    title_text = _title_text(title, redactor)
    summary_text = _summary_text(summary, redactor)
    entries = _evidence_entries(evidence)

    environment = _environment(redactor) if include_environment else None
    config = _config_diff(root_path, redactor) if include_config else None
    excluded: list[dict[str, Any]] = []
    if not include_environment:
        excluded.append({
            "type": "section",
            "source": "environment",
            "reason": "excluded by request",
        })
    if not include_config:
        excluded.append({
            "type": "section",
            "source": "config",
            "reason": "excluded by request",
        })

    included: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    signatures: list[str] = []
    task_classes: set[str] = set()
    reproduction: list[dict[str, Any]] = []
    seen: set[Path] = set()
    estimated_bytes = 0
    evidence_budget = max(0, MAX_BUNDLE_BYTES - BUNDLE_OVERHEAD_BYTES)

    for entry in entries:
        target = _resolve_target(root_path, entry)
        source = _path_display(redactor, root_path, target)
        if target in seen:
            excluded.append({
                "type": "evidence",
                "source": source,
                "reason": "duplicate evidence path",
            })
            continue
        seen.add(target)
        if not target.exists():
            raise UpstreamError(f"evidence path does not exist: {source}")
        try:
            item, reason = _inspect_target(root_path, target, redactor)
        except UpstreamError:
            raise
        except OSError as exc:
            raise UpstreamError(
                f"cannot read evidence {source}: {exc}") from exc
        if item is None:
            excluded.append({
                "type": "evidence",
                "source": source,
                "reason": reason or "unsupported evidence",
            })
            continue
        entry_payload = {
            "kind": item.kind,
            "source": item.source or source,
            "source_sha256": item.source_sha256,
            "source_bytes": item.source_bytes,
            "content": item.content,
        }
        content_bytes = len(_json_bytes(_evidence_payload(entry_payload)))
        if estimated_bytes + content_bytes > evidence_budget:
            excluded.append({
                "type": "evidence",
                "source": source,
                "reason": (
                    f"would exceed the {MAX_BUNDLE_BYTES}-byte bundle limit "
                    "after README, issue and manifest overhead"),
            })
            continue
        estimated_bytes += content_bytes
        index = len(included) + 1
        path = f"{EVIDENCE_DIR}/{index:02d}-{_safe_kind(item.kind)}.json"
        included.append({
            "index": index,
            "kind": item.kind,
            "path": path,
            "source": item.source or source,
            "source_bytes": item.source_bytes,
            "source_sha256": item.source_sha256,
            "bytes": content_bytes,
            "content": item.content,
            "excluded_fields": list(item.excluded_fields),
        })
        findings.extend(item.findings)
        signatures.extend(item.signatures)
        task_classes.update(item.task_classes)
        reproduction.extend(item.reproduction)

    deduped_findings: list[dict[str, Any]] = []
    seen_findings: set[tuple[Any, ...]] = set()
    for item in findings:
        key = (
            item.get("kind"), item.get("severity"), item.get("summary"),
            item.get("location"),
        )
        if key in seen_findings:
            continue
        seen_findings.add(key)
        deduped_findings.append(item)
        if len(deduped_findings) >= MAX_FINDINGS:
            break

    deduped_reproduction: list[dict[str, Any]] = []
    seen_commands: set[str] = set()
    for item in reproduction:
        command = str(item.get("command") or "")
        if not command or command in seen_commands:
            continue
        seen_commands.add(command)
        deduped_reproduction.append(item)

    unique_signatures = list(dict.fromkeys(
        text for text in signatures if text))[:5]
    evidence_excluded = sum(
        1 for item in excluded if item.get("type") == "evidence")
    planned_files = [
        MANIFEST_FILE,
        README_FILE,
        ISSUE_FILE,
        *(item["path"] for item in included),
    ]

    return {
        "schema": PREVIEW_SCHEMA,
        "schema_version": SCHEMA,
        "tool": TOOL_NAME,
        "title": title_text,
        "summary": summary_text,
        "root": redactor.text(_home_display(root_path), limit=MAX_TEXT),
        "consent_required": True,
        "consent": {
            "required": True,
            "granted": False,
            "scope": "local-bundle-only",
            "network": False,
            "uploaded": False,
            "issue_created": False,
        },
        "local_only": True,
        "network": False,
        "uploaded": False,
        "issue_created": False,
        "truth_assessed": False,
        "environment": environment,
        "config": config,
        "task_classes": sorted(task_classes),
        "failure_signatures": unique_signatures,
        "reproduction": deduped_reproduction,
        "findings": deduped_findings,
        "included": included,
        "excluded": excluded,
        "excluded_categories": list(EXCLUDED_CATEGORIES),
        "planned_files": planned_files,
        "redactions": redactor.to_dict(),
        "limits": {
            "evidence_paths": MAX_EVIDENCE,
            "evidence_file_bytes": MAX_EVIDENCE_FILE_BYTES,
            "bundle_bytes": MAX_BUNDLE_BYTES,
            "findings": MAX_FINDINGS,
            "findings_per_evidence": MAX_FINDINGS_PER_EVIDENCE,
            "config_changes": MAX_CONFIG_CHANGES,
            "text_chars": MAX_TEXT,
            "evidence_requested": len(entries),
            "evidence_included": len(included),
            "evidence_excluded": evidence_excluded,
        },
        "not_assessed": list(NOT_ASSESSED),
    }


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
_preview_report = preview


def _state_text(value: Any) -> str:
    return "unknown" if value is None else str(value)


def format_preview(preview_data: Mapping[str, Any]) -> str:
    """A human-readable preview. It never writes and never contacts anyone."""
    lines = ["Upstream field-improvement preview"]
    lines.append(f"Title: {_state_text(preview_data.get('title'))}")
    summary = preview_data.get("summary")
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append(f"Root: {_state_text(preview_data.get('root'))}")
    lines.append(
        "Consent: required - review this preview, then write with --consent")
    lines.append(
        "Local only: true; network: false; uploaded: false; "
        "issue created: false")

    included = [item for item in _sequence(preview_data.get("included"))
                if isinstance(item, Mapping)]
    lines.append("")
    lines.append(f"Included ({len(included)}):")
    if not included:
        lines.append("  (no supported evidence)")
    for item in included:
        content = _mapping(item.get("content"))
        detail = ""
        if item.get("kind") == "log_report":
            summary_info = _mapping(content.get("summary"))
            detail = (
                f"{summary_info.get('failures')} failure(s), "
                f"{content.get('findings_shown')} finding(s) shown")
        elif item.get("kind") == "code_review":
            summary_info = _mapping(content.get("summary"))
            detail = (
                f"{summary_info.get('findings')} finding(s), "
                f"{summary_info.get('blocking')} blocking")
        elif item.get("kind") == "task_run":
            detail = (
                f"run {content.get('run_id')}: "
                f"{content.get('status')} "
                f"{content.get('stop') or ''}").strip()
        elif item.get("kind") == "repo_scan":
            detail = f"{content.get('opportunity_count')} opportunity(ies)"
        elif item.get("kind") == "status":
            detail = f"status ok={content.get('ok')}"
        lines.append(
            f"  [{item.get('index')}] {item.get('kind')}  "
            f"{item.get('source')}  {item.get('bytes')} B")
        if detail:
            lines.append(f"      {detail}")
        lines.append(
            "      fields: " + ", ".join(sorted(content))[:500])

    excluded = [item for item in _sequence(preview_data.get("excluded"))
                if isinstance(item, Mapping)]
    lines.append("")
    lines.append(f"Excluded ({len(excluded)}):")
    if not excluded:
        lines.append("  (nothing was excluded)")
    for item in excluded:
        lines.append(
            f"  - {item.get('source')}: {item.get('reason')}")
    lines.append(
        "Always excluded: "
        + "; ".join(_sequence(preview_data.get("excluded_categories"))))

    environment = _mapping(preview_data.get("environment"))
    lines.append("")
    if environment:
        lines.append(
            "Environment: Python %s on %s %s (%s); ozagia %s" % (
                environment.get("python_version"),
                environment.get("system"),
                environment.get("release") or "",
                environment.get("machine") or "unknown",
                environment.get("ozagia_version") or "unknown",
            ))
    else:
        lines.append("Environment: excluded")

    config = _mapping(preview_data.get("config"))
    lines.append("")
    if config:
        lines.append(
            f"Config diff: {config.get('state')} "
            f"({config.get('changed_count', 0)} non-default setting(s))")
        for item in _sequence(config.get("changed"))[:5]:
            entry = _mapping(item)
            lines.append(
                f"  {entry.get('key')} = {entry.get('value')} "
                f"[{entry.get('source')}]")
    else:
        lines.append("Config diff: excluded")

    task_classes = list(_sequence(preview_data.get("task_classes")))
    if task_classes:
        lines.append("")
        lines.append("Task classes: " + ", ".join(map(str, task_classes)))
    signatures = list(_sequence(preview_data.get("failure_signatures")))
    if signatures:
        lines.append("")
        lines.append("Failure signatures:")
        for text in signatures:
            lines.append(f"  - {text}")

    findings = [item for item in _sequence(preview_data.get("findings"))
                if isinstance(item, Mapping)]
    lines.append("")
    lines.append(f"Selected findings ({len(findings)}):")
    if not findings:
        lines.append("  (none derived from the included evidence)")
    for item in findings:
        location = f" ({item.get('location')})" if item.get("location") else ""
        lines.append(
            f"  - [{item.get('severity')}] {item.get('kind')}: "
            f"{item.get('summary')}{location}")

    commands = [item for item in _sequence(preview_data.get("reproduction"))
                if isinstance(item, Mapping)]
    if commands:
        lines.append("")
        lines.append("Reproduction commands (suggestions; not executed):")
        for item in commands:
            lines.append(f"  {item.get('command')}")

    lines.append("")
    limits = _mapping(preview_data.get("limits"))
    lines.append(
        "Limits: %s evidence path(s), %s bytes per input, %s bytes per "
        "bundle, %s finding(s) shown" % (
            limits.get("evidence_paths"),
            limits.get("evidence_file_bytes"),
            limits.get("bundle_bytes"),
            limits.get("findings"),
        ))
    lines.append("")
    lines.append("Not assessed:")
    for text in _sequence(preview_data.get("not_assessed")):
        lines.append(f"  - {text}")
    return "\n".join(lines) + "\n"


def _safe_kind(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", _one_line(value)).strip("-")
    return text[:60] or "evidence"


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _environment_markdown(preview_data: Mapping[str, Any]) -> list[str]:
    environment = _mapping(preview_data.get("environment"))
    if not environment:
        return ["Environment summary was excluded from this bundle."]
    lines = [
        f"- Python: {environment.get('python_version')} "
        f"({environment.get('python_implementation')})",
        f"- System: {environment.get('system')} "
        f"{environment.get('release') or ''} "
        f"({environment.get('machine') or 'unknown'})",
        f"- ozagia: {environment.get('ozagia_version') or 'unknown'}",
    ]
    return lines


def _config_markdown(preview_data: Mapping[str, Any]) -> list[str]:
    config = _mapping(preview_data.get("config"))
    if not config:
        return ["Configuration differences were excluded from this bundle."]
    lines = [
        f"- State: {config.get('state')}",
        f"- Non-default settings: {config.get('changed_count', 0)}",
    ]
    for item in _sequence(config.get("changed"))[:MAX_SECTION_ITEMS]:
        entry = _mapping(item)
        lines.append(
            f"- `{entry.get('key')}` = `{entry.get('value')}` "
            f"(source: {entry.get('source')}, "
            f"default: `{entry.get('default')}`)")
    if config.get("omitted"):
        lines.append(f"- {config.get('omitted')} setting(s) omitted by caps")
    return lines


def issue_markdown(preview_data: Mapping[str, Any]) -> str:
    """A deterministic Markdown issue draft. It is never posted automatically."""
    lines = [
        f"# {_one_line(preview_data.get('title'))}",
        "",
        "> Local draft created by ozagia's upstream export. Nothing was "
        "uploaded and no issue or pull request was created. Review this file "
        "and the rest of the bundle before posting anything.",
        "",
    ]
    summary = preview_data.get("summary")
    if summary:
        lines.extend(["## Summary", "", _one_line(summary), ""])
    else:
        lines.extend([
            "## Summary",
            "",
            "No summary was supplied with this bundle.",
            "",
        ])

    lines.extend(["## Environment", ""])
    lines.extend(_environment_markdown(preview_data))
    lines.append("")

    lines.extend(["## Configuration differences", ""])
    lines.extend(_config_markdown(preview_data))
    lines.append("")

    task_classes = list(_sequence(preview_data.get("task_classes")))
    if task_classes:
        lines.extend([
            "## Task classes",
            "",
            *[f"- `{_one_line(item)}`" for item in task_classes],
            "",
        ])

    signatures = list(_sequence(preview_data.get("failure_signatures")))
    if signatures:
        lines.extend(["## Failure signatures", ""])
        lines.extend([f"- {_one_line(item)}" for item in signatures])
        lines.append("")

    findings = [item for item in _sequence(preview_data.get("findings"))
                if isinstance(item, Mapping)]
    lines.extend(["## Selected findings", ""])
    if not findings:
        lines.extend(["No selected findings were derived from the evidence."])
    for index, item in enumerate(findings, 1):
        lines.append(
            f"### {index}. [{item.get('severity')}] {item.get('kind')}: "
            f"{_one_line(item.get('summary'))}")
        if item.get("location"):
            lines.append(f"- Location: {_one_line(item.get('location'))}")
        lines.append(f"- Occurrences: {item.get('count', 1)}")
        lines.append("")

    commands = [item for item in _sequence(preview_data.get("reproduction"))
                if isinstance(item, Mapping) and item.get("command")]
    lines.extend(["## Reproduction", ""])
    if not commands:
        lines.extend([
            "No reproduction command was derived from this evidence.",
        ])
    else:
        lines.extend([
            "The following commands are suggestions. They were not executed.",
            "",
            "```sh",
            *[str(item.get("command")) for item in commands],
            "```",
        ])
    lines.append("")

    included = [item for item in _sequence(preview_data.get("included"))
                if isinstance(item, Mapping)]
    lines.extend(["## Evidence included", ""])
    if not included:
        lines.append("No evidence was included.")
    for item in included:
        lines.append(
            f"- [{item.get('index')}] `{item.get('kind')}` - "
            f"`{item.get('source')}` "
            f"({item.get('bytes')} bytes redacted, "
            f"source {item.get('source_bytes')} bytes)")
    lines.append("")

    excluded = [item for item in _sequence(preview_data.get("excluded"))
                if isinstance(item, Mapping)]
    lines.extend(["## Excluded", ""])
    if not excluded:
        lines.append("Nothing was explicitly excluded.")
    for item in excluded:
        lines.append(
            f"- `{item.get('source')}`: {item.get('reason')}")
    lines.append("")

    lines.extend([
        "## Privacy and limits",
        "",
        *[f"- {text}" for text in _sequence(preview_data.get("not_assessed"))],
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _readme(preview_data: Mapping[str, Any]) -> str:
    included = [item for item in _sequence(preview_data.get("included"))
                if isinstance(item, Mapping)]
    excluded = [item for item in _sequence(preview_data.get("excluded"))
                if isinstance(item, Mapping)]
    lines = [
        "# ozagia field-improvement bundle",
        "",
        f"Title: {_one_line(preview_data.get('title'))}",
        "",
        "This is a local diagnostic bundle prepared from evidence a person "
        "explicitly selected. It is not uploaded, and no issue or pull "
        "request was created.",
        "",
        "## Consent",
        "",
        "- Consent was explicitly granted for this local bundle.",
        "- Scope: local bundle only.",
        "- Network: false. Uploaded: false. Issue created: false.",
        "",
        "## Contents",
        "",
        f"- `{MANIFEST_FILE}`: deterministic metadata, consent record and "
        "SHA-256 hashes for every other file.",
        f"- `{README_FILE}`: this review guide.",
        f"- `{ISSUE_FILE}`: a Markdown issue draft; nothing posts it.",
        f"- `{EVIDENCE_DIR}/`: {len(included)} redacted evidence "
        "summary file(s).",
        "",
        "## What is included",
        "",
        f"- Environment summary: "
        f"{'yes' if preview_data.get('environment') else 'no'}.",
        f"- Configuration differences: "
        f"{'yes' if preview_data.get('config') else 'no'}.",
        f"- Selected findings: {len(_sequence(preview_data.get('findings')))}.",
        f"- Evidence inputs: {len(included)}.",
        "",
    ]
    for item in included:
        lines.append(
            f"- [{item.get('index')}] `{item.get('kind')}` from "
            f"`{item.get('source')}` ({item.get('bytes')} bytes).")
        fields = list(_sequence(item.get("excluded_fields")))
        if fields:
            lines.append(
                "  - Omitted from this input: " + "; ".join(fields) + ".")
    if not included:
        lines.append("- No supported evidence was included.")
    lines.append("")

    lines.extend(["## What is excluded", ""])
    for category in _sequence(preview_data.get("excluded_categories")):
        lines.append(f"- {category}.")
    for item in excluded:
        lines.append(
            f"- `{item.get('source')}`: {item.get('reason')}.")
    lines.append("")

    lines.extend([
        "## How to review",
        "",
        f"1. Read `{MANIFEST_FILE}` and check the consent record and file "
        "hashes.",
        f"2. Read `{ISSUE_FILE}` and remove anything you do not want to share.",
        f"3. Inspect each file under `{EVIDENCE_DIR}/`.",
        "4. Check names, paths, model names, tool names and error classes; "
        "redaction is practical, not a proof of anonymity.",
        "",
        "## How to open an issue manually",
        "",
        "1. Open your issue tracker in a browser or editor.",
        "2. Create a new issue and paste the reviewed contents of "
        f"`{ISSUE_FILE}`.",
        "3. Attach this bundle only if you are comfortable sharing every "
        "file it contains.",
        "4. Nothing in this bundle will do that for you.",
        "",
        "## Limits",
        "",
        *[f"- {text}" for text in _sequence(preview_data.get("not_assessed"))],
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def issue_text(preview_data: Mapping[str, Any]) -> str:
    """Alias for :func:`issue_markdown` used by the CLI."""
    return issue_markdown(preview_data)


def format_bundle(result: Mapping[str, Any]) -> str:
    """A short text summary of a written bundle."""
    lines = [
        f"Bundle written: {_state_text(result.get('destination'))}",
        f"Format: {_state_text(result.get('format'))}",
        "Consent: granted locally; network: false; uploaded: false; "
        "issue created: false",
        f"Files ({len(_sequence(result.get('files')))}):",
    ]
    for item in _sequence(result.get("files")):
        entry = _mapping(item)
        lines.append(
            f"  {entry.get('path')}  {entry.get('sha256')}  "
            f"{entry.get('bytes')} B")
    lines.append(
        f"Evidence included: {result.get('evidence_included')}; "
        f"excluded: {result.get('evidence_excluded')}")
    lines.append(
        f"Total bytes: {result.get('bytes')}; "
        f"manifest: {result.get('manifest_sha256')}")
    lines.append(
        "Review the bundle, then open issue.md in your tracker manually; "
        "nothing was posted.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# bundle writing
# --------------------------------------------------------------------------- #
def _validated_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UpstreamError("preview must be a mapping")
    if value.get("schema") != PREVIEW_SCHEMA:
        raise UpstreamError(
            f"preview schema must be {PREVIEW_SCHEMA!r}")
    title = value.get("title")
    if not isinstance(title, str) or not title:
        raise UpstreamError("preview has no title")
    included = value.get("included")
    if isinstance(included, (str, bytes, Mapping)) or not isinstance(
            included, Sequence):
        raise UpstreamError("preview has no included list")
    excluded = value.get("excluded", [])
    if isinstance(excluded, (str, bytes, Mapping)) or not isinstance(
            excluded, Sequence):
        raise UpstreamError("preview has no excluded list")
    result = dict(value)
    result["included"] = [dict(item) for item in included
                          if isinstance(item, Mapping)]
    result["excluded"] = [dict(item) for item in excluded
                          if isinstance(item, Mapping)]
    return result


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _destination(root: Path, destination: str | Path) -> tuple[Path, str]:
    raw = Path(destination).expanduser()
    if not str(raw).strip() or str(raw).strip() == ".":
        raise BundleError(
            "destination must be an explicit directory path or .zip file")
    try:
        target = raw.resolve()
    except OSError as exc:
        raise BundleError(f"cannot resolve destination: {exc}") from exc
    if target == Path(target.anchor):
        raise BundleError("refusing the filesystem root as a destination")
    if target == root:
        raise BundleError("refusing the repository root as a destination")
    if _within(target, root / ".git"):
        raise BundleError("refusing a destination inside .git")
    if _within(root, target):
        raise BundleError(
            "refusing a destination that is an ancestor of the repository "
            "root")
    parent = target.parent
    if not parent.is_dir():
        raise UpstreamError(
            f"destination parent directory does not exist: {parent}")
    is_zip = target.suffix.lower() == ".zip"
    if is_zip:
        if target.exists() and not target.is_file():
            raise BundleError("zip destination exists and is not a file")
        format_kind = "zip"
    else:
        if target.exists() and not target.is_dir():
            raise BundleError(
                "directory destination exists and is not a directory")
        if target.is_dir() and any(target.iterdir()):
            raise BundleError(
                "destination directory is not empty; choose a new path")
        format_kind = "directory"
    return target, format_kind


def _bundle_parts(preview_data: Mapping[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        README_FILE: _readme(preview_data).encode("utf-8"),
        ISSUE_FILE: issue_markdown(preview_data).encode("utf-8"),
    }
    included_meta: list[dict[str, Any]] = []
    for index, item in enumerate(
            [entry for entry in _sequence(preview_data.get("included"))
             if isinstance(entry, Mapping)], 1):
        kind = _safe_kind(item.get("kind"))
        path = f"{EVIDENCE_DIR}/{index:02d}-{kind}.json"
        payload = _evidence_payload(item)
        data = _json_bytes(payload)
        files[path] = data
        included_meta.append({
            "index": index,
            "kind": kind,
            "source": item.get("source"),
            "source_sha256": item.get("source_sha256"),
            "source_bytes": item.get("source_bytes"),
            "path": path,
            "sha256": _sha256(data),
            "bytes": len(data),
            "excluded_fields": list(_sequence(item.get("excluded_fields"))),
        })

    excluded = [item for item in _sequence(preview_data.get("excluded"))
                if isinstance(item, Mapping)]
    file_table = [
        {"path": name, "sha256": _sha256(data), "bytes": len(data)}
        for name, data in sorted(files.items())
    ]
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "schema_version": SCHEMA,
        "tool": TOOL_NAME,
        "title": preview_data.get("title"),
        "summary": preview_data.get("summary"),
        "root": preview_data.get("root"),
        "consent": {
            "granted": True,
            "scope": "local-bundle-only",
            "network": False,
            "uploaded": False,
            "issue_created": False,
        },
        "local_only": True,
        "network": False,
        "uploaded": False,
        "issue_created": False,
        "truth_assessed": False,
        "environment": preview_data.get("environment"),
        "config": preview_data.get("config"),
        "task_classes": list(_sequence(preview_data.get("task_classes"))),
        "failure_signatures": list(
            _sequence(preview_data.get("failure_signatures"))),
        "reproduction": [
            dict(item) for item in _sequence(preview_data.get("reproduction"))
            if isinstance(item, Mapping)
        ],
        "findings": [
            dict(item) for item in _sequence(preview_data.get("findings"))
            if isinstance(item, Mapping)
        ],
        "included": included_meta,
        "excluded": [dict(item) for item in excluded],
        "excluded_categories": list(
            _sequence(preview_data.get("excluded_categories"))),
        "evidence_count": len(included_meta),
        "evidence_excluded": sum(
            1 for item in excluded if item.get("type") == "evidence"),
        "redactions": dict(_mapping(preview_data.get("redactions"))),
        "limits": dict(_mapping(preview_data.get("limits"))),
        "not_assessed": list(_sequence(preview_data.get("not_assessed"))),
        "files": file_table,
        "manifest_note": (
            f"{MANIFEST_FILE} is not listed in its own hash table; hash it "
            "separately if needed"),
    }
    files[MANIFEST_FILE] = _json_bytes(manifest)
    return files


def _bundle_order(files: Mapping[str, bytes]) -> list[str]:
    fixed = [MANIFEST_FILE, README_FILE, ISSUE_FILE]
    return [name for name in fixed if name in files] + sorted(
        name for name in files if name not in fixed)


def _check_bundle_size(files: Mapping[str, bytes]) -> None:
    total = sum(len(data) for data in files.values())
    if total > MAX_BUNDLE_BYTES:
        raise BundleError(
            f"bundle would be {total} bytes; the limit is "
            f"{MAX_BUNDLE_BYTES} bytes")


def _write_directory(destination: Path, files: Mapping[str, bytes]) -> None:
    parent = destination.parent
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.", dir=str(parent)))
    try:
        for name in _bundle_order(files):
            target = _contained_path(temporary, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[name])
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        if destination.exists():
            destination.rmdir()
        os.replace(temporary, destination)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise BundleError(f"cannot write bundle: {exc}") from exc
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _validate_bundle_name(name: str) -> None:
    if (not name or name.startswith("/") or name.startswith("\\")
            or ".." in name.split("/")):
        raise BundleError(f"unsafe bundle path: {name!r}")


def _write_zip(destination: Path, files: Mapping[str, bytes]) -> None:
    handle = None
    temporary: str | None = None
    try:
        handle, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".zip",
            dir=str(destination.parent))
        os.close(handle)
        handle = None
        with zipfile.ZipFile(temporary, "w",
                             compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for name in _bundle_order(files):
                _validate_bundle_name(name)
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, files[name])
        os.replace(temporary, destination)
        temporary = None
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError(f"cannot write zip bundle: {exc}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _contained_path(base: Path, relative: str) -> Path:
    if (not relative or relative.startswith("/")
            or relative.startswith("\\") or ".." in relative.split("/")):
        raise BundleError(f"unsafe bundle path: {relative!r}")
    target = (base / relative).resolve()
    base_resolved = base.resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise BundleError(f"bundle path escapes the destination: {relative!r}")
    return target


def write_bundle(root: str | Path, destination: str | Path, *,
                 consent: bool, preview: Mapping[str, Any] | None = None,
                 title: str | None = None, summary: str | None = None,
                 evidence: Iterable[str | Path] = (),
                 include_environment: bool = True,
                 include_config: bool = True) -> dict[str, Any]:
    """Write a local bundle only after explicit consent.

    The destination is a new or empty directory, or a ``.zip`` file. Every
    bundle file is written inside that destination. The manifest and all
    hashes are deterministic for the same preview content. No network call is
    made and nothing is uploaded.
    """
    if consent is not True:
        raise ConsentRequired(
            "explicit consent is required; review the preview and pass "
            "consent=True (CLI: --consent)")
    root_path = _root_path(root)
    if preview is None:
        if title is None:
            raise UpstreamError(
                "title is required when no preview is supplied")
        preview_data = _preview_report(
            root_path, title=title, summary=summary, evidence=evidence,
            include_environment=include_environment,
            include_config=include_config)
    else:
        preview_data = _validated_preview(preview)

    target, format_kind = _destination(root_path, destination)
    files = _bundle_parts(preview_data)
    _check_bundle_size(files)
    if format_kind == "zip":
        _write_zip(target, files)
    else:
        _write_directory(target, files)

    file_table = [
        {"path": name, "sha256": _sha256(files[name]), "bytes": len(files[name])}
        for name in _bundle_order(files)
    ]
    manifest_sha = next(
        (item["sha256"] for item in file_table
         if item["path"] == MANIFEST_FILE), None)
    return {
        "schema": BUNDLE_SCHEMA,
        "schema_version": SCHEMA,
        "ok": True,
        "destination": str(target),
        "format": format_kind,
        "files": file_table,
        "manifest_sha256": manifest_sha,
        "bytes": sum(len(data) for data in files.values()),
        "evidence_included": len(preview_data.get("included") or []),
        "evidence_excluded": sum(
            1 for item in preview_data.get("excluded") or []
            if isinstance(item, Mapping)
            and item.get("type") == "evidence"),
        "consent": {
            "granted": True,
            "scope": "local-bundle-only",
            "network": False,
            "uploaded": False,
            "issue_created": False,
        },
        "local_only": True,
        "network": False,
        "uploaded": False,
        "issue_created": False,
    }


def _atomic_write_text(path: Path, text: str) -> None:
    handle = None
    temporary: str | None = None
    try:
        handle, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            handle = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise BundleError(f"cannot write issue draft: {exc}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_issue(root: str | Path, destination: str | Path, *,
                title: str, summary: str | None = None,
                evidence: Iterable[str | Path] = (),
                include_environment: bool = True,
                include_config: bool = True) -> dict[str, Any]:
    """Write the Markdown issue draft only. Nothing is uploaded."""
    root_path = _root_path(root)
    preview_data = _preview_report(
        root_path, title=title, summary=summary, evidence=evidence,
        include_environment=include_environment, include_config=include_config)
    target = Path(destination).expanduser()
    if not str(target).strip() or str(target).strip() == ".":
        raise BundleError("issue destination must be an explicit file path")
    target = target.resolve()
    if target == Path(target.anchor) or target == root_path:
        raise BundleError("refusing the repository root or filesystem root "
                          "as an issue destination")
    if _within(target, root_path / ".git"):
        raise BundleError("refusing an issue destination inside .git")
    if target.exists() and target.is_dir():
        raise BundleError("issue destination is a directory")
    if not target.parent.is_dir():
        raise UpstreamError(
            f"issue destination parent directory does not exist: "
            f"{target.parent}")
    text = issue_markdown(preview_data)
    _atomic_write_text(target, text)
    data = text.encode("utf-8")
    return {
        "schema": ISSUE_SCHEMA,
        "schema_version": SCHEMA,
        "ok": True,
        "path": str(target),
        "bytes": len(data),
        "sha256": _sha256(data),
        "local_only": True,
        "network": False,
        "uploaded": False,
        "issue_created": False,
    }


__all__ = [
    "BUNDLE_SCHEMA",
    "BundleError",
    "ConsentRequired",
    "EVIDENCE_SCHEMA",
    "ISSUE_SCHEMA",
    "MAX_BUNDLE_BYTES",
    "MAX_EVIDENCE",
    "MAX_EVIDENCE_FILE_BYTES",
    "PREVIEW_SCHEMA",
    "UpstreamError",
    "format_bundle",
    "format_preview",
    "issue_markdown",
    "issue_text",
    "preview",
    "write_bundle",
    "write_issue",
]
