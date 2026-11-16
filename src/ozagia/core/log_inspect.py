"""Bounded, redacted inspection of explicitly listed local logs.

The inspector reads only the files a caller names. It never walks a home
directory, a repository or a state directory on its own. Inputs may be gzip
compressed. Reading is bounded per file by bytes and lines and across a run by
the number of files.

Before a value reaches a report, it passes through :class:`Redactor`: API keys,
tokens, authorization headers, URLs with credentials, email addresses, prompt
or message payloads and file-content fields are replaced with counted
placeholders. The report never includes the replaced values.

Failures are clustered by error type and normalized message signature plus the
tool, intent, harness, model and task class that were present. Findings contain
counts, timestamps, dimensions, a likely component and a *proposed* action.
Nothing is applied, executed, uploaded or sent anywhere by this module.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import zlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1
TOOL_NAME = "log_inspect"

DEFAULT_MAX_FILES = 16
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_LINES = 20_000
MAX_FILES_CAP = 64
MAX_BYTES_CAP = 64 * 1024 * 1024
MAX_LINES_CAP = 200_000
MAX_FINDINGS = 100
MAX_OBSERVATIONS = 50_000
MAX_SIGNATURE = 240
MAX_DIMENSION = 80

SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEVERITY_WEIGHT = {
    "critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1,
}
_LEVEL_SEVERITY = {
    "critical": "critical", "crit": "critical", "fatal": "critical",
    "panic": "critical", "error": "high", "err": "high",
    "exception": "high", "traceback": "high",
    "warn": "medium", "warning": "medium",
    "info": "info", "information": "info", "debug": "info",
    "notice": "low",
}
_CONVERSATION_ROLES = frozenset({"user", "assistant", "system", "tool"})
_CONVERSATION_KINDS = frozenset({
    "message", "messages", "prompt", "completion", "assistant", "user",
    "system", "conversation", "chat",
})


class LogInspectError(ValueError):
    """The inspection request is invalid or an output cannot be written."""


@dataclass(frozen=True)
class Limits:
    """Hard bounds for one inspection run.

    Values are clamped to the module caps. ``max_bytes`` applies to each
    input after decompression; the total is implicitly ``max_files *
    max_bytes``.
    """

    max_files: int = DEFAULT_MAX_FILES
    max_bytes: int = DEFAULT_MAX_BYTES
    max_lines: int = DEFAULT_MAX_LINES

    def __post_init__(self) -> None:
        for name, value, cap in (
                ("max_files", self.max_files, MAX_FILES_CAP),
                ("max_bytes", self.max_bytes, MAX_BYTES_CAP),
                ("max_lines", self.max_lines, MAX_LINES_CAP)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
            if value > cap:
                object.__setattr__(self, name, cap)

    def to_dict(self) -> dict[str, int]:
        return {
            "max_files": self.max_files,
            "max_bytes": self.max_bytes,
            "max_lines": self.max_lines,
        }


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #
_KNOWN_KEY = re.compile(
    r"(?i)\b(?:"
    r"sk-ant-[a-z0-9_-]{8,}|sk-[a-z0-9_-]{8,}|"
    r"gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"xox[abprs]-[a-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,}"
    r")\b"
)
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_URL_CREDENTIALS = re.compile(
    r"(?i)\b(https?://)[^\s/@:]+:[^\s/@]+@")
_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|(?:(?:bearer|basic|token)\s+)?[^\s,;}]+)")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|access[_-]?token|"
    r"refresh[_-]?token|password|passwd|client[_-]?secret|private[_-]?key)"
    r"(\s*[:=]\s*)(?!<redacted:)"
    r"(\"(?:\\.|[^\"\\])*\"|'[^']*'|[^\s,;}]+)")
_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PROMPT_FIELD = re.compile(
    r"(?i)(?<![\w.-])(\"?(?:prompt|prompts|content|contents|messages?|text|"
    r"completion|input|arguments|args)\"?\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'[^']*'|[\[{].*|[^\n]+)")
_FILE_CONTENT_FIELD = re.compile(
    r"(?i)(?<![\w.-])(\"?(?:file_text|file_content|diff|patch|transcript|"
    r"stdout|stderr|output|tool_output)\"?\s*[:=]\s*)"
    r"(\"(?:\\.|[^\"\\])*\"|'[^']*'|[\[{].*|[^\n]+)")
_WINDOWS_USER = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_PREFIXED_ID = re.compile(r"\b(?:ses|run|req|err|msg|task)_[A-Za-z0-9_-]+\b")
_ABS_PATH = re.compile(
    r"(?<![\w:<])(?:/[A-Za-z0-9._~@%+=-]+){2,}/?")
_WINDOWS_PATH = re.compile(r"(?<![\w])[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*")


class Redactor:
    """Replace sensitive values and count categories, never values."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def _sub(self, pattern: re.Pattern[str], replacement: str, text: str,
             label: str) -> str:
        result, count = pattern.subn(replacement, text)
        if count:
            self.counts[label] += count
        return result

    def text(self, value: Any, *, limit: int = MAX_SIGNATURE) -> str:
        """Redact a value and bound its length."""
        text = "" if value is None else str(value)
        if not text:
            return ""
        text = self._sub(_URL_CREDENTIALS, r"\1<redacted:credentials>@",
                         text, "url_credentials")
        text = self._sub(
            _AUTHORIZATION, r"\1<redacted:authorization>", text,
            "authorization")
        text = self._sub(_BEARER, "<redacted:auth>", text, "authorization")
        text = self._sub(_KNOWN_KEY, "<redacted:api_key>", text, "api_key")
        text = self._sub(_JWT, "<redacted:token>", text, "api_key")
        text = self._sub(
            _SECRET_ASSIGN, r"\1\2<redacted:secret>", text, "secret")
        text = self._sub(_EMAIL, "<redacted:email>", text, "email")
        text = self._sub(
            _PROMPT_FIELD, r"\1<redacted:prompt>", text, "prompt")
        text = self._sub(
            _FILE_CONTENT_FIELD, r"\1<redacted:file_content>", text,
            "file_content")
        text = self._sub(_WINDOWS_USER, r"~", text, "home_path")
        text = self._sub(_WINDOWS_PATH, "<path>", text, "home_path")
        home = _home_text()
        if home and home in text:
            self.counts["home_path"] += text.count(home)
            text = text.replace(home, "~")
        text = " ".join(text.split())
        return text[:limit]

    def to_dict(self) -> dict[str, int]:
        return {key: self.counts[key] for key in sorted(self.counts)}


