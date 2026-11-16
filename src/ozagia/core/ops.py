"""Append-only, bounded operations receipts for resumable work handoffs.

This module stores caller-curated facts only. It does not store commands,
prompts, command output, or arbitrary metadata, and it never executes or
replays an operation.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, overload

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
OPS_FILENAME = "records.jsonl"
MAX_RECORD_BYTES = 16 * 1024
MAX_FIELD_CHARS = 512
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_READ_RECORDS = 500
MAX_SCAN_LINES = 10_000
MAX_DIAGNOSTICS = 50
MAX_RESUME_ITEMS = 10

_SCALAR_FIELDS = ("harness", "session", "environment", "kind", "summary")
_LIST_LIMITS = {
    "findings": 16,
    "changes": 16,
    "resources": 32,
    "approvals": 16,
    "next_steps": 16,
    "source_keys": 32,
}
_REQUIRED_INPUT_FIELDS = frozenset(_SCALAR_FIELDS)
_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "timestamp",
        *_SCALAR_FIELDS,
        *_LIST_LIMITS,
    }
)
_IDENTIFIER_FIELDS = frozenset({"harness", "session", "environment", "kind", "resources", "source_keys"})

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?P<label>aws_secret_access_key|aws_access_key_id|secret[_-]?key|"
    r"access[_-]?key|authorization|credential|password|token|secret)"
    r"(?![A-Za-z0-9])(?P<separator>\s*[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>.*)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,})\b"
)
_PRIVATE_KEY = re.compile(r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*")
_PROMPT_MARKERS = (
    re.compile(r"(?i)\b(?:system|developer|user|assistant)\s+prompt\b"),
    re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b"),
    re.compile(r"(?i)^\s*(?:system|developer|assistant)\s*:"),
    re.compile(r"(?i)\b(?:conversation|chat)\s+(?:history|transcript)\b"),
)


class OpsValidationError(ValueError):
    """Raised when input is outside the bounded ops-record schema."""


@dataclass(frozen=True, eq=False)
class ReadResult(Sequence[dict[str, Any]]):
    """Validated records plus bounded, content-free corruption diagnostics.

    It behaves as a read-only sequence of records; diagnostics are available
    separately so a damaged line is never mistaken for a valid record.
    """

    records: tuple[dict[str, Any], ...]
    diagnostics: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.records)

    @overload
    def __getitem__(self, index: int) -> dict[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[dict[str, Any], ...]: ...

    def __getitem__(self, index: int | slice) -> dict[str, Any] | tuple[dict[str, Any], ...]:
        return self.records[index]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.records)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ReadResult):
            return self.records == other.records and self.diagnostics == other.diagnostics
        if isinstance(other, Sequence):
            return list(self.records) == list(other)
        return NotImplemented


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise OpsValidationError(f"{field} must be an ISO-8601 UTC timestamp")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise OpsValidationError(f"{field} must include a UTC offset")
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise OpsValidationError(f"{field} must be an ISO-8601 UTC timestamp") from exc


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _redact_text(value: str) -> str:
    value = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('label')}{match.group('separator')}[REDACTED]",
        value,
    )
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _AWS_KEY_ID.sub("[REDACTED]", value)
    value = _TOKEN_SHAPES.sub("[REDACTED]", value)
    value = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    return value


def _clean_text(value: Any, field: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str):
        raise OpsValidationError(f"{field} must be text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise OpsValidationError(f"{field} contains a control character")
    max_chars = 200 if identifier else MAX_FIELD_CHARS
    if len(value) > max_chars:
        raise OpsValidationError(f"{field} exceeds {max_chars} characters")
    cleaned = _redact_text(value)
    if len(cleaned) > max_chars:
        raise OpsValidationError(f"{field} exceeds {max_chars} characters")
    if any(marker.search(cleaned) for marker in _PROMPT_MARKERS):
        raise OpsValidationError(f"{field} contains prompt-like text")
    return cleaned


def _normalize_record(record: Mapping[str, Any], *, allow_defaults: bool) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise OpsValidationError("record must be a mapping")

    keys = set(record)
    if any(not isinstance(key, str) for key in keys) or keys - _ALLOWED_FIELDS:
        # Do not include user-supplied keys or values in exceptions.
        raise OpsValidationError("record contains an unsupported field")
    if not _REQUIRED_INPUT_FIELDS.issubset(keys):
        raise OpsValidationError("record is missing a required field")

    version = record.get("schema_version", SCHEMA_VERSION if allow_defaults else None)
    if type(version) is not int or version != SCHEMA_VERSION:
        raise OpsValidationError("unsupported schema version")

    timestamp_value = record.get("timestamp")
    if timestamp_value is None and allow_defaults:
        timestamp_value = datetime.now(timezone.utc)
    timestamp = _utc_text(_parse_timestamp(timestamp_value, field="timestamp"))

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp,
    }
    for field in _SCALAR_FIELDS:
        normalized[field] = _clean_text(
            record[field],
            field,
            identifier=field in _IDENTIFIER_FIELDS,
        )
        if not normalized[field].strip():
            raise OpsValidationError(f"{field} must not be empty")

    for field, max_items in _LIST_LIMITS.items():
        if field not in record:
            if allow_defaults:
                normalized[field] = []
            continue
        values = record[field]
        if not isinstance(values, list) or len(values) > max_items:
            raise OpsValidationError(f"{field} must be a bounded list")
        identifier = field in _IDENTIFIER_FIELDS
        normalized[field] = [
            _clean_text(item, field, identifier=identifier) for item in values
        ]
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) > MAX_RECORD_BYTES:
        raise OpsValidationError(f"record exceeds {MAX_RECORD_BYTES} bytes")
    return normalized


def _directory_fd(directory: str | os.PathLike[str], *, create: bool) -> int | None:
    try:
        raw_path = os.fspath(directory)
        if not isinstance(raw_path, str):
            raise OpsValidationError("directory must be a text path")
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            raise OpsValidationError("ops directory must not be a symlink")
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif not path.exists():
            return None
        if path.is_symlink() or not path.is_dir():
            raise OpsValidationError("ops path must be a directory")
        resolved = path.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        return os.open(resolved, flags)
    except OpsValidationError:
        raise
    except FileNotFoundError:
        if not create:
            return None
        raise


def append_record(directory: str | os.PathLike[str], record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and append one receipt to ``directory/records.jsonl``.

    The fixed filename prevents record fields from influencing paths. Appends
    are serialized between cooperating processes and fsynced before return.
    """

    normalized = _normalize_record(record, allow_defaults=True)
    payload = (
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_RECORD_BYTES + 1:
        raise OpsValidationError(f"record exceeds {MAX_RECORD_BYTES} bytes")

    directory_fd = _directory_fd(directory, create=True)
    assert directory_fd is not None
    file_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(OPS_FILENAME, flags, 0o600, dir_fd=directory_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise OpsValidationError("ops journal must be a regular, unlinked file")
        if fcntl is not None:
            fcntl.flock(file_fd, fcntl.LOCK_EX)
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short write to ops journal")
            view = view[written:]
        os.fsync(file_fd)
        if fcntl is not None:
            fcntl.flock(file_fd, fcntl.LOCK_UN)
        return normalized
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _append_diagnostic(diagnostics: list[str], message: str) -> None:
    if len(diagnostics) < MAX_DIAGNOSTICS:
        diagnostics.append(message)
    elif len(diagnostics) == MAX_DIAGNOSTICS:
        diagnostics.append("additional diagnostics omitted")


def _read_raw_lines(directory_fd: int, diagnostics: list[str]) -> list[tuple[int, bytes]]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(OPS_FILENAME, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return []
    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OpsValidationError("ops journal must be a regular file")
        size = file_stat.st_size
        offset = max(0, size - MAX_READ_BYTES)
        os.lseek(file_fd, offset, os.SEEK_SET)
        raw = os.read(file_fd, min(size, MAX_READ_BYTES))
    finally:
        os.close(file_fd)

    if not raw:
        return []

    if offset:
        _append_diagnostic(diagnostics, "older records omitted by byte limit")
        first_newline = raw.find(b"\n")
        if first_newline < 0:
            return []
        raw = raw[first_newline + 1 :]

    complete = raw.endswith(b"\n")
    pieces = raw.split(b"\n")
    if complete:
        pieces.pop()
    elif pieces:
        _append_diagnostic(diagnostics, "unterminated final journal line")
        pieces.pop()

    if len(pieces) > MAX_SCAN_LINES:
        _append_diagnostic(diagnostics, "older records omitted by line limit")
        first_line = len(pieces) - MAX_SCAN_LINES + 1
        pieces = pieces[-MAX_SCAN_LINES:]
    else:
        first_line = 1
    return [(first_line + index, line) for index, line in enumerate(pieces)]


def read_records(
    directory: str | os.PathLike[str],
    *,
    limit: int = 100,
    since: str | datetime | None = None,
) -> ReadResult:
    """Read the latest bounded records in append order.

    Missing directories and journals are empty. Corrupt lines are skipped and
    described in ``ReadResult.diagnostics`` without including their contents.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    limit = min(limit, MAX_READ_RECORDS)
    since_time = _parse_timestamp(since, field="since") if since is not None else None
    diagnostics: list[str] = []
    directory_fd = _directory_fd(directory, create=False)
    if directory_fd is None:
        return ReadResult((), ())
    records: list[dict[str, Any]] = []
    try:
        for line_number, raw_line in _read_raw_lines(directory_fd, diagnostics):
            if len(raw_line) > MAX_RECORD_BYTES:
                _append_diagnostic(diagnostics, f"line {line_number}: record exceeds byte limit")
                continue
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            try:
                parsed = json.loads(raw_line)
                if not isinstance(parsed, dict):
                    raise OpsValidationError("record is not an object")
                normalized = _normalize_record(parsed, allow_defaults=False)
                if normalized != parsed:
                    raise OpsValidationError("record is not in canonical form")
            except (UnicodeDecodeError, json.JSONDecodeError, OpsValidationError, TypeError):
                _append_diagnostic(diagnostics, f"line {line_number}: invalid ops record")
                continue
            if since_time is not None and _parse_timestamp(
                normalized["timestamp"], field="timestamp"
            ) < since_time:
                continue
            records.append(normalized)
    finally:
        os.close(directory_fd)
    return ReadResult(tuple(records[-limit:] if limit else ()), tuple(diagnostics))


def resume_summary(
    directory: str | os.PathLike[str],
    *,
    limit: int = 20,
    environment: str | None = None,
) -> dict[str, Any]:
    """Return a compact handoff; this function never replays operations."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    limit = min(limit, 100)
    if environment is not None:
        environment = _clean_text(environment, "environment", identifier=True)

    result = read_records(directory, limit=MAX_READ_RECORDS)
    selected = [
        record
        for record in result
        if environment is None or record["environment"] == environment
    ][-limit:] if limit else []

    latest = selected[-1] if selected else None
    latest_state = None
    open_items: list[str] = []
    if latest is not None:
        latest_state = {
            "timestamp": latest["timestamp"],
            "harness": latest["harness"],
            "session": latest["session"],
            "environment": latest["environment"],
            "kind": latest["kind"],
            "summary": latest["summary"],
            "findings": latest.get("findings", []),
            "source_keys": latest.get("source_keys", []),
        }
        open_items = list(dict.fromkeys(latest.get("next_steps", [])))[:MAX_RESUME_ITEMS]

    recent_changes: list[dict[str, str]] = []
    recent_approvals: list[dict[str, str]] = []
    for record in reversed(selected):
        if len(recent_changes) < MAX_RESUME_ITEMS:
            for change in reversed(record.get("changes", [])):
                recent_changes.append(
                    {
                        "timestamp": record["timestamp"],
                        "environment": record["environment"],
                        "kind": record["kind"],
                        "item": change,
                    }
                )
                if len(recent_changes) >= MAX_RESUME_ITEMS:
                    break
        if len(recent_approvals) < MAX_RESUME_ITEMS:
            for approval in reversed(record.get("approvals", [])):
                recent_approvals.append(
                    {
                        "timestamp": record["timestamp"],
                        "environment": record["environment"],
                        "kind": record["kind"],
                        "item": approval,
                    }
                )
                if len(recent_approvals) >= MAX_RESUME_ITEMS:
                    break
        if len(recent_changes) >= MAX_RESUME_ITEMS and len(recent_approvals) >= MAX_RESUME_ITEMS:
            break

    return {
        "latest_state": latest_state,
        "open_items": open_items,
        "recent_changes": recent_changes,
        "recent_approvals": recent_approvals,
        "diagnostics": list(result.diagnostics),
    }
