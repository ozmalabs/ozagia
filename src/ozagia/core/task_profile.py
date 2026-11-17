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

from dataclasses import dataclass, fields
from typing import Any, Iterable, Mapping

from ozagia.core.contracts import (
    CLOCKS,
    EVIDENCE,
    RESULTS,
    canonical_json,
    clock as check_clock,
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
# Validation, hashing and DAG conversion
# ---------------------------------------------------------------------------


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
    "DEFAULT_MAX_BYTES", "DEFAULT_MAX_EDGES", "DEFAULT_MAX_SPANS",
    "FakeClock", "MAX_PROFILE_BYTES", "MAX_PROFILE_EDGES",
    "MAX_PROFILE_SPANS", "MEASUREMENTS", "PROFILE_ERROR_CODES",
    "PROFILE_NAMESPACE", "PROFILE_SCHEMA", "PROFILE_VERSION", "SPAN_KINDS",
    "SpanRecord", "TaskProfileBoundsError", "TaskProfileCycleError",
    "TaskProfileError", "build_profile", "edges_from_spans",
    "profile_critical_path", "profile_dag", "profile_hash", "profile_json",
    "profile_schema", "profile_schema_fingerprint", "validate_profile",
]
