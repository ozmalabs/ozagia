"""Validation and loading for deep-review baseline artifacts.

The original report contract stores ``findings.json`` as a JSON list. Keep
that shape supported; report directories may additionally carry the v1
manifest, whose schema is checked when present.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from .contract import SUBJECTS

MAX_BASELINE_BYTES = 32 * 1024 * 1024
MAX_BASELINE_REPORT_BYTES = 32 * 1024 * 1024
_FINDING_ID = re.compile(r"dr-[0-9a-f]{16}\Z")
_MANIFEST_SCHEMA = "ozagia.deep_review.manifest.v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _artifact_matches(directory: Path, relative: Any, expected: Any,
                      byte_limit: int) -> int | None:
    """Check one report artifact without following its final symlink.

    Coverage in a baseline is useful only while the corresponding subject
    report still matches the digest recorded by the report manifest.
    """
    if (not isinstance(relative, str) or not relative
            or not isinstance(expected, str) or not _SHA256.fullmatch(expected)):
        return None
    rel = PurePosixPath(relative)
    if (rel.is_absolute() or len(rel.parts) != 1
            or any(part in {"", ".", ".."} for part in rel.parts)):
        return None
    path = directory.joinpath(*rel.parts)
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_size > byte_limit):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_size > MAX_BASELINE_BYTES):
                return None
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = handle.read(min(1024 * 1024, byte_limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > byte_limit:
                    return None
                digest.update(chunk)
            after = os.fstat(handle.fileno())
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return None
    except OSError:
        return None
    return total if "sha256:" + digest.hexdigest() == expected else None


def _manifest_artifacts_match(directory: Path, manifest: dict[str, Any]) -> bool:
    """Validate the subject reports that justify baseline coverage claims."""
    subjects = manifest.get("subjects")
    hashes = manifest.get("output_hashes")
    if not isinstance(subjects, dict) or not isinstance(hashes, dict):
        return False
    if (len(subjects) != len(SUBJECTS)
            or set(subjects) != set(SUBJECTS)):
        return False
    total_bytes = 0
    for row in subjects.values():
        if not isinstance(row, dict):
            return False
        report = row.get("report")
        if report is None:
            if row.get("status") == "complete":
                return False
            continue
        if not isinstance(report, str):
            return False
        expected = hashes.get(report)
        report_hash = row.get("report_hash")
        # Subject summaries use the historical bare hex digest while the
        # manifest's output_hashes use the sha256: prefix.
        if (not isinstance(expected, str) or not isinstance(report_hash, str)
                or report_hash not in (expected, expected.removeprefix("sha256:"))):
            return False
        remaining = MAX_BASELINE_REPORT_BYTES - total_bytes
        if remaining <= 0:
            return False
        artifact_bytes = _artifact_matches(directory, report, expected, remaining)
        if artifact_bytes is None:
            return False
        total_bytes += artifact_bytes
    return True


class BaselineValidationError(ValueError):
    """The supplied path is not a compatible deep-review baseline."""


def validate_findings(value: Any) -> list[dict[str, Any]]:
    """Validate the backwards-compatible findings-list artifact shape."""
    if not isinstance(value, list):
        raise BaselineValidationError("findings.json must contain a list")
    seen: set[str] = set()
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise BaselineValidationError(
                f"finding {index} must be an object")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not _FINDING_ID.fullmatch(identifier):
            raise BaselineValidationError(
                f"finding {index} has an invalid deep-review ID")
        if identifier in seen:
            raise BaselineValidationError(
                f"duplicate finding ID: {identifier}")
        seen.add(identifier)
        findings.append(item)
    return findings


def _read_json(path: Path, *, limit: int = MAX_BASELINE_BYTES, with_digest: bool = False) -> Any:
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BaselineValidationError("JSON contains a duplicate object key")
            result[key] = value
        return result

    try:
        before = path.lstat()
    except OSError as exc:
        raise BaselineValidationError(f"{path.name} is missing or unreadable") from exc
    if not stat.S_ISREG(before.st_mode):
        raise BaselineValidationError(f"{path.name} must be a regular file")
    if before.st_size > limit:
        raise BaselineValidationError(
            f"{path.name} exceeds {limit // (1024 * 1024)} MiB")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise BaselineValidationError(f"{path.name} must be a regular file")
            if opened.st_size > limit:
                raise BaselineValidationError(
                    f"{path.name} exceeds {limit // (1024 * 1024)} MiB")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise BaselineValidationError(f"{path.name} changed while opening")
            payload = handle.read(limit + 1)
    except OSError as exc:
        raise BaselineValidationError(f"{path.name} is missing or unreadable") from exc
    if len(payload) > limit:
        raise BaselineValidationError(
            f"{path.name} exceeds {limit // (1024 * 1024)} MiB")
    try:
        value = json.loads(payload, object_pairs_hook=no_duplicate_keys)
        if with_digest:
            return value, "sha256:" + hashlib.sha256(payload).hexdigest()
        return value
    except BaselineValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BaselineValidationError(f"{path.name} is not valid JSON") from exc


def load_baseline_artifacts(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Load findings from a report directory or a standalone findings file.

    A directory may omit its manifest for compatibility with older report
    artifacts. If present, the manifest must be a deep-review v1 manifest.
    Standalone findings files are accepted as the historical public format.
    """
    source = Path(path)
    manifest = None
    try:
        source_stat = source.lstat()
    except OSError as exc:
        raise BaselineValidationError(
            "baseline path is missing or unreadable") from exc
    if stat.S_ISLNK(source_stat.st_mode):
        raise BaselineValidationError("baseline path must not be a symlink")
    if stat.S_ISDIR(source_stat.st_mode):
        findings_path = source / "findings.json"
        manifest_path = source / "manifest.json"
        try:
            if manifest_path.exists() or manifest_path.is_symlink():
                manifest = _read_json(manifest_path)
                if (not isinstance(manifest, dict)
                        or manifest.get("schema") != _MANIFEST_SCHEMA):
                    raise BaselineValidationError(
                        "manifest.json has an unsupported deep-review schema")
            findings, findings_digest = _read_json(findings_path, with_digest=True)
        except OSError as exc:
            raise BaselineValidationError(
                "report directory must contain readable findings.json") from exc
    else:
        findings, findings_digest = _read_json(source, with_digest=True)
    validated = validate_findings(findings)
    # Coverage is evidence only when it is bound to the exact findings bytes.
    if not (isinstance(manifest, dict)
            and isinstance(manifest.get("output_hashes"), dict)
            and manifest["output_hashes"].get("findings.json") == findings_digest
            and manifest.get("findings") == len(validated)
            and isinstance(manifest.get("subjects"), dict)
            and stat.S_ISDIR(source_stat.st_mode)
            and _manifest_artifacts_match(source, manifest)):
        manifest = None
    return validated, manifest


def load_baseline(path: str | Path) -> list[dict[str, Any]]:
    """Load the historical findings-list format, discarding coverage metadata."""
    return load_baseline_artifacts(path)[0]


__all__ = ["BaselineValidationError", "load_baseline", "load_baseline_artifacts", "validate_findings"]
