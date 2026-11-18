"""Optional, non-authoritative model review for deep-review findings.

This module deliberately has no default runner. Callers must explicitly inject
one, keeping the normal deep-review path offline and deterministic.

Model output is untrusted data: it is strictly validated, bounded, redacted and
always carries ``truth_assessed: false``. Budgets bound the number, duration
and (when the harness reports usage) token/cost spend of the pass.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from itertools import islice
from pathlib import Path
from typing import Any

from ozagia.core.security import redact_text

DEFAULT_ROLES = ("security", "performance", "failures", "data",
                 "scalability", "architecture", "tests")
MAX_PACKET_FINDINGS = 60
MAX_PATH_LENGTH = 240
MAX_METADATA_LENGTH = 160

SEVERITIES = ("blocking", "major", "minor", "note")
SEVERITY_RANK = {severity: rank for rank, severity in enumerate(reversed(SEVERITIES))}
MAX_SUMMARY_LENGTH = 2000
MAX_WORDING_LENGTH = 1000
MAX_ISSUE_TYPE_LENGTH = 80
MAX_RECOMMENDATION_LENGTH = 1000
MAX_CITATION_KEYS = MAX_PACKET_FINDINGS
MAX_CITATION_LENGTH = 80
MAX_FINDINGS_PER_RESPONSE = MAX_PACKET_FINDINGS
MAX_VALIDATION_ERRORS = 80
MAX_ERROR_DETAILS = 8
MAX_REDACTION_DEPTH = 4
MAX_REDACTION_ITEMS = 32
MAX_PROVENANCE = 16

DEFAULT_DEADLINE_S = 120.0
DEFAULT_MAX_REQUESTS = 32

#: Reviewer roles are harness roles; candidates belong to report subjects.
ROLE_SUBJECTS = {
    "security": "security",
    "performance": "performance",
    "failures": "failure-modes",
    "failure-modes": "failure-modes",
    "failure_modes": "failure-modes",
    "data": "data-structures",
    "data-structures": "data-structures",
    "data_structures": "data-structures",
    "scalability": "scalability",
    "architecture": "architecture",
    "tests": "tests-and-coverage",
    "tests-and-coverage": "tests-and-coverage",
    "tests_and_coverage": "tests-and-coverage",
}


def _safe_metadata(value: Any) -> str | None:
    """Bound and redact provider supplied metadata before it enters artifacts."""
    if value is None:
        return None
    text = redact_text(str(value)).strip()
    return text[:MAX_METADATA_LENGTH] or None


def _safe_text(value: Any, limit: int) -> str | None:
    """Redact and bound one untrusted string; empty values become ``None``."""
    if value is None:
        return None
    text = redact_text(str(value)).strip()
    return text[:limit] or None


def _safe_path(value: Any) -> str:
    """Keep packet locations repository-relative, redacted and bounded."""
    path = _safe_text(value, MAX_PATH_LENGTH) or ""
    # Filenames are attacker-controlled data. Keep newlines and terminal
    # controls from becoming extra prompt instructions or display escapes.
    path = "".join(char if char.isprintable() else "_" for char in path)
    # Absolute/home paths can reveal usernames and machine layout. Replace
    # those rather than attempting to infer a repository root here.
    if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
        return "<absolute-path-redacted>"
    return path


def _safe_assertion(item: Mapping[str, Any]) -> dict[str, str]:
    """Redact model-provided assertion strings before storing them."""
    limits = {"path": MAX_PATH_LENGTH, "symbol": 160,
              "source_sha256": 64}
    return {
        "schema": "ozagia.deep_review.assertion.v1",
        "kind": "python.mutable_default",
        **{key: _safe_text(item.get(key), limit) or ""
           for key, limit in limits.items()},
    }


def _redact_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact and bound JSON-ish metadata (usage, run fields)."""
    if depth > MAX_REDACTION_DEPTH:
        return "<redacted:depth>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_REDACTION_ITEMS:
                result["<redacted:items>"] = True
                break
            safe_key = _safe_text(key, MAX_METADATA_LENGTH) or "<empty-key>"
            result[safe_key] = _redact_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_redact_value(item, depth + 1)
                  for item in islice(value, MAX_REDACTION_ITEMS)]
        if len(value) > MAX_REDACTION_ITEMS:
            result.append("<redacted:items>")
        return result
    if isinstance(value, str):
        return _safe_text(value, MAX_METADATA_LENGTH)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<redacted:non-finite>"
    return _safe_text(value, MAX_METADATA_LENGTH)