def _home_text() -> str:
    try:
        home = str(Path.home())
    except (KeyError, OSError, RuntimeError):
        return ""
    return home if home not in ("", "/") else ""


def _display_path(path: Path) -> str:
    text = str(path)
    home = _home_text()
    if home and (text == home or text.startswith(home + os.sep)):
        text = "~" + text[len(home):]
    return text


def _normalise(text: str) -> str:
    """A stable signature: no stack frames, ids, paths or loose numbers."""
    text = text.split("\n", 1)[0]
    text = re.sub(r"\s+", " ", text).strip()
    text = _ABS_PATH.sub("<path>", text)
    text = _WINDOWS_PATH.sub("<path>", text)
    text = _UUID.sub("<id>", text)
    text = _HEX_ID.sub("<id>", text)
    text = _PREFIXED_ID.sub("<id>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", text)
    text = re.sub(r"\s+", " ", text).strip(" :;,-")
    return text[:MAX_SIGNATURE]


_ERROR_TYPE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_.]*(?:Error|Exception))\b")
_FAILED_TYPE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_.]*(?:Failure|Failed|Blocked|Timeout))\b")


def _error_type(text: str, fallback: str) -> str:
    match = _ERROR_TYPE_RE.search(text) or _FAILED_TYPE_RE.search(text)
    if match:
        return match.group(1)
    return fallback


def _first_line(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    for marker in ("\n    at ", "\n  at ", "\n\tat "):
        if marker in text:
            text = text.split(marker, 1)[0]
            break
    return text.splitlines()[0] if text.splitlines() else ""


def _signature_text(value: Any, error_type: str) -> str:
    """First line without a leading copy of the already reported type."""
    head = _first_line(value)
    prefix = error_type.rstrip(":") + ":"
    if error_type and head.startswith(prefix):
        head = head[len(prefix):].lstrip()
    return head or _first_line(value)


def _dimension(redactor: Redactor, value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, bytes, bool)):
        return ""
    text = redactor.text(value, limit=MAX_DIMENSION)
    return text.strip().lower()


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _since_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    moment = _parse_timestamp(value)
    if moment is None:
        raise LogInspectError(f"--since is not an ISO timestamp: {value!r}")
    return moment


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _severity_from_level(value: Any) -> str:
    text = str(value or "").strip().lower()
    return _LEVEL_SEVERITY.get(text, "")


def _max_severity(left: str, right: str) -> str:
    if _SEVERITY_WEIGHT.get(right, 0) > _SEVERITY_WEIGHT.get(left, 0):
        return right
    return left


def _scalar(value: Any) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, bytes, bool)):
        return ""
    return str(value)


def _scalar_value(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            value = record[key]
            if value is not None and not isinstance(
                    value, (Mapping, list, tuple, bytes, bool)):
                return value
    return None


def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
    for key in ("ts", "timestamp", "time", "created_at", "at",
                "started_at", "finished_at"):
        if key in record:
            moment = _parse_timestamp(record[key])
            if moment is not None:
                return moment
    return None


def _looks_conversation(record: Mapping[str, Any]) -> bool:
    role = str(record.get("role") or "").strip().lower()
    kind = str(record.get("type") or record.get("kind") or "").strip().lower()
    return role in _CONVERSATION_ROLES or kind in _CONVERSATION_KINDS


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #
@dataclass
class _Observation:
    source: str
    source_kind: str
    severity: str
    error_type: str
    signature: str
    component: str
    weight: int = 1
    tool: str = ""
    intent: str = ""
    harness: str = ""
    model: str = ""
    task_class: str = ""
    timestamp: datetime | None = None
    failure_fields: tuple[str, ...] = ()
    redacted_values: int = 0

    def dimensions(self) -> dict[str, str]:
        return {
            "tool": self.tool,
            "intent": self.intent,
            "harness": self.harness,
            "model": self.model,
            "task_class": self.task_class,
        }

    def key(self) -> tuple[str, ...]:
        return (
            self.error_type, self.signature, self.tool, self.intent,
            self.harness, self.model, self.task_class,
        )


@dataclass
class _Source:
    name: str
    path: Path
    display: str
    kind: str = "unknown"
    bytes_read: int = 0
    lines_read: int = 0
    records: int = 0
    malformed: int = 0
    truncated_bytes: bool = False
    truncated_lines: bool = False
    observations_dropped: int = 0
    skipped: str | None = None
    error: str | None = None
    redactor: Redactor = field(default_factory=Redactor)

    def report(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "path": self.display,
            "kind": self.kind,
            "bytes_read": self.bytes_read,
            "lines_read": self.lines_read,
            "records": self.records,
            "malformed_lines": self.malformed,
            "truncated_bytes": self.truncated_bytes,
            "truncated_lines": self.truncated_lines,
            "redactions": self.redactor.to_dict(),
        }
        if self.observations_dropped:
            payload["observations_dropped"] = self.observations_dropped
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.error:
            payload["error"] = self.error
        return payload


def _add(observations: list[_Observation], source: _Source,
         observation: _Observation | None, since: datetime | None) -> None:
    if observation is None:
        return
    if since is not None and observation.timestamp is not None:
        if observation.timestamp < since:
            return
    if len(observations) >= MAX_OBSERVATIONS:
        source.observations_dropped += 1
        return
    observation.source = source.name
    observations.append(observation)


# --------------------------------------------------------------------------- #
# per-format parsing
# --------------------------------------------------------------------------- #
_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_.-]*)=("(?:\\.|[^"\\])*"|\S+)')
_TEXT_LEVEL_RE = re.compile(
    r"\b(CRITICAL|FATAL|PANIC|ERROR|EXCEPTION|TRACEBACK|WARN(?:ING)?|"
    r"INFO|DEBUG|NOTICE)\b", re.IGNORECASE)
