"""Validation and loading for deep-review baseline artifacts.

The original report contract stores ``findings.json`` as a JSON list. Keep
that shape supported; report directories may additionally carry the v1
manifest, whose schema is checked when present.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

MAX_BASELINE_BYTES = 32 * 1024 * 1024
_FINDING_ID = re.compile(r"dr-[0-9a-f]{16}\Z")
_MANIFEST_SCHEMA = "ozagia.deep_review.manifest.v1"


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


def _read_json(path: Path, *, limit: int = MAX_BASELINE_BYTES) -> Any:
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
        return json.loads(payload, object_pairs_hook=no_duplicate_keys)
    except BaselineValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BaselineValidationError(f"{path.name} is not valid JSON") from exc


def load_baseline(path: str | Path) -> list[dict[str, Any]]:
    """Load findings from a report directory or a standalone findings file.

    A directory may omit its manifest for compatibility with older report
    artifacts. If present, the manifest must be a deep-review v1 manifest.
    Standalone findings files are accepted as the historical public format.
    """
    source = Path(path)
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
            findings = _read_json(findings_path)
        except OSError as exc:
            raise BaselineValidationError(
                "report directory must contain readable findings.json") from exc
    else:
        findings = _read_json(source)
    return validate_findings(findings)


__all__ = ["BaselineValidationError", "load_baseline", "validate_findings"]