def _packet(findings: Iterable[Mapping[str, Any]], *, root: str | Path | None = None) -> dict[str, Any]:
    findings = list(findings)
    rows = []
    source_hashes: dict[str, str | None] = {}
    for finding in findings[:MAX_PACKET_FINDINGS]:
        locations = []
        for loc in (finding.get("locations") or [])[:4]:
            if isinstance(loc, Mapping):
                safe_path = _safe_path(loc.get("path"))
                location = {"path": safe_path,
                            **({"line": loc["line"]}
                               if isinstance(loc.get("line"), int) else {})}
                if root is not None and safe_path.endswith(".py"):
                    if safe_path not in source_hashes and len(source_hashes) < 8:
                        from ozagia.core.deep_review.verification import python_source_sha256
                        source_hashes[safe_path] = python_source_sha256(root, safe_path)
                    if source_hashes.get(safe_path):
                        location["source_sha256"] = source_hashes[safe_path]
                locations.append(location)
        rows.append({"id": _safe_text(finding.get("id", ""), 80) or "",
                     "subject": _safe_text(finding.get("subject", "unknown"), 40)
                                or "unknown",
                     "type": _safe_text(finding.get("type", "unknown"), 80)
                             or "unknown",
                     "severity": _safe_text(finding.get("severity", "note"), 16)
                                 or "note",
                     "locations": locations})
    return {
        "schema": "ozagia.deep_review.model_packet.v1",
        "findings": rows,
        "findings_omitted": max(0, len(findings) - len(rows)),
        "instructions": (
            "Review these finding labels and locations for plausible omissions "
            "or relationships. Return checkable candidate findings only. Cite "
            "packet finding IDs in citation_keys. For a machine-checkable "
            "assertion, return assertions as a list of objects with schema "
            "ozagia.deep_review.assertion.v1, kind python.mutable_default, "
            "path, exact function or Class.method symbol, and source_sha256 "
            "copied from a packet Python location. Only this narrow assertion "
            "is checked; prose, impact and severity remain unverified."),
        "truth_assessed": False,
    }


def _bounded_string(item: Mapping[str, Any], field: str, limit: int,
                    prefix: str, *, required: bool) -> str | None:
    value = item.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{prefix}.{field} must be a string")
    if required and not value.strip():
        raise ValueError(f"{prefix}.{field} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{prefix}.{field} exceeds {limit} characters")
    return value