_TEXT_TS_RE = re.compile(
    r"^\s*(\d{4}-\d\d-\d\d[T ][0-9:.]+(?:Z|[+-]\d\d:?\d\d)?)\s*")
_TEXT_TOOL_RE = re.compile(r"\btool[=:]\s*[\"']?([A-Za-z0-9_.-]+)")
_TEXT_INTENT_RE = re.compile(r"\bintent[=:]\s*[\"']?([A-Za-z0-9_.-]+)")
_TEXT_HARNESS_RE = re.compile(r"\bharness[=:]\s*[\"']?([A-Za-z0-9_.-]+)")
_TEXT_MODEL_RE = re.compile(r"\bmodel[=:]\s*[\"']?([A-Za-z0-9_./:-]+)")


def _unquote(token: str) -> str:
    if token.startswith('"') and token.endswith('"'):
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            value = token[1:-1].replace('\\"', '"').replace("\\n", "\n")
        return value if isinstance(value, str) else str(value)
    return token


def _parse_kv_line(line: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for match in _KV_RE.finditer(line):
        pairs[match.group(1)] = _unquote(match.group(2))
    return pairs


def _looks_like_opencode(lines: Sequence[str]) -> bool:
    for line in lines[:40]:
        if not line.strip():
            continue
        pairs = _parse_kv_line(line)
        if "timestamp" not in pairs:
            return False
        return "level" in pairs and (
            "message" in pairs or "msg" in pairs or "cause" in pairs)
    return False


def _process_opencode(source: _Source, lines: Sequence[str],
                      observations: list[_Observation],
                      since: datetime | None) -> None:
    for line in lines:
        if not line.strip():
            continue
        pairs = _parse_kv_line(line)
        level = pairs.get("level") or pairs.get("severity") or ""
        severity = _severity_from_level(level)
        if severity not in ("critical", "high", "medium"):
            continue
        message = (pairs.get("message") or pairs.get("msg")
                   or pairs.get("event") or level)
        cause = pairs.get("cause") or pairs.get("error") or ""
        error_type = _error_type(cause, "LogError" if severity != "medium"
                                 else "LogWarning")
        signature = _signature_text(cause, error_type) or message
        redactor = source.redactor
        before = redactor.total
        dims = {
            "tool": _dimension(redactor, pairs.get("tool")),
            "intent": _dimension(redactor, pairs.get("intent")),
            "harness": "opencode",
            "model": _dimension(
                redactor, pairs.get("model") or pairs.get("model_id")),
            "task_class": _dimension(
                redactor, pairs.get("task") or pairs.get("task_class")),
        }
        role = _dimension(redactor, pairs.get("role") or pairs.get("service"))
        component = "opencode" + (f" {role}" if role else "")
        source.records += 1
        _add(observations, source, _Observation(
            source=source.name, source_kind="opencode",
            severity=severity,
            error_type=redactor.text(error_type, limit=MAX_DIMENSION)
            or ("LogWarning" if severity == "medium" else "LogError"),
            signature=redactor.text(_normalise(signature)),
            component=component,
            timestamp=_parse_timestamp(pairs.get("timestamp")),
            failure_fields=(f"level={level}",),
            redacted_values=redactor.total - before,
            **dims,
        ), since)


def _process_text(source: _Source, lines: Sequence[str],
                  observations: list[_Observation],
                  since: datetime | None) -> None:
    redactor = source.redactor
    for line in lines:
        if not line.strip():
            continue
        level_match = _TEXT_LEVEL_RE.search(line)
        if level_match is None:
            continue
        level = level_match.group(1).upper()
        severity = _severity_from_level(level)
        if severity not in ("critical", "high", "medium"):
            continue
        timestamp = None
        ts_match = _TEXT_TS_RE.match(line)
        if ts_match:
            timestamp = _parse_timestamp(ts_match.group(1))
        body = line[level_match.end():].lstrip(" :;,-[]")
        error_type = _error_type(body, "LogError" if severity != "medium"
                                 else "LogWarning")
        signature = _signature_text(body, error_type) or body
        redactor = source.redactor
        before = redactor.total
        tool_match = _TEXT_TOOL_RE.search(line)
        intent_match = _TEXT_INTENT_RE.search(line)
        harness_match = _TEXT_HARNESS_RE.search(line)
        model_match = _TEXT_MODEL_RE.search(line)
        tool = _dimension(redactor, tool_match.group(1) if tool_match else "")
        component = "text log" + (f" {tool}" if tool else "")
        source.records += 1
        _add(observations, source, _Observation(
            source=source.name, source_kind="text", severity=severity,
            error_type=redactor.text(error_type, limit=MAX_DIMENSION)
            or ("LogWarning" if severity == "medium" else "LogError"),
            signature=redactor.text(_normalise(signature)),
            component=component,
            tool=tool,
            intent=_dimension(redactor,
                              intent_match.group(1) if intent_match else ""),
            harness=_dimension(
                redactor, harness_match.group(1) if harness_match else ""),
            model=_dimension(redactor,
                             model_match.group(1) if model_match else ""),
            timestamp=timestamp,
            failure_fields=(f"level={level}",),
            redacted_values=redactor.total - before,
        ), since)


def _daemon_observation(source: _Source,
                        record: Mapping[str, Any]) -> _Observation | None:
    op = _scalar(record.get("op"))
    is_error = record.get("is_error") is True
    expired = record.get("expired") is True
    unknown = str(record.get("fallback") or "") == "unknown_outcome"
    if not (is_error or expired or unknown):
        return None
    reason = str(record.get("reason") or record.get("error") or "")
    redactor = source.redactor
    fields: list[str] = []
    if is_error:
        fields.append("is_error")
        severity = "high"
        error_type = _error_type(reason, "DaemonToolError"
                                 if op == "tool" else "DaemonError")
        signature = reason or f"{op or 'request'} failed"
    elif unknown:
        fields.append("fallback=unknown_outcome")
        severity = "medium"
        error_type = "UnknownOutcome"
        signature = "after hook outcome unknown"
    else:
        fields.append("expired")
        severity = "low"
        error_type = "RequestExpired"
        signature = reason or f"{op or 'request'} expired"
    component = "daemon" + (f" {op}" if op else "")
    before = redactor.total
    tool = _dimension(redactor, record.get("tool"))
    intent = _dimension(redactor, record.get("intent"))
    return _Observation(
        source=source.name, source_kind="daemon", severity=severity,
        error_type=redactor.text(error_type, limit=MAX_DIMENSION)
        or "DaemonError",
        signature=redactor.text(_normalise(
            _signature_text(signature, error_type))),
        component=component,
        tool=tool, intent=intent,
        harness=_dimension(redactor, record.get("harness")),
        model=_dimension(redactor, record.get("model")),
        task_class=_dimension(redactor, record.get("task_class")),
        timestamp=_parse_timestamp(record.get("ts")),
        failure_fields=tuple(fields),
        redacted_values=redactor.total - before,
    )


def _record_message(record: Mapping[str, Any]) -> str:
    error = record.get("error")
    if isinstance(error, Mapping):
        for key in ("message", "msg", "reason", "name", "type"):
            value = _scalar(error.get(key))
            if value:
                return value
    for key in ("error_message", "message", "msg", "reason", "detail",
                "exception"):
        value = _scalar(record.get(key))
        if value:
            return value
    return _scalar(error)


def _generic_observation(source: _Source,
                         record: Mapping[str, Any]) -> _Observation | None:
    if _looks_conversation(record):
        return None
    level = _scalar_value(
        record, "level", "severity", "lvl", "log_level", "status")
    severity = _severity_from_level(level)
    fields: list[str] = []
    if record.get("is_error") is True:
        fields.append("is_error")
        severity = _max_severity(severity, "high")
    if record.get("ok") is False:
        fields.append("ok=false")
        severity = _max_severity(severity, "medium")
    if record.get("success") is False:
        fields.append("success=false")
        severity = _max_severity(severity, "medium")
    if record.get("timed_out") is True:
        fields.append("timed_out=true")
        severity = _max_severity(severity, "high")
    error_value = record.get("error")
    if error_value not in (None, "", False, {}, []):
        fields.append("error")
        severity = _max_severity(severity, "high")
    if not severity or severity == "info":
        if fields:
            severity = "medium"
        else:
            return None
    if severity not in ("critical", "high", "medium"):
        return None
    redactor = source.redactor
    message = _record_message(record)
    explicit_type = _scalar_value(record, "error_type", "exception_type",
                                  "type")
    if isinstance(error_value, Mapping):
        explicit_type = explicit_type or _scalar(
            error_value.get("name") or error_value.get("type"))
    error_type = explicit_type or _error_type(
        message, "LogError" if severity != "medium" else "LogWarning")
    before = redactor.total
    tool = _dimension(redactor, record.get("tool"))
    component = "structured log" + (f" {tool}" if tool else "")
    return _Observation(
        source=source.name, source_kind="jsonl", severity=severity,
        error_type=redactor.text(error_type, limit=MAX_DIMENSION)
        or ("LogWarning" if severity == "medium" else "LogError"),
        signature=redactor.text(_normalise(
            _signature_text(message, error_type) or str(level))),
        component=component,
        tool=tool,
        intent=_dimension(redactor, record.get("intent")),
        harness=_dimension(redactor, record.get("harness")),
        model=_dimension(redactor, record.get("model")),
        task_class=_dimension(
            redactor, record.get("task_class") or record.get("task")),
        timestamp=_record_timestamp(record),
        failure_fields=tuple(fields or [f"level={level}"]),
        redacted_values=redactor.total - before,
    )


def _eval_observation(source: _Source, doc: Mapping[str, Any],
                      run: Mapping[str, Any]) -> _Observation | None:
    fields: list[str] = []
    severity = ""
    primary = ""
    error_type = ""
    if run.get("driver_error"):
        fields.append("driver_error")
        severity = "high"
        primary = str(run.get("driver_error"))
        error_type = _error_type(primary, "DriverError")
    if run.get("timed_out") is True:
        fields.append("timed_out")
        severity = "high"
        primary = primary or "run timed out"
        error_type = error_type or "Timeout"
    if run.get("prep_ok") is False or run.get("prep_error"):
        fields.append("prep_error")
        severity = _max_severity(severity, "medium")
        primary = primary or str(run.get("prep_error") or "preparation failed")
        error_type = error_type or _error_type(primary, "PrepError")
    if run.get("daemon_ok") is False or run.get("daemon_error"):
        fields.append("daemon_error")
        severity = _max_severity(severity, "medium")
        primary = primary or str(run.get("daemon_error") or "daemon failed")
        error_type = error_type or _error_type(primary, "DaemonError")
    if run.get("success") is False:
        fields.append("success=false")
        severity = _max_severity(severity, "medium")
        primary = primary or str(
            run.get("error") or run.get("driver_error")
            or "eval check failed")
        error_type = error_type or _error_type(primary, "CheckFailed")
    if run.get("error"):
        fields.append("error")
        severity = _max_severity(severity, "high")
        primary = primary or str(run.get("error"))
        error_type = error_type or _error_type(primary, "EvalError")
    if not severity:
        return None
    redactor = source.redactor
    before = redactor.total
    backend = doc.get("backend")
    model = ""
    if isinstance(backend, Mapping):
        model = _scalar(backend.get("model"))
    model = model or _scalar(run.get("model"))
    task_class = _dimension(
        redactor, run.get("task") or run.get("task_class"))
    harness = _dimension(
        redactor, run.get("driver") or doc.get("driver"))
    return _Observation(
        source=source.name, source_kind="eval", severity=severity,
        error_type=redactor.text(error_type or "EvalError",
                                 limit=MAX_DIMENSION)
        or "EvalError",
        signature=redactor.text(_normalise(
            _signature_text(primary, error_type) or "eval run failed")),
        component="eval harness" + (f" {task_class}" if task_class else ""),
        tool="eval",
        intent=_dimension(redactor, run.get("arm") or "eval"),
        harness=harness,
        model=_dimension(redactor, model),
        task_class=task_class,
        timestamp=_record_timestamp(run),
        failure_fields=tuple(fields),
        redacted_values=redactor.total - before,
    )


def _process_eval(source: _Source, doc: Mapping[str, Any],
                  observations: list[_Observation],
                  since: datetime | None) -> None:
    runs = doc.get("runs")
    if not isinstance(runs, list):
        _add(observations, source,
             _generic_observation(source, doc), since)
        source.records += 1
        return
    malformed = 0
    for run in runs:
        if not isinstance(run, Mapping):
            malformed += 1
            continue
        source.records += 1
        _add(observations, source,
             _eval_observation(source, doc, run), since)
    _record_malformed(source, observations, malformed, "eval run", since)


def _looks_eval_run(record: Mapping[str, Any]) -> bool:
    if isinstance(record.get("runs"), list):
        return True
    return (
        any(key in record for key in (
            "driver_error", "prep_error", "daemon_error", "timed_out",
            "success", "arm"))
        and any(key in record for key in (
            "task", "arm", "driver", "success"))
    )


def _process_jsonl(source: _Source, lines: Sequence[str],
                   observations: list[_Observation],
                   since: datetime | None) -> None:
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(item, Mapping):
            malformed += 1
            continue
        if isinstance(item.get("runs"), list):
            source.kind = "eval"
            _process_eval(source, item, observations, since)
            continue
        source.records += 1
        if _looks_eval_run(item):
            source.kind = "eval"
            _add(observations, source,
                 _eval_observation(source, item, item), since)
        elif _daemon_record(item):
            _add(observations, source,
                 _daemon_observation(source, item), since)
        else:
            _add(observations, source,
                 _generic_observation(source, item), since)
    _record_malformed(source, observations, malformed, "JSONL", since)


def _daemon_record(record: Mapping[str, Any]) -> bool:
    return "op" in record and any(
        key in record for key in ("tool", "intent", "handled", "is_error",
                                  "fallback", "expired"))


def _record_malformed(source: _Source, observations: list[_Observation],
                      count: int, kind: str, since: datetime | None) -> None:
    source.malformed += count
    if not count:
        return
    _add(observations, source, _Observation(
        source=source.name, source_kind=source.kind, severity="low",
        error_type="MalformedInput",
        signature=f"{count} unparseable {kind} line(s)",
        component="log format", weight=count,
        failure_fields=("malformed",),
    ), since)


def _process_json_document(source: _Source, doc: Any,
                           observations: list[_Observation],
                           since: datetime | None) -> None:
    if isinstance(doc, Mapping) and isinstance(doc.get("runs"), list):
        source.kind = "eval"
        _process_eval(source, doc, observations, since)
        return
    if isinstance(doc, Mapping):
        source.records += 1
        if _looks_eval_run(doc):
            source.kind = "eval"
            _add(observations, source,
                 _eval_observation(source, doc, doc), since)
        else:
            _add(observations, source,
                 _generic_observation(source, doc), since)
        return
    if isinstance(doc, list):
        malformed = 0
        for item in doc:
            if isinstance(item, Mapping):
                source.records += 1
                if _looks_eval_run(item):
                    source.kind = "eval"
                    _add(observations, source,
                         _eval_observation(source, item, item), since)
                else:
                    _add(observations, source,
                         _generic_observation(source, item), since)
            else:
                malformed += 1
        _record_malformed(source, observations, malformed, "JSON", since)
        return
    source.malformed += 1
    _record_malformed(source, observations, 1, "JSON", since)


# --------------------------------------------------------------------------- #
# bounded reading
# --------------------------------------------------------------------------- #
def _logical_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".gz":
        suffix = Path(path.stem).suffix.lower()
    return suffix


def _read_input(path: Path, limits: Limits) -> tuple[str, int, bool]:
    """Read at most ``limits.max_bytes`` (decompressed) and one extra byte."""
    with path.open("rb") as handle:
        head = handle.read(2)
        handle.seek(0)
        if head == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=handle) as stream:
                data = stream.read(limits.max_bytes + 1)
        else:
            data = handle.read(limits.max_bytes + 1)
    truncated = len(data) > limits.max_bytes
    if truncated:
        data = data[:limits.max_bytes]
    return data.decode("utf-8", errors="replace"), len(data), truncated


