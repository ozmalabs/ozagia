"""Versioned task-profile assembly over injected records.

This is the offline WP3 primitive.  It accepts already-collected span records
and returns a bounded, canonical, hashed ``ozagia.task_profile`` v1.0 instance.
It does not read a file, a graph, a task store or a clock, does not call a
model, import a target project or start a daemon.  WP4 owns the adapters that
turn real task, goal, daemon, session and evaluation records into these spans.

Missing evidence
----------------

Every optional field is ``None`` in the API and ``"unknown"`` in the report.
A missing duration is never written as zero.  A span with no ``parents``
declaration is represented as ``parents="unknown"`` (dependency information was
not supplied); a root span declares ``parents=[]`` explicitly.  A duration is
derived from ``start_ms``/``end_ms`` only for ``monotonic`` or ``injected``
clocks; wall-clock and unknown clocks never yield a duration by subtraction.

Edges
-----

Edges are explicit finish-to-start dependencies derived from each span's
``parents`` list.  No timestamp comparison creates an edge.  Timing that
contradicts a declared edge is reported in ``data.integrity`` as an anomaly;
the edge is kept because the declaration is authoritative.

Determinism and bounds
----------------------

Spans are ordered by a deterministic topological sort (ties by span ID), and
truncation keeps an ancestor-closed prefix so every kept parent is present.
The report is truncated by span count, edge count and canonical byte size, with
an explicit :class:`ozagia.core.contracts.Truncation` envelope.  The profile is
validated with :mod:`ozagia.core.contracts` and hashed with
``contracts.instance_hash``; transport fields such as ``created_at`` do not
change the hash.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.core.contracts import (
    CLOCKS,
    EVIDENCE,
    FRESHNESS,
    RESULTS,
    canonical_json,
    clock as check_clock,
    content_hash,
    instance_hash,
    normalize_version,
    schema_fingerprint,
    truncation,
    validate_instance,
    validate_schema,
    validate_truncation,
)
from ozagia.core.critical_path import (
    MAX_DAG_EDGES,
    MAX_DAG_NODES,
    NODE_KINDS,
    OUTCOMES,
    RESOURCE_CLASSES,
    TERMINAL_OUTCOMES,
    UNKNOWN,
    CriticalPathBoundsError,
    CycleError,
    Dag,
    DagEdge,
    DagNode,
    critical_path,
    topological_order,
    validate_node_id,
)

# NODE_KINDS is the shared span-kind vocabulary; the alias keeps call sites in
# this module reading as profile code rather than graph code.
SPAN_KINDS = NODE_KINDS

PROFILE_NAMESPACE = "ozagia.task_profile"
PROFILE_VERSION = "1.0"

MEASUREMENTS = ("measured", "estimated", "unknown")

DEFAULT_MAX_SPANS = 512
DEFAULT_MAX_EDGES = 2048
DEFAULT_MAX_BYTES = 128 * 1024
MAX_PROFILE_SPANS = MAX_DAG_NODES
MAX_PROFILE_EDGES = MAX_DAG_EDGES
MAX_PROFILE_BYTES = 1024 * 1024

PROFILE_ERROR_CODES = frozenset({
    "task_profile.invalid_record",
    "task_profile.invalid_field",
    "task_profile.duplicate_id",
    "task_profile.unknown_parent",
    "task_profile.cycle",
    "task_profile.inconsistent_edges",
    "task_profile.inconsistent_data",
    "task_profile.bounds_exceeded",
    "task_profile.invalid_profile",
})

PROFILE_SCHEMA = {
    "namespace": PROFILE_NAMESPACE,
    "version": PROFILE_VERSION,
    "types": {
        "span": {
            "fields": [
                "id", "kind", "parents", "start_ms", "end_ms",
                "duration_ms", "clock", "outcome", "result", "evidence",
                "measurement", "attempt", "resource_class", "queue_wait_ms",
                "readiness_wait_ms", "input_tokens", "cached_input_tokens",
                "output_tokens", "prefill_ms", "generation_ms", "cost_micros",
            ],
        },
        "edge": {"fields": ["from", "to", "kind"]},
        "coverage": {
            "fields": [
                "complete", "timing_complete", "evidence_complete",
                "truncated", "spans_included", "spans_total", "edges",
                "unknown_dependencies", "unknown_duration",
                "unknown_queue_wait", "unknown_readiness_wait",
                "unknown_tokens", "unknown_cost", "unknown_evidence",
                "measured", "estimated", "unknown_measurement", "by_kind",
                "by_outcome", "by_resource_class",
            ],
        },
        "integrity": {
            "fields": [
                "unknown_dependencies", "unknown_durations",
                "time_order_anomalies", "terminal_with_descendants",
                "scope_declared",
            ],
        },
    },
    "required": [
        "profile_version", "clock", "time_origin", "spans", "edges",
        "coverage", "integrity",
    ],
    "constraints": {
        "max_spans": DEFAULT_MAX_SPANS,
        "max_edges": DEFAULT_MAX_EDGES,
        "max_bytes": DEFAULT_MAX_BYTES,
        "hard_max_spans": MAX_PROFILE_SPANS,
        "hard_max_bytes": MAX_PROFILE_BYTES,
    },
    "sensitivity": "internal",
}

# ---------------------------------------------------------------------------
# WP4 adapters and post-run report
# ---------------------------------------------------------------------------

ADAPTER_VERSION = "1.0"
REPORT_NAMESPACE = "ozagia.task_report"
REPORT_VERSION = "1.0"

DEFAULT_MAX_REPORT_BYTES = 128 * 1024
MAX_TASK_REPORT_BYTES = 1024 * 1024
MAX_ADAPTED_SPANS = 1024
MAX_REPORT_CRITERIA = 50
MAX_REPORT_CHANGED = 100
MAX_REPORT_ITEMS = 100
MAX_REPORT_SOURCES = 32
MAX_REPORT_UNKNOWNS = 40
MAX_REPORT_TEXT = 400

REPORT_ERROR_CODES = frozenset({
    "task_report.invalid_input",
    "task_report.invalid_record",
    "task_report.bounds_exceeded",
    "task_report.invalid_report",
})

REPORT_STATUSES = ("passed", "failed", "cancelled", "incomplete", "unknown")
REPORT_CRITERIA_STATUSES = ("passed", "failed", "unknown")
REPORT_VERIFIED_KINDS = ("criterion", "session", "test")

STATUS_MAP = {
    "pass": "passed",
    "fail": "failed",
    "stale": "unknown",
    "unknown": "unknown",
    "unchecked": "not_assessed",
}

TASK_TOOL_KINDS = {
    "read": ("tool_call", "tool"),
    "glob": ("tool_call", "tool"),
    "grep": ("tool_call", "tool"),
    "list": ("tool_call", "tool"),
    "stat": ("tool_call", "tool"),
    "graph_search": ("tool_call", "graph"),
    "graph_impact": ("tool_call", "graph"),
    "model": ("model_call", "model"),
    "test": ("check", "check"),
    "lint": ("check", "check"),
    "build": ("check", "check"),
    "write": ("action", "tool"),
    "edit": ("action", "tool"),
    "patch": ("action", "tool"),
    "shell": ("action", "tool"),
}

REPORT_SCHEMA = {
    "namespace": REPORT_NAMESPACE,
    "version": REPORT_VERSION,
    "types": {
        "report": {
            "fields": [
                "report_version", "scope", "run", "outcome", "work",
                "selections", "snapshot", "failures", "fallbacks", "blocked",
                "skipped", "measurements", "critical_path",
                "execution_critical_path", "prefetch", "sources", "coverage",
                "not_assessed", "bounds", "truncated", "hash",
            ],
        },
    },
    "required": [
        "report_version", "scope", "run", "outcome", "work", "selections",
        "snapshot", "failures", "fallbacks", "blocked", "skipped",
        "measurements", "critical_path", "execution_critical_path",
        "prefetch", "sources", "coverage", "not_assessed", "bounds",
        "truncated", "hash",
    ],
    "constraints": {
        "max_criteria": MAX_REPORT_CRITERIA,
        "max_changed": MAX_REPORT_CHANGED,
        "max_sources": MAX_REPORT_SOURCES,
        "max_bytes": DEFAULT_MAX_REPORT_BYTES,
        "hard_max_bytes": MAX_TASK_REPORT_BYTES,
    },
    "sensitivity": "internal",
}


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class TaskProfileError(ValueError):
    """A task-profile error with a stable, machine-readable ``code``."""

    code = "task_profile.invalid_record"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        if resolved not in PROFILE_ERROR_CODES:
            raise ValueError(f"unknown task-profile error code {resolved!r}")
        self.code = resolved
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class TaskProfileCycleError(TaskProfileError):
    """The injected records contain a dependency cycle."""

    code = "task_profile.cycle"

    def __init__(self, cycles: Iterable[Iterable[str]],
                 components: Iterable[Iterable[str]]) -> None:
        self.cycles = tuple(tuple(cycle) for cycle in cycles)
        self.components = tuple(tuple(component) for component in components)
        described = "; ".join(
            " -> ".join((*cycle, cycle[0])) if cycle else "empty cycle"
            for cycle in self.cycles
        ) or "unknown cycle"
        super().__init__(f"profile contains a dependency cycle: {described}")


class TaskProfileBoundsError(TaskProfileError):
    code = "task_profile.bounds_exceeded"


# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A deterministic injected monotonic clock for tests and fixtures.

    The clock never reads the host.  ``advance`` moves it forward by a
    caller-supplied number of milliseconds; ``tick`` uses ``step_ms``.
    """

    now_ms: int = 0
    step_ms: int = 0
    kind: str = "injected"

    def __post_init__(self) -> None:
        if isinstance(self.now_ms, bool) or not isinstance(self.now_ms, int):
            raise TaskProfileError("FakeClock.now_ms must be an integer")
        if isinstance(self.step_ms, bool) or not isinstance(self.step_ms, int):
            raise TaskProfileError("FakeClock.step_ms must be an integer")
        if self.now_ms < 0 or self.step_ms < 0:
            raise TaskProfileError("FakeClock values must be non-negative")
        if self.kind != "injected":
            raise TaskProfileError("FakeClock.kind must be 'injected'")

    def now(self) -> int:
        return self.now_ms

    def advance(self, delta_ms: int) -> int:
        if isinstance(delta_ms, bool) or not isinstance(delta_ms, int):
            raise TaskProfileError("FakeClock.advance expects an integer")
        if delta_ms < 0:
            raise TaskProfileError("FakeClock cannot move backwards")
        self.now_ms += delta_ms
        return self.now_ms

    def tick(self) -> int:
        return self.advance(self.step_ms)


# ---------------------------------------------------------------------------
# Span records
# ---------------------------------------------------------------------------