def _item_errors(index: int, item: Any) -> list[str]:
    """Strictly validate one model candidate; no value is echoed in errors."""
    prefix = f"findings[{index}]"
    if not isinstance(item, Mapping):
        return [f"{prefix} must be an object"]
    errors: list[str] = []
    for field, limit in (("wording", MAX_WORDING_LENGTH),
                         ("issue_type", MAX_ISSUE_TYPE_LENGTH)):
        try:
            _bounded_string(item, field, limit, prefix, required=True)
        except ValueError as exc:
            errors.append(str(exc))
    assertions = item.get("assertions", [])
    if not isinstance(assertions, list) or len(assertions) > 4:
        errors.append(f"{prefix}.assertions must be a list of at most 4 entries")
    else:
        from ozagia.core.deep_review.verification import PYTHON_ASSERTION_SCHEMA
        for assertion_index, assertion in enumerate(assertions):
            ap = f"{prefix}.assertions[{assertion_index}]"
            required = {"schema", "kind", "path", "symbol", "source_sha256"}
            if not isinstance(assertion, Mapping) or set(assertion) != required:
                errors.append(f"{ap} must contain exactly the assertion contract fields")
                continue
            path = assertion.get("path")
            symbol = assertion.get("symbol")
            digest = assertion.get("source_sha256")
            if (assertion.get("schema") != PYTHON_ASSERTION_SCHEMA
                    or assertion.get("kind") != "python.mutable_default"):
                errors.append(f"{ap} has an unsupported assertion type")
            if (not isinstance(path, str) or not path.endswith(".py")
                    or len(path) > MAX_PATH_LENGTH or path.startswith(("/", "~"))
                    or "\\" in path
                    or any(part in {"", ".", ".."} for part in path.split("/"))):
                errors.append(f"{ap}.path must be a bounded relative Python path")
            if (not isinstance(symbol, str) or len(symbol) > 160
                    or any(not part.isidentifier() for part in symbol.split("."))
                    or len(symbol.split(".")) > 2):
                errors.append(f"{ap}.symbol must be an exact function or Class.method")
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                errors.append(f"{ap}.source_sha256 must be a lowercase SHA-256 digest")
    severity = item.get("severity")
    if not isinstance(severity, str) or severity not in SEVERITIES:
        errors.append(f"{prefix}.severity must be one of {SEVERITIES}")
    citations = item.get("citation_keys")
    if not isinstance(citations, list):
        errors.append(f"{prefix}.citation_keys must be a list")
    elif len(citations) > MAX_CITATION_KEYS:
        errors.append(
            f"{prefix}.citation_keys exceeds {MAX_CITATION_KEYS} entries")
    else:
        for citation_index, citation in enumerate(citations):
            if not isinstance(citation, str):
                errors.append(
                    f"{prefix}.citation_keys[{citation_index}] must be a string")
            elif not citation.strip():
                errors.append(
                    f"{prefix}.citation_keys[{citation_index}] must not be empty")
            elif len(citation) > MAX_CITATION_LENGTH:
                errors.append(
                    f"{prefix}.citation_keys[{citation_index}] exceeds "
                    f"{MAX_CITATION_LENGTH} characters")
    try:
        _bounded_string(item, "recommendation", MAX_RECOMMENDATION_LENGTH,
                        prefix, required=False)
    except ValueError as exc:
        errors.append(str(exc))
    truth = item.get("truth_assessed")
    if truth is not None and truth is not False:
        errors.append(f"{prefix}.truth_assessed must be false")
    return errors


def validate_model_output(output: Any) -> dict[str, Any]:
    """Strictly validate a model response shape.

    This is validation, not truth assessment: a structurally valid response
    can still be wrong. Counts are per candidate finding; an invalid envelope
    (root, summary or findings list) rejects the response as a whole.
    """
    errors: list[str] = []
    accepted = 0
    rejected = 0
    if not isinstance(output, Mapping):
        return {"valid": False, "errors": ["output must be a JSON object"],
                "accepted": 0, "rejected": 0}
    try:
        _bounded_string(output, "summary", MAX_SUMMARY_LENGTH, "output",
                        required=True)
    except ValueError as exc:
        errors.append(str(exc))
    truth = output.get("truth_assessed")
    if truth is not None and truth is not False:
        errors.append("output.truth_assessed must be false")
    items = output.get("findings")
    if not isinstance(items, list):
        errors.append("findings must be a list")
        items = []
    if len(items) > MAX_FINDINGS_PER_RESPONSE:
        errors.append(
            f"findings exceeds {MAX_FINDINGS_PER_RESPONSE} entries")
        rejected += max(0, len(items) - MAX_FINDINGS_PER_RESPONSE)
        items = items[:MAX_FINDINGS_PER_RESPONSE]
    for index, item in enumerate(items):
        item_errors = _item_errors(index, item)
        if item_errors:
            rejected += 1
            errors.extend(item_errors)
        else:
            accepted += 1
    return {
        "valid": not errors,
        "errors": errors[:MAX_VALIDATION_ERRORS],
        "accepted": accepted,
        "rejected": rejected,
        "errors_omitted": max(0, len(errors) - MAX_VALIDATION_ERRORS),
    }


def _names(value: Iterable[str] | str, label: str) -> list[str]:
    """A deterministic, de-duplicated non-empty name list."""
    if isinstance(value, str):
        pieces: Iterable[Any] = value.split(",")
    elif isinstance(value, Mapping):
        raise ValueError(f"{label} must be a list or comma-separated string")
    elif isinstance(value, (set, frozenset)):
        pieces = sorted(value, key=str)
    else:
        try:
            pieces = list(value)
        except TypeError as exc:
            raise ValueError(
                f"{label} must be a list or comma-separated string") from exc
    names: list[str] = []
    for piece in pieces:
        name = str(piece).strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise ValueError(f"{label} is empty")
    return names