def _parse_source(source: _Source, text: str, limits: Limits,
                  observations: list[_Observation],
                  since: datetime | None) -> None:
    suffix = _logical_suffix(source.path)
    lines = text.splitlines()
    if len(lines) > limits.max_lines:
        source.truncated_lines = True
        lines = lines[:limits.max_lines]
    source.lines_read = sum(1 for line in lines if line.strip())

    whole_json: Any = None
    json_attempted = suffix == ".json"
    if json_attempted:
        try:
            whole_json = json.loads(text)
        except json.JSONDecodeError:
            whole_json = None

    if whole_json is not None:
        source.kind = "json"
        _process_json_document(source, whole_json, observations, since)
        return

    force_jsonl = suffix in {".jsonl", ".ndjson"} or "journal" in path_name(
        source.path)
    if force_jsonl:
        source.kind = "jsonl"
        _process_jsonl(source, lines, observations, since)
        return

    if json_attempted:
        # A malformed .json document: try JSONL lines, then keep the
        # malformed count. The source is still readable.
        source.kind = "json"
        _process_jsonl(source, lines, observations, since)
        if not source.malformed:
            _record_malformed(source, observations, 1, "JSON document",
                              since)
        return

    if _looks_like_opencode(lines):
        source.kind = "opencode"
        _process_opencode(source, lines, observations, since)
        return

    sample = [line for line in lines[:50] if line.strip()]
    parsed = 0
    for line in sample:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, Mapping):
            parsed += 1
    if sample and parsed >= max(1, int(len(sample) * 0.8)):
        source.kind = "jsonl"
        _process_jsonl(source, lines, observations, since)
        return

    source.kind = "text"
    _process_text(source, lines, observations, since)


