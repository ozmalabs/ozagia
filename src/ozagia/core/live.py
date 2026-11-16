"""Strict import and comparison helpers for offline live-state artifacts.

This module intentionally has no provider or network integration.  A caller
supplies a bounded JSON document and assigns its provenance at import time.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 8
MAX_IMAGES = 64
MAX_SNAPSHOTS_PER_DIFF = 512
MAX_COUNT = 1_000_000
MAX_SNAPSHOT_FILE_BYTES = 128 * 1024

_TOP_LEVEL_KEYS = {
    "schema_version",
    "observed_at",
    "environment",
    "service",
    "region",
    "deployed_images",
    "desired_replicas",
    "ready_replicas",
    "target_health",
    "last_pipeline",
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROVENANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}$")
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_PIPELINE_STATUSES = {
    "canceled",
    "failed",
    "in_progress",
    "succeeded",
    "unknown",
}
_TARGET_STATUSES = {"degraded", "healthy", "unknown", "unhealthy"}


class SnapshotError(ValueError):
    """A supplied snapshot does not match the bounded import schema."""


@dataclass(frozen=True)
class DeployedImage:
    name: str
    tag: str | None = None
    digest: str | None = None


@dataclass(frozen=True)
class TargetHealth:
    status: str | None = None
    healthy: int | None = None
    unhealthy: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class PipelineRun:
    name: str
    status: str
    at: str


@dataclass(frozen=True)
class LiveSnapshot:
    schema_version: int
    observed_at: str
    provenance: str
    environment: str
    service: str
    region: str | None
    deployed_images: tuple[DeployedImage, ...]
    desired_replicas: int
    ready_replicas: int
    target_health: TargetHealth
    last_pipeline: PipelineRun | None = None


@dataclass(frozen=True)
class SnapshotDifference:
    environment: str
    service: str
    change: str
    changed_fields: tuple[str, ...] = ()


def import_snapshot(text: str, *, provenance: str) -> LiveSnapshot:
    """Parse a strict, bounded snapshot document and attach caller provenance.

    The artifact contains no source metadata: provenance is supplied separately
    so it cannot silently claim a trusted source.
    """
    if not isinstance(text, str):
        raise SnapshotError("snapshot must be text")
    try:
        byte_size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SnapshotError("snapshot is not valid UTF-8 text") from exc
    if byte_size > MAX_DOCUMENT_BYTES:
        raise SnapshotError("snapshot exceeds the document size limit")
    if not isinstance(provenance, str) or not _PROVENANCE_RE.fullmatch(provenance):
        raise SnapshotError("provenance must be a safe identifier")
    _check_json_depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except SnapshotError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SnapshotError("snapshot is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SnapshotError("snapshot must be a JSON object")

    unknown = set(value) - _TOP_LEVEL_KEYS
    if unknown:
        raise SnapshotError("snapshot contains an unknown top-level field")
    required = _TOP_LEVEL_KEYS - {"region", "last_pipeline"}
    if required - value.keys():
        raise SnapshotError("snapshot is missing a required field")
    version = value["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise SnapshotError("unsupported snapshot schema version")

    observed_at = _timestamp(value["observed_at"], "observed_at")
    environment = _identifier(value["environment"], "environment")
    service = _identifier(value["service"], "service")
    region = value.get("region")
    if region is not None and (
        not isinstance(region, str) or not _REGION_RE.fullmatch(region)
    ):
        raise SnapshotError("region must be a safe region identifier")

    images_value = value["deployed_images"]
    if not isinstance(images_value, list) or len(images_value) > MAX_IMAGES:
        raise SnapshotError("deployed_images must be a bounded JSON array")
    images = tuple(_image(item) for item in images_value)
    desired = _count(value["desired_replicas"], "desired_replicas")
    ready = _count(value["ready_replicas"], "ready_replicas")
    if ready > desired:
        raise SnapshotError("ready_replicas cannot exceed desired_replicas")

    target_health = _target_health(value["target_health"])
    last_pipeline = None
    if "last_pipeline" in value and value["last_pipeline"] is not None:
        last_pipeline = _pipeline(value["last_pipeline"])

    return LiveSnapshot(
        schema_version=version,
        observed_at=observed_at,
        provenance=provenance,
        environment=environment,
        service=service,
        region=region,
        deployed_images=images,
        desired_replicas=desired,
        ready_replicas=ready,
        target_health=target_health,
        last_pipeline=last_pipeline,
    )


def snapshot_age(snapshot: LiveSnapshot, *, now: datetime) -> timedelta:
    """Return snapshot age; a future observation has a negative age."""
    current = _aware_datetime(now, "now")
    observed = _parse_timestamp(snapshot.observed_at, "observed_at")
    return current - observed


def is_stale(snapshot: LiveSnapshot, *, now: datetime, ttl: timedelta) -> bool:
    """Report whether an imported snapshot is older than a non-negative TTL."""
    if not isinstance(ttl, timedelta) or ttl < timedelta(0):
        raise ValueError("ttl must be a non-negative timedelta")
    return snapshot_age(snapshot, now=now) > ttl


def diff_snapshots(
    old: LiveSnapshot | Iterable[LiveSnapshot],
    new: LiveSnapshot | Iterable[LiveSnapshot],
) -> tuple[SnapshotDifference, ...]:
    """Return bounded, value-free differences keyed by environment/service."""
    old_by_key = _snapshot_map(old)
    new_by_key = _snapshot_map(new)
    differences: list[SnapshotDifference] = []
    for key in sorted(old_by_key.keys() | new_by_key.keys()):
        before = old_by_key.get(key)
        after = new_by_key.get(key)
        if before is None:
            differences.append(SnapshotDifference(*key, "added"))
        elif after is None:
            differences.append(SnapshotDifference(*key, "missing"))
        else:
            fields = tuple(
                name
                for name in (
                    "region",
                    "deployed_images",
                    "desired_replicas",
                    "ready_replicas",
                    "target_health",
                    "last_pipeline",
                )
                if getattr(before, name) != getattr(after, name)
            )
            if fields:
                differences.append(SnapshotDifference(*key, "changed", fields))
    return tuple(differences)


def snapshot_to_dict(snapshot: LiveSnapshot) -> dict[str, Any]:
    """Serialize only the allowlisted snapshot fields (no provenance claim)."""
    if not isinstance(snapshot, LiveSnapshot):
        raise SnapshotError("snapshot must be a LiveSnapshot")
    return {
        "schema_version": snapshot.schema_version,
        "observed_at": snapshot.observed_at,
        "environment": snapshot.environment,
        "service": snapshot.service,
        "region": snapshot.region,
        "deployed_images": [asdict(item) for item in snapshot.deployed_images],
        "desired_replicas": snapshot.desired_replicas,
        "ready_replicas": snapshot.ready_replicas,
        "target_health": asdict(snapshot.target_health),
        "last_pipeline": (asdict(snapshot.last_pipeline)
                          if snapshot.last_pipeline is not None else None),
    }


def write_snapshot(directory: str | os.PathLike[str],
                   snapshot: LiveSnapshot) -> Path:
    """Atomically write one validated snapshot under a caller-chosen directory.

    The file name is derived only from the already-validated environment and
    service identifiers, and the payload is the allowlisted snapshot object.
    Provenance is not silently re-imported by ``read_snapshot`` unless it is
    supplied by the caller again.
    """
    if not isinstance(snapshot, LiveSnapshot):
        raise SnapshotError("snapshot must be a LiveSnapshot")
    target_dir = Path(directory)
    if target_dir.is_symlink():
        raise SnapshotError("snapshot directory must not be a symlink")
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not target_dir.is_dir():
        raise SnapshotError("snapshot path must be a directory")
    name = f"{snapshot.environment}.{snapshot.service}.json"
    if not _IDENTIFIER_RE.fullmatch(name[:-5].replace(".", "-", 1)):
        # The component identifiers are already validated; this guard keeps
        # future callers from changing the naming rule accidentally.
        raise SnapshotError("snapshot file name is not safe")
    payload = json.dumps(snapshot_to_dict(snapshot), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_SNAPSHOT_FILE_BYTES:
        raise SnapshotError("snapshot file exceeds the size limit")
    path = target_dir / name
    fd, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".tmp",
                                     dir=target_dir)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def read_snapshot(path: str | os.PathLike[str], *, provenance: str) -> LiveSnapshot:
    """Read one saved snapshot; the caller still supplies provenance."""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("snapshot path must be a regular file")
    if source.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
        raise SnapshotError("snapshot file exceeds the size limit")
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SnapshotError("snapshot file could not be read") from exc
    return import_snapshot(text, provenance=provenance)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("snapshot contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise SnapshotError("snapshot contains a non-standard JSON constant")


def _check_json_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise SnapshotError("snapshot exceeds the JSON nesting limit")
        elif char in "]}":
            depth -= 1


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise SnapshotError(f"{field} must be a safe identifier")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{field} must be an ISO timestamp")
    _parse_timestamp(value, field)
    return value


def _parse_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise SnapshotError(f"{field} must be an ISO timestamp with a timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(f"{field} must be a valid ISO timestamp") from exc
    if result.utcoffset() is None:
        raise SnapshotError(f"{field} must include a timezone")
    return result


def _aware_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _count(value: Any, field: str) -> int:
    if type(value) is not int or value < 0 or value > MAX_COUNT:
        raise SnapshotError(f"{field} must be a bounded non-negative integer")
    return value


def _image(value: Any) -> DeployedImage:
    if not isinstance(value, dict):
        raise SnapshotError("each deployed image must be an object")
    if set(value) - {"name", "tag", "digest"} or "name" not in value:
        raise SnapshotError("deployed image has an unknown or missing field")
    name = value["name"]
    if not isinstance(name, str) or not _IMAGE_NAME_RE.fullmatch(name):
        raise SnapshotError("image name must be a safe identifier")
    tag = value.get("tag")
    if tag is not None and (
        not isinstance(tag, str) or not _IMAGE_TAG_RE.fullmatch(tag)
    ):
        raise SnapshotError("image tag must be a safe identifier")
    digest = value.get("digest")
    if digest is not None and (
        not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest)
    ):
        raise SnapshotError("image digest must be a sha256 digest")
    if tag is None and digest is None:
        raise SnapshotError("deployed image must include a tag or digest")
    return DeployedImage(name=name, tag=tag, digest=digest)


def _target_health(value: Any) -> TargetHealth:
    if not isinstance(value, dict):
        raise SnapshotError("target_health must be a summary object")
    allowed = {"status", "healthy", "unhealthy", "total"}
    if set(value) - allowed or not value:
        raise SnapshotError("target_health contains an unknown or missing field")
    status = value.get("status")
    if status is not None and (
        not isinstance(status, str) or status not in _TARGET_STATUSES
    ):
        raise SnapshotError("target_health status is not supported")
    counts = {
        field: _count(value[field], f"target_health.{field}")
        if field in value
        else None
        for field in ("healthy", "unhealthy", "total")
    }
    if status is None and all(count is None for count in counts.values()):
        raise SnapshotError("target_health must include a status or a count")
    if (
        counts["total"] is not None
        and counts["healthy"] is not None
        and counts["unhealthy"] is not None
        and counts["healthy"] + counts["unhealthy"] > counts["total"]
    ):
        raise SnapshotError("target_health counts exceed total")
    return TargetHealth(status=status, **counts)


def _pipeline(value: Any) -> PipelineRun:
    if not isinstance(value, dict):
        raise SnapshotError("last_pipeline must be an object")
    if set(value) != {"name", "status", "at"}:
        raise SnapshotError("last_pipeline contains an unknown or missing field")
    name = _identifier(value["name"], "last_pipeline.name")
    status = value["status"]
    if not isinstance(status, str) or status.casefold() not in _PIPELINE_STATUSES:
        raise SnapshotError("last_pipeline.status is not supported")
    at = _timestamp(value["at"], "last_pipeline.at")
    return PipelineRun(name=name, status=status.casefold(), at=at)


def _snapshot_map(
    snapshots: LiveSnapshot | Iterable[LiveSnapshot],
) -> dict[tuple[str, str], LiveSnapshot]:
    items = (snapshots,) if isinstance(snapshots, LiveSnapshot) else snapshots
    result: dict[tuple[str, str], LiveSnapshot] = {}
    for index, snapshot in enumerate(items):
        if index >= MAX_SNAPSHOTS_PER_DIFF:
            raise ValueError("snapshot diff exceeds the item limit")
        if not isinstance(snapshot, LiveSnapshot):
            raise TypeError("diff inputs must be imported LiveSnapshot values")
        key = (snapshot.environment, snapshot.service)
        if key in result:
            raise ValueError("snapshot diff input has duplicate environment/service keys")
        result[key] = snapshot
    return result