def _usage_values(usage: Any) -> tuple[float, float | None]:
    """Return ``(tokens, cost)`` from harness usage metadata when present."""
    if not isinstance(usage, Mapping):
        return 0.0, None

    def number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    def first(keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = number(usage.get(key))
            if value is not None:
                return value
        return None

    tokens = first(("total_tokens",))
    if tokens is None:
        prompt = first(("prompt_tokens", "input_tokens", "input")) or 0.0
        completion = first(("completion_tokens", "output_tokens", "output")) or 0.0
        tokens = prompt + completion
    cost = first(("cost_usd", "cost", "total_cost"))
    return tokens, cost


def _safe_error(error: Any) -> dict[str, Any]:
    """Keep only non-sensitive, bounded error metadata."""
    if not isinstance(error, Mapping):
        return {"stage": "model", "error": "Model review failed"}
    stage = _safe_metadata(error.get("stage")) or "model"
    safe: dict[str, Any] = {"stage": stage[:40]}
    for key in ("role", "model"):
        value = _safe_metadata(error.get(key))
        if value is not None:
            safe[key] = value
    error_type = _safe_metadata(error.get("error_type"))
    if error_type is not None:
        safe["error_type"] = error_type
    if stage == "validation":
        validation = error.get("validation")
        details = validation.get("errors") if isinstance(validation, Mapping) else []
        safe_details = []
        for detail in (details if isinstance(details, list) else [])[:MAX_ERROR_DETAILS]:
            text = _safe_metadata(detail)
            if text is not None:
                safe_details.append(text)
        message = "validation failed"
        if safe_details:
            message += ": " + "; ".join(safe_details)
        safe["error"] = message[:500]
    else:
        safe["error"] = (
            f"{error_type or 'ProviderError'}: model review failed")
    return safe


def _rejected_items(error: Any, stats: dict[str, int]) -> None:
    """Account for findings in a rejected response without retaining output."""
    if not isinstance(error, Mapping):
        return
    if error.get("stage") != "validation":
        return
    validation = error.get("validation")
    if isinstance(validation, Mapping):
        rejected = validation.get("rejected")
        accepted = validation.get("accepted")
        if isinstance(rejected, int) and not isinstance(rejected, bool):
            stats["rejected"] += max(0, rejected)
        if (not validation.get("valid") and isinstance(accepted, int)
                and not isinstance(accepted, bool)):
            # Accepted items in an invalid envelope are discarded with it.
            stats["rejected"] += max(0, accepted)
        return
    output = error.get("output")
    items = output.get("findings") if isinstance(output, Mapping) else None
    if isinstance(items, list):
        stats["rejected"] += len(items[:MAX_FINDINGS_PER_RESPONSE])


def _safe_run(run: Any) -> dict[str, Any]:
    if not isinstance(run, Mapping):
        return {"usage": {}}
    safe: dict[str, Any] = {}
    for key in ("role", "model", "provider", "packet_sha256"):
        value = _safe_metadata(run.get(key))
        if value is not None:
            safe[key] = value
    reviewer = _run_reviewer(run)
    if reviewer:
        safe["reviewer_id"] = reviewer
    safe["usage"] = _redact_value(run.get("usage") or {})
    return safe


def _slug(value: Any) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-",
                  str(value or "").strip().casefold())
    return slug.strip("-") or "unknown"


def _run_reviewer(run: Mapping[str, Any]) -> str | None:
    """Reconstruct the reviewer id from sanitized parts.

    The harness derives its id by slugging the raw role and model. A secret in
    a model name can therefore survive slugging (``password=...`` becomes
    ``password-...``), so never trust the raw ``reviewer_id`` when it can be
    rebuilt from the already-redacted values.
    """
    role = _safe_metadata(run.get("role"))
    model = _safe_metadata(run.get("model"))
    if role and model:
        return f"{_slug(role)}:{_slug(model)}"
    return _safe_metadata(run.get("reviewer_id"))