def path_name(path: Path) -> str:
    return path.name.lower()


def _inspect_path(source: _Source, limits: Limits, since: datetime | None) -> list[_Observation]:
    observations: list[_Observation] = []
    try:
        text, bytes_read, truncated = _read_input(source.path, limits)
    except (OSError, EOFError, zlib.error) as exc:
        source.error = f"cannot read input: {exc}"
        return observations
    source.bytes_read = bytes_read
    source.truncated_bytes = truncated
    try:
        _parse_source(source, text, limits, observations, since)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        source.error = f"cannot parse input: {exc}"
        return []
    return observations


# --------------------------------------------------------------------------- #
# clustering and proposals
# --------------------------------------------------------------------------- #
def _cluster_id(key: Sequence[str]) -> str:
    payload = json.dumps(list(key), separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return "LI-" + digest.upper()


def _proposal(cluster: Mapping[str, Any]) -> dict[str, Any]:
    error_type = str(cluster["error_type"])
    signature = str(cluster["signature"])
    dimensions = cluster["dimensions"]
    haystack = " ".join([
        error_type, signature,
        " ".join(str(value or "") for value in dimensions.values()),
    ]).lower()
    source_kinds = cluster["source_kinds"]
    count = int(cluster["count"])
    if error_type == "MalformedInput":
        action = "capture_rule"
        summary = ("Add or fix a structured capture rule for this source; "
                   "malformed lines cannot be clustered reliably.")
    elif any(word in haystack for word in (
            "auth", "credential", "api key", "api_key", "token", "oauth",
            "permission", "unauthorized", "forbidden", "rate limit",
            "quota", "provider", "model not found", "context length",
            "plugin", "config", "not configured")):
        action = "config_change"
        summary = ("Review configuration for this failure: provider, model, "
                   "credentials, plugin or timeout settings.")
    elif any(word in haystack for word in (
            "timeout", "timed out", "deadline", "expired", "too slow")):
        action = "config_change"
        summary = ("Review timeout and resource-budget settings for this "
                   "operation; the observed failure is time related.")
    elif "eval" in source_kinds and dimensions.get("task_class"):
        action = "task_class"
        summary = ("Add or tune a bounded task class for this eval failure "
                   "(for example log_inspect or test_triage) instead of "
                   "rerunning the same open-ended prompt.")
    elif "text" in source_kinds or "jsonl" in source_kinds:
        if not dimensions.get("harness") or not dimensions.get("tool"):
            action = "capture_rule"
            summary = ("Capture more structure for this failure (harness, "
                       "tool, intent and model) so it can be clustered and "
                       "routed deterministically.")
        else:
            action = "bug_report"
            summary = ("Prepare a local bug report with a redacted excerpt "
                       "and a reproduction; do not upload it automatically.")
    else:
        action = "bug_report"
        summary = ("Prepare a local bug report with a redacted excerpt and "
                   "a reproduction; do not upload it automatically.")
    return {
        "kind": "proposal",
        "action": action,
        "summary": summary,
        "confidence": "medium" if count >= 3 else "low",
        "auto_applied": False,
        "requires_approval": True,
        "network": False,
    }


def _build_clusters(observations: Iterable[_Observation]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for observation in observations:
        key = observation.key()
        group = groups.get(key)
        if group is None:
            group = {
                "key": key,
                "count": 0,
                "records": 0,
                "severity": observation.severity,
                "first_seen": observation.timestamp,
                "last_seen": observation.timestamp,
                "timestamps_known": 0,
                "sources": Counter(),
                "severities": Counter(),
                "components": Counter(),
                "failure_fields": Counter(),
                "source_kinds": set(),
                "redacted_values": 0,
            }
            groups[key] = group
        weight = max(1, observation.weight)
        group["count"] += weight
        group["records"] += 1
        group["severity"] = _max_severity(
            group["severity"], observation.severity)
        group["severities"][observation.severity] += weight
        group["sources"][observation.source] += weight
        group["source_kinds"].add(observation.source_kind)
        group["components"][observation.component] += weight
        group["redacted_values"] += observation.redacted_values
        if observation.timestamp is not None:
            group["timestamps_known"] += 1
            if (group["first_seen"] is None
                    or observation.timestamp < group["first_seen"]):
                group["first_seen"] = observation.timestamp
            if (group["last_seen"] is None
                    or observation.timestamp > group["last_seen"]):
                group["last_seen"] = observation.timestamp
        for item in observation.failure_fields:
            group["failure_fields"][item] += weight
    clusters: list[dict[str, Any]] = []
    for key, group in groups.items():
        error_type, signature, tool, intent, harness, model, task_class = key
        dimensions = {
            "tool": tool or None,
            "intent": intent or None,
            "harness": harness or None,
            "model": model or None,
            "task_class": task_class or None,
        }
        components = group["components"]
        component = (sorted(components.items(), key=lambda item: (-item[1],
                                                                   item[0]))[0][0]
                     if components else "unattributed")
        clusters.append({
            "id": _cluster_id(key),
            "severity": group["severity"],
            "count": group["count"],
            "first_seen": group["first_seen"],
            "last_seen": group["last_seen"],
            "error_type": error_type or "Unknown",
            "signature": signature or "(no message)",
            "likely_component": component,
            "dimensions": dimensions,
            "source_kinds": sorted(group["source_kinds"]),
            "evidence": {
                "occurrences": group["count"],
                "records": group["records"],
                "sources": dict(sorted(group["sources"].items())),
                "severities": dict(sorted(group["severities"].items())),
                "timestamps_known": group["timestamps_known"],
                "failure_fields": dict(sorted(
                    group["failure_fields"].items())),
                "redacted_values": group["redacted_values"],
                "components": dict(sorted(components.items())),
            },
            "source_kinds_set": group["source_kinds"],
        })
    return clusters


def _rank(clusters: list[dict[str, Any]],
          now: datetime) -> list[dict[str, Any]]:
    for cluster in clusters:
        weight = _SEVERITY_WEIGHT.get(cluster["severity"], 0)
        last_seen = cluster["last_seen"]
        if last_seen is None:
            recency = 0.0
        else:
            hours = max(0.0, (now - last_seen).total_seconds() / 3600.0)
            recency = max(0.0, 720.0 - min(hours, 720.0))
        frequency = min(cluster["count"], 10_000)
        cluster["rank_score"] = (
            weight * 1_000_000 + frequency * 100 + recency)
    clusters.sort(key=lambda item: (-item["rank_score"], item["id"]))
    findings: list[dict[str, Any]] = []
    for rank, cluster in enumerate(clusters, start=1):
        source_kinds = cluster.pop("source_kinds_set")
        proposal = _proposal({
            **cluster,
            "source_kinds": source_kinds,
        })
        dimensions = cluster["dimensions"]
        findings.append({
            "id": cluster["id"],
            "rank": rank,
            "severity": cluster["severity"],
            "count": cluster["count"],
            "first_seen": _format_time(cluster["first_seen"]),
            "last_seen": _format_time(cluster["last_seen"]),
            "error_type": cluster["error_type"],
            "signature": cluster["signature"],
            "likely_component": cluster["likely_component"],
            "dimensions": dimensions,
            "source_kinds": cluster["source_kinds"],
            "evidence": cluster["evidence"],
            "proposed_action": proposal,
            "truth_assessed": False,
        })
    return findings


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def inspect_logs(paths: str | Path | Sequence[str | Path], *,
                 root: str | Path | None = None,
                 since: str | None = None,
                 limit: int = 20,
                 limits: Limits | None = None) -> dict[str, Any]:
    """Read only ``paths`` and return a redacted, clustered report.

    ``paths`` may not be empty: there is no implicit home, root or state
    directory scan. ``root`` only resolves relative paths. Records without a
    timestamp are kept even when ``since`` is set; absence of a timestamp is
    not evidence that a record is old.
    """
    if isinstance(paths, (str, Path)):
        entries = [paths]
    else:
        entries = list(paths)
    if not entries:
        raise LogInspectError(
            "at least one explicit input path is required; log inspection "
            "never scans the home directory or a root by default")
    active_limits = limits or Limits()
    since_dt = _since_value(since)
    root_path = Path(root).expanduser().resolve() if root else None
    now = datetime.now(timezone.utc)
    observations: list[_Observation] = []
    sources: list[_Source] = []
    errors: list[str] = []
    skipped: list[dict[str, str]] = []
    findings_limit = max(1, min(int(limit), MAX_FINDINGS))

    for index, entry in enumerate(entries, start=1):
        raw = Path(entry).expanduser()
        if not raw.is_absolute():
            raw = (root_path / raw) if root_path is not None else raw.resolve()
        target = raw.resolve()
        source = _Source(name=f"source_{index}", path=target,
                         display=_display_path(target))
        if len(sources) >= active_limits.max_files:
            source.skipped = "max_files"
            sources.append(source)
            skipped.append({"path": source.display, "reason": "max_files"})
            continue
        if not target.exists():
            source.error = "no such file"
        elif not target.is_file():
            source.error = "not a regular file (pass explicit file paths)"
        else:
            observations.extend(_inspect_path(source, active_limits, since_dt))
        if source.error:
            errors.append(f"{source.display}: {source.error}")
        sources.append(source)

    clusters = _build_clusters(observations)
    ranked = _rank(clusters, now) if clusters else []
    findings = ranked[:findings_limit]
    redactions: Counter[str] = Counter()
    for source in sources:
        redactions.update(source.redactor.counts)
    total_records = sum(source.records for source in sources)
    malformed = sum(source.malformed for source in sources)
    dropped = sum(source.observations_dropped for source in sources)
    failures = sum(item["count"] for item in ranked)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": TOOL_NAME,
        "generated_at": _format_time(now),
        "root": _display_path(root_path) if root_path else None,
        "since": _format_time(since_dt),
        "local_only": True,
        "uploaded": False,
        "applied": False,
        "truth_assessed": False,
        "limits": {
            **active_limits.to_dict(),
            "findings": findings_limit,
        },
        "summary": {
            "inputs": len(sources),
            "inputs_read": sum(1 for source in sources
                               if not source.error and not source.skipped),
            "inputs_skipped": len(skipped),
            "inputs_unreadable": len(errors),
            "records": total_records,
            "failures": failures,
            "clusters": len(ranked),
            "findings_shown": len(findings),
            "malformed_lines": malformed,
            "observations_dropped": dropped,
            "truncated_inputs": sum(
                1 for source in sources
                if source.truncated_bytes or source.truncated_lines),
        },
        "redactions": dict(sorted(redactions.items())),
        "inputs": [source.report() for source in sources],
        "skipped": skipped,
        "errors": errors,
        "findings": findings,
        "not_assessed": [
            "Absence of a finding is not proof that no failure occurred.",
            "Only the explicitly listed inputs were read; no directory or "
            "home-directory scan was performed.",
            "Prompt, message and file-content payloads are redacted, not "
            "analysed for meaning.",
            "Root cause is not established; findings describe observed "
            "clusters only.",
            "Proposed actions are advisory. Nothing was applied, executed, "
            "uploaded or sent.",
        ],
    }
    if dropped:
        report["not_assessed"].insert(
            1, f"{dropped} additional observation(s) exceeded the bounded "
               f"observation cap ({MAX_OBSERVATIONS}) and were not clustered.")
    if len(findings) < len(ranked):
        report["findings_truncated"] = {
            "shown": len(findings),
            "total": len(ranked),
            "limit": findings_limit,
        }
    return report


def proposed_task(report: Mapping[str, Any],
                  *, spec_id: str = "log_inspect") -> dict[str, Any]:
    """A bounded, read-only task specification describing this inspection.

    The result follows :mod:`ozagia.core.tasks` concepts and validates as a
    TOML/JSON task spec. It is data only: this module never runs it and never
    supplies an executor.
    """
    inputs = [
        item["name"] for item in report.get("inputs") or []
        if not item.get("error") and not item.get("skipped")
    ]
    summary = report.get("summary") or {}
    return {
        "id": spec_id,
        "version": 1,
        "title": "Inspect local agent logs (redacted, bounded)",
        "objective": (
            "Read only the explicitly listed local log inputs, redact "
            "secrets and payloads before reporting, cluster observed "
            "failures by type and dimensions, and return advisory findings. "
            f"This report observed {summary.get('failures', 0)} failure "
            "occurrence(s) in "
            f"{summary.get('findings_shown', 0)} shown cluster(s). Do not "
            "upload, apply or execute proposals."
        ),
        "inputs": inputs,
        "tools": ["read"],
        "write_scope": [],
        "steps": [{
            "id": "inspect",
            "tool": "read",
            "goal": (
                "Read the named local logs only; never scan a home directory "
                "or repository; redact before reporting."
            ),
            "paths": [],
            "write_paths": [],
            "budget": {
                "tokens": 0, "model_calls": 0, "cost_usd": 0.0,
                "wall_s": 120,
            },
            "args": {
                "sources": inputs,
                "findings": summary.get("findings_shown", 0),
                "truth_assessed": False,
                "redaction": "required",
                "upload": False,
            },
        }],
        "resources": {
            "memory_mb": 256, "disk_mb": 32, "pids": 16, "wall_s": 120,
        },
        "budget": {
            "tokens": 0, "model_calls": 0, "cost_usd": 0.0, "wall_s": 120,
        },
        "gates": {
            "before": ["resources_fit"],
            "after": ["schema_valid", "evidence_present"],
        },
        "output": "log_findings",
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["findings", "redactions", "truth_assessed"],
            "properties": {
                "findings": {
                    "type": "array",
                    "maxItems": MAX_FINDINGS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "id", "severity", "count", "proposed_action",
                        ],
                        "properties": {
                            "id": {"type": "string"},
                            "severity": {"type": "string"},
                            "count": {"type": "integer", "minimum": 1},
                            "proposed_action": {"type": "object"},
                        },
                    },
                },
                "redactions": {"type": "object"},
                "truth_assessed": {"type": "boolean", "enum": [False]},
                "uploaded": {"type": "boolean", "enum": [False]},
            },
        },
        "approval": {"mutate": "deny", "network": "deny"},
        "requires_graph": False,
    }