def _optional_int(value: Any, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskProfileError(f"{name} must be an integer or None",
                               code="task_profile.invalid_field")
    if value < minimum:
        raise TaskProfileError(f"{name} must be >= {minimum}",
                               code="task_profile.invalid_field")
    return value


def _validate_time_origin(value: Any) -> int | str:
    if value == UNKNOWN:
        return UNKNOWN
    if isinstance(value, bool):
        raise TaskProfileError("time_origin must not be a bool",
                               code="task_profile.invalid_field")
    if isinstance(value, int):
        return value
    if (isinstance(value, str) and value and len(value) <= 64
            and not any(ord(char) < 0x20 for char in value)):
        return value
    raise TaskProfileError(
        "time_origin must be an integer, a bounded string or 'unknown'",
        code="task_profile.invalid_field")


def _check_bound(value: Any, name: str, *, maximum: int,
                 minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskProfileError(
            f"{name} must be an integer",
            code="task_profile.invalid_field")
    if value < minimum or value > maximum:
        raise TaskProfileBoundsError(
            f"{name} must be between {minimum} and {maximum}")
    return value


def _unknown_none(value: Any) -> Any:
    return None if value is None or value == UNKNOWN else value


# ---------------------------------------------------------------------------
# Adapter helpers: source IDs, redaction and wall-clock timestamps
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)
_SAFE_TEXT = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")


def redact_task_text(value: Any, limit: int = MAX_REPORT_TEXT) -> str:
    """Bound and best-effort redact a free-text task label.

    The primary defence in this module is the allowlist of fields an adapter
    copies; this function is the second layer for the few free-text fields
    (stop detail, recorded reasons and labels) that callers may legitimately
    want in a report.
    """
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return " ".join(text.split())[:limit]


def source_span_id(source: str, *parts: Any) -> str:
    """Return a deterministic, bounded span ID for one source record.

    The readable prefix preserves as much of the source identity as fits; the
    trailing SHA-256 prefix disambiguates IDs that sanitise to the same text or
    are truncated.  The original source ID is kept in the report's ``sources``
    section, not guessed from this span ID.
    """
    source_text = redact_task_text(source, 32).lower()
    source_text = re.sub(r"[^a-z0-9._-]+", "-", source_text).strip("-._")
    if not source_text or not source_text[0].isalnum():
        source_text = f"src-{source_text}".strip("-")
    raw = ":".join([str(source)] + [str(part) for part in parts])
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    body_source = redact_task_text(raw, 4096)
    body = re.sub(r"[^a-z0-9._-]+", "-", body_source.lower()).strip("-._") or "id"
    head = f"{source_text}."
    room = max(1, 128 - len(head) - len(digest) - 1)
    body = body[:room].strip("-._") or "id"
    return f"{head}{body}-{digest}"


def _bounded_text(value: Any, limit: int = MAX_REPORT_TEXT) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return redact_task_text(value, limit)


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value != value or value < 0:
            return None
        return int(round(value))
    return None


def _seconds_to_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number < 0:
            return None
        return int(round(number * 1000))
    return None


def _timestamp_ms(value: Any) -> int | None:
    """Parse a recorded timestamp as wall-clock epoch milliseconds.

    No duration is ever derived from the result: the caller marks such spans
    ``clock="system"``.  Numeric values are seconds unless they are already in
    the millisecond range.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number < 0:
            return None
        return int(round(number if number >= 10**11 else number * 1000))
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(round(parsed.timestamp() * 1000))


def _safe_span_id(value: str) -> str:
    return source_span_id("source", value)


@dataclass(frozen=True)
class SpanRecord:
    """One injected task-profile observation.

    ``parents=None`` means dependency information is unknown; ``parents=()``
    explicitly declares a root.  Optional fields use ``None`` for unknown.
    """

    id: str
    kind: str
    parents: tuple[str, ...] | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    duration_ms: int | None = None
    clock: str = UNKNOWN
    outcome: str = UNKNOWN
    result: str = UNKNOWN
    evidence: str = UNKNOWN
    measurement: str = UNKNOWN
    attempt: int | None = None
    resource_class: str = UNKNOWN
    queue_wait_ms: int | None = None
    readiness_wait_ms: int | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    prefill_ms: int | None = None
    generation_ms: int | None = None
    cost_micros: int | None = None

    def __post_init__(self) -> None:
        try:
            validate_node_id(self.id, what="span id")
        except ValueError as exc:
            raise TaskProfileError(
                str(exc), code="task_profile.invalid_field") from exc
        if self.kind not in SPAN_KINDS:
            raise TaskProfileError(
                f"span {self.id!r} has invalid kind {self.kind!r}",
                code="task_profile.invalid_field")
        if self.clock not in CLOCKS:
            raise TaskProfileError(
                f"span {self.id!r} has invalid clock {self.clock!r}",
                code="task_profile.invalid_field")
        if self.outcome not in OUTCOMES:
            raise TaskProfileError(
                f"span {self.id!r} has invalid outcome {self.outcome!r}",
                code="task_profile.invalid_field")
        if self.result != UNKNOWN and self.result not in RESULTS:
            raise TaskProfileError(
                f"span {self.id!r} has invalid result {self.result!r}",
                code="task_profile.invalid_field")
        if self.evidence != UNKNOWN and self.evidence not in EVIDENCE:
            raise TaskProfileError(
                f"span {self.id!r} has invalid evidence {self.evidence!r}",
                code="task_profile.invalid_field")
        if self.measurement not in MEASUREMENTS:
            raise TaskProfileError(
                f"span {self.id!r} has invalid measurement "
                f"{self.measurement!r}",
                code="task_profile.invalid_field")
        if self.resource_class not in RESOURCE_CLASSES:
            raise TaskProfileError(
                f"span {self.id!r} has invalid resource class "
                f"{self.resource_class!r}",
                code="task_profile.invalid_field")

        parents = self.parents
        if parents is not None:
            if not isinstance(parents, (tuple, list)):
                raise TaskProfileError(
                    f"span {self.id!r} parents must be a list or None",
                    code="task_profile.invalid_field")
            checked: list[str] = []
            for parent in parents:
                try:
                    validate_node_id(parent,
                                     what=f"span {self.id!r} parent")
                except ValueError as exc:
                    raise TaskProfileError(
                        str(exc),
                        code="task_profile.invalid_field") from exc
                checked.append(parent)
            if len(set(checked)) != len(checked):
                raise TaskProfileError(
                    f"span {self.id!r} has duplicate parents",
                    code="task_profile.invalid_field")
            parents = tuple(sorted(checked))
            object.__setattr__(self, "parents", parents)

        for name in (
            "start_ms", "end_ms", "duration_ms", "queue_wait_ms",
            "readiness_wait_ms", "input_tokens", "cached_input_tokens",
            "output_tokens", "prefill_ms", "generation_ms", "cost_micros",
        ):
            object.__setattr__(
                self, name, _optional_int(getattr(self, name), name))
        object.__setattr__(self, "attempt",
                           _optional_int(self.attempt, "attempt", minimum=1))

        if self.input_tokens is not None and self.cached_input_tokens is not None \
                and self.cached_input_tokens > self.input_tokens:
            raise TaskProfileError(
                f"span {self.id!r} cached_input_tokens exceeds input_tokens",
                code="task_profile.invalid_field")
        if (self.start_ms is not None and self.end_ms is not None
                and self.end_ms < self.start_ms):
            raise TaskProfileError(
                f"span {self.id!r} ends before it starts",
                code="task_profile.invalid_field")
        duration = self.resolved_duration_ms
        if (self.duration_ms is not None and self.start_ms is not None
                and self.end_ms is not None
                and self.clock in ("monotonic", "injected")
                and self.end_ms - self.start_ms != self.duration_ms):
            raise TaskProfileError(
                f"span {self.id!r} start/end contradict duration_ms",
                code="task_profile.invalid_field")
        if (duration is not None and self.prefill_ms is not None
                and self.generation_ms is not None
                and self.prefill_ms + self.generation_ms > duration):
            raise TaskProfileError(
                f"span {self.id!r} prefill+generation exceeds duration",
                code="task_profile.invalid_field")

    @property
    def resolved_duration_ms(self) -> int | None:
        """Return the supplied duration or a safe monotonic start/end delta."""
        if self.duration_ms is not None:
            return self.duration_ms
        if (self.start_ms is not None and self.end_ms is not None
                and self.clock in ("monotonic", "injected")):
            return self.end_ms - self.start_ms
        return None

    @property
    def resolved_end_ms(self) -> int | None:
        """Return the supplied end or a safe derived end for anomaly checks."""
        if self.end_ms is not None:
            return self.end_ms
        duration = self.resolved_duration_ms
        if (self.start_ms is not None and duration is not None
                and self.clock in ("monotonic", "injected")):
            return self.start_ms + duration
        return None

    @classmethod
    def from_mapping(cls, value: Any) -> "SpanRecord":
        """Build a span from a strict JSON-shaped mapping.

        ``"unknown"`` (and ``null`` for optional fields) maps to ``None``.
        Unknown fields are errors; there is no silent extension field here.
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TaskProfileError("span record must be a mapping")
        allowed = {item.name for item in fields(cls)}
        unknown = [key for key in value if key not in allowed]
        if unknown:
            raise TaskProfileError(
                f"unknown span fields: {sorted(unknown)}",
                code="task_profile.invalid_field")
        missing = [name for name in ("id", "kind") if name not in value]
        if missing:
            raise TaskProfileError(
                f"span record is missing required fields: {missing}",
                code="task_profile.invalid_field")
        kwargs: dict[str, Any] = {}
        for key, item in value.items():
            if key == "parents":
                if item is None or item == UNKNOWN:
                    kwargs[key] = None
                elif isinstance(item, (list, tuple)):
                    kwargs[key] = tuple(item)
                else:
                    raise TaskProfileError(
                        "span parents must be a list, 'unknown' or null",
                        code="task_profile.invalid_field")
                continue
            if key in ("id", "kind"):
                kwargs[key] = item
                continue
            if key in ("clock", "outcome", "result", "evidence",
                       "measurement", "resource_class"):
                kwargs[key] = UNKNOWN if item is None else item
                continue
            kwargs[key] = _unknown_none(item)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        def optional(value: Any) -> Any:
            return UNKNOWN if value is None else value

        return {
            "id": self.id,
            "kind": self.kind,
            "parents": (UNKNOWN if self.parents is None else list(self.parents)),
            "start_ms": optional(self.start_ms),
            "end_ms": optional(self.end_ms),
            "duration_ms": optional(self.resolved_duration_ms),
            "clock": self.clock,
            "outcome": self.outcome,
            "result": self.result,
            "evidence": self.evidence,
            "measurement": self.measurement,
            "attempt": optional(self.attempt),
            "resource_class": self.resource_class,
            "queue_wait_ms": optional(self.queue_wait_ms),
            "readiness_wait_ms": optional(self.readiness_wait_ms),
            "input_tokens": optional(self.input_tokens),
            "cached_input_tokens": optional(self.cached_input_tokens),
            "output_tokens": optional(self.output_tokens),
            "prefill_ms": optional(self.prefill_ms),
            "generation_ms": optional(self.generation_ms),
            "cost_micros": optional(self.cost_micros),
        }


# ---------------------------------------------------------------------------
# Coverage and integrity helpers
# ---------------------------------------------------------------------------


def _counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return {key: result[key] for key in sorted(result)}


def _coverage(spans: Iterable[SpanRecord], total: int,
              truncated: bool) -> dict[str, Any]:
    spans = tuple(spans)
    unknown_duration = sum(1 for span in spans
                           if span.resolved_duration_ms is None)
    unknown_dependencies = sum(1 for span in spans if span.parents is None)
    unknown_tokens = sum(
        1 for span in spans
        if span.kind == "model_call"
        and (span.input_tokens is None or span.cached_input_tokens is None
             or span.output_tokens is None))
    timing_complete = unknown_duration == 0 and unknown_dependencies == 0
    evidence_complete = all(span.evidence != UNKNOWN for span in spans)
    return {
        "complete": timing_complete and not truncated,
        "timing_complete": timing_complete,
        "evidence_complete": evidence_complete,
        "truncated": truncated,
        "spans_included": len(spans),
        "spans_total": total,
        "edges": sum(len(span.parents or ()) for span in spans),
        "unknown_dependencies": unknown_dependencies,
        "unknown_duration": unknown_duration,
        "unknown_queue_wait": sum(1 for span in spans
                                  if span.queue_wait_ms is None),
        "unknown_readiness_wait": sum(1 for span in spans
                                      if span.readiness_wait_ms is None),
        "unknown_tokens": unknown_tokens,
        "unknown_cost": sum(1 for span in spans
                            if span.cost_micros is None),
        "unknown_evidence": sum(1 for span in spans
                                if span.evidence == UNKNOWN),
        "measured": sum(1 for span in spans
                        if span.measurement == "measured"),
        "estimated": sum(1 for span in spans
                         if span.measurement == "estimated"),
        "unknown_measurement": sum(1 for span in spans
                                   if span.measurement == UNKNOWN),
        "by_kind": _counts(span.kind for span in spans),
        "by_outcome": _counts(span.outcome for span in spans),
        "by_resource_class": _counts(span.resource_class for span in spans),
    }


def _descendants(start: str, children: Mapping[str, tuple[str, ...]]) -> set[str]:
    found: set[str] = set()
    pending = list(children.get(start, ()))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children.get(current, ()))
    return found


def _integrity(spans: Iterable[SpanRecord],
               scope: Mapping[str, Any]) -> dict[str, Any]:
    spans = tuple(spans)
    by_id = {span.id: span for span in spans}
    children: dict[str, list[str]] = {span.id: [] for span in spans}
    for span in spans:
        for parent in span.parents or ():
            if parent in children:
                children[parent].append(span.id)
    children = {key: tuple(sorted(value)) for key, value in children.items()}

    anomalies: list[dict[str, Any]] = []
    for span in spans:
        if not span.parents or span.clock not in ("monotonic", "injected"):
            continue
        for parent_id in span.parents:
            parent = by_id.get(parent_id)
            if parent is None or parent.clock != span.clock:
                continue
            parent_end = parent.resolved_end_ms
            if (parent_end is not None and span.start_ms is not None
                    and parent_end > span.start_ms):
                anomalies.append({
                    "from": parent_id,
                    "to": span.id,
                    "parent_end_ms": parent_end,
                    "child_start_ms": span.start_ms,
                })
    anomalies.sort(key=lambda item: (item["from"], item["to"]))

    terminal: list[dict[str, Any]] = []
    for span in spans:
        if span.outcome not in TERMINAL_OUTCOMES:
            continue
        downstream = sorted(_descendants(span.id, children))
        if downstream:
            terminal.append({"id": span.id, "descendants": downstream})
    terminal.sort(key=lambda item: item["id"])

    return {
        "unknown_dependencies": sorted(span.id for span in spans
                                       if span.parents is None),
        "unknown_durations": sorted(span.id for span in spans
                                    if span.resolved_duration_ms is None),
        "time_order_anomalies": anomalies,
        "terminal_with_descendants": terminal,
        "scope_declared": bool(scope),
    }


def _profile_clock(spans: Iterable[SpanRecord]) -> str:
    values = {span.clock for span in spans}
    if len(values) == 1:
        return next(iter(values))
    return UNKNOWN


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _coerce_span(record: Any) -> SpanRecord:
    if isinstance(record, SpanRecord):
        return record
    if isinstance(record, Mapping):
        return SpanRecord.from_mapping(record)
    raise TaskProfileError(
        f"span record must be a SpanRecord or mapping, got "
        f"{type(record).__name__}")


def _derived_edges(spans: Iterable[SpanRecord]) -> list[dict[str, str]]:
    edges = [
        {"from": parent, "to": span.id, "kind": "finish_to_start"}
        for span in spans for parent in (span.parents or ())
    ]
    edges.sort(key=lambda item: (item["from"], item["to"], item["kind"]))
    return edges


def edges_from_spans(spans: Iterable[SpanRecord]) -> list[dict[str, str]]:
    """Return the explicit finish-to-start edges declared by ``spans``.

    This is the only edge source for a profile.  It never inspects timestamps:
    an overlapping start/end pair without a declared parent produces no edge.
    """
    return _derived_edges(spans)