def _role_subject(role: Any) -> str:
    key = str(role or "").strip().casefold()
    return ROLE_SUBJECTS.get(key, "correctness")


def _normalized_wording(wording: str) -> str:
    return " ".join(wording.casefold().split())


def _candidate_id(wording: str, citations: Iterable[str]) -> str:
    seed = f"{_normalized_wording(wording)}\0{','.join(sorted(set(citations)))}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"drm-{digest}"


def _empty_ocelot(note: str) -> dict[str, Any]:
    return {
        "schema": "ozagia.ocelot_code.clusters.v1",
        "requested": False,
        "available": False,
        "state": "not_requested",
        "mode": "auto",
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
        "note": note,
    }


def _ocelot_candidates(candidates: list[dict[str, Any]], *, enabled: bool,
                       clusterer: Any, root: str | Path | None,
                       ocelot_root: str | Path | None, mode: str,
                       timeout: int) -> dict[str, Any]:
    """Opt-in clustering of model candidates; off is an exact no-op.

    Only ``ozagia.core.ocelot_code.cluster_section`` is used. It never
    installs, downloads, calls a model or executes target code; absence or
    failure leaves the candidates unchanged and records the reason.
    """
    if not enabled and clusterer is None:
        return _empty_ocelot(
            "Ocelot clustering was not requested; model candidates are unchanged.")
    try:
        from ozagia.core.ocelot_code import cluster_section
        section = cluster_section(
            candidates, root=root, ocelot_root=ocelot_root, mode=mode,
            timeout=timeout, clusterer=clusterer)
    except Exception as exc:  # noqa: BLE001 - a review must report, not crash
        reason = _safe_metadata(f"{type(exc).__name__}: {exc}")
        return {
            "requested": True, "available": False, "state": "error",
            "engine": "exact", "clusters": [], "cluster_count": 0,
            "truth_assessed": False,
            "error": reason or "Ocelot clustering failed",
            "note": ("Ocelot clustering could not run; model candidates are "
                     "unchanged. No install or download was attempted."),
        }
    safe = _redact_value(section)
    if not isinstance(safe, dict):
        return {
            "requested": True, "available": False, "state": "error",
            "engine": "exact", "clusters": [], "cluster_count": 0,
            "truth_assessed": False,
            "error": "Ocelot clustering returned an invalid section",
            "note": ("Ocelot clustering could not run; model candidates are "
                     "unchanged. No install or download was attempted."),
        }
    safe.setdefault("requested", True)
    safe["truth_assessed"] = False
    if not safe.get("note") and safe.get("error"):
        safe["note"] = str(safe["error"])
    return safe