def task_spec_json(report: Mapping[str, Any],
                   *, spec_id: str = "log_inspect") -> str:
    return json.dumps(proposed_task(report, spec_id=spec_id), indent=2,
                      sort_keys=True, ensure_ascii=False) + "\n"


def report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False) + "\n"


def format_report(report: Mapping[str, Any]) -> str:
    """A compact text summary with no raw log values."""
    summary = report.get("summary") or {}
    limits = report.get("limits") or {}
    lines = [
        "log inspection (local, redacted, read-only)",
        f"inputs: {summary.get('inputs_read', 0)} read, "
        f"{summary.get('inputs_skipped', 0)} skipped, "
        f"{summary.get('inputs_unreadable', 0)} unreadable",
        f"records: {summary.get('records', 0)}; failures: "
        f"{summary.get('failures', 0)}; malformed lines: "
        f"{summary.get('malformed_lines', 0)}",
        f"clusters: {summary.get('findings_shown', 0)} shown of "
        f"{summary.get('clusters', 0)}; limit {limits.get('findings')}",
    ]
    redactions = report.get("redactions") or {}
    if redactions:
        counts = ", ".join(f"{key}={value}"
                           for key, value in sorted(redactions.items()))
        lines.append(f"redactions (counts only): {counts}")
    for item in report.get("inputs") or []:
        state = "skipped" if item.get("skipped") else (
            "error" if item.get("error") else item.get("kind"))
        detail = (f"records={item.get('records', 0)} "
                  f"malformed={item.get('malformed_lines', 0)}")
        suffix = ""
        if item.get("truncated_bytes") or item.get("truncated_lines"):
            suffix = " (truncated)"
        lines.append(f"  {item.get('name')}: {item.get('path')} "
                     f"[{state}] {detail}{suffix}")
    findings = report.get("findings") or []
    lines.append(f"findings ({len(findings)}):")
    if not findings:
        lines.append("  none observed in the listed inputs")
    for item in findings:
        times = ""
        if item.get("first_seen") or item.get("last_seen"):
            times = (f" first={item.get('first_seen') or '?'}"
                     f" last={item.get('last_seen') or '?'}")
        lines.append(
            f"  {item.get('rank')}. [{item.get('severity')}] "
            f"{item.get('id')} count={item.get('count')}{times}")
        lines.append(f"     {item.get('error_type')}: "
                     f"{item.get('signature')}")
        lines.append(f"     component: {item.get('likely_component')}")
        dimensions = item.get("dimensions") or {}
        shown = ", ".join(f"{key}={value}" for key, value in dimensions.items()
                          if value)
        if shown:
            lines.append(f"     dimensions: {shown}")
        proposal = item.get("proposed_action") or {}
        lines.append(
            f"     proposed action (not applied): {proposal.get('action')} "
            f"- {proposal.get('summary')}")
    if report.get("findings_truncated"):
        truncation = report["findings_truncated"]
        lines.append(
            f"  ... {truncation.get('shown')} of "
            f"{truncation.get('total')} clusters shown; use --limit to see "
            "more.")
    errors = report.get("errors") or []
    if errors:
        lines.append("errors:")
        lines.extend(f"  {error}" for error in errors)
    lines.append("not assessed:")
    lines.extend(f"  - {item}" for item in report.get("not_assessed") or [])
    lines.append("truth_assessed: false")
    return "\n".join(lines) + "\n"


def atomic_write(path: str | Path, text: str) -> None:
    """Write ``text`` atomically, requiring an existing parent directory."""
    try:
        target = Path(path).expanduser()
    except (OSError, RuntimeError) as exc:
        raise LogInspectError(
            f"cannot resolve output path {path!r}: {exc}") from exc
    parent = target.parent
    if not parent.is_dir():
        raise LogInspectError(f"output directory does not exist: {parent}")
    handle = None
    temporary: str | None = None
    try:
        handle, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            handle = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise LogInspectError(f"cannot write {target}: {exc}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


__all__ = [
    "DEFAULT_MAX_BYTES", "DEFAULT_MAX_FILES", "DEFAULT_MAX_LINES",
    "Limits", "LogInspectError", "MAX_FINDINGS", "Redactor", "SCHEMA",
    "TOOL_NAME", "atomic_write", "format_report", "inspect_logs",
    "proposed_task", "report_json", "task_spec_json",
]
