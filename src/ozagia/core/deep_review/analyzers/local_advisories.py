"""Bounded local advisory matching against caller-supplied advisory data.

The data source is never downloaded. A match requires a parsed lockfile pin
and either an exact tuple or a supported OSV introduced/fixed or last_affected range. A no-match
result does not mean dependencies are free of vulnerabilities; the supplied
data may be incomplete or out of date.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ozagia.core.security import redact_text

SCHEMA = "ozagia.local_advisories.v1"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_ADVISORIES = 10_000
MAX_MATCHES = 2_000
MAX_VERSIONS_PER_ADVISORY = 100
MAX_MAPPING_NODES = 100_000
MAX_JSON_DEPTH = 64
MAX_SUMMARY_LENGTH = 500
ECOSYSTEMS = frozenset({"python", "node", "rust", "go", "ruby",
                        "composer", "maven"})
SEVERITIES = frozenset({"blocking", "major", "minor", "note"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PACKAGE = re.compile(r"[!-~]{1,240}\Z")
_PEP440_VERSION = re.compile(
    r"(?i)v?\d+(?:!\d+)?(?:\.\d+)*"
    r"(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?"
    r"(?:(?:[-_.]?post|[-_.]?rev|[-_.]?r|-(?=\d))[-_.]?\d*)?"
    r"(?:[-_.]?dev[-_.]?\d*)?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?\Z")
_SEMVER_VERSION = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
_GO_VERSION = re.compile(r"v" + _SEMVER_VERSION.pattern)
_RUBY_VERSION = re.compile(
    r"\d+(?:\.\d+)*(?:[-.][A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*)?\Z")
_RUBY_PACKAGE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?:\.[A-Za-z0-9_-]+)*\Z")
_COMPOSER_VERSION = re.compile(
    r"(?i)v?\d+(?:\.\d+)*(?:[-.]?(?:dev|alpha\d*|a\d*|beta\d*|b\d*|"
    r"rc\d*|patch\d*|p\d+))?\Z")
_COMPOSER_PACKAGE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_MAVEN_PACKAGE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,118}:[A-Za-z0-9][A-Za-z0-9_.-]{0,118}\Z")
_MAVEN_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAVEN_UNPINNED_VERSIONS = frozenset({
    "latest", "latest.integration", "latest.release", "release", "unspecified",
})


def _exact_version(ecosystem: str, version: str) -> bool:
    if ecosystem == "python":
        return bool(_PEP440_VERSION.fullmatch(version))
    if ecosystem == "go":
        return bool(_GO_VERSION.fullmatch(version))
    if ecosystem == "ruby":
        return bool(_RUBY_VERSION.fullmatch(version))
    if ecosystem == "composer":
        return bool(_COMPOSER_VERSION.fullmatch(version))
    if ecosystem == "maven":
        return bool(_MAVEN_VERSION.fullmatch(version)
                    and version.lower() not in _MAVEN_UNPINNED_VERSIONS
                    and not version.lower().endswith("-snapshot"))
    return bool(_SEMVER_VERSION.fullmatch(version))


def _package_key(ecosystem: str, package: str) -> str:
    value = package.strip()
    if ecosystem == "python":
        # Keep matching consistent with the Python dependency inventory's
        # PEP 503 normalization; Unicode casefolding can merge distinct names.
        from ozagia.core.manifests import normalize
        value = normalize(value)
    elif ecosystem == "node":
        value = value.lower()
    elif ecosystem == "composer":
        value = value.lower()
    # RubyGems package identity is case-sensitive (Gem::NameTuple compares
    # the original name). Preserve it exactly so `rack` advisories cannot
    # accidentally match a distinct `Rack` gem.
    return value


def _version_key(ecosystem: str, version: str) -> str:
    value = version.strip()
    # Go lock metadata uses module versions prefixed with v. Mirror the
    # inventory's existing Go normalization and do no range interpretation.
    if ecosystem == "go" and value.startswith("v"):
        value = value[1:]
    if ecosystem == "composer":
        value = value.lower().removeprefix("v")
    return value


def _semver_parts(value: str) -> tuple[int, int, int, tuple[tuple[int, int | str], ...]]:
    """Parse strict SemVer for safe local range ordering."""
    if not _SEMVER_VERSION.fullmatch(value):
        raise ValueError("range versions must be strict SemVer")
    core, _, build = value.partition("+")
    core, dash, prerelease = core.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    identifiers: list[tuple[int, int | str]] = []
    if dash:
        for item in prerelease.split("."):
            if item.isdigit():
                identifiers.append((0, int(item)))
            else:
                identifiers.append((1, item))
    return major, minor, patch, tuple(identifiers)


def _semver_compare(left: str, right: str) -> int:
    a, b = _semver_parts(left), _semver_parts(right)
    if a[:3] != b[:3]:
        return (a[:3] > b[:3]) - (a[:3] < b[:3])
    ap, bp = a[3], b[3]
    if not ap or not bp:
        return (bool(ap) < bool(bp)) - (bool(ap) > bool(bp))
    for x, y in zip(ap, bp):
        if x != y:
            return (x > y) - (x < y)
    return (len(ap) > len(bp)) - (len(ap) < len(bp))


def _validate_semver_events(ecosystem: str, value: Any, index: int
                            ) -> list[tuple[str, str, bool]]:
    if ecosystem not in {"node", "rust", "go"}:
        raise ValueError(
            f"advisories[{index}].affected_ranges is unsupported for this ecosystem")
    if not isinstance(value, list) or not value or len(value) > 200:
        raise ValueError(f"advisories[{index}].affected_ranges is invalid")
    ranges: list[tuple[str, str, bool]] = []
    for range_index, block in enumerate(value):
        events = block.get("events") if isinstance(block, Mapping) else None
        if isinstance(block, Mapping) and block.get("type", "SEMVER") != "SEMVER":
            raise ValueError("OSV range type must be SEMVER")
        if not isinstance(events, list) or not events or len(events) > 200:
            raise ValueError(
                f"advisories[{index}].affected_ranges[{range_index}] is invalid")
        # Limits constrain the entire OSV range, independently of timeline
        # events. Multiple limits form a union, hence the greatest finite limit.
        limits: list[str] = []
        infinite_limit = False
        timeline = []
        for event in events:
            if isinstance(event, Mapping) and set(event) == {"limit"}:
                limit = event["limit"]
                if limit == "*":
                    infinite_limit = True
                    continue
                if not isinstance(limit, str):
                    raise ValueError("OSV limit must be strict SemVer or '*'")
                if ecosystem == "go":
                    if not limit.startswith("v"):
                        raise ValueError("Go OSV limit must have a v prefix")
                    limit = limit[1:]
                if not _SEMVER_VERSION.fullmatch(limit):
                    raise ValueError("OSV limit must be strict SemVer or '*'")
                limits.append(limit)
            else:
                timeline.append(event)
        block_ranges: list[tuple[str, str, bool]] = []
        start: str | None = None
        last_kind: str | None = None
        previous_fixed: str | None = None
        closing_kind: str | None = None
        for event in timeline:
            if not isinstance(event, Mapping) or len(event) != 1:
                raise ValueError("OSV range events must contain one introduced/fixed/last_affected key")
            kind, version = next(iter(event.items()))
            if kind not in {"introduced", "fixed", "last_affected"} or not isinstance(version, str):
                raise ValueError("OSV range supports only introduced/fixed/last_affected SemVer events")
            if ecosystem == "go" and version != "0":
                if not version.startswith("v"):
                    raise ValueError("Go OSV range versions must have a v prefix")
                version = version[1:]
            elif ecosystem != "go" and version.startswith("v"):
                raise ValueError("OSV range versions must be strict SemVer")
            if kind == "introduced":
                if start is not None or last_kind == "introduced":
                    raise ValueError("OSV range events must alternate introduced/fixed")
                if version == "0":
                    start = "0.0.0-0"
                elif _SEMVER_VERSION.fullmatch(version):
                    start = version
                else:
                    raise ValueError("OSV introduced event must be strict SemVer or '0'")
                # OSV encodes ranges as an ordered event stream. Accepting a
                # later interval that starts before the previous fix would
                # silently reinterpret malformed source data as the union of
                # intervals. Equal boundaries are ambiguous as well.
                if (previous_fixed is not None
                        and _semver_compare(start, previous_fixed) <= 0):
                    raise ValueError(
                        "OSV introduced event must follow the previous fixed event")
            else:
                if start is None or last_kind != "introduced":
                    raise ValueError("OSV range ending must follow introduced")
                if closing_kind is not None and closing_kind != kind:
                    raise ValueError("OSV range cannot mix fixed and last_affected")
                closing_kind = kind
                if not _SEMVER_VERSION.fullmatch(version):
                    raise ValueError("OSV range ending must be strict SemVer")
                comparison = _semver_compare(version, start)
                if comparison < 0 or (comparison == 0 and kind == "fixed"):
                    raise ValueError("OSV range ending must follow introduced")
                block_ranges.append((start, version, kind == "last_affected"))
                previous_fixed = version
                start = None
            last_kind = kind
        if start is not None:
            block_ranges.append((start, "", False))
        elif last_kind not in {"fixed", "last_affected"}:
            raise ValueError("OSV range must begin with introduced")
        cap = None
        if limits and not infinite_limit:
            cap = limits[0]
            for candidate in limits[1:]:
                if _semver_compare(candidate, cap) > 0:
                    cap = candidate
        for start, end, inclusive in block_ranges:
            if cap is not None:
                if _semver_compare(start, cap) >= 0:
                    continue
                if not end or _semver_compare(end, cap) >= 0:
                    end, inclusive = cap, False
            ranges.append((start, end, inclusive))
    return ranges


def _in_semver_ranges(version: str, ranges: list[tuple[str, str, bool]]) -> bool:
    for start, end, inclusive in ranges:
        if (_semver_compare(version, start) >= 0
                and (not end or _semver_compare(version, end) < 0
                     or (inclusive and _semver_compare(version, end) == 0))):
            return True
    return False


def _load(source: str | Path | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(source, Mapping):
        _check_mapping_bounds(source)
        try:
            raw = json.dumps(source, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
            raise ValueError("local advisory data must be JSON-compatible") from exc
    else:
        path = Path(source).expanduser()
        descriptor = None
        try:
            # Open nonblocking and inspect the opened descriptor so a FIFO or
            # device cannot hang the offline report. Read at most limit + 1;
            # a stat-then-read check could race with file growth.
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("local advisory data must be a regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_SOURCE_BYTES + 1)
        except OSError as exc:
            raise ValueError("local advisory data could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("local advisory data exceeds the size limit")
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("local advisory data exceeds the size limit")

    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def reject_non_json_constant(value):
        raise ValueError(f"non-JSON numeric constant: {value}")

    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=object_without_duplicates,
            parse_constant=reject_non_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError,
            RecursionError) as exc:
        raise ValueError("local advisory data is not valid JSON") from exc
    _check_mapping_bounds(document)
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"local advisory data schema must be {SCHEMA}")
    rows = document.get("advisories")
    if not isinstance(rows, list) or len(rows) > MAX_ADVISORIES:
        raise ValueError("local advisory data must have a bounded advisories list")
    return document, hashlib.sha256(raw).hexdigest()


def _check_mapping_bounds(source: Mapping[str, Any]) -> None:
    """Bound an in-memory JSON object before serializing it for hashing."""
    stack: list[tuple[Any, int]] = [(source, 0)]
    nodes = chars = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_MAPPING_NODES:
            raise ValueError("local advisory data exceeds the item limit")
        if depth > MAX_JSON_DEPTH:
            raise ValueError("local advisory data exceeds the nesting limit")
        if isinstance(value, Mapping):
            if nodes + len(stack) + len(value) > MAX_MAPPING_NODES:
                raise ValueError("local advisory data exceeds the item limit")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("local advisory object keys must be strings")
                chars += len(key)
                if chars > MAX_SOURCE_BYTES:
                    raise ValueError("local advisory data exceeds the size limit")
                stack.append((item, depth + 1))
        elif isinstance(value, (list, tuple)):
            if nodes + len(stack) + len(value) > MAX_MAPPING_NODES:
                raise ValueError("local advisory data exceeds the item limit")
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            chars += len(value)
        elif isinstance(value, bool) or value is None:
            continue
        elif isinstance(value, int):
            if value.bit_length() > MAX_SOURCE_BYTES * 4:
                raise ValueError("local advisory integer exceeds the size limit")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("local advisory numbers must be finite")
        else:
            raise ValueError("local advisory data must be JSON-compatible")
        if chars > MAX_SOURCE_BYTES:
            raise ValueError("local advisory data exceeds the size limit")


def match_local_advisories(
        inventory: Mapping[str, Any],
        source: str | Path | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Match explicit advisory records to exact versions from lockfiles.

    Advisory rows use either ``affected_versions`` or OSV-style
    ``affected_ranges`` introduced/fixed/last_affected SemVer events (Node, Rust and Go
    only). Ruby, Composer and Maven support exact versions only. Only parsed lockfile
    pins match.
    """
    lockfiles = inventory.get("lockfiles")
    pins: dict[tuple[str, str, str], set[str]] = {}
    composer_provenance_skipped = 0
    cargo_provenance_skipped = 0
    npm_provenance_skipped = 0
    python_provenance_skipped = 0
    maven_provenance_unknown = 0
    maven_unknown_pins: set[tuple[str, str, str]] = set()
    if isinstance(lockfiles, list):
        for lock in lockfiles:
            if not isinstance(lock, Mapping):
                continue
            ecosystem = lock.get("ecosystem")
            packages = lock.get("packages")
            if (not isinstance(ecosystem, str) or ecosystem not in ECOSYSTEMS
                    or not isinstance(packages, list)):
                continue
            if ecosystem == "composer" and lock.get("advisory_matchable") is False:
                composer_provenance_skipped += len(packages)
                continue
            for row in packages:
                if not isinstance(row, Mapping):
                    continue
                name, version = row.get("name"), row.get("version")
                if (ecosystem == "python" and row.get("provenance") in
                        {"git", "workspace-or-path", "direct-url"}):
                    python_provenance_skipped += 1
                    continue
                # Cargo package names and versions are not globally unique
                # across registries, Git forks, and local workspace sources.
                if (ecosystem == "rust" and row.get("provenance") is not None
                        and row.get("provenance") != "crates-io"):
                    cargo_provenance_skipped += 1
                    continue
                if (ecosystem == "node" and lock.get("format") == "npm"
                        and row.get("provenance") is not None
                        and row.get("provenance") != "npm-registry"):
                    npm_provenance_skipped += 1
                    continue
                if ecosystem == "composer" and row.get("provenance") == "path":
                    composer_provenance_skipped += 1
                    continue
                if ecosystem == "maven" and row.get("provenance") == "repository-unknown":
                    maven_provenance_unknown += 1
                # Gradle lockfiles identify coordinates and versions but omit
                # repository URLs. Keep provenance unknown in inventory while
                # allowing exact coordinate advisory matching.
                if ecosystem == "ruby" and row.get("source") != "rubygems":
                    continue
                if (not isinstance(name, str) or not isinstance(version, str)
                        or not _exact_version(ecosystem, version)):
                    continue
                key = (ecosystem, _package_key(ecosystem, name),
                       _version_key(ecosystem, version))
                pins.setdefault(key, set()).add(str(lock.get("file") or ""))
                if ecosystem == "maven" and row.get("provenance") == "repository-unknown":
                    maven_unknown_pins.add(key)

    if source is None:
        return {"state": "unknown", "source_hash": None,
                "advisories_loaded": 0, "pinned_packages": len(pins),
                "matched_packages": 0, "findings": [],
                "not_checked": ("no local advisory data was supplied; CVE state is unknown "
                                "for every dependency")}
    document, digest = _load(source)
    rows = document["advisories"]
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"advisories[{index}] must be an object")
        identifier, ecosystem, package = (row.get("id"), row.get("ecosystem"),
                                          row.get("package"))
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise ValueError(f"advisories[{index}].id is invalid")
        if identifier in seen_ids:
            raise ValueError("local advisory IDs must be unique")
        seen_ids.add(identifier)
        if not isinstance(ecosystem, str) or ecosystem not in ECOSYSTEMS:
            raise ValueError(f"advisories[{index}].ecosystem is unsupported")
        if (not isinstance(package, str) or not package.isascii()
                or not _PACKAGE.fullmatch(package)):
            raise ValueError(f"advisories[{index}].package is invalid")
        if ecosystem == "ruby" and not _RUBY_PACKAGE.fullmatch(package):
            raise ValueError(f"advisories[{index}].package is invalid")
        if ecosystem == "composer" and not _COMPOSER_PACKAGE.fullmatch(package):
            raise ValueError(f"advisories[{index}].package is invalid")
        if ecosystem == "maven" and not _MAVEN_PACKAGE.fullmatch(package):
            raise ValueError(f"advisories[{index}].package is invalid")
        versions: set[str] = set()
        ranges: list[tuple[str, str, bool]] = []
        if ("affected_versions" in row) == ("affected_ranges" in row):
            raise ValueError(
                f"advisories[{index}] must have exactly one affected_versions or affected_ranges")
        if "affected_versions" in row:
            affected = row["affected_versions"]
            if (not isinstance(affected, list) or not affected
                    or len(affected) > MAX_VERSIONS_PER_ADVISORY):
                raise ValueError(
                    f"advisories[{index}].affected_versions is invalid")
            for version in affected:
                if (not isinstance(version, str)
                        or not _exact_version(ecosystem, version)):
                    raise ValueError(
                        f"advisories[{index}].affected_versions must contain exact versions")
                versions.add(_version_key(ecosystem, version))
        else:
            ranges = _validate_semver_events(
                ecosystem, row["affected_ranges"], index)
        severity = row.get("severity", "minor")
        if not isinstance(severity, str) or severity not in SEVERITIES:
            raise ValueError(f"advisories[{index}].severity is invalid")
        summary = row.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError(f"advisories[{index}].summary must be a string")
        validated.append({
            "id": identifier,
            "ecosystem": ecosystem,
            "package": package,
            "package_key": _package_key(ecosystem, package),
            "versions": versions,
            "ranges": ranges,
            "severity": severity,
            "summary": redact_text(summary.strip())[:MAX_SUMMARY_LENGTH],
        })

    pins_by_package: dict[tuple[str, str], list[tuple[str, set[str]]]] = {}
    for (ecosystem, package, version), files in pins.items():
        pins_by_package.setdefault((ecosystem, package), []).append((version, files))

    findings: list[dict[str, Any]] = []
    matched: set[tuple[str, str]] = set()
    truncated = False
    for advisory in validated:
        package_pins = pins_by_package.get(
            (advisory["ecosystem"], advisory["package_key"]), [])
        for version, files in sorted(package_pins):
            exact = version in advisory["versions"]
            in_range = bool(advisory["ranges"]) and _in_semver_ranges(
                version, advisory["ranges"])
            if not exact and not in_range:
                continue
            if not files:
                continue
            provenance_unknown = (
                advisory["ecosystem"] == "maven"
                and (advisory["ecosystem"], advisory["package_key"], version)
                in maven_unknown_pins)
            matched.add((advisory["ecosystem"], advisory["package_key"]))
            for lockfile in sorted(files):
                is_range_match = in_range and not exact
                findings.append({
                    "section": "dependencies",
                    "type": ("local_advisory_semver_range_match"
                             if is_range_match
                             else "local_advisory_exact_version_match"),
                    # A Gradle lock records GAV but not the repository that
                    # supplied it. Keep the advisory as a visible candidate,
                    # but prevent an ambiguous-origin match from blocking CI.
                    "severity": "note" if provenance_unknown else advisory["severity"],
                    "confidence": "low" if provenance_unknown else "medium",
                    "evidence_kind": "declared",
                    "keys": [f"advisory:{advisory['id']}",
                             f"dependency:{advisory['ecosystem']}:{advisory['package_key']}:{version}",
                             f"file:{lockfile}"],
                    "locations": [{"path": lockfile}],
                    "file": lockfile,
                    "summary": (f"Local advisory {advisory['id']} "
                                f"{'range-matches' if is_range_match else 'lists exact'} "
                                f"{advisory['ecosystem']} package "
                                f"{advisory['package']} {version} as affected."
                                + (f" {advisory['summary']}"
                                   if advisory["summary"] else "")
                                + (" Repository origin is unknown, so this match "
                                   "requires coordinate provenance review."
                                   if provenance_unknown else "")),
                    "recommendation": ("Verify the advisory's provenance and "
                                       "applicability, then update or mitigate "
                                       "the pinned dependency if confirmed."
                                       if not provenance_unknown else
                                       "Verify which repository supplied this Maven coordinate and "
                                       "confirm the advisory applies before treating it as affected."),
                    **({"advisory_severity": advisory["severity"],
                        "repository_provenance": "unknown"}
                       if provenance_unknown else {}),
                    "advisory_id": advisory["id"],
                    "advisory_source_hash": "sha256:" + digest,
                    "dependency": advisory["package"],
                    "version": version,
                    "lockfile": lockfile,
                    "truth_assessed": False,
                })
                if len(findings) > MAX_MATCHES:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    findings.sort(key=lambda item: (item["advisory_id"], item["dependency"],
                                    item["version"], item["lockfile"]))
    if truncated:
        findings = findings[:MAX_MATCHES]
    return {
        "state": "matches_found" if findings else "no_exact_match_in_supplied_data",
        "source_hash": "sha256:" + digest,
        "advisories_loaded": len(validated),
        "pinned_packages": len(pins),
        "matched_packages": len(matched),
        "findings": findings,
        "truncated": truncated,
        "not_checked": (
            "CVE state is unknown outside local advisory matches: a no-match "
            "only means the supplied local advisory records did not match a supported "
            "lockfile pin; the data may be incomplete or stale."
            + (f" {composer_provenance_skipped} Composer pins were excluded because "
               "their repository provenance is custom, missing, or path-based."
               if composer_provenance_skipped else "")
            + (f" {npm_provenance_skipped} npm pins were excluded because their "
               "source is not known to be the npm registry."
               if npm_provenance_skipped else "")
            + (f" {python_provenance_skipped} Python pins were excluded because "
               "they use a direct Git, path, or URL source."
               if python_provenance_skipped else "")
            + (f" {cargo_provenance_skipped} Cargo pins were excluded because their "
               "source is not known to be crates.io."
               if cargo_provenance_skipped else "")
            + (f" {maven_provenance_unknown} Gradle lockfile pins use exact Maven "
               "coordinates, but repository origin is not recorded in the lockfile."
               if maven_provenance_unknown else "")),
    }