def _resolve_candidates(
        runs: Iterable[Mapping[str, Any]], valid_ids: set[str],
        stats: dict[str, int]) -> list[dict[str, Any]]:
    """Validate, redact, map and deduplicate model candidates deterministically."""
    candidates: dict[str, dict[str, Any]] = {}
    unknown_keys_by_candidate: dict[str, set[str]] = {}
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        raw_role = run.get("role")
        role = _safe_metadata(raw_role) or "unknown"
        subject = _role_subject(raw_role)
        reviewer = _run_reviewer(run)
        model = _safe_metadata(run.get("model"))
        output = run.get("output")
        items = output.get("findings") if isinstance(output, Mapping) else None
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items[:MAX_FINDINGS_PER_RESPONSE]):
            if _item_errors(index, item):
                stats["rejected"] += 1
                continue
            wording = _safe_text(item.get("wording"), MAX_WORDING_LENGTH)
            issue_type = _safe_text(item.get("issue_type"),
                                    MAX_ISSUE_TYPE_LENGTH)
            if not wording or not issue_type:
                stats["rejected"] += 1
                continue
            severity = str(item.get("severity"))
            citations = [key.strip() for key in item.get("citation_keys", [])
                         if isinstance(key, str) and key.strip()]
            accepted_keys = sorted(set(citations) & valid_ids)
            unknown_keys = sorted(set(citations) - valid_ids)
            stats["unknown_citations"] += len(unknown_keys)
            stats["accepted"] += 1
            candidate_id = _candidate_id(wording, accepted_keys)
            provenance = {"role": role}
            if reviewer:
                provenance["reviewer"] = reviewer
            if model:
                provenance["model"] = model
            recommendation = _safe_text(item.get("recommendation"),
                                        MAX_RECOMMENDATION_LENGTH) or ""
            if candidate_id not in candidates:
                candidates[candidate_id] = {
                    "id": candidate_id,
                    "subject": subject,
                    "type": issue_type,
                    "title": wording,
                    "severity": "note",
                    "candidate_severity": severity,
                    "candidate_severities": [severity],
                    "confidence": "unknown",
                    "evidence_kind": "inferred",
                    "status": "open",
                    "locations": [],
                    "evidence": [{
                        "kind": "model",
                        "source": "ozagia.harness.review",
                        "method": "independent_role_review",
                        **({"reviewer": reviewer} if reviewer else {}),
                        **({"model": model} if model else {}),
                        "role": role,
                        "citation_ids": accepted_keys,
                        "unknown_citation_count": len(unknown_keys),
                        "provenance": [provenance],
                    }],
                    "impact": wording,
                    "recommendation": recommendation,
                    "scope": ("Model-generated candidate; target needs "
                              "independent review."),
                    "effort": "unknown",
                    "owner": None,
                    "tags": ["model-review"],
                    "related_ids": accepted_keys,
                    "truth_assessed": False,
                    "assertions": [_safe_assertion(assertion)
                                   for assertion in item.get("assertions", [])],
                }
                unknown_keys_by_candidate[candidate_id] = set()
            else:
                stats["deduplicated"] += 1
                candidate = candidates[candidate_id]
                evidence = candidate["evidence"][0]
                provenance_list = evidence.setdefault("provenance", [])
                if (provenance not in provenance_list
                        and len(provenance_list) < MAX_PROVENANCE):
                    provenance_list.append(provenance)
                for key, value in (("reviewer", reviewer), ("model", model)):
                    if value and not evidence.get(key):
                        evidence[key] = value
                evidence["citation_ids"] = sorted(
                    set(evidence.get("citation_ids", [])) | set(accepted_keys))
                if not candidate.get("recommendation") and recommendation:
                    candidate["recommendation"] = recommendation
                candidate_assertions = candidate.setdefault("assertions", [])
                for assertion in item.get("assertions", []):
                    safe_assertion = _safe_assertion(assertion)
                    if (safe_assertion not in candidate_assertions
                            and len(candidate_assertions) < 4):
                        candidate_assertions.append(safe_assertion)
                severities = candidate.setdefault("candidate_severities", [])
                if severity not in severities:
                    severities.append(severity)
                if (SEVERITY_RANK.get(severity, -1)
                        > SEVERITY_RANK.get(candidate.get("candidate_severity"), -1)):
                    candidate["candidate_severity"] = severity
            unknown_keys_by_candidate[candidate_id].update(unknown_keys)
    for candidate_id, candidate in candidates.items():
        evidence = candidate["evidence"][0]
        provenance = evidence.get("provenance", [])
        evidence["provenance"] = sorted(
            provenance, key=lambda row: (
                str(row.get("role") or ""), str(row.get("reviewer") or ""),
                str(row.get("model") or "")))
        evidence["roles"] = sorted({
            str(row.get("role") or "") for row in evidence["provenance"]})
        evidence["unknown_citation_count"] = len(unknown_keys_by_candidate.get(candidate_id, ()))
        candidate["candidate_severities"] = sorted(
            set(candidate.get("candidate_severities", [])),
            key=lambda value: SEVERITY_RANK.get(value, -1))
        candidate["tags"] = sorted({
            "model-review",
            *(f"model-role:{row}" for row in evidence["roles"] if row),
        })
        candidate["related_ids"] = sorted(set(evidence.get("citation_ids", [])))
    return [candidates[key] for key in sorted(candidates)]


