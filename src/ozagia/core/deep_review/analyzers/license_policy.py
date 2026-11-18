"""Offline evaluation of declared package licenses against caller policy."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA = "ozagia.license_policy.v1"
MAX_BYTES = 64 * 1024
MAX_LICENSES = 500
MAX_RESULTS = 2_000
MAX_NODES = 10_000
MAX_LOCKFILES = 3_000
MAX_SCANNED_PACKAGES = 10_000
MAX_EXPRESSION_CHARS = 1_024
MAX_EXPRESSION_TOKENS = 256
MAX_EXPRESSION_DEPTH = 32
_SPDX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}\Z")
_EXPRESSION_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9][A-Za-z0-9.+-]{0,63})")
# Deliberately finite: unsupported SPDX identifiers remain unknown rather than
# acquiring meaning from an incomplete bundled registry.
_RECOGNIZED = frozenset({
    "0BSD", "AFL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "Apache-2.0", "Artistic-2.0", "BSD-2-Clause", "BSD-3-Clause",
    "BSL-1.0", "CC0-1.0", "CDDL-1.0", "EPL-1.0", "EPL-2.0",
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only",
    "GPL-3.0-or-later", "ISC", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0-only", "LGPL-3.0-or-later", "MIT", "MPL-2.0",
    "MS-PL", "Unlicense", "WTFPL", "Zlib",
})


def _expression_state(expression: str, allowed: set[str],
                      denied: set[str]) -> str:
    """Evaluate a bounded SPDX subset; unsupported syntax stays unknown.

    AND is denied if any component is denied and allowed only if all are
    allowed. OR is allowed only if all alternatives are allowed and denied
    only if all alternatives are denied. This conservative policy semantics
    avoids assuming which alternative a consumer will select.
    """
    if len(expression) > MAX_EXPRESSION_CHARS:
        return "unknown"
    tokens = []
    offset = 0
    while offset < len(expression):
        match = _EXPRESSION_TOKEN.match(expression, offset)
        if not match:
            if expression[offset:].strip():
                return "unknown"
            break
        tokens.append(match.group(1))
        if len(tokens) > MAX_EXPRESSION_TOKENS:
            return "unknown"
        offset = match.end()
    if not tokens:
        return "unknown"

    position = 0

    def parse_atom(depth: int) -> str:
        nonlocal position
        if depth > MAX_EXPRESSION_DEPTH or position >= len(tokens):
            raise ValueError
        token = tokens[position]
        position += 1
        if token == "(":
            value = parse_or(depth + 1)
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError
            position += 1
            return value
        if token in {"AND", "OR", "WITH", ")"}:
            raise ValueError
        if token not in _RECOGNIZED:
            return "unknown"
        if token in denied:
            return "denied"
        if token in allowed:
            return "allowed"
        return "unknown"

    def combine(operator: str, left: str, right: str) -> str:
        if operator == "AND":
            if "denied" in {left, right}:
                return "denied"
            if left == right == "allowed":
                return "allowed"
            return "unknown"
        if left == right == "allowed":
            return "allowed"
        if left == right == "denied":
            return "denied"
        return "unknown"

    def parse_and(depth: int) -> str:
        nonlocal position
        value = parse_atom(depth)
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            value = combine("AND", value, parse_atom(depth))
        return value

    def parse_or(depth: int) -> str:
        nonlocal position
        value = parse_and(depth)
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            value = combine("OR", value, parse_and(depth))
        return value

    try:
        result = parse_or(0)
        if position != len(tokens):
            return "unknown"
        return result
    except ValueError:
        return "unknown"


def _load(source: str | Path | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(source, Mapping):
        stack: list[tuple[Any, int]] = [(source, 0)]
        nodes = chars = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > MAX_NODES or depth > 16:
                raise ValueError("license policy exceeds a structural bound")
            if isinstance(item, Mapping):
                stack.extend((value, depth + 1) for value in item.values())
                chars += sum(len(key) for key in item if isinstance(key, str))
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("license policy object keys must be strings")
            elif isinstance(item, (list, tuple)):
                stack.extend((value, depth + 1) for value in item)
            elif isinstance(item, str):
                chars += len(item)
            elif item is not None and not isinstance(item, (bool, int, float)):
                raise ValueError("license policy must be JSON-compatible")
            if nodes + len(stack) > MAX_NODES or chars > MAX_BYTES:
                raise ValueError("license policy exceeds a structural bound")
        try:
            raw = json.dumps(source, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise ValueError("license policy must be JSON-compatible") from exc
    else:
        descriptor = None
        try:
            descriptor = os.open(Path(source).expanduser(),
                                 os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("license policy must be a regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_BYTES + 1)
        except OSError as exc:
            raise ValueError("license policy could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    if len(raw) > MAX_BYTES:
        raise ValueError("license policy exceeds the size limit")

    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    try:
        document = json.loads(raw.decode("utf-8"),
                              object_pairs_hook=object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as exc:
        raise ValueError("license policy is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"license policy schema must be {SCHEMA}")
    if set(document) - {"schema", "allow", "deny"}:
        raise ValueError("license policy has unsupported fields")
    for field in ("allow", "deny"):
        values = document.get(field, [])
        if (not isinstance(values, list) or len(values) > MAX_LICENSES
                or any(not isinstance(item, str) or not _SPDX_ID.fullmatch(item)
                       or item not in _RECOGNIZED for item in values)
                or len(set(values)) != len(values)):
            raise ValueError(f"license policy {field} must contain unique supported SPDX IDs")
    if not document.get("allow") and not document.get("deny"):
        raise ValueError("license policy must specify a non-empty allow or deny list")
    if set(document.get("allow", [])) & set(document.get("deny", [])):
        raise ValueError("license policy allow and deny lists must not overlap")
    return document, hashlib.sha256(raw).hexdigest()


def evaluate_license_policy(inventory: Mapping[str, Any],
                            source: str | Path | Mapping[str, Any] | None
                            ) -> dict[str, Any]:
    """Evaluate explicit lockfile license strings, leaving ambiguity unknown."""
    if source is None:
        return {"state": "unknown", "policy_hash": None, "allowed": 0,
                "denied": 0, "unknown": 0, "results": [], "findings": [],
                "truth_assessed": False,
                "not_checked": "no local license policy was supplied"}
    policy, digest = _load(source)
    allowed, denied = set(policy.get("allow", [])), set(policy.get("deny", []))
    rows = []
    findings = []
    lockfiles = inventory.get("lockfiles")
    lockfiles = lockfiles if isinstance(lockfiles, list) else []
    truncated = len(lockfiles) > MAX_LOCKFILES
    scanned_packages = 0
    for lock in lockfiles[:MAX_LOCKFILES]:
        if not isinstance(lock, Mapping) or not isinstance(lock.get("packages"), list):
            continue
        for package in lock["packages"]:
            scanned_packages += 1
            if scanned_packages > MAX_SCANNED_PACKAGES:
                truncated = True
                break
            if not isinstance(package, Mapping):
                continue
            license_text = package.get("license")
            license_id = (license_text.strip()
                          if isinstance(license_text, str) else "")
            state = _expression_state(license_id, allowed, denied)
            result = {
                "file": str(lock.get("file") or ""),
                "dependency": str(package.get("name") or ""),
                "version": str(package.get("version") or ""),
                "license": license_text if isinstance(license_text, str) else None,
                "state": state,
            }
            if state == "unknown":
                result["reason"] = ("missing, unsupported, uncovered, or "
                                    "ambiguous license expression")
            rows.append(result)
            if state == "denied":
                findings.append({
                    "section": "dependencies",
                    "type": "declared_license_denied_by_local_policy",
                    "severity": "minor", "confidence": "medium",
                    "evidence_kind": "declared",
                    "keys": [f"license-policy:{digest}",
                             f"dependency:{result['dependency']}:{result['version']}",
                             f"file:{result['file']}"],
                    "locations": [{"path": result["file"]}],
                    "file": result["file"],
                    "summary": (f"Declared license expression for dependency "
                                f"{result['dependency']} evaluates as denied "
                                "under the supplied local policy."),
                    "recommendation": ("Confirm the license declaration and "
                                       "review it against the project's policy."),
                    "license": license_id, "dependency": result["dependency"],
                    "version": result["version"], "truth_assessed": False,
                })
            if len(rows) > MAX_RESULTS:
                truncated = True
                break
        if truncated and (scanned_packages > MAX_SCANNED_PACKAGES
                          or len(rows) > MAX_RESULTS):
            break
    rows = rows[:MAX_RESULTS]
    findings = findings[:MAX_RESULTS]
    return {
        "state": "evaluated",
        "policy_hash": "sha256:" + digest,
        "allowed": sum(row["state"] == "allowed" for row in rows),
        "denied": sum(row["state"] == "denied" for row in rows),
        # Counts describe the returned (possibly truncated) records, like the
        # allowed and denied totals below. ``unknown_count`` can include the
        # one extra sentinel row that caused truncation.
        "unknown": sum(row["state"] == "unknown" for row in rows),
        "results": rows,
        "findings": findings,
        "truncated": truncated,
        "truth_assessed": False,
        "not_checked": ("Declared metadata only; no legal or compliance conclusion. "
                        "Unsupported, ambiguous and uncovered license expressions "
                        "remain unknown."),
    }