def build_profile(records: Any, *, scope: Mapping[str, Any],
                  snapshot: str = UNKNOWN,
                  provenance: Mapping[str, Any] | None = None,
                  inputs: Iterable[str] = (),
                  created_at: int | str | None = None,
                  trace_id: str | None = None,
                  time_origin: int | str = UNKNOWN,
                  max_spans: int = DEFAULT_MAX_SPANS,
                  max_edges: int = DEFAULT_MAX_EDGES,
                  max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Assemble a bounded, canonical task-profile instance.

    ``records`` are injected :class:`SpanRecord` values or strict mappings.
    ``scope`` is the caller's run/task/goal scope mapping.  ``snapshot``
    defaults to the explicit ``"unknown"`` placeholder; a caller with a real
    snapshot ID should pass it.  No clock is read: ``created_at`` is included
    only when supplied and is excluded from the instance hash.
    """
    if not isinstance(scope, Mapping):
        raise TaskProfileError("scope must be a mapping")
    if (not isinstance(snapshot, str) or not snapshot
            or len(snapshot) > 512
            or any(ord(char) < 0x20 for char in snapshot)):
        raise TaskProfileError("snapshot must be a bounded non-empty string")
    if provenance is not None and not isinstance(provenance, Mapping):
        raise TaskProfileError("provenance must be a mapping")
    if isinstance(records, (SpanRecord, Mapping)):
        raw_records = [records]
    elif isinstance(records, (str, bytes)):
        raise TaskProfileError("records must be an iterable of span records")
    else:
        try:
            raw_records = list(records)
        except TypeError as exc:
            raise TaskProfileError(
                "records must be an iterable of span records") from exc
    if len(raw_records) > MAX_PROFILE_SPANS:
        raise TaskProfileBoundsError(
            f"{len(raw_records)} spans exceed the hard maximum of "
            f"{MAX_PROFILE_SPANS}")
    max_spans = _check_bound(max_spans, "max_spans",
                             maximum=MAX_PROFILE_SPANS)
    max_edges = _check_bound(max_edges, "max_edges",
                             maximum=MAX_PROFILE_EDGES)
    max_bytes = _check_bound(max_bytes, "max_bytes",
                             maximum=MAX_PROFILE_BYTES, minimum=1)
    if isinstance(inputs, (str, bytes)) or not hasattr(inputs, "__iter__"):
        raise TaskProfileError(
            "inputs must be an iterable of strings, not one string",
            code="task_profile.invalid_field")
    base_inputs = list(inputs)
    time_origin = _validate_time_origin(time_origin)

    spans: list[SpanRecord] = []
    seen: set[str] = set()
    for record in raw_records:
        span = _coerce_span(record)
        if span.id in seen:
            raise TaskProfileError(
                f"duplicate span id {span.id!r}",
                code="task_profile.duplicate_id")
        seen.add(span.id)
        spans.append(span)
    by_id = {span.id: span for span in spans}
    for span in spans:
        for parent in span.parents or ():
            if parent not in by_id:
                raise TaskProfileError(
                    f"span {span.id!r} depends on unknown parent "
                    f"{parent!r}",
                    code="task_profile.unknown_parent")

    nodes = tuple(
        DagNode(
            id=span.id,
            duration_ms=span.resolved_duration_ms,
            outcome=span.outcome,
            resource_class=span.resource_class,
            kind=span.kind,
            parents_unknown=span.parents is None,
        )
        for span in spans
    )
    edges = tuple(
        DagEdge(parent, span.id, "finish_to_start")
        for span in spans for parent in (span.parents or ())
    )
    try:
        order = topological_order(Dag(nodes, edges))
    except CycleError as error:
        raise TaskProfileCycleError(error.cycles,
                                    error.components) from error
    except CriticalPathBoundsError as error:
        raise TaskProfileBoundsError(str(error)) from error

    total = len(spans)
    kept: list[SpanRecord] = []
    edge_count = 0
    for span_id in order:
        span = by_id[span_id]
        new_edges = len(span.parents or ())
        if len(kept) >= max_spans or edge_count + new_edges > max_edges:
            break
        kept.append(span)
        edge_count += new_edges
    prefix_reason = "limit" if len(kept) < total else None

    base_provenance: dict[str, Any] = {
        "source": "injected",
        "assembler": "ozagia.core.task_profile",
        "profile_version": PROFILE_VERSION,
    }
    if provenance is not None:
        base_provenance.update(provenance)

    def assemble(keep_count: int, reason: str | None) -> dict[str, Any]:
        included = kept[:keep_count]
        omitted = total - len(included)
        truncated = omitted > 0
        resolved_reason = reason if truncated else None
        next_cursor = (included[-1].id
                       if truncated and included else None)
        data = {
            "profile_version": PROFILE_VERSION,
            "clock": _profile_clock(included),
            "time_origin": time_origin,
            "spans": [span.to_dict() for span in included],
            "edges": _derived_edges(included),
            "coverage": _coverage(included, total, truncated),
            "integrity": _integrity(included, scope),
        }
        instance: dict[str, Any] = {
            "schema": {"namespace": PROFILE_NAMESPACE,
                       "version": PROFILE_VERSION},
            "scope": dict(scope),
            "snapshot": snapshot,
            "inputs": base_inputs,
            "provenance": dict(base_provenance),
            "data": data,
            "omissions": truncation(
                truncated,
                omitted=omitted,
                next_cursor=next_cursor,
                reason=resolved_reason,
            ).to_dict(),
        }
        if created_at is not None:
            instance["created_at"] = created_at
        if trace_id is not None:
            instance["trace_id"] = trace_id
        return instance

    candidate = assemble(len(kept), prefix_reason)
    if len(canonical_json(candidate).encode("utf-8")) <= max_bytes:
        return validate_instance(candidate)

    low, high = 0, len(kept)
    best: int | None = None
    while low <= high:
        middle = (low + high) // 2
        current = assemble(middle, "bytes")
        if len(canonical_json(current).encode("utf-8")) <= max_bytes:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise TaskProfileBoundsError(
            "the smallest possible profile exceeds the byte bound of "
            f"{max_bytes}")
    # Adding a span is normally monotonic in size; check the neighbouring
    # counts so omitted-digit changes cannot hide a slightly larger fit.
    for candidate_count in range(best + 1, len(kept) + 1):
        current = assemble(candidate_count, "bytes")
        if len(canonical_json(current).encode("utf-8")) <= max_bytes:
            best = candidate_count
        else:
            break
    return validate_instance(assemble(best, "bytes"))


# ---------------------------------------------------------------------------
# WP4 adapters: existing records to injected spans
# ---------------------------------------------------------------------------


class TaskReportError(ValueError):
    """A task-report request or record error with a stable ``code``."""

    def __init__(self, message: str, *,
                 code: str = "task_report.invalid_report") -> None:
        if code not in REPORT_ERROR_CODES:
            raise ValueError(f"unknown task-report error code {code!r}")
        self.code = code
        super().__init__(redact_task_text(message, 500))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True)
class TaskRecords:
    """The retained, permitted records one task report is built from.

    Every field is an injected value.  ``run`` is a ``tasks.show_run`` record
    (or a bare manifest); ``goal`` is a goal record; ``test_record`` is a
    working-set test record; ``session`` is a ``DevSession`` data mapping;
    ``daemon`` is an optional list of journal/response timing records; and
    ``evaluation`` is an optional eval run record.  ``prefetch`` is an
    optional ``ozagia.prefetch_ledger`` report.
    """

    run: Mapping[str, Any] | None = None
    goal: Mapping[str, Any] | None = None
    test_record: Mapping[str, Any] | None = None
    session: Mapping[str, Any] | None = None
    daemon: tuple[Mapping[str, Any], ...] = ()
    evaluation: Mapping[str, Any] | None = None
    prefetch: Mapping[str, Any] | None = None
    goal_id: str | None = None
    session_id: str | None = None
    evaluation_id: str | None = None
    observed_at: int | str = UNKNOWN

    def __post_init__(self) -> None:
        for name in ("run", "goal", "test_record", "session", "evaluation",
                     "prefetch"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Mapping):
                raise TaskReportError(
                    f"{name} must be a mapping or None",
                    code="task_report.invalid_record")
        if isinstance(self.daemon, Mapping) or isinstance(self.daemon, (str, bytes)):
            raise TaskReportError(
                "daemon must be an iterable of timing mappings",
                code="task_report.invalid_record")
        records = tuple(self.daemon)
        for item in records:
            if not isinstance(item, Mapping):
                raise TaskReportError(
                    "every daemon timing record must be a mapping",
                    code="task_report.invalid_record")
        object.__setattr__(self, "daemon", records)
        for name in ("goal_id", "session_id", "evaluation_id"):
            value = getattr(self, name)
            if value is not None and (
                    not isinstance(value, str) or not value
                    or len(value) > 128
                    or any(ord(char) < 0x20 for char in value)):
                raise TaskReportError(
                    f"{name} must be a bounded non-empty string or None",
                    code="task_report.invalid_record")
        if isinstance(self.observed_at, bool) or not isinstance(
                self.observed_at, (int, str)):
            raise TaskReportError(
                "observed_at must be an injected integer or bounded string",
                code="task_report.invalid_record")
        if isinstance(self.observed_at, str) and (
                not self.observed_at or len(self.observed_at) > 64):
            raise TaskReportError(
                "observed_at string must be bounded",
                code="task_report.invalid_record")


def _manifest_of(run: Mapping[str, Any] | None) -> tuple[dict[str, Any],
                                                        str]:
    if not isinstance(run, Mapping):
        return {}, "absent"
    if "manifest" in run:
        manifest = run.get("manifest")
        if isinstance(manifest, Mapping):
            return dict(manifest), str(run.get("state") or "complete")
        return {}, str(run.get("state") or "incomplete")
    if any(key in run for key in ("run_id", "status", "steps", "spec")):
        return dict(run), "complete"
    return {}, "invalid"


def _run_outcome(status: Any, state: str) -> str:
    value = str(status or "")
    if value in ("failed", "error"):
        return "failed"
    if value in ("cancelled", "canceled"):
        return "cancelled"
    if state in ("incomplete", "invalid", "absent"):
        return "unknown"
    if value in ("ok", "completed", "passed"):
        return "completed"
    return "unknown"


def _report_status(manifest: Mapping[str, Any], state: str) -> str:
    if not manifest:
        if state == "incomplete":
            return "incomplete"
        return "unknown"
    value = str(manifest.get("status") or "")
    if value in ("skipped", "pending", "running"):
        return "incomplete"
    outcome = _run_outcome(value, state)
    if outcome == "unknown" and state == "incomplete":
        return "incomplete"
    return outcome


def _task_step_kind(tool: Any) -> tuple[str, str]:
    return TASK_TOOL_KINDS.get(str(tool or ""), ("other", "unknown"))


def _task_step_outcome(status: Any) -> str:
    value = str(status or "")
    if value in ("ok", "completed", "passed"):
        return "completed"
    if value in ("error", "failed"):
        return "failed"
    if value in ("blocked", "skipped"):
        return "skipped"
    return "unknown"


def adapt_task_run(run: Mapping[str, Any], *,
                   parents: Iterable[str] | None = None) -> list[SpanRecord]:
    """Turn a task run record or manifest into the run's span tree.

    Only step reports and their explicit root are linked.  Two steps that ran
    in a timestamp order are **not** given a dependency: the runner's steps are
    sequential, but the record does not declare that edge, so it is left
    unknown rather than inferred from timestamps.

    A run directory with no manifest is still a run record.  It becomes a
    single unknown root span (evidence ``inferred``); no step or duration is
    fabricated.
    """
    manifest, state = _manifest_of(run)
    if not manifest:
        if isinstance(run, Mapping) and run.get("state") in (
                "incomplete", "invalid"):
            run_id = str(run.get("run_id") or "unknown")
            return [SpanRecord(
                source_span_id("task", run_id, "run"), "action",
                parents=parents, clock=UNKNOWN, outcome="unknown",
                result=UNKNOWN, evidence="inferred", measurement=UNKNOWN,
                resource_class="tool",
            )]
        raise TaskProfileError("task run has no manifest",
                               code="task_profile.invalid_record")
    run_id = str(manifest.get("run_id") or run.get("run_id") or "unknown")
    root_id = source_span_id("task", run_id, "run")
    started = _timestamp_ms(manifest.get("started_at"))
    finished = _timestamp_ms(manifest.get("finished_at"))
    duration = _seconds_to_ms(manifest.get("duration_s"))
    root_clock = "system" if started is not None or finished is not None else UNKNOWN
    spans = [SpanRecord(
        root_id, "action", parents=parents,
        start_ms=started, end_ms=finished, duration_ms=duration,
        clock=root_clock, outcome=_run_outcome(manifest.get("status"), state),
        result=UNKNOWN, evidence="observed", measurement=(
            "measured" if duration is not None else UNKNOWN),
        resource_class="tool",
    )]
    previous_step: str | None = None
    for index, step in enumerate(list(manifest.get("steps") or [])[:MAX_ADAPTED_SPANS]):
        if not isinstance(step, Mapping):
            continue
        tool = step.get("tool")
        kind, resource_class = _task_step_kind(tool)
        step_duration = _seconds_to_ms(step.get("elapsed_s"))
        usage = step.get("usage") if isinstance(step.get("usage"), Mapping) else {}
        cost = usage.get("cost_usd")
        cost_micros = (int(round(float(cost) * 1_000_000))
                       if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                       and float(cost) >= 0 else None)
        step_id = source_span_id(
            "task", run_id, "step", step.get("id") or index)
        # The task runner executes its steps in manifest order.  That is a
        # declared property of the runner, not an edge inferred from elapsed
        # times; the run-total span stays a separate root so the total is not
        # added to the steps a second time.
        spans.append(SpanRecord(
            step_id,
            kind, parents=((previous_step,) if previous_step else ()),
            duration_ms=step_duration,
            clock="monotonic" if step_duration is not None else UNKNOWN,
            outcome=_task_step_outcome(step.get("status")),
            result=UNKNOWN, evidence="observed",
            measurement="measured" if step_duration is not None else UNKNOWN,
            attempt=None,
            resource_class=resource_class, cost_micros=cost_micros,
        ))
        previous_step = step_id
    return spans


def adapt_goal_record(goal: Mapping[str, Any], *,
                      goal_id: str | None = None,
                      parents: Iterable[str] | None = None) -> list[SpanRecord]:
    """Turn one goal record and its criteria into spans.

    Criterion results are recorded declarations, not re-evaluated evidence:
    the span evidence is ``declared`` and its freshness is unknown.  A
    criterion with no recorded result is ``not_assessed``.
    """
    local_id = str(goal.get("id") or goal_id or "unknown")
    root_id = source_span_id("goal", local_id, "status")
    status = str(goal.get("status") or "unknown")
    outcome = {
        "met": "completed", "open": "pending", "regressed": "failed",
        "blocked": "skipped", "dropped": "cancelled",
    }.get(status, "unknown")
    result = {"met": "passed", "regressed": "failed",
              "dropped": "failed"}.get(status, UNKNOWN)
    spans = [SpanRecord(
        root_id, "other", parents=parents, clock=UNKNOWN, outcome=outcome,
        result=result, evidence="declared", measurement=UNKNOWN,
        resource_class="human",
    )]
    criteria = [item for item in goal.get("criteria") or []
                 if isinstance(item, Mapping)]
    criteria.sort(key=lambda item: str(item.get("id") or ""))
    for criterion in criteria[:MAX_REPORT_CRITERIA]:
        last = criterion.get("last") if isinstance(criterion.get("last"),
                                                    Mapping) else {}
        recorded = str(last.get("status") or "unchecked")
        criterion_result = STATUS_MAP.get(recorded, "unknown")
        spans.append(SpanRecord(
            source_span_id("goal", local_id, "criterion",
                           criterion.get("id") or "unknown"),
            "check", parents=(root_id,), clock=UNKNOWN,
            outcome="completed" if last else "pending",
            result=criterion_result,
            evidence="declared" if last else "inferred",
            measurement=UNKNOWN, resource_class="check",
        ))
    return spans


def _selected_test(selector: str, key: str) -> bool:
    path = key[5:] if key.startswith("test:") else key
    return (path == selector or path.startswith(selector + "::")
            or selector in path)


def _test_outcome(record: Mapping[str, Any] | None,
                  key: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"outcome": "unknown", "recorded_at": None, "run": None}
    try:
        from ozagia.core import working_set as W

        got = W.recorded_outcome(dict(record), key, evidence=True)
        run = got.get("run") if isinstance(got.get("run"), Mapping) else None
        return {"outcome": str(got.get("outcome") or "unknown"),
                "recorded_at": _timestamp_ms(got.get("ts")), "run": run}
    except Exception:  # noqa: BLE001 - the local join below is enough
        pass
    for run in reversed(list(record.get("runs") or [])):
        if not isinstance(run, Mapping):
            continue
        if key in (run.get("errors") or []):
            return {"outcome": "error", "recorded_at": _timestamp_ms(run.get("ts")),
                    "run": dict(run)}
        if key in (run.get("failed") or []):
            return {"outcome": "failed", "recorded_at": _timestamp_ms(run.get("ts")),
                    "run": dict(run)}
        if any(_selected_test(str(selector), key)
               for selector in run.get("selectors") or []):
            if (run.get("complete") and not run.get("filtered")
                    and not run.get("stops")):
                return {"outcome": "passed",
                        "recorded_at": _timestamp_ms(run.get("ts")),
                        "run": dict(run)}
    return {"outcome": "unknown", "recorded_at": None, "run": None}


def adapt_test_record(record: Mapping[str, Any], *,
                      keys: Iterable[str] = (),
                      parents: Iterable[str] | None = None) -> list[SpanRecord]:
    """Turn a working-set test record into run and per-test spans.

    A test result is linked to its deciding run only when ``recorded_outcome``
    names that run.  No run-to-run edge is created from run times.
    """
    if isinstance(record.get("record"), Mapping):
        record = record["record"]  # type: ignore[assignment]
    runs = [run for run in record.get("runs") or [] if isinstance(run, Mapping)]
    runs = runs[-MAX_ADAPTED_SPANS:]
    spans: list[SpanRecord] = []
    run_ids: dict[tuple[int, int], str] = {}
    for index, run in enumerate(runs):
        ts = _timestamp_ms(run.get("ts"))
        run_span_id = source_span_id("test", "run", index, run.get("ts") or "unknown")
        run_ids[(index, int(ts or 0))] = run_span_id
        spans.append(SpanRecord(
            run_span_id, "check", parents=parents,
            start_ms=ts, clock="system" if ts is not None else UNKNOWN,
            outcome="completed" if run.get("complete") else "unknown",
            result=UNKNOWN, evidence="observed", measurement=UNKNOWN,
            resource_class="check",
        ))
    explicit = [str(key) for key in keys if isinstance(key, str)]
    failing = [str(key) for key in (record.get("tests") or {})
               if isinstance(key, str)]
    all_keys = sorted(dict.fromkeys(explicit + failing))
    for key in all_keys[:MAX_REPORT_CRITERIA]:
        got = _test_outcome(record, key)
        outcome = str(got["outcome"])
        result = {"passed": "passed", "failed": "failed",
                  "error": "invalid"}.get(outcome, "unknown")
        deciding = got.get("run") if isinstance(got.get("run"), Mapping) else None
        parent: tuple[str, ...] | None = None
        if deciding is not None:
            deciding_ts = int(_timestamp_ms(deciding.get("ts")) or 0)
            for (index, ts), run_span_id in run_ids.items():
                if ts == deciding_ts:
                    parent = (run_span_id,)
                    break
        spans.append(SpanRecord(
            source_span_id("test", "key", key),
            "check", parents=parent,
            start_ms=got.get("recorded_at"),
            clock="system" if got.get("recorded_at") is not None else UNKNOWN,
            outcome="completed" if deciding is not None else "unknown",
            result=result, evidence="observed" if deciding is not None else "inferred",
            measurement=UNKNOWN, resource_class="check",
        ))
    return spans


SESSION_EDGE_TYPES = frozenset({
    "has_task", "has_plan", "has_step", "attempt", "edit", "verification",
    "decision", "next",
})


def adapt_session_record(session: Mapping[str, Any], *,
                         parents: Iterable[str] | None = None) -> list[SpanRecord]:
    """Turn a ``DevSession`` data record into spans and its declared edges.

    Session timestamps are wall-clock ISO strings, so every span is
    ``clock="system"`` and no duration is derived.  The only dependencies are
    the session's explicit graph edges; a node without an incoming declared
    edge has unknown dependency information.
    """
    nodes = [node for node in session.get("nodes") or [] if isinstance(node, Mapping)]
    nodes = nodes[:MAX_ADAPTED_SPANS]
    node_ids = {str(node.get("id")): node for node in nodes
                if node.get("id") is not None}
    span_ids = {node_id: source_span_id("session", session.get("session_id")
                                        or "unknown", node_id)
                for node_id in node_ids}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in session.get("edges") or []:
        if not isinstance(edge, Mapping):
            continue
        if edge.get("type") not in SESSION_EDGE_TYPES:
            continue
        src = str(edge.get("src") or "")
        tgt = str(edge.get("tgt") or "")
        if src in span_ids and tgt in span_ids:
            incoming[tgt].append(span_ids[src])
    spans: list[SpanRecord] = []
    for node_id, node in node_ids.items():
        node_type = str(node.get("type") or node.get("kind") or "")
        kind, resource_class = _session_kind(node_type)
        passed = node.get("passed")
        outcome = "completed"
        result = UNKNOWN
        if node_type == "Verification" and isinstance(passed, bool):
            result = "passed" if passed else "failed"
            outcome = "completed" if passed else "failed"
        ts = _timestamp_ms(node.get("ts"))
        node_parents: tuple[str, ...] | None
        if incoming[node_id]:
            node_parents = tuple(sorted(set(incoming[node_id])))
        elif node_id == "s1" or node_type == "Session":
            node_parents = tuple(parents or ())
        else:
            node_parents = None
        spans.append(SpanRecord(
            span_ids[node_id], kind, parents=node_parents,
            start_ms=ts, clock="system" if ts is not None else UNKNOWN,
            outcome=outcome, result=result, evidence="declared",
            measurement=UNKNOWN, resource_class=resource_class,
        ))
    return spans


def _session_kind(node_type: str) -> tuple[str, str]:
    if node_type == "Verification":
        return "check", "check"
    if node_type in ("Step", "Attempt", "Edit"):
        return "action", "tool"
    if node_type == "Decision":
        return "human_gate", "human"
    if node_type == "Plan":
        return "other", "human"
    return "other", "human"


def adapt_daemon_evidence(records: Iterable[Mapping[str, Any]], *,
                          parents: Iterable[str] | None = None) -> list[SpanRecord]:
    """Turn daemon request/journal timing records into bounded spans.

    Only timing, status and source identifiers are read.  ``ms`` is the
    daemon's monotonic handler time and becomes a measured duration; a record
    with only a wall-clock ``ts`` has no duration.
    """
    spans: list[SpanRecord] = []
    for index, record in enumerate(list(records)[:MAX_ADAPTED_SPANS]):
        duration = _optional_non_negative_int(record.get("duration_ms"))
        if duration is None:
            ms = record.get("ms")
            if isinstance(ms, (int, float)) and not isinstance(ms, bool):
                duration = max(0, int(round(float(ms))))
        start = _timestamp_ms(record.get("started_at") or record.get("ts"))
        expired = bool(record.get("expired"))
        is_error = bool(record.get("is_error"))
        status = str(record.get("status") or "")
        outcome = ("cancelled" if expired else
                   "failed" if is_error or status in ("error", "failed") else
                   "completed" if record.get("handled") is not False else
                   "unknown")
        if status in ("cancelled", "canceled"):
            outcome = "cancelled"
        op = record.get("op") or record.get("tool") or record.get("type") or "request"
        queue_wait = _optional_non_negative_int(record.get("queue_wait_ms"))
        if queue_wait is None:
            queue_wait = _seconds_to_ms(record.get("waited_s"))
        readiness_wait = _optional_non_negative_int(
            record.get("readiness_wait_ms"))
        spans.append(SpanRecord(
            source_span_id("daemon", record.get("seq") or record.get("id")
                           or index, op),
            "tool_call" if record.get("tool") or record.get("op") else "other",
            parents=parents,
            start_ms=start,
            clock=("monotonic" if duration is not None else
                   "system" if start is not None else UNKNOWN),
            duration_ms=duration, outcome=outcome, result=UNKNOWN,
            evidence="observed",
            measurement="measured" if duration is not None else UNKNOWN,
            resource_class="tool", queue_wait_ms=queue_wait,
            readiness_wait_ms=readiness_wait,
        ))
    return spans


def adapt_evaluation_record(record: Mapping[str, Any], *,
                            parents: Iterable[str] | None = None) -> list[SpanRecord]:
    """Turn one evaluation run record into timing/token spans.

    Timing components are separate roots, not children of the run-total span:
    the run wall time contains the components and model/tool work can overlap,
    so a finish-to-start edge would double-count.  No component-to-component
    edge is invented.
    """
    run_id = str(record.get("run_id") or record.get("id") or "evaluation")
    root_id = source_span_id("eval", run_id, "run")
    duration = _seconds_to_ms(record.get("wall_s")
                              or record.get("elapsed_s"))
    status = str(record.get("status") or "")
    spans = [SpanRecord(
        root_id, "action", parents=parents, duration_ms=duration,
        clock=UNKNOWN, outcome=(
            "completed" if status in ("ok", "completed", "passed") else
            "failed" if status in ("failed", "error") else
            "cancelled" if status in ("cancelled", "canceled") else "unknown"),
        result=UNKNOWN, evidence="observed",
        measurement="measured" if duration is not None else UNKNOWN,
        resource_class="tool",
    )]
    timing = record.get("timing") if isinstance(record.get("timing"),
                                                Mapping) else {}
    tokens_in = _optional_non_negative_int(record.get("tokens_in"))
    tokens_out = _optional_non_negative_int(record.get("tokens_out"))
    cost = record.get("cost_usd")
    cost_micros = (int(round(float(cost) * 1_000_000))
                   if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                   and float(cost) >= 0 else None)
    component_values: dict[str, int] = {}
    for component in ("setup", "index", "model", "tools", "other", "check"):
        value = timing.get(component)
        if isinstance(value, Mapping):
            seconds = value.get("s", value.get("elapsed_s"))
        else:
            seconds = value
        measured = _seconds_to_ms(seconds)
        if measured is None:
            continue
        component_values[component] = measured
        kind, resource_class = {
            "model": ("model_call", "model"),
            "tools": ("tool_call", "tool"),
            "check": ("check", "check"),
            "setup": ("other", "tool"),
            "index": ("index_prepare", "index"),
            "other": ("other", "unknown"),
        }.get(component, ("other", "unknown"))
        spans.append(SpanRecord(
            source_span_id("eval", run_id, component), kind,
            parents=(), duration_ms=measured, clock=UNKNOWN,
            outcome="completed", result=UNKNOWN, evidence="observed",
            measurement="measured", resource_class=resource_class,
            input_tokens=tokens_in if component == "model" else None,
            output_tokens=tokens_out if component == "model" else None,
            cost_micros=cost_micros if component == "model" else None,
        ))
    if "model" not in component_values and (
            tokens_in is not None or tokens_out is not None
            or cost_micros is not None or record.get("model")):
        spans.append(SpanRecord(
            source_span_id("eval", run_id, "model"), "model_call",
            parents=(), clock=UNKNOWN,
            outcome="completed" if status in ("ok", "completed", "passed")
            else "unknown", result=UNKNOWN, evidence="observed",
            measurement="measured" if tokens_in is not None
            or tokens_out is not None else UNKNOWN,
            resource_class="model", input_tokens=tokens_in,
            output_tokens=tokens_out, cost_micros=cost_micros,
        ))
    return spans


def task_profile_spans(records: TaskRecords) -> list[SpanRecord]:
    """Adapt every supplied record, in deterministic source order."""
    spans: list[SpanRecord] = []
    if records.run is not None:
        spans.extend(adapt_task_run(records.run, parents=()))
    if records.goal is not None:
        spans.extend(adapt_goal_record(records.goal, goal_id=records.goal_id,
                                       parents=()))
    if records.test_record is not None:
        criteria = (records.goal or {}).get("criteria") or []
        keys = [str((item.get("args") or {}).get("key"))
                for item in criteria if isinstance(item, Mapping)
                and isinstance(item.get("args"), Mapping)
                and item["args"].get("key")]
        spans.extend(adapt_test_record(records.test_record, keys=keys,
                                       parents=()))
    if records.session is not None:
        spans.extend(adapt_session_record(records.session))
    if records.daemon:
        spans.extend(adapt_daemon_evidence(records.daemon, parents=()))
    if records.evaluation is not None:
        spans.extend(adapt_evaluation_record(records.evaluation, parents=()))
    if len(spans) > MAX_ADAPTED_SPANS:
        raise TaskProfileError(
            f"adapted records produce {len(spans)} spans; the maximum is "
            f"{MAX_ADAPTED_SPANS}", code="task_profile.bounds_exceeded")
    return spans


def task_execution_spans(records: TaskRecords) -> list[SpanRecord]:
    """Adapt only work with measured timing: the run, daemon and eval.

    Goal, test and session spans are evidence records with wall-clock
    timestamps and no durations.  Keeping them out of the execution DAG lets
    the observed critical path be computed without claiming that recorded
    evidence has a measured stage time.
    """
    spans: list[SpanRecord] = []
    if records.run is not None:
        spans.extend(adapt_task_run(records.run, parents=()))
    if records.daemon:
        spans.extend(adapt_daemon_evidence(records.daemon))
    if records.evaluation is not None:
        spans.extend(adapt_evaluation_record(records.evaluation))
    return spans



def build_task_profile_from_records(records: TaskRecords, *,
                                    scope: Mapping[str, Any],
                                    snapshot: str = UNKNOWN,
                                    provenance: Mapping[str, Any] | None = None,
                                    inputs: Iterable[str] = (),
                                    created_at: int | str | None = None,
                                    time_origin: int | str = UNKNOWN,
                                    max_spans: int = DEFAULT_MAX_SPANS,
                                    max_edges: int = DEFAULT_MAX_EDGES,
                                    max_bytes: int = DEFAULT_MAX_BYTES
                                    ) -> dict[str, Any]:
    """Adapt retained records and assemble the WP3 profile instance."""
    if not isinstance(records, TaskRecords):
        raise TaskReportError("records must be task_profile.TaskRecords",
                              code="task_report.invalid_input")
    spans = task_profile_spans(records)
    source_provenance: dict[str, Any] = {
        "source": "retained-records",
        "adapter_version": ADAPTER_VERSION,
    }
    if provenance is not None:
        if not isinstance(provenance, Mapping):
            raise TaskReportError("provenance must be a mapping",
                                  code="task_report.invalid_input")
        source_provenance.update(provenance)
    return build_profile(
        spans, scope=scope, snapshot=snapshot, provenance=source_provenance,
        inputs=inputs, created_at=created_at, time_origin=time_origin,
        max_spans=max_spans, max_edges=max_edges, max_bytes=max_bytes)


# ---------------------------------------------------------------------------
# Bounded post-run task report
# ---------------------------------------------------------------------------


def _run_report(manifest: Mapping[str, Any], state: str,
                fallback_run_id: str | None = None) -> dict[str, Any]:
    run_id = manifest.get("run_id") or fallback_run_id
    spec = manifest.get("spec") if isinstance(manifest.get("spec"),
                                               Mapping) else {}
    return {
        "run_id": _bounded_text(run_id, 120) if run_id else None,
        "state": state,
        "status": _bounded_text(manifest.get("status"), 40) or None,
        "stop": _bounded_text(manifest.get("stop"), 40) or None,
        "stop_reason": _bounded_text(manifest.get("stop_detail"), 300) or None,
        "started_at": _bounded_text(manifest.get("started_at"), 64) or None,
        "finished_at": _bounded_text(manifest.get("finished_at"), 64) or None,
        "duration_ms": _seconds_to_ms(manifest.get("duration_s")),
        "task": {
            "id": _bounded_text(spec.get("id"), 64) or None,
            "version": spec.get("version")
            if isinstance(spec.get("version"), int) else None,
            "title": _bounded_text(spec.get("title"), 200) or None,
            "tools": sorted(_bounded_text(item, 60) or ""
                            for item in (spec.get("tools") or [])
                            if isinstance(item, str))[:MAX_REPORT_ITEMS],
            "requires_graph": bool(spec.get("requires_graph")),
        },
    }


def _criterion_items(goal: Mapping[str, Any] | None,
                     test_record: Mapping[str, Any] | None,
                     freshness: Mapping[str, Any] | None,
                     limit: int) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(goal, Mapping):
        return [], 0
    criteria = [item for item in goal.get("criteria") or []
                if isinstance(item, Mapping)]
    criteria.sort(key=lambda item: tuple(
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", str(item.get("id") or ""))))
    total = len(criteria)
    rows: list[dict[str, Any]] = []
    for criterion in criteria[:limit]:
        kind = str(criterion.get("kind") or "unknown")
        args = criterion.get("args") if isinstance(criterion.get("args"),
                                                    Mapping) else {}
        last = criterion.get("last") if isinstance(criterion.get("last"),
                                                    Mapping) else {}
        recorded = str(last.get("status") or "unchecked")
        result = STATUS_MAP.get(recorded, "unknown")
        join = None
        key = args.get("key")
        if kind in ("test_passes", "test_fails") and isinstance(key, str):
            join = _test_outcome(test_record, key)
            join = {
                "key": _bounded_text(key, 300),
                "outcome": join["outcome"],
                "recorded_at": join["recorded_at"],
            }
        explicit = (freshness or {}).get(str(criterion.get("id") or ""))
        if explicit in FRESHNESS:
            item_freshness = explicit
        elif last:
            item_freshness = "unknown"
        else:
            item_freshness = "absent"
        evidence = ("observed" if join and join["outcome"] != "unknown"
                    else "declared" if last else "inferred")
        rows.append({
            "id": _bounded_text(criterion.get("id"), 64) or "unknown",
            "kind": _bounded_text(kind, 60) or "unknown",
            "result": result,
            "recorded_status": _bounded_text(recorded, 40) or "unchecked",
            "freshness": item_freshness,
            "evidence": evidence,
            "current": item_freshness == "current",
            "observed_at": _timestamp_ms(last.get("at")),
            "reason": _bounded_text(last.get("reason"), 300) or None,
            "test": join,
            "source": "ozagia.core.goals",
        })
    return rows, total


def _criteria_status(rows: Sequence[Mapping[str, Any]],
                     total: int) -> str:
    if not rows or total == 0:
        return "unknown"
    if any(row.get("result") == "failed" for row in rows):
        return "failed"
    if len(rows) < total:
        return "unknown"
    if all(row.get("result") == "passed" for row in rows):
        return "passed"
    return "unknown"


def _safe_report_path(value: Any) -> str | None:
    """A bounded repository-relative path, or None for an unsafe value."""
    text = _bounded_text(value, 300)
    if not text:
        return None
    path = text.replace("\\", "/")
    if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
        return None
    parts = path.split("/")
    if any(part == ".." for part in parts):
        return None
    return path


def _report_work(manifest: Mapping[str, Any], *,
                 limit: int) -> dict[str, Any]:
    steps = [step for step in manifest.get("steps") or []
             if isinstance(step, Mapping)]
    planned = [{
        "id": _bounded_text(step.get("id"), 60) or "unknown",
        "tool": _bounded_text(step.get("tool"), 60) or "unknown",
        "status": _bounded_text(step.get("status"), 40) or "unknown",
        "duration_ms": _seconds_to_ms(step.get("elapsed_s")),
    } for step in steps[:limit]]
    executed = [step for step in steps
                if str(step.get("status") or "") not in ("skipped",)]
    artifacts = [item for item in manifest.get("artifacts") or []
                 if isinstance(item, Mapping)]
    changed = []
    for item in artifacts[:limit]:
        path = _safe_report_path(item.get("path"))
        if not path:
            continue
        entry = {"path": path}
        digest = item.get("sha256")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{12,64}", digest):
            entry["sha256"] = digest
        size = item.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            entry["size"] = size
        changed.append(entry)
    verified = []
    for step in steps:
        if str(step.get("tool") or "") in ("test", "lint", "build") \
                and str(step.get("status") or "") in ("ok", "passed"):
            verified.append({"kind": "step", "id": step.get("id"),
                             "tool": step.get("tool")})
    spec = manifest.get("spec") if isinstance(manifest.get("spec"),
                                               Mapping) else {}
    return {
        "requested": {
            "task": _bounded_text(spec.get("id"), 64) or None,
            "objective": (_bounded_text(manifest.get("objective"), 200)
                          or _bounded_text(spec.get("title"), 200) or None),
            "criteria": None,
        },
        "planned": {
            "steps": len(steps),
            "items": planned,
        },
        "executed": {
            "steps": len(executed),
            "failed": sum(1 for step in steps
                          if str(step.get("status")) in ("error", "failed")),
            "skipped": sum(1 for step in steps
                           if str(step.get("status")) == "skipped"),
        },
        "changed": {"count": len(changed), "files": changed},
        "verified": {"count": len(verified),
                     "items": verified[:limit]},
    }


def _environment_label(value: Any) -> str | None:
    text = _bounded_text(value, 120)
    if not text:
        return None
    if text.startswith(("/", "~")) or (len(text) > 1 and text[1] == ":"):
        name = Path(text).name
        return _bounded_text(name, 120)
    return text


def _selections(records: TaskRecords, manifest: Mapping[str, Any],
                goal: Mapping[str, Any] | None) -> dict[str, Any]:
    models: set[str] = set()
    harnesses: set[str] = set()
    environments: set[str] = set()
    spec = manifest.get("spec") if isinstance(manifest.get("spec"),
                                               Mapping) else {}
    tools = {_bounded_text(item, 60) for item in spec.get("tools") or []
             if isinstance(item, str)}
    tools.update(_bounded_text(step.get("tool"), 60)
                 for step in manifest.get("steps") or []
                 if isinstance(step, Mapping) and step.get("tool"))
    tools.discard(None)
    for key in ("model", "harness"):
        value = manifest.get(key)
        if isinstance(value, str) and value:
            (models if key == "model" else harnesses).add(
                redact_task_text(value, 120))
    if isinstance(goal, Mapping):
        for claim in goal.get("claimed_by") or []:
            if isinstance(claim, Mapping) and isinstance(claim.get("harness"),
                                                         str):
                harnesses.add(redact_task_text(claim["harness"], 120))
    evaluation = records.evaluation
    if isinstance(evaluation, Mapping):
        for value in (evaluation.get("model"),
                      (evaluation.get("backend") or {}).get("model")
                      if isinstance(evaluation.get("backend"), Mapping)
                      else None):
            if isinstance(value, str) and value:
                models.add(redact_task_text(value, 120))
        env = evaluation.get("environment")
        if isinstance(env, str) and env:
            label = _environment_label(env)
            if label:
                environments.add(label)
        elif isinstance(env, Mapping):
            for key in ("name", "id", "interpreter"):
                if isinstance(env.get(key), str) and env[key]:
                    label = _environment_label(env[key])
                    if label:
                        environments.add(label)
    return {
        "model": sorted(models)[:MAX_REPORT_ITEMS],
        "harness": sorted(harnesses)[:MAX_REPORT_ITEMS],
        "tools": sorted(tools)[:MAX_REPORT_ITEMS],
        "environment": sorted(environments)[:MAX_REPORT_ITEMS],
        "recorded": {
            "model": bool(models),
            "harness": bool(harnesses),
            "tools": bool(tools),
            "environment": bool(environments),
        },
    }


def _snapshot_report(manifest: Mapping[str, Any],
                     snapshot: str) -> dict[str, Any]:
    preflight = manifest.get("preflight") if isinstance(
        manifest.get("preflight"), Mapping) else {}
    recorded = preflight.get("snapshot") if isinstance(
        preflight.get("snapshot"), Mapping) else {}
    state = str(recorded.get("state") or "not_recorded")
    freshness = {
        "fresh": "current", "stale": "stale", "missing": "absent",
        "invalid": "unknown", "not_checked": "unknown",
        "not_recorded": "unknown",
    }.get(state, "unknown")
    details = recorded.get("details") if isinstance(
        recorded.get("details"), Mapping) else {}
    return {
        "id": snapshot,
        "state": state,
        "freshness": freshness,
        "required": bool(recorded.get("required")),
        "source": "ozagia.core.tasks preflight snapshot",
        "details": {
            key: details[key] for key in (
                "snapshot_head", "head", "tree_changed", "root_mismatch",
                "generated_at")
            if key in details and isinstance(details[key], (str, int, bool,
                                                            type(None)))
        },
        "reason": _bounded_text(recorded.get("error"), 300) or None,
    }


def _failures(manifest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    stop = manifest.get("stop")
    if stop:
        failures.append({"kind": "stop", "id": _bounded_text(stop, 40),
                         "reason": _bounded_text(manifest.get("stop_detail"),
                                                 300)})
    preflight = manifest.get("preflight") if isinstance(
        manifest.get("preflight"), Mapping) else {}
    preflight_failures = preflight.get("failures")
    if not isinstance(preflight_failures, list):
        preflight_failures = []
    for item in preflight_failures[:MAX_REPORT_ITEMS]:
        if isinstance(item, str):
            failures.append({"kind": "preflight", "reason":
                             _bounded_text(item, 300)})
    test_plan = manifest.get("test_plan") if isinstance(
        manifest.get("test_plan"), Mapping) else {}
    if test_plan.get("fallback_used") or test_plan.get("fallback"):
        fallbacks.append({
            "kind": "test_plan",
            "fallback": _bounded_text(test_plan.get("fallback"), 60),
            "reason": _bounded_text(test_plan.get("fallback_reason"), 300),
        })
    if test_plan.get("refused"):
        skipped.append({"kind": "test_plan", "id": "tests",
                        "reason": _bounded_text(test_plan.get("fallback_reason"),
                                                300)})
    for step in manifest.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        status = str(step.get("status") or "")
        row = {"kind": "step", "id": _bounded_text(step.get("id"), 60),
               "tool": _bounded_text(step.get("tool"), 60),
               "reason": _bounded_text(step.get("summary"), 300)}
        if status in ("error", "failed", "blocked"):
            (blocked if status == "blocked" else failures).append(row)
        elif status == "skipped":
            skipped.append(row)
    return {"failures": failures, "fallbacks": fallbacks,
            "blocked": blocked, "skipped": skipped}


def _measurements(manifest: Mapping[str, Any],
                  evaluation: Mapping[str, Any] | None,
                  limit: int) -> dict[str, Any]:
    budget = manifest.get("budget") if isinstance(manifest.get("budget"),
                                                   Mapping) else {}
    used = budget.get("used") if isinstance(budget.get("used"), Mapping) else {}
    tokens_total = _optional_non_negative_int(used.get("tokens"))
    cost = used.get("cost_usd")
    cost_micros = (int(round(float(cost) * 1_000_000))
                   if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                   and float(cost) >= 0 else None)
    tokens_in = tokens_out = None
    if isinstance(evaluation, Mapping):
        tokens_in = _optional_non_negative_int(evaluation.get("tokens_in"))
        tokens_out = _optional_non_negative_int(evaluation.get("tokens_out"))
        eval_cost = evaluation.get("cost_usd")
        if cost_micros is None and isinstance(eval_cost, (int, float)) \
                and not isinstance(eval_cost, bool) and float(eval_cost) >= 0:
            cost_micros = int(round(float(eval_cost) * 1_000_000))
    stage_timing: list[dict[str, Any]] = []
    for step in list(manifest.get("steps") or [])[:limit]:
        if not isinstance(step, Mapping):
            continue
        duration = _seconds_to_ms(step.get("elapsed_s"))
        if duration is None:
            continue
        stage_timing.append({
            "stage": f"step:{_bounded_text(step.get('id'), 60) or 'unknown'}",
            "duration_ms": duration,
            "measurement": "measured",
            "source": "task step elapsed_s",
        })
    if isinstance(evaluation, Mapping) and isinstance(
            evaluation.get("timing"), Mapping):
        for name, value in sorted(evaluation["timing"].items()):
            seconds = (value.get("s", value.get("elapsed_s"))
                       if isinstance(value, Mapping) else value)
            duration = _seconds_to_ms(seconds)
            if duration is None:
                continue
            stage_timing.append({
                "stage": f"evaluation:{_bounded_text(name, 60)}",
                "duration_ms": duration,
                "measurement": "measured",
                "source": "evaluation timing",
            })
    resources = manifest.get("resources") if isinstance(
        manifest.get("resources"), Mapping) else {}
    declared = resources.get("declared") if isinstance(
        resources.get("declared"), Mapping) else {}
    return {
        "tokens": {
            "total": tokens_total,
            "input": tokens_in,
            "output": tokens_out,
            "measurement": "measured" if tokens_total is not None
            or tokens_in is not None or tokens_out is not None else "unknown",
            "source": "executor self-report",
        },
        "cost": {
            "micros": cost_micros,
            "measurement": "measured" if cost_micros is not None else "unknown",
            "source": "executor self-report",
        },
        "resources": {
            "declared": {
                key: _optional_non_negative_int(declared.get(key))
                for key in ("memory_mb", "disk_mb", "pids", "wall_s")
            },
            "used": {
                key: _optional_non_negative_int(used.get(key))
                for key in ("memory_mb", "disk_mb", "pids", "wall_s")
            },
            "measurement": "measured" if used else "unknown",
            "source": "executor self-report",
        },
        "stage_timing": stage_timing[:limit],
        "model_calls": _optional_non_negative_int(used.get("model_calls")),
    }


def _report_prefetch(prefetch: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(prefetch, Mapping):
        return {
            "state": "not_recorded",
            "prepared": 0,
            "hits": 0,
            "unused": 0,
            "wasted_ms": 0,
            "wasted_bytes": 0,
            "overhead_ms": 0,
            "estimated_saved_ms": 0,
            "rejected": {},
        }
    schema = prefetch.get("schema") if isinstance(prefetch.get("schema"),
                                                  Mapping) else {}
    if schema.get("namespace") != "ozagia.prefetch_ledger":
        return {"state": "invalid", "reason": "not a prefetch ledger report"}
    if "hash" in prefetch:
        try:
            expected = content_hash({
                key: value for key, value in prefetch.items() if key != "hash"})
        except Exception:  # noqa: BLE001 - a non-canonical ledger is invalid
            return {"state": "invalid",
                    "reason": "the prefetch ledger is not canonical"}
        if prefetch.get("hash") != expected:
            return {"state": "invalid",
                    "reason": "the prefetch ledger hash does not match"}
    return {
        "state": "recorded",
        "prepared": _optional_non_negative_int(prefetch.get("prepared")) or 0,
        "hits": _optional_non_negative_int(prefetch.get("hits")) or 0,
        "unused": _optional_non_negative_int(prefetch.get("unused")) or 0,
        "discarded": _optional_non_negative_int(
            prefetch.get("discarded")) or 0,
        "wasted_ms": _optional_non_negative_int(prefetch.get("wasted_ms")) or 0,
        "wasted_bytes": _optional_non_negative_int(
            prefetch.get("wasted_bytes")) or 0,
        "overhead_ms": _optional_non_negative_int(
            prefetch.get("overhead_ms")) or 0,
        "estimated_saved_ms": _optional_non_negative_int(
            prefetch.get("estimated_saved_ms")) or 0,
        "rejected": {
            str(key): value for key, value in sorted(
                (prefetch.get("rejected") or {}).items())
            if isinstance(value, int)
        } if isinstance(prefetch.get("rejected"), Mapping) else {},
    }


def _schema_version(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value and len(value) <= 64:
        return value
    return None


def _source_rows(records: TaskRecords, manifest: Mapping[str, Any],
                 state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    run_id = manifest.get("run_id")
    if not run_id and isinstance(records.run, Mapping):
        run_id = records.run.get("run_id")
    run_id = str(run_id or "unknown")
    rows.append({
        "source": "ozagia.core.tasks.show_run",
        "source_id": run_id,
        "schema_version": _schema_version(manifest.get("schema")),
        "state": state,
        "observed_at": _timestamp_ms(manifest.get("finished_at")
                                     or manifest.get("started_at")),
        "freshness": "current" if state == "complete" else "unknown",
    })
    if isinstance(records.goal, Mapping):
        rows.append({
            "source": "ozagia.core.goals",
            "source_id": str(records.goal.get("id") or records.goal_id
                             or "unknown"),
            "schema_version": _schema_version(records.goal.get("version")),
            "state": "present",
            "observed_at": records.observed_at,
            "freshness": "unknown",
        })
    if records.test_record is not None:
        rows.append({
            "source": "ozagia.core.working_set.load_test_record",
            "source_id": "working_set/tests.json",
            "schema_version": None,
            "state": "present",
            "observed_at": records.observed_at,
            "freshness": "unknown",
        })
    if isinstance(records.session, Mapping):
        rows.append({
            "source": "ozagia.core.session",
            "source_id": str(records.session.get("session_id")
                             or records.session_id or "unknown"),
            "schema_version": _schema_version(records.session.get("version")),
            "state": "present",
            "observed_at": records.observed_at,
            "freshness": "unknown",
        })
    if records.daemon:
        rows.append({
            "source": "ozagia.daemon.timing",
            "source_id": "records",
            "schema_version": None,
            "state": "present",
            "observed_at": records.observed_at,
            "freshness": "unknown",
        })
    if isinstance(records.evaluation, Mapping):
        rows.append({
            "source": "ozagia.eval.run",
            "source_id": str(records.evaluation.get("run_id")
                             or records.evaluation_id or "unknown"),
            "schema_version": _schema_version(
                records.evaluation.get("schema")),
            "state": "present",
            "observed_at": records.observed_at,
            "freshness": "unknown",
        })
    rows.sort(key=lambda row: (str(row["source"]), str(row["source_id"])))
    return rows[:MAX_REPORT_SOURCES]


def _standard_unknowns(records: TaskRecords,
                       manifest: Mapping[str, Any]) -> list[str]:
    items = [
        "criterion results are read from the goal record; the report does "
        "not re-evaluate them",
        "a recorded snapshot state is not proof that the graph is current now",
        "prompts, tool inputs and outputs, raw logs and file contents are not "
        "included",
        "the interpreter used by a recorded test is not recorded",
    ]
    if not records.evaluation:
        items.append("no evaluation record was supplied; model and token "
                     "measurements may be incomplete")
    if not records.daemon:
        items.append("no daemon timing record referenced this run or session")
    if not isinstance(records.prefetch, Mapping):
        items.append("no prefetch ledger was supplied; prefetch use and waste "
                     "were not measured")
    raw_unassessed = manifest.get("what_was_not_assessed")
    if not isinstance(raw_unassessed, list):
        raw_unassessed = []
    for item in raw_unassessed:
        text = _bounded_text(item, 300)
        if text:
            items.append(text)
    return sorted(dict.fromkeys(items))[:MAX_REPORT_UNKNOWNS]


def _report_coverage(records: TaskRecords, manifest: Mapping[str, Any],
                     state: str,
                     profile: Mapping[str, Any],
                     execution_profile: Mapping[str, Any],
                     criteria_rows: Sequence[Mapping[str, Any]],
                     criteria_total: int) -> dict[str, Any]:
    data = profile.get("data") if isinstance(profile.get("data"),
                                             Mapping) else {}
    coverage = data.get("coverage") if isinstance(data.get("coverage"),
                                                  Mapping) else {}
    execution_data = execution_profile.get("data") if isinstance(
        execution_profile.get("data"), Mapping) else {}
    execution_coverage = execution_data.get("coverage") if isinstance(
        execution_data.get("coverage"), Mapping) else {}
    criteria_current = sum(1 for row in criteria_rows if row.get("current"))
    test_criteria = any(
        row.get("kind") in ("test_passes", "test_fails")
        or row.get("test") is not None for row in criteria_rows)
    complete = bool(
        manifest and isinstance(records.goal, Mapping)
        and criteria_total > 0 and criteria_current == criteria_total
        and (not test_criteria or records.test_record is not None)
        and execution_coverage.get("timing_complete") is True
        and not (profile.get("omissions") or {}).get("truncated")
    )
    return {
        "complete": complete,
        "run": ("present" if manifest else
                state if state in ("incomplete", "invalid") else "absent"),
        "goal": ("present" if isinstance(records.goal, Mapping)
                 else "not_requested"),
        "criteria": {
            "total": criteria_total,
            "reported": len(criteria_rows),
            "current": criteria_current,
        },
        "test_record": ("present" if records.test_record is not None
                        else "not_requested"),
        "session": ("present" if isinstance(records.session, Mapping)
                    else "not_requested"),
        "daemon": {"records": len(records.daemon),
                   "state": "present" if records.daemon else "not_requested"},
        "evaluation": ("present" if isinstance(records.evaluation, Mapping)
                       else "not_requested"),
        "timing": {
            "spans": execution_coverage.get("spans_included"),
            "measured": execution_coverage.get("measured"),
            "unknown_duration": execution_coverage.get("unknown_duration"),
            "timing_complete": execution_coverage.get("timing_complete"),
        },
        "evidence_timing": {
            "spans": coverage.get("spans_included"),
            "unknown_duration": coverage.get("unknown_duration"),
            "timing_complete": coverage.get("timing_complete"),
        },
        "prefetch": ("recorded" if isinstance(records.prefetch, Mapping)
                     else "not_recorded"),
    }


def _trim_report(report: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    def size() -> int:
        return len(canonical_json(report).encode("utf-8"))

    def trim_lists(limit: int) -> int:
        dropped = 0
        for path in (
            ("outcome", "criteria", "items"),
            ("work", "planned", "items"),
            ("work", "changed", "files"),
            ("measurements", "stage_timing"),
            ("failures",),
            ("fallbacks",),
            ("blocked",),
            ("skipped",),
            ("sources",),
            ("not_assessed",),
            ("critical_path", "blocking"),
            ("execution_critical_path", "blocking"),
            ("prefetch", "items"),
            ("critical_path", "path"),
            ("execution_critical_path", "path"),
        ):
            target: Any = report
            for part in path[:-1]:
                target = target.get(part) if isinstance(target, dict) else None
                if target is None:
                    break
            if not isinstance(target, dict):
                continue
            items = target.get(path[-1])
            while isinstance(items, list) and items and size() > limit:
                items.pop()
                dropped += 1
        return dropped

    if size() <= max_bytes:
        return report
    dropped = trim_lists(max_bytes)
    if dropped:
        previous = report.get("truncated") if isinstance(
            report.get("truncated"), Mapping) else {}
        omitted = int(previous.get("omitted") or 0) + dropped
        report["truncated"] = _truncation_dict(
            True, omitted=omitted, reason="bytes")
        # The envelope itself adds bytes; trim to a small margin so the
        # final canonical document still fits the caller's bound.
        more = trim_lists(max_bytes - 128)
        if more:
            report["truncated"] = _truncation_dict(
                True, omitted=omitted + more, reason="bytes")
    if size() > max_bytes:
        raise TaskReportError(
            f"task report exceeds the byte bound of {max_bytes}",
            code="task_report.bounds_exceeded")
    return report


def _truncation_dict(truncated: bool, *, omitted: int = 0,
                     reason: str | None = None) -> dict[str, Any]:
    return truncation(truncated, omitted=omitted, reason=reason).to_dict()


def build_task_report(records: TaskRecords, *,
                      scope: Mapping[str, Any] | None = None,
                      snapshot: str = UNKNOWN,
                      criterion_freshness: Mapping[str, Any] | None = None,
                      created_at: int | str | None = None,
                      max_criteria: int = MAX_REPORT_CRITERIA,
                      max_changed: int = MAX_REPORT_CHANGED,
                      max_sources: int = MAX_REPORT_SOURCES,
                      max_bytes: int = DEFAULT_MAX_REPORT_BYTES
                      ) -> dict[str, Any]:
    """Build the bounded ``ozagia.task_report`` v1.0 document.

    The function is deterministic over ``records``.  It reads no clock, file
    or store: a caller that wants a real elapsed-time value injects
    ``created_at``.  The report contains allowlisted metadata, hashes and
    redacted reasons only.
    """
    if not isinstance(records, TaskRecords):
        raise TaskReportError("records must be task_profile.TaskRecords",
                              code="task_report.invalid_input")
    manifest, state = _manifest_of(records.run)
    fallback_run_id = None
    if isinstance(records.run, Mapping):
        value = records.run.get("run_id")
        if isinstance(value, str) and value:
            fallback_run_id = value
    if not manifest and state not in ("incomplete", "invalid"):
        raise TaskReportError("a task report needs a run manifest",
                              code="task_report.invalid_record")
    if isinstance(max_criteria, bool) or not isinstance(max_criteria, int) \
            or max_criteria < 1 or max_criteria > MAX_REPORT_CRITERIA:
        raise TaskReportError(
            f"max_criteria must be between 1 and {MAX_REPORT_CRITERIA}",
            code="task_report.invalid_input")
    if isinstance(max_changed, bool) or not isinstance(max_changed, int) \
            or max_changed < 1 or max_changed > MAX_REPORT_CHANGED:
        raise TaskReportError(
            f"max_changed must be between 1 and {MAX_REPORT_CHANGED}",
            code="task_report.invalid_input")
    if isinstance(max_sources, bool) or not isinstance(max_sources, int) \
            or max_sources < 1 or max_sources > MAX_REPORT_SOURCES:
        raise TaskReportError(
            f"max_sources must be between 1 and {MAX_REPORT_SOURCES}",
            code="task_report.invalid_input")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or max_bytes < 1024 or max_bytes > MAX_TASK_REPORT_BYTES:
        raise TaskReportError(
            f"max_bytes must be between 1024 and {MAX_TASK_REPORT_BYTES}",
            code="task_report.invalid_input")
    if criterion_freshness is not None and not isinstance(
            criterion_freshness, Mapping):
        raise TaskReportError("criterion_freshness must be a mapping",
                              code="task_report.invalid_input")
    run_id = str(manifest.get("run_id") or fallback_run_id or "unknown")
    spec = manifest.get("spec") if isinstance(manifest.get("spec"),
                                               Mapping) else {}
    task_id = str(spec.get("id") or run_id)
    if scope is None:
        scope = {"task": f"task:{task_id}", "run": f"run:{run_id}"}
        if records.goal_id or (isinstance(records.goal, Mapping)
                               and records.goal.get("id")):
            goal_id = records.goal_id or records.goal.get("id")
            scope["goal"] = f"goal:{goal_id}"
    if not isinstance(scope, Mapping):
        raise TaskReportError("scope must be a mapping",
                              code="task_report.invalid_input")
    if not isinstance(snapshot, str) or not snapshot or len(snapshot) > 512:
        raise TaskReportError("snapshot must be a bounded non-empty string",
                              code="task_report.invalid_input")
    profile = build_task_profile_from_records(
        records, scope=scope, snapshot=snapshot, inputs=(f"run:{run_id}",),
        time_origin=UNKNOWN)
    critical = profile_critical_path(profile)
    execution_profile = build_profile(
        task_execution_spans(records), scope=scope, snapshot=snapshot,
        provenance={"source": "retained-execution-records",
                    "adapter_version": ADAPTER_VERSION},
        time_origin=UNKNOWN)
    execution_critical = profile_critical_path(execution_profile)
    goal = records.goal if isinstance(records.goal, Mapping) else None
    rows, criteria_total = _criterion_items(
        goal, records.test_record, criterion_freshness, max_criteria)
    criteria_status = _criteria_status(rows, criteria_total)
    run_status = _report_status(manifest, state)
    failed_criterion = any(row.get("result") == "failed" for row in rows)
    current = bool(rows) and all(row.get("current") for row in rows)
    if run_status == "cancelled":
        outcome_status = "cancelled"
    elif failed_criterion or run_status == "failed":
        outcome_status = "failed"
    elif (run_status == "completed" and criteria_status == "passed" and current):
        outcome_status = "passed"
    elif run_status == "unknown" and not rows:
        outcome_status = "unknown"
    else:
        outcome_status = "incomplete"
    if outcome_status == "passed":
        outcome_reason = "every criterion is current and passed"
    elif failed_criterion:
        outcome_reason = "one or more criteria failed"
    elif run_status in ("failed", "cancelled"):
        outcome_reason = "the run did not complete"
    elif run_status != "completed":
        outcome_reason = f"the run status is {run_status}"
    elif criteria_status == "failed":
        outcome_reason = "one or more criteria failed"
    else:
        outcome_reason = ("one or more criteria are unknown, unassessed or "
                          "not current")
    work = _report_work(manifest, limit=max_changed)
    work["requested"]["criteria"] = criteria_total
    verified_criteria = [row["id"] for row in rows
                         if row.get("result") == "passed"
                         and row.get("test") and row["test"].get("outcome")
                         == "passed"]
    work["verified"]["count"] = max(
        work["verified"]["count"], len(verified_criteria))
    work["verified"]["criteria"] = verified_criteria[:max_changed]
    failures = _failures(manifest)
    coverage = _report_coverage(records, manifest, state, profile,
                                execution_profile, rows, criteria_total)
    report: dict[str, Any] = {
        "schema": {"namespace": REPORT_NAMESPACE, "version": REPORT_VERSION},
        "report_version": REPORT_VERSION,
        "scope": dict(scope),
        "run": _run_report(manifest, state, fallback_run_id),
        "outcome": {
            "status": outcome_status,
            "run_status": run_status,
            "criteria_status": criteria_status,
            "criteria_current": current,
            "complete": outcome_status == "passed" and coverage["complete"],
            "reason": outcome_reason,
            "criteria": {
                "total": criteria_total,
                "counts": _result_counts(rows),
                "items": rows,
            },
        },
        "work": work,
        "selections": _selections(records, manifest, goal),
        "snapshot": _snapshot_report(manifest, snapshot),
        "failures": failures["failures"],
        "fallbacks": failures["fallbacks"],
        "blocked": failures["blocked"],
        "skipped": failures["skipped"],
        "measurements": _measurements(manifest, records.evaluation,
                                      MAX_REPORT_ITEMS),
        "critical_path": critical,
        "execution_critical_path": execution_critical,
        "prefetch": _report_prefetch(records.prefetch),
        "sources": _source_rows(records, manifest, state)[:max_sources],
        "coverage": coverage,
        "not_assessed": _standard_unknowns(records, manifest),
        "bounds": {
            "criteria": max_criteria,
            "changed": max_changed,
            "sources": max_sources,
            "max_bytes": max_bytes,
            "spans": profile.get("data", {}).get("coverage", {}).get(
                "spans_total"),
            "profile_truncated": (profile.get("omissions") or {}).get(
                "truncated"),
        },
        "truncated": _truncation_dict(False),
    }
    if created_at is not None:
        if isinstance(created_at, bool) or not isinstance(created_at, (int, str)):
            raise TaskReportError("created_at must be an integer or string",
                                  code="task_report.invalid_input")
        report["created_at"] = created_at
    profile_omissions = profile.get("omissions") or {}
    if profile_omissions.get("truncated"):
        report["truncated"] = _truncation_dict(
            True, omitted=int(profile_omissions.get("omitted") or 1),
            reason=str(profile_omissions.get("reason") or "limit"))
    report = _trim_report(report, max_bytes)
    if report.get("truncated", {}).get("truncated"):
        report["outcome"]["complete"] = False
    report["hash"] = task_report_hash(report)
    return validate_task_report(report)


def _result_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        result = str(row.get("result") or "unknown")
        counts[result] = counts.get(result, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def task_report_hash(report: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical report, excluding ``hash`` and ``created_at``."""
    if not isinstance(report, Mapping):
        raise TaskReportError("report must be a mapping",
                              code="task_report.invalid_report")
    body = {key: value for key, value in report.items()
            if key not in ("hash", "created_at")}
    return content_hash(body)


def validate_task_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the report version, hash and canonical form."""
    if not isinstance(report, Mapping):
        raise TaskReportError("report must be a mapping",
                              code="task_report.invalid_report")
    normalized = dict(report)
    schema = normalized.get("schema")
    if not isinstance(schema, Mapping) \
            or schema.get("namespace") != REPORT_NAMESPACE:
        raise TaskReportError(
            f"report schema namespace must be {REPORT_NAMESPACE!r}",
            code="task_report.invalid_report")
    if schema.get("version") != REPORT_VERSION:
        raise TaskReportError(
            f"report schema version must be {REPORT_VERSION!r}",
            code="task_report.invalid_report")
    if normalized.get("report_version") != REPORT_VERSION:
        raise TaskReportError(
            f"report_version must be {REPORT_VERSION!r}",
            code="task_report.invalid_report")
    for key in REPORT_SCHEMA["required"]:
        if key not in normalized:
            raise TaskReportError(
                f"report is missing required field {key!r}",
                code="task_report.invalid_report")
    allowed = set(REPORT_SCHEMA["types"]["report"]["fields"]) | {
        "schema", "created_at"}
    unknown = [key for key in normalized
               if key not in allowed and not str(key).startswith("x-")]
    if unknown:
        raise TaskReportError(
            f"report has unknown fields: {sorted(unknown)}",
            code="task_report.invalid_report")
    if normalized.get("outcome", {}).get("status") not in REPORT_STATUSES:
        raise TaskReportError("report outcome status is invalid",
                              code="task_report.invalid_report")
    try:
        canonical_json(normalized)
    except Exception as exc:  # noqa: BLE001 - re-raised with a report code
        raise TaskReportError(f"report is not canonical JSON: {exc}",
                              code="task_report.invalid_report") from exc
    expected = task_report_hash(normalized)
    if normalized.get("hash") != expected:
        raise TaskReportError("report hash does not match its content",
                              code="task_report.invalid_report")
    return normalized


def task_report_json(report: Mapping[str, Any]) -> str:
    """Return a validated task report as canonical JSON."""
    return canonical_json(validate_task_report(report))


def task_report_schema() -> dict[str, Any]:
    """Return the normalized v1.0 task-report schema record."""
    return validate_schema(REPORT_SCHEMA)


def task_report_schema_fingerprint() -> str:
    """Return the SHA-256 schema fingerprint for the task-report schema."""
    return schema_fingerprint(REPORT_SCHEMA)


def format_task_report(report: Mapping[str, Any]) -> str:
    """Render a bounded, readable post-run task report."""
    report = validate_task_report(report)
    run = report.get("run") if isinstance(report.get("run"), Mapping) else {}
    outcome = report.get("outcome") if isinstance(report.get("outcome"),
                                                  Mapping) else {}
    work = report.get("work") if isinstance(report.get("work"), Mapping) else {}
    lines = [
        f"task report: {run.get('run_id') or 'unknown'}",
        f"  outcome: {outcome.get('status')} "
        f"(run {outcome.get('run_status')}, criteria "
        f"{outcome.get('criteria_status')})",
    ]
    for row in (outcome.get("criteria") or {}).get("items") or []:
        test = row.get("test") if isinstance(row.get("test"), Mapping) else {}
        suffix = f" test={test.get('outcome')}" if test else ""
        lines.append(f"  criterion {row.get('id')}: {row.get('result')} "
                     f"[{row.get('freshness')}/{row.get('evidence')}]{suffix}")
        if row.get("reason"):
            lines.append(f"    reason: {row['reason']}")
    requested = work.get("requested") if isinstance(work.get("requested"),
                                                     Mapping) else {}
    planned = work.get("planned") if isinstance(work.get("planned"),
                                                 Mapping) else {}
    executed = work.get("executed") if isinstance(work.get("executed"),
                                                   Mapping) else {}
    changed = work.get("changed") if isinstance(work.get("changed"),
                                                 Mapping) else {}
    verified = work.get("verified") if isinstance(work.get("verified"),
                                                   Mapping) else {}
    lines.append(
        f"  work: requested task={requested.get('task')} "
        f"criteria={requested.get('criteria')}; planned "
        f"{planned.get('steps')} step(s); executed {executed.get('steps')}; "
        f"changed {changed.get('count')}; verified {verified.get('count')}")
    snapshot = report.get("snapshot") if isinstance(report.get("snapshot"),
                                                     Mapping) else {}
    lines.append(f"  snapshot: {snapshot.get('id')} "
                 f"[{snapshot.get('state')}/{snapshot.get('freshness')}]")
    for heading in ("failures", "fallbacks", "blocked", "skipped"):
        items = report.get(heading) or []
        if items:
            lines.append(f"  {heading}:")
            for item in items[:20]:
                if isinstance(item, Mapping):
                    reason = item.get("reason") or item.get("fallback") or ""
                    lines.append(f"    {item.get('kind')} {item.get('id') or ''}"
                                 f"{': ' + str(reason) if reason else ''}")
    measurements = report.get("measurements") if isinstance(
        report.get("measurements"), Mapping) else {}
    tokens = measurements.get("tokens") if isinstance(
        measurements.get("tokens"), Mapping) else {}
    cost = measurements.get("cost") if isinstance(measurements.get("cost"),
                                                   Mapping) else {}
    lines.append(f"  tokens: total={tokens.get('total')} "
                 f"input={tokens.get('input')} output={tokens.get('output')} "
                 f"[{tokens.get('measurement')}]")
    lines.append(f"  cost: {cost.get('micros')} micros "
                 f"[{cost.get('measurement')}]")
    critical = report.get("critical_path") if isinstance(
        report.get("critical_path"), Mapping) else {}
    lines.append(f"  critical path: {critical.get('duration_ms')} ms "
                 f"(lower bound {critical.get('lower_bound_ms')}, "
                 f"status {critical.get('status')})")
    execution = report.get("execution_critical_path") if isinstance(
        report.get("execution_critical_path"), Mapping) else {}
    lines.append(f"  execution path: {execution.get('duration_ms')} ms "
                 f"(lower bound {execution.get('lower_bound_ms')}, "
                 f"status {execution.get('status')})")
    prefetch = report.get("prefetch") if isinstance(report.get("prefetch"),
                                                     Mapping) else {}
    lines.append(f"  prefetch: {prefetch.get('state')} hits={prefetch.get('hits')} "
                 f"unused={prefetch.get('unused')} "
                 f"overhead={prefetch.get('overhead_ms')} ms")
    coverage = report.get("coverage") if isinstance(report.get("coverage"),
                                                     Mapping) else {}
    lines.append(f"  coverage: complete={coverage.get('complete')}")
    for item in report.get("not_assessed") or []:
        lines.append(f"  not assessed: {item}")
    return "\n".join(lines[:250])


# ---------------------------------------------------------------------------
# Live read-only record sources
# ---------------------------------------------------------------------------


def _root_path(root: str | Path) -> Path:
    try:
        path = Path(root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise TaskReportError(f"repository root does not exist: {root}",
                              code="task_report.invalid_input") from exc
    if not path.is_dir():
        raise TaskReportError(f"repository root is not a directory: {path}",
                              code="task_report.invalid_input")
    return path


def _safe_local_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value):
        raise TaskReportError(
            f"{what} must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,119}}",
            code="task_report.invalid_input")
    return value


def default_run_source(root: Path, run_id: str) -> Mapping[str, Any]:
    """Read one existing task run; an absent run is an explicit error."""
    from ozagia.core import tasks as TASKS

    try:
        return TASKS.show_run(root, _safe_local_id(run_id, "run_id"))
    except TASKS.TaskRunError as exc:
        raise TaskReportError(f"cannot read task run {run_id!r}: {exc}",
                              code="task_report.invalid_record") from exc


def default_goal_source(root: Path, goal_id: str | None
                        ) -> Mapping[str, Any] | None:
    """Read one goal record if the caller named it; never infer a goal."""
    if not goal_id:
        return None
    from ozagia.core import goals as GOALS

    local = goal_id.strip()
    if local.startswith("goal:"):
        local = local[5:]
    local = _safe_local_id(local, "goal_id")
    for goal in GOALS.load_store(root).get("goals") or []:
        if isinstance(goal, Mapping) and goal.get("id") == local:
            return goal
    return None


def default_test_record_source(root: Path) -> Mapping[str, Any] | None:
    """Read the working-set test record, or None when no file exists."""
    from ozagia.config import state_path
    from ozagia.core import working_set as W

    path = state_path(root, *W.TEST_RECORD)
    if not path.is_file():
        return None
    try:
        return W.load_test_record(root)
    except Exception as exc:  # noqa: BLE001 - unreadable is not absent
        raise TaskReportError(
            f"cannot read the test record: {type(exc).__name__}: {exc}",
            code="task_report.invalid_record") from exc


def default_session_source(root: Path, session_id: str | None
                           ) -> Mapping[str, Any] | None:
    """Read one session JSON record if the caller named it."""
    if not session_id:
        return None
    import json

    from ozagia.config import state_path

    local = _safe_local_id(session_id, "session_id")
    path = state_path(root, "sessions", f"{local}.json")
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskReportError(
            f"cannot read session {local!r}: {exc}",
            code="task_report.invalid_record") from exc
    if not isinstance(value, Mapping):
        raise TaskReportError(
            f"session {local!r} is not a JSON object",
            code="task_report.invalid_record")
    return value


def _read_jsonl_tail(path: Path, *, limit: int,
                     max_bytes: int = 512 * 1024) -> list[dict[str, Any]]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    rows: list[dict[str, Any]] = []
    import json

    for line in data.decode("utf-8", "replace").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def default_daemon_source(root: Path, run_id: str | None,
                          session_id: str | None
                          ) -> tuple[Mapping[str, Any], ...]:
    """Read daemon journal timing only when it names this run or session."""
    from ozagia.config import state_path

    path = state_path(root, "daemon_journal.jsonl")
    rows = _read_jsonl_tail(path, limit=MAX_ADAPTED_SPANS)
    selected = []
    for row in rows:
        if run_id and row.get("run_id") == run_id:
            selected.append(row)
        elif session_id and row.get("session") == session_id:
            selected.append(row)
    return tuple(selected)


def default_evaluation_source(root: Path, evaluation_id: str | None
                              ) -> Mapping[str, Any] | None:
    """Read a retained eval-run JSON record from ``.ozagia/eval/<id>.json``.

    No owner store exists yet; a missing file is None and the report marks the
    evaluation as not recorded.  The read is read-only and local.
    """
    if not evaluation_id:
        return None
    import json

    from ozagia.config import state_path

    local = _safe_local_id(evaluation_id, "evaluation_id")
    path = state_path(root, "eval", f"{local}.json")
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskReportError(
            f"cannot read evaluation {local!r}: {exc}",
            code="task_report.invalid_record") from exc
    if not isinstance(value, Mapping):
        raise TaskReportError(
            f"evaluation {local!r} is not a JSON object",
            code="task_report.invalid_record")
    return value


@dataclass(frozen=True)
class TaskSources:
    """Injection seams for the read-only task-record collector."""

    run: Callable[[Path, str], Mapping[str, Any]] = default_run_source
    goal: Callable[[Path, str | None], Mapping[str, Any] | None] = \
        default_goal_source
    test_record: Callable[[Path], Mapping[str, Any] | None] = \
        default_test_record_source
    session: Callable[[Path, str | None], Mapping[str, Any] | None] = \
        default_session_source
    daemon: Callable[[Path, str | None, str | None],
                     Sequence[Mapping[str, Any]]] = default_daemon_source
    evaluation: Callable[[Path, str | None], Mapping[str, Any] | None] = \
        default_evaluation_source

    def __post_init__(self) -> None:
        for name in ("run", "goal", "test_record", "session", "daemon",
                     "evaluation"):
            if not callable(getattr(self, name)):
                raise TypeError(f"task source {name} must be callable")


def collect_task_records(root: str | Path, run_id: str, *,
                         goal_id: str | None = None,
                         session_id: str | None = None,
                         evaluation_id: str | None = None,
                         sources: TaskSources | None = None,
                         observed_at: int | str = UNKNOWN,
                         prefetch: Mapping[str, Any] | None = None
                         ) -> TaskRecords:
    """Collect retained records from the existing read-only owners."""
    root_path = _root_path(root)
    source_set = sources or TaskSources()
    if not isinstance(source_set, TaskSources):
        raise TypeError("sources must be task_profile.TaskSources")
    run = source_set.run(root_path, _safe_local_id(run_id, "run_id"))
    goal = source_set.goal(root_path, goal_id)
    test_record = source_set.test_record(root_path)
    session = source_set.session(root_path, session_id)
    daemon = tuple(source_set.daemon(root_path, run_id, session_id))
    evaluation = source_set.evaluation(root_path, evaluation_id)
    return TaskRecords(
        run=run, goal=goal, test_record=test_record, session=session,
        daemon=daemon, evaluation=evaluation, prefetch=prefetch,
        goal_id=goal_id, session_id=session_id,
        evaluation_id=evaluation_id, observed_at=observed_at)


def build_task_profile_from_root(root: str | Path, run_id: str, *,
                                 goal_id: str | None = None,
                                 session_id: str | None = None,
                                 evaluation_id: str | None = None,
                                 sources: TaskSources | None = None,
                                 observed_at: int | str = UNKNOWN,
                                 scope: Mapping[str, Any] | None = None,
                                 snapshot: str = UNKNOWN,
                                 created_at: int | str | None = None,
                                 max_spans: int = DEFAULT_MAX_SPANS,
                                 max_edges: int = DEFAULT_MAX_EDGES,
                                 max_bytes: int = DEFAULT_MAX_BYTES
                                 ) -> dict[str, Any]:
    """Collect retained records from ``root`` and assemble their profile."""
    records = collect_task_records(
        root, run_id, goal_id=goal_id, session_id=session_id,
        evaluation_id=evaluation_id, sources=sources,
        observed_at=observed_at)
    manifest, _state = _manifest_of(records.run)
    spec = manifest.get("spec") if isinstance(manifest.get("spec"),
                                               Mapping) else {}
    if scope is None:
        scope = {"task": f"task:{spec.get('id') or run_id}",
                 "run": f"run:{run_id}"}
        if records.goal_id or (isinstance(records.goal, Mapping)
                               and records.goal.get("id")):
            scope["goal"] = f"goal:{records.goal_id or records.goal.get('id')}"
    return build_task_profile_from_records(
        records, scope=scope, snapshot=snapshot, created_at=created_at,
        inputs=(f"run:{run_id}",), max_spans=max_spans, max_edges=max_edges,
        max_bytes=max_bytes)


def build_task_report_from_root(root: str | Path, run_id: str, *,
                                goal_id: str | None = None,
                                session_id: str | None = None,
                                evaluation_id: str | None = None,
                                sources: TaskSources | None = None,
                                observed_at: int | str = UNKNOWN,
                                prefetch: Mapping[str, Any] | None = None,
                                scope: Mapping[str, Any] | None = None,
                                snapshot: str = UNKNOWN,
                                criterion_freshness: Mapping[str, Any] | None = None,
                                created_at: int | str | None = None,
                                max_bytes: int = DEFAULT_MAX_REPORT_BYTES
                                ) -> dict[str, Any]:
    """Collect retained records from ``root`` and build the report."""
    records = collect_task_records(
        root, run_id, goal_id=goal_id, session_id=session_id,
        evaluation_id=evaluation_id, sources=sources,
        observed_at=observed_at, prefetch=prefetch)
    return build_task_report(
        records, scope=scope, snapshot=snapshot,
        criterion_freshness=criterion_freshness, created_at=created_at,
        max_bytes=max_bytes)




def validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a task-profile instance and its derived data.

    Beyond :func:`contracts.validate_instance`, this checks the namespace and
    version, every span field, duplicate IDs, parent existence, cycles, explicit
    edges matching the declared parents, and the recomputed coverage and
    integrity sections.  A tampered count or edge is an explicit error, not a
    silently accepted view.
    """
    normalized = validate_instance(profile)
    schema = normalized["schema"]
    if schema["namespace"] != PROFILE_NAMESPACE:
        raise TaskProfileError(
            f"profile schema namespace must be {PROFILE_NAMESPACE!r}, got "
            f"{schema['namespace']!r}",
            code="task_profile.invalid_profile")
    if normalize_version(schema["version"]) != PROFILE_VERSION:
        raise TaskProfileError(
            f"profile schema version must be {PROFILE_VERSION!r}",
            code="task_profile.invalid_profile")
    if "omissions" not in normalized:
        raise TaskProfileError(
            "profile must carry an omissions truncation envelope",
            code="task_profile.invalid_profile")
    omissions = validate_truncation(normalized["omissions"])
    data = normalized["data"]
    if not isinstance(data, Mapping):
        raise TaskProfileError("profile data must be a mapping",
                               code="task_profile.invalid_profile")
    allowed_data = {
        "profile_version", "clock", "time_origin", "spans", "edges",
        "coverage", "integrity",
    }
    unknown_data = [key for key in data
                    if key not in allowed_data
                    and not str(key).startswith("x-")]
    if unknown_data:
        raise TaskProfileError(
            f"unknown profile data fields: {sorted(unknown_data)}",
            code="task_profile.invalid_profile")
    if data.get("profile_version") != PROFILE_VERSION:
        raise TaskProfileError(
            f"profile_version must be {PROFILE_VERSION!r}",
            code="task_profile.invalid_profile")
    clock_value = check_clock(data.get("clock"))
    _validate_time_origin(data.get("time_origin"))

    raw_spans = data.get("spans")
    if not isinstance(raw_spans, list):
        raise TaskProfileError("profile data.spans must be a list",
                               code="task_profile.invalid_profile")
    spans = [SpanRecord.from_mapping(item) for item in raw_spans]
    ids = [span.id for span in spans]
    if len(set(ids)) != len(ids):
        raise TaskProfileError("profile contains duplicate span IDs",
                               code="task_profile.duplicate_id")
    by_id = set(ids)
    for span in spans:
        for parent in span.parents or ():
            if parent not in by_id:
                raise TaskProfileError(
                    f"span {span.id!r} depends on omitted or unknown parent "
                    f"{parent!r}",
                    code="task_profile.unknown_parent")
    nodes = tuple(
        DagNode(
            id=span.id,
            duration_ms=span.resolved_duration_ms,
            outcome=span.outcome,
            resource_class=span.resource_class,
            kind=span.kind,
            parents_unknown=span.parents is None,
        )
        for span in spans
    )
    edge_values = tuple(
        DagEdge(parent, span.id, "finish_to_start")
        for span in spans for parent in (span.parents or ())
    )
    try:
        topological_order(Dag(nodes, edge_values))
    except CycleError as error:
        raise TaskProfileCycleError(error.cycles,
                                    error.components) from error
    except CriticalPathBoundsError as error:
        raise TaskProfileBoundsError(str(error)) from error

    supplied_edges = data.get("edges")
    if not isinstance(supplied_edges, list):
        raise TaskProfileError("profile data.edges must be a list",
                               code="task_profile.invalid_profile")
    normalized_edges: list[dict[str, str]] = []
    for edge in supplied_edges:
        if not isinstance(edge, Mapping):
            raise TaskProfileError("profile edge must be a mapping",
                                   code="task_profile.invalid_profile")
        unknown = [key for key in edge if key not in {"from", "to", "kind"}]
        if unknown:
            raise TaskProfileError(
                f"unknown profile edge fields: {sorted(unknown)}",
                code="task_profile.invalid_profile")
        try:
            normalized_edges.append({
                "from": validate_node_id(edge.get("from"), what="edge from"),
                "to": validate_node_id(edge.get("to"), what="edge to"),
                "kind": edge.get("kind", "finish_to_start"),
            })
        except ValueError as exc:
            raise TaskProfileError(
                f"invalid profile edge: {exc}",
                code="task_profile.invalid_profile") from exc
    expected_edges = _derived_edges(spans)
    normalized_edges.sort(key=lambda item: (
        item["from"], item["to"], item["kind"]))
    if normalized_edges != expected_edges:
        raise TaskProfileError(
            "profile edges do not match the spans' declared parents",
            code="task_profile.inconsistent_edges")

    if clock_value != _profile_clock(spans):
        raise TaskProfileError(
            "profile clock does not match its spans",
            code="task_profile.inconsistent_data")
    total = len(spans) + omissions["omitted"]
    expected_coverage = _coverage(spans, total, omissions["truncated"])
    supplied_coverage = data.get("coverage")
    if not isinstance(supplied_coverage, Mapping):
        raise TaskProfileError("profile data.coverage must be a mapping",
                               code="task_profile.invalid_profile")
    if dict(supplied_coverage) != expected_coverage:
        raise TaskProfileError(
            "profile coverage does not match its spans",
            code="task_profile.inconsistent_data")
    expected_integrity = _integrity(spans, normalized["scope"])
    supplied_integrity = data.get("integrity")
    if not isinstance(supplied_integrity, Mapping):
        raise TaskProfileError("profile data.integrity must be a mapping",
                               code="task_profile.invalid_profile")
    if dict(supplied_integrity) != expected_integrity:
        raise TaskProfileError(
            "profile integrity does not match its spans",
            code="task_profile.inconsistent_data")
    return normalized


def profile_json(profile: Mapping[str, Any]) -> str:
    """Return a validated profile as canonical JSON."""
    return canonical_json(validate_profile(profile))


def profile_hash(profile: Mapping[str, Any]) -> str:
    """Return the SHA-256 instance hash of a validated profile."""
    return instance_hash(validate_profile(profile))


def profile_schema() -> dict[str, Any]:
    """Return the normalized, validated v1.0 profile schema record."""
    return validate_schema(PROFILE_SCHEMA)


def profile_schema_fingerprint() -> str:
    """Return the SHA-256 schema fingerprint for the profile schema."""
    return schema_fingerprint(PROFILE_SCHEMA)


def profile_dag(profile: Mapping[str, Any]) -> Dag:
    """Convert a validated profile into the explicit timing DAG."""
    normalized = validate_profile(profile)
    data = normalized["data"]
    spans = [SpanRecord.from_mapping(item) for item in data["spans"]]
    nodes = tuple(
        DagNode(
            id=span.id,
            duration_ms=span.resolved_duration_ms,
            outcome=span.outcome,
            resource_class=span.resource_class,
            kind=span.kind,
            parents_unknown=span.parents is None,
        )
        for span in spans
    )
    edges = tuple(
        DagEdge(item["from"], item["to"], item["kind"])
        for item in data["edges"]
    )
    return Dag(nodes, edges)


def profile_critical_path(profile: Mapping[str, Any], *,
                          include_schedule: bool = False) -> dict[str, Any]:
    """Convenience wrapper: validate a profile and compute its critical path."""
    return critical_path(profile_dag(profile),
                         include_schedule=include_schedule)


__all__ = [
    "ADAPTER_VERSION", "DEFAULT_MAX_BYTES", "DEFAULT_MAX_EDGES",
    "DEFAULT_MAX_REPORT_BYTES", "DEFAULT_MAX_SPANS", "FakeClock",
    "MAX_ADAPTED_SPANS", "MAX_PROFILE_BYTES", "MAX_PROFILE_EDGES",
    "MAX_PROFILE_SPANS", "MAX_TASK_REPORT_BYTES", "MEASUREMENTS",
    "PROFILE_ERROR_CODES", "PROFILE_NAMESPACE", "PROFILE_SCHEMA",
    "PROFILE_VERSION", "REPORT_ERROR_CODES", "REPORT_NAMESPACE",
    "REPORT_SCHEMA", "REPORT_STATUSES", "REPORT_VERSION", "SPAN_KINDS",
    "SpanRecord", "STATUS_MAP", "TaskProfileBoundsError",
    "TaskProfileCycleError", "TaskProfileError", "TaskRecords",
    "TaskReportError", "TaskSources", "adapt_daemon_evidence",
    "adapt_evaluation_record", "adapt_goal_record", "adapt_session_record",
    "adapt_task_run", "adapt_test_record", "build_profile",
    "build_task_profile_from_records", "build_task_profile_from_root",
    "build_task_report",
    "build_task_report_from_root", "collect_task_records",
    "default_daemon_source", "default_evaluation_source",
    "default_goal_source", "default_run_source", "default_session_source",
    "default_test_record_source", "edges_from_spans", "format_task_report",
    "profile_critical_path", "profile_dag", "profile_hash", "profile_json",
    "profile_schema", "profile_schema_fingerprint", "redact_task_text",
    "source_span_id", "task_execution_spans", "task_profile_spans",
    "task_report_hash",
    "task_report_json", "task_report_schema",
    "task_report_schema_fingerprint", "validate_profile",
    "validate_task_report",
]
