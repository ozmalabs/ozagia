"""Stable finding identities and JSON contract helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

SUBJECTS = ("security", "performance", "failure-modes", "data-structures",
            "scalability", "architecture", "tests-and-coverage", "dependencies",
            "operations", "documents-and-api", "correctness")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def finding_id(subject: str, finding: Mapping[str, Any]) -> str:
    """Return an ID independent of line movement and collector ordering."""
    # Prefer explicit rule/location keys supplied by collectors. Older sources
    # often provide only structured locations, so derive the same stable
    # identity from path + symbol (never line/column, which move during edits).
    stable_key = finding.get("stable_key")
    keys = stable_key if stable_key else (finding.get("keys") or finding.get("paths") or [])
    if isinstance(keys, (str, bytes)) or not isinstance(keys, (list, tuple, set, frozenset)):
        keys = [keys]
    if not keys:
        locations = finding.get("locations") or []
        keys = [": ".join(str(part) for part in (loc.get("path"), loc.get("symbol")) if part)
                for loc in locations if isinstance(loc, Mapping)]
    # A location-free finding must still have a meaningful, caller-provided
    # scope identity. Avoid collapsing unrelated findings of the same type.
    if not keys:
        scope = finding.get("scope") or finding.get("rule_key") or finding.get("rule")
        if scope:
            keys = [str(scope)]
    # Some repository-wide collectors have neither a path nor a named scope.
    # Including only subject + type would make every such finding collide and
    # be silently merged by the report normalizer. Use the caller's stable
    # descriptive text as the last-resort discriminator.
    if not keys:
        description = finding.get("summary") or finding.get("message")
        if description:
            keys = [f"description:{description}"]
    # Keys are location/rule identities. Normalize their representation and
    # order, so equivalent findings from collectors with different iteration
    # order retain the same ID.
    keys = sorted({str(key).strip() for key in keys if str(key).strip()})
    rule_key = "" if stable_key else finding.get("rule_key", finding.get("rule", ""))
    if rule_key and not stable_key:
        keys = sorted({*keys, f"rule:{rule_key}"})
    seed = [subject, finding.get("type", "unknown"), rule_key, keys]
    digest = hashlib.sha256(canonical_json(seed).encode()).hexdigest()[:16]
    return f"dr-{digest}"


def cross_subject_identity(finding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Return a conservative key for merging the same typed rule observation.

    A supplied stable key is authoritative within its finding type. Otherwise,
    merging requires both a rule key and stable location keys. Unanchored
    findings remain independent.
    """
    stable_key = finding.get("stable_key")
    if stable_key:
        return ("stable", str(finding.get("type", "unknown")),
                str(stable_key).strip())
    keys = finding.get("keys") or finding.get("paths") or []
    if isinstance(keys, (str, bytes)):
        keys = [keys]
    normalized = tuple(sorted({str(key).strip() for key in keys
                               if str(key).strip()}))
    rule = finding.get("rule_key", finding.get("rule", ""))
    if not normalized or not rule:
        return None
    return ("rule", str(finding.get("type", "unknown")), str(rule), normalized)