def run_model_pass(findings: Iterable[Mapping[str, Any]], *,
                   model_runner: Callable[..., Any], models: Iterable[str] | str,
                   roles: Iterable[str] | str = DEFAULT_ROLES,
                   max_findings: int = MAX_PACKET_FINDINGS,
                   deadline_s: float | None = DEFAULT_DEADLINE_S,
                   max_requests: int | None = DEFAULT_MAX_REQUESTS,
                   max_tokens: int | None = None,
                   max_cost_usd: float | None = None,
                   ocelot: bool | Callable[..., Mapping[str, Any]] = False,
                   ocelot_clusterer: Any = None,
                   root: str | Path | None = None,
                   ocelot_root: str | Path | None = None,
                   ocelot_mode: str = "auto",
                   ocelot_timeout: int = 60) -> dict[str, Any]:
    """Run injected independent reviewers and return separate candidate data.

    Runner/provider failures are captured in the result so callers can retain
    the deterministic report. Model claims are always non-blocking and carry
    `truth_assessed: false`; a separate deterministic check or person must
    promote them. The pass is bounded by ``deadline_s`` and ``max_requests``,
    plus optional token/cost caps when the harness reports usage. A bound
    returns the partial result with an explicit ``stage: budget`` error. The
    deadline is cooperative between requests: an in-flight provider call is
    not preempted.
    """
    from ozagia.harness.review import run_review_matrix

    if isinstance(max_findings, bool) or not isinstance(max_findings, int) or max_findings < 0:
        raise ValueError("max_findings must be an integer >= 0")
    if deadline_s is not None and (
            isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float))
            or not 0 <= deadline_s < float("inf")):
        raise ValueError("deadline_s must be a finite number >= 0")
    if max_requests is not None and (
            isinstance(max_requests, bool) or not isinstance(max_requests, int)
            or max_requests < 0):
        raise ValueError("max_requests must be an integer >= 0")
    if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
            or max_tokens < 0):
        raise ValueError("max_tokens must be an integer >= 0")
    if max_cost_usd is not None and (
            isinstance(max_cost_usd, bool)
            or not isinstance(max_cost_usd, (int, float))
            or not 0 <= max_cost_usd < float("inf")):
        raise ValueError("max_cost_usd must be a finite number >= 0")
    if isinstance(ocelot_timeout, bool) or not isinstance(ocelot_timeout, int) \
            or ocelot_timeout <= 0:
        raise ValueError("ocelot_timeout must be a positive integer")
    role_names = _names(roles, "roles")
    model_names = _names(models, "models")
    if callable(ocelot) and ocelot_clusterer is None:
        ocelot_clusterer = ocelot
        ocelot = True

    packet_limit = min(MAX_PACKET_FINDINGS, max_findings)
    # Do not materialize the caller's complete iterable just to apply a packet
    # limit. This matters for large repositories and generator-backed callers.
    # Read one extra item so an unsized iterable can still report truncation.
    source = list(islice(iter(findings), packet_limit + 1))
    packet_rows = _packet(source[:packet_limit], root=root)
    try:
        omitted = max(0, len(findings) - len(packet_rows["findings"]))
    except TypeError:
        omitted = None if len(source) > packet_limit else 0
    packet_rows["findings_omitted"] = omitted
    packet_rows["findings_omitted_unknown"] = omitted is None
    valid_ids = {str(row["id"]) for row in packet_rows["findings"] if row["id"]}

    started = time.monotonic()
    deadline = (started + float(deadline_s)
                if deadline_s is not None else None)
    jobs = [(role, model) for role in role_names for model in model_names]
    runs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stats = {"accepted": 0, "rejected": 0, "unknown_citations": 0,
             "deduplicated": 0}
    requests = 0
    tokens_used = 0.0
    cost_used = 0.0
    cost_observed = False
    usage_observed = False
    budget_reasons: list[str] = []

    def budget_error(message: str) -> None:
        if message in budget_reasons:
            return
        budget_reasons.append(message)
        errors.append({"stage": "budget", "error": message})

    for role, model in jobs:
        if deadline is not None and time.monotonic() >= deadline:
            budget_error(
                "model review deadline exceeded before all requests ran")
            break
        if max_requests is not None and requests >= max_requests:
            budget_error(
                f"model review request cap reached ({max_requests} requests)")
            break
        if max_tokens is not None and tokens_used >= max_tokens:
            budget_error(
                f"model review token budget reached ({max_tokens} tokens)")
            break
        if max_cost_usd is not None and cost_used >= max_cost_usd:
            budget_error(
                f"model review cost budget reached (${max_cost_usd:g})")
            break
        requests += 1
        try:
            result = run_review_matrix(
                {"id": "deep-review"}, packet_rows, model_runner=model_runner,
                roles=(role,), models=(model,), validator=validate_model_output,
                max_workers=1,
            )
        except Exception as exc:  # noqa: BLE001 - retain the deterministic report
            errors.append({
                "role": _safe_metadata(role),
                "model": _safe_metadata(model),
                "stage": "model",
                "error_type": _safe_metadata(type(exc).__name__) or "Exception",
                "error": "model review failed",
            })
            continue
        if not isinstance(result, Mapping):
            errors.append({
                "role": _safe_metadata(role),
                "model": _safe_metadata(model),
                "stage": "model",
                "error_type": "TypeError",
                "error": "model review failed",
            })
            continue
        job_runs = result.get("runs")
        runs.extend(job_runs if isinstance(job_runs, list) else [])
        job_errors = result.get("errors")
        for error in job_errors if isinstance(job_errors, list) else []:
            errors.append(_safe_error(error))
            _rejected_items(error, stats)
        for run in (job_runs if isinstance(job_runs, list) else []):
            if not isinstance(run, Mapping):
                continue
            run_tokens, run_cost = _usage_values(run.get("usage"))
            if isinstance(run.get("usage"), Mapping) and run.get("usage"):
                usage_observed = True
            tokens_used += run_tokens
            if run_cost is not None:
                cost_observed = True
                cost_used += run_cost

    if deadline is not None and time.monotonic() >= deadline and not any(
            "deadline" in reason for reason in budget_reasons):
        budget_error("model review deadline exceeded")
    if max_tokens is not None and tokens_used > max_tokens and not any(
            "token budget" in reason for reason in budget_reasons):
        budget_error(f"model review token budget exceeded ({max_tokens} tokens)")
    if max_cost_usd is not None and cost_used > max_cost_usd and not any(
            "cost budget" in reason for reason in budget_reasons):
        budget_error(
            f"model review cost budget exceeded (${max_cost_usd:g})")

    candidate_runs: list[Mapping[str, Any]] = []
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        validation = validate_model_output(run.get("output"))
        if not validation.get("valid"):
            rejected = validation.get("rejected")
            accepted = validation.get("accepted")
            if isinstance(rejected, int) and not isinstance(rejected, bool):
                stats["rejected"] += max(0, rejected)
            if isinstance(accepted, int) and not isinstance(accepted, bool):
                stats["rejected"] += max(0, accepted)
            errors.append(_safe_error({
                "role": run.get("role"), "model": run.get("model"),
                "stage": "validation", "validation": validation,
            }))
            continue
        candidate_runs.append(run)
    candidates = _resolve_candidates(candidate_runs, valid_ids, stats)
    ocelot_section = _ocelot_candidates(
        candidates, enabled=bool(ocelot), clusterer=ocelot_clusterer,
        root=root, ocelot_root=ocelot_root, mode=ocelot_mode,
        timeout=ocelot_timeout)
    safe_runs = [_safe_run(run) for run in runs]
    status = ("complete" if not errors else
              "partial" if runs else "unavailable")
    elapsed = time.monotonic() - started
    return {
        "status": status,
        "runs": safe_runs,
        "errors": errors,
        "findings": candidates,
        "validation": {
            "accepted": stats["accepted"],
            "rejected": stats["rejected"],
            "unknown_citations": stats["unknown_citations"],
            "deduplicated": stats["deduplicated"],
            "responses": len(runs),
        },
        "budgets": {
            "deadline_s": deadline_s,
            "max_requests": max_requests,
            "max_tokens": max_tokens,
            "max_cost_usd": max_cost_usd,
            "requests": requests,
            "elapsed_s": round(elapsed, 6),
            "tokens_used": int(tokens_used),
            "cost_usd": cost_used if cost_observed else None,
            "usage_observed": usage_observed,
            "exceeded": bool(budget_reasons),
            "reasons": list(budget_reasons),
        },
        "ocelot": ocelot_section,
        "packet_findings": len(packet_rows["findings"]),
        "packet_omitted": packet_rows["findings_omitted"],
        "packet_omitted_unknown": packet_rows["findings_omitted_unknown"],
        "truth_assessed": False,
    }
