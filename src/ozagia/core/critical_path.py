"""Explicit-edge timing DAGs, critical paths and deterministic what-if schedules.

This module is pure and stdlib-only.  It never reads a clock, a file, a graph
or a task store: every duration, edge and estimate is injected by the caller.
The one rule that matters most is stated once here and enforced everywhere:

    dependencies come from explicit edges; timestamps never create an edge.

A DAG edge is a finish-to-start dependency declared by the caller (in
:class:`DagEdge` or, for task profiles, by a span's ``parents``).  Two spans
that overlap or end in a particular order are still independent unless an edge
says otherwise.

Unknown values
--------------

A missing duration is ``None`` in the API and ``"unknown"`` in reports.  It is
never replaced by zero.  If any node has an unknown duration or unknown
dependency information, the overall critical path is reported as
``duration_ms="unknown"`` with a separate ``lower_bound_ms``.  The
``lower_bound_ms`` is computed with unknown durations contributing zero *and
is labelled as a lower bound*; it is not presented as the critical path.

Cycles
------

Cycles are invalid input.  :func:`critical_path` raises :class:`CycleError`
with the deterministic representative cycles and cyclic SCCs, or returns a
report with ``status="cycle"`` when ``on_cycle="report"``.

What-if scheduling
------------------

:func:`what_if` re-schedules the same explicit DAG under a caller-supplied
:class:`ScheduleProfile` and per-node :class:`ModelCallEstimate` values.  It
reports two different numbers and never conflates them:

* ``dependency_lower_bound_ms`` is the longest dependency path with no
  resource limits (the dependency-only lower bound);
* ``makespan_ms`` is a deterministic greedy list schedule with caller-supplied
  concurrency limits.  It is a simulation, not an optimum and not an
  observation.

Missing queue, token, rate or duration evidence produces ``"unknown"`` unless
the caller explicitly asks for a labelled lower bound with
``unknown_policy="lower_bound"``.  In that mode only missing queue/startup
waits become zero; a missing token or rate remains unknown because zero tokens
or zero rates cannot bound the work.

Bounds
------

A DAG is capped at :data:`MAX_DAG_NODES` nodes and :data:`MAX_DAG_EDGES` edges.
:func:`what_if` refuses more than :data:`DEFAULT_MAX_SCHEDULE_NODES` nodes
rather than silently dropping stages from a schedule.  Reports are ordinary
canonical JSON values: no floats, tuples, sets or ``None`` appear.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Mapping

from ozagia.core.contracts import canonical_json, content_hash

# ---------------------------------------------------------------------------
# Versioned report schemas and vocabulary
# ---------------------------------------------------------------------------

SCHEMA_NAMESPACE = "ozagia.critical_path"
SCHEMA_VERSION = "1.0"
WHAT_IF_NAMESPACE = "ozagia.what_if_schedule"
WHAT_IF_VERSION = "1.0"
COMPARISON_NAMESPACE = "ozagia.what_if_comparison"
COMPARISON_VERSION = "1.0"

UNKNOWN = "unknown"

NODE_KINDS = (
    "model_call", "tool_call", "graph_prepare", "index_prepare",
    "queue_wait", "readiness_wait", "check", "human_gate", "prefetch",
    "action", "other",
)
OUTCOMES = (
    "completed", "failed", "cancelled", "pending", "running", "skipped",
    "unknown",
)
TERMINAL_OUTCOMES = frozenset({"failed", "cancelled", "skipped"})
RESOURCE_CLASSES = (
    "model", "tool", "cpu", "io", "graph", "index", "human", "check",
    "unknown",
)
EDGE_KINDS = ("finish_to_start",)

MAX_NODE_ID = 128
MAX_DAG_NODES = 4096
MAX_DAG_EDGES = 16384
DEFAULT_MAX_SCHEDULE_NODES = 512
MAX_BOTTLENECKS = 8

CRITICAL_PATH_ERROR_CODES = frozenset({
    "critical_path.invalid_input",
    "critical_path.invalid_node",
    "critical_path.unknown_node",
    "critical_path.cycle",
    "critical_path.bounds_exceeded",
})

_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

CRITICAL_PATH_SCHEMA = {
    "namespace": SCHEMA_NAMESPACE,
    "version": SCHEMA_VERSION,
    "types": {
        "report": {
            "fields": [
                "status", "run_status", "complete", "path", "duration_ms",
                "lower_bound_ms", "path_is_critical", "unknown_nodes",
                "unknown_duration_nodes", "unknown_dependency_nodes", "nodes",
                "edges", "blocking", "work_ms", "work_is_lower_bound",
                "overlap_ms", "terminal_nodes", "blocked_descendants",
                "anomalies", "ties",
            ],
        },
    },
    "required": [
        "status", "run_status", "complete", "path", "duration_ms",
        "lower_bound_ms",
    ],
    "constraints": {"max_nodes": MAX_DAG_NODES, "max_edges": MAX_DAG_EDGES},
    "sensitivity": "internal",
}

WHAT_IF_SCHEMA = {
    "namespace": WHAT_IF_NAMESPACE,
    "version": WHAT_IF_VERSION,
    "types": {
        "schedule": {
            "fields": [
                "status", "complete", "makespan_ms", "lower_bound_ms",
                "dependency_lower_bound_ms", "critical_path",
                "dependency_critical_path", "critical_path_changed",
                "per_stage", "unresolved", "unknown_nodes",
                "resource_wait_total_ms", "bottlenecks", "assumptions",
                "profile", "unknown_policy", "lower_bound", "nodes", "edges",
                "confidence",
            ],
        },
    },
    "required": [
        "status", "complete", "makespan_ms", "lower_bound_ms",
        "dependency_lower_bound_ms", "critical_path",
    ],
    "constraints": {"max_nodes": MAX_DAG_NODES, "max_stages": 512},
    "sensitivity": "internal",
}

COMPARISON_SCHEMA = {
    "namespace": COMPARISON_NAMESPACE,
    "version": COMPARISON_VERSION,
    "types": {
        "comparison": {
            "fields": [
                "status", "summary", "fast", "resource_limited",
                "bottlenecks", "assumptions",
            ],
        },
    },
    "required": ["status", "summary", "fast", "resource_limited"],
    "constraints": {"max_profiles": 2},
    "sensitivity": "internal",
}


def schema_ref(namespace: str, version: str) -> dict[str, str]:
    """Return the instance schema reference for a report namespace."""
    return {"namespace": namespace, "version": version}


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class CriticalPathError(ValueError):
    """A timing-DAG error with a stable, machine-readable ``code``."""

    code = "critical_path.invalid_input"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        resolved = code or type(self).code
        if resolved not in CRITICAL_PATH_ERROR_CODES:
            raise ValueError(f"unknown critical-path error code {resolved!r}")
        self.code = resolved
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class InvalidNodeError(CriticalPathError):
    code = "critical_path.invalid_node"


class UnknownNodeError(CriticalPathError):
    code = "critical_path.unknown_node"


class CriticalPathBoundsError(CriticalPathError):
    code = "critical_path.bounds_exceeded"


class CycleError(CriticalPathError):
    """A dependency cycle, carrying deterministic representative cycles."""

    code = "critical_path.cycle"

    def __init__(self, cycles: Iterable[Iterable[str]],
                 components: Iterable[Iterable[str]], *,
                 nodes: int = 0, edges: int = 0) -> None:
        self.cycles = tuple(tuple(cycle) for cycle in cycles)
        self.components = tuple(tuple(component) for component in components)
        self.nodes = nodes
        self.edges = edges
        described = "; ".join(
            " -> ".join((*cycle, cycle[0])) if cycle else "empty cycle"
            for cycle in self.cycles
        ) or "unknown cycle"
        super().__init__(f"dependency cycle: {described}", code=self.code)

    def report(self) -> dict[str, Any]:
        return cycle_report(self)


def cycle_report(error: CycleError) -> dict[str, Any]:
    """Return a bounded, canonical critical-path report for a cycle."""
    return {
        "schema": schema_ref(SCHEMA_NAMESPACE, SCHEMA_VERSION),
        "status": "cycle",
        "run_status": "invalid",
        "complete": False,
        "path": [],
        "duration_ms": UNKNOWN,
        "lower_bound_ms": 0,
        "path_is_critical": False,
        "unknown_nodes": [],
        "unknown_duration_nodes": [],
        "unknown_dependency_nodes": [],
        "nodes": error.nodes,
        "edges": error.edges,
        "blocking": [],
        "work_ms": 0,
        "work_is_lower_bound": False,
        "overlap_ms": 0,
        "terminal_nodes": [],
        "blocked_descendants": [],
        "anomalies": [],
        "ties": [],
        "cycles": [list(cycle) for cycle in error.cycles],
        "cycle_components": [list(component)
                             for component in error.components],
    }


# ---------------------------------------------------------------------------
# Small validators
# ---------------------------------------------------------------------------


def validate_node_id(value: Any, *, what: str = "node id") -> str:
    """Validate a local DAG node ID.

    IDs are lowercase ASCII, start with a letter or digit, are at most 128
    characters and contain only ``[a-z0-9._:-]``.  They are compared exactly:
    there is no case folding, Unicode normalization or escaping.  This local
    rule is deliberately narrower than the self-graph ``repo:``/``run:``
    identities; a profile span ID is not a self-graph identity.
    """
    if not isinstance(value, str) or not _NODE_ID_RE.fullmatch(value):
        raise InvalidNodeError(
            f"{what} {value!r} must match {_NODE_ID_RE.pattern}")
    return value


def _check_profile_name(value: Any) -> str:
    if not isinstance(value, str) or not _PROFILE_NAME_RE.fullmatch(value):
        raise CriticalPathError(
            f"schedule profile name {value!r} must match "
            f"{_PROFILE_NAME_RE.pattern}")
    return value


def _check_text(value: Any, name: str, limit: int = 200) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(char) < 0x20 for char in value)):
        raise CriticalPathError(
            f"{name} must be a non-empty string of <= {limit} printable chars")
    return value


def _optional_int(value: Any, name: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CriticalPathError(f"{name} must be an integer or None")
    if value < minimum:
        raise CriticalPathError(f"{name} must be >= {minimum}")
    return value


def _required_int(value: Any, name: str, *, minimum: int = 1) -> int:
    checked = _optional_int(value, name, minimum=minimum)
    if checked is None:
        raise CriticalPathError(f"{name} must be an integer")
    return checked


def _coerce_rate(value: Any, name: str) -> Fraction | None:
    """Accept an exact rate (int, Fraction or decimal string), never a float."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise CriticalPathError(f"{name} must not be a bool")
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, int):
        rate = Fraction(value)
    elif isinstance(value, str):
        try:
            rate = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise CriticalPathError(
                f"{name} {value!r} is not an exact rate") from exc
    elif isinstance(value, float):
        raise CriticalPathError(
            f"{name} must be int, Fraction or a decimal string; floats are "
            "not exact and would make the schedule non-deterministic")
    else:
        raise CriticalPathError(
            f"{name} must be int, Fraction, a decimal string or None")
    if rate <= 0:
        raise CriticalPathError(f"{name} must be greater than zero")
    return rate


def _rate_text(rate: Fraction | None) -> str:
    if rate is None:
        return UNKNOWN
    if rate.denominator == 1:
        return str(rate.numerator)
    return f"{rate.numerator}/{rate.denominator}"


def _ceil_fraction(value: Fraction) -> int:
    if value < 0:
        raise CriticalPathError("duration arithmetic produced a negative value")
    return -(-value.numerator // value.denominator)


# ---------------------------------------------------------------------------
# Model-call estimates and schedule profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCallEstimate:
    """Caller-supplied inputs for one model call's modelled duration.

    Rates are tokens per second and are exact (:class:`~fractions.Fraction`).
    ``queue_wait_ms`` and ``startup_ms`` are not inferred: when both the
    estimate and the schedule profile leave one out, the duration is unknown
    unless ``unknown_policy="lower_bound"`` explicitly labels a zero wait.
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    prefill_rate: Fraction | None = None
    generation_rate: Fraction | None = None
    queue_wait_ms: int | None = None
    startup_ms: int | None = None

    def __post_init__(self) -> None:
        input_tokens = _optional_int(self.input_tokens, "input_tokens")
        cached = _optional_int(self.cached_input_tokens,
                               "cached_input_tokens")
        output = _optional_int(self.output_tokens, "output_tokens")
        if cached is not None and input_tokens is None:
            raise CriticalPathError(
                "cached_input_tokens requires input_tokens")
        if cached is not None and input_tokens is not None \
                and cached > input_tokens:
            raise CriticalPathError(
                "cached_input_tokens cannot exceed input_tokens")
        object.__setattr__(self, "input_tokens", input_tokens)
        object.__setattr__(self, "cached_input_tokens", cached)
        object.__setattr__(self, "output_tokens", output)
        object.__setattr__(self, "prefill_rate",
                           _coerce_rate(self.prefill_rate, "prefill_rate"))
        object.__setattr__(
            self, "generation_rate",
            _coerce_rate(self.generation_rate, "generation_rate"))
        object.__setattr__(self, "queue_wait_ms",
                           _optional_int(self.queue_wait_ms, "queue_wait_ms"))
        object.__setattr__(self, "startup_ms",
                           _optional_int(self.startup_ms, "startup_ms"))

    @classmethod
    def from_mapping(cls, value: Any) -> "ModelCallEstimate":
        """Build an estimate from a JSON-shaped mapping, strict about fields."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise CriticalPathError("model-call estimate must be a mapping")
        allowed = {
            "input_tokens", "cached_input_tokens", "output_tokens",
            "prefill_rate", "generation_rate", "queue_wait_ms", "startup_ms",
        }
        unknown = [key for key in value if key not in allowed]
        if unknown:
            raise CriticalPathError(
                f"unknown model-call estimate fields: {sorted(unknown)}")
        kwargs: dict[str, Any] = {}
        for key, item in value.items():
            kwargs[key] = None if item == UNKNOWN else item
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": (UNKNOWN if self.input_tokens is None
                             else self.input_tokens),
            "cached_input_tokens": (UNKNOWN
                                    if self.cached_input_tokens is None
                                    else self.cached_input_tokens),
            "output_tokens": (UNKNOWN if self.output_tokens is None
                              else self.output_tokens),
            "prefill_rate": _rate_text(self.prefill_rate),
            "generation_rate": _rate_text(self.generation_rate),
            "queue_wait_ms": (UNKNOWN if self.queue_wait_ms is None
                              else self.queue_wait_ms),
            "startup_ms": (UNKNOWN if self.startup_ms is None
                           else self.startup_ms),
        }


@dataclass(frozen=True)
class ScheduleProfile:
    """An inference and resource profile for a what-if schedule."""

    name: str
    prefill_rate: Fraction | None = None
    generation_rate: Fraction | None = None
    queue_wait_ms: int | None = None
    startup_ms: int | None = None
    max_concurrency: int | None = None
    resource_concurrency: tuple[tuple[str, int], ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        _check_profile_name(self.name)
        object.__setattr__(self, "prefill_rate",
                           _coerce_rate(self.prefill_rate, "prefill_rate"))
        object.__setattr__(
            self, "generation_rate",
            _coerce_rate(self.generation_rate, "generation_rate"))
        object.__setattr__(self, "queue_wait_ms",
                           _optional_int(self.queue_wait_ms, "queue_wait_ms"))
        object.__setattr__(self, "startup_ms",
                           _optional_int(self.startup_ms, "startup_ms"))
        object.__setattr__(self, "max_concurrency",
                           _optional_int(self.max_concurrency,
                                         "max_concurrency", minimum=1))
        raw = self.resource_concurrency
        if raw is None:
            items = []
        elif isinstance(raw, Mapping):
            items = list(raw.items())
        else:
            try:
                items = list(raw)
            except TypeError as exc:
                raise CriticalPathError(
                    "resource_concurrency must be a mapping or iterable of "
                    "(class, limit) pairs") from exc
        checked: dict[str, int] = {}
        for item in items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise CriticalPathError(
                    "resource_concurrency entries must be (class, limit) pairs")
            resource_class, limit = item
            if resource_class not in RESOURCE_CLASSES:
                raise CriticalPathError(
                    f"unknown resource class {resource_class!r}")
            checked[resource_class] = _required_int(
                limit, f"resource_concurrency[{resource_class}]")
        object.__setattr__(self, "resource_concurrency",
                           tuple(sorted(checked.items())))
        object.__setattr__(self, "description",
                           self.description if self.description else "")
        if self.description:
            _check_text(self.description, "description", 500)

    def limit_for(self, resource_class: str) -> int | None:
        for name, limit in self.resource_concurrency:
            if name == resource_class:
                return limit
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prefill_rate": _rate_text(self.prefill_rate),
            "generation_rate": _rate_text(self.generation_rate),
            "queue_wait_ms": (UNKNOWN if self.queue_wait_ms is None
                              else self.queue_wait_ms),
            "startup_ms": (UNKNOWN if self.startup_ms is None
                           else self.startup_ms),
            "max_concurrency": (UNKNOWN if self.max_concurrency is None
                                else self.max_concurrency),
            "resource_concurrency": {
                name: limit for name, limit in self.resource_concurrency
            },
        }

    @classmethod
    def fast(cls, **overrides: Any) -> "ScheduleProfile":
        """An explicitly assumed fast profile; validate it per provider."""
        values: dict[str, Any] = {
            "name": "fast",
            "prefill_rate": 2000,
            "generation_rate": 100,
            "queue_wait_ms": 0,
            "startup_ms": 0,
            "max_concurrency": 8,
            "resource_concurrency": {
                "model": 8, "tool": 8, "check": 8, "human": 1,
            },
            "description": (
                "assumed fast inference and resource profile; not measured"),
        }
        values.update(overrides)
        return cls(**values)

    @classmethod
    def resource_limited(cls, **overrides: Any) -> "ScheduleProfile":
        """An explicitly assumed constrained profile: one model slot."""
        values: dict[str, Any] = {
            "name": "resource_limited",
            "prefill_rate": 800,
            "generation_rate": 40,
            "queue_wait_ms": 250,
            "startup_ms": 100,
            "max_concurrency": 2,
            "resource_concurrency": {
                "model": 1, "tool": 1, "check": 1, "human": 1,
            },
            "description": (
                "assumed constrained profile with one model slot; simulated, "
                "not observed"),
        }
        values.update(overrides)
        return cls(**values)


# ---------------------------------------------------------------------------
# DAG values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DagNode:
    """One explicit timing-DAG node.

    ``duration_ms`` is the measured or caller-supplied elapsed duration.
    ``parents_unknown`` means the record does not say what this node depended
    on; it is not the same as a root with an empty parent set.
    """

    id: str
    duration_ms: int | None = None
    outcome: str = "unknown"
    resource_class: str = "unknown"
    kind: str = "other"
    parents_unknown: bool = False
    estimate: ModelCallEstimate | None = None

    def __post_init__(self) -> None:
        validate_node_id(self.id, what="DAG node id")
        object.__setattr__(self, "duration_ms",
                           _optional_int(self.duration_ms, "duration_ms"))
        if self.outcome not in OUTCOMES:
            raise InvalidNodeError(
                f"node {self.id!r} has invalid outcome {self.outcome!r}")
        if self.resource_class not in RESOURCE_CLASSES:
            raise InvalidNodeError(
                f"node {self.id!r} has invalid resource class "
                f"{self.resource_class!r}")
        if self.kind not in NODE_KINDS:
            raise InvalidNodeError(
                f"node {self.id!r} has invalid kind {self.kind!r}")
        if self.parents_unknown is not True and self.parents_unknown is not False:
            raise InvalidNodeError(
                f"node {self.id!r} parents_unknown must be a bool")
        if self.estimate is not None:
            object.__setattr__(
                self, "estimate", ModelCallEstimate.from_mapping(self.estimate))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "duration_ms": (UNKNOWN if self.duration_ms is None
                            else self.duration_ms),
            "outcome": self.outcome,
            "resource_class": self.resource_class,
            "kind": self.kind,
            "parents_unknown": self.parents_unknown,
            "estimate": (UNKNOWN if self.estimate is None
                         else self.estimate.to_dict()),
        }


@dataclass(frozen=True)
class DagEdge:
    """An explicit finish-to-start dependency edge."""

    parent: str
    child: str
    kind: str = "finish_to_start"

    def __post_init__(self) -> None:
        validate_node_id(self.parent, what="edge parent")
        validate_node_id(self.child, what="edge child")
        if self.kind not in EDGE_KINDS:
            raise CriticalPathError(
                f"unknown edge kind {self.kind!r}; expected {EDGE_KINDS}")

    def to_dict(self) -> dict[str, str]:
        return {"from": self.parent, "to": self.child, "kind": self.kind}


@dataclass(frozen=True)
class Dag:
    """A validated explicit dependency graph."""

    nodes: tuple[DagNode, ...]
    edges: tuple[DagEdge, ...] = ()

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        if len(nodes) > MAX_DAG_NODES:
            raise CriticalPathBoundsError(
                f"DAG has {len(nodes)} nodes; the maximum is {MAX_DAG_NODES}")
        if len(edges) > MAX_DAG_EDGES:
            raise CriticalPathBoundsError(
                f"DAG has {len(edges)} edges; the maximum is {MAX_DAG_EDGES}")
        by_id: dict[str, DagNode] = {}
        for node in nodes:
            if not isinstance(node, DagNode):
                raise CriticalPathError("DAG nodes must be DagNode values")
            if node.id in by_id:
                raise CriticalPathError(f"duplicate DAG node {node.id!r}")
            by_id[node.id] = node
        seen_edges: set[tuple[str, str]] = set()
        for edge in edges:
            if not isinstance(edge, DagEdge):
                raise CriticalPathError("DAG edges must be DagEdge values")
            if edge.parent not in by_id:
                raise UnknownNodeError(
                    f"edge parent {edge.parent!r} is not a DAG node")
            if edge.child not in by_id:
                raise UnknownNodeError(
                    f"edge child {edge.child!r} is not a DAG node")
            key = (edge.parent, edge.child)
            if key in seen_edges:
                raise CriticalPathError(
                    f"duplicate DAG edge {edge.parent!r} -> {edge.child!r}")
            seen_edges.add(key)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)

    @classmethod
    def from_pairs(cls, nodes: Iterable[DagNode],
                   edges: Iterable[Any] = ()) -> "Dag":
        """Build a DAG from nodes and ``(parent, child)`` pairs or edges."""
        edge_values: list[DagEdge] = []
        for edge in edges:
            if isinstance(edge, DagEdge):
                edge_values.append(edge)
            elif isinstance(edge, (tuple, list)) and len(edge) == 2:
                edge_values.append(DagEdge(edge[0], edge[1]))
            else:
                raise CriticalPathError(
                    "edges must be DagEdge values or (parent, child) pairs")
        return cls(tuple(nodes), tuple(edge_values))

    @property
    def node_map(self) -> dict[str, DagNode]:
        return {node.id: node for node in self.nodes}

    @property
    def parents(self) -> dict[str, tuple[str, ...]]:
        return _adjacency(self)[0]

    @property
    def children(self) -> dict[str, tuple[str, ...]]:
        return _adjacency(self)[1]


def _adjacency(dag: Dag) -> tuple[
        dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    parents: dict[str, list[str]] = {node.id: [] for node in dag.nodes}
    children: dict[str, list[str]] = {node.id: [] for node in dag.nodes}
    for edge in dag.edges:
        parents[edge.child].append(edge.parent)
        children[edge.parent].append(edge.child)
    return (
        {key: tuple(sorted(value)) for key, value in parents.items()},
        {key: tuple(sorted(value)) for key, value in children.items()},
    )


# ---------------------------------------------------------------------------
# Cycle detection and topological order
# ---------------------------------------------------------------------------


def find_cycles(dag: Dag) -> tuple[tuple[tuple[str, ...], ...],
                                  tuple[tuple[str, ...], ...]]:
    """Return ``(representative_cycles, cyclic_components)`` deterministically.

    One representative cycle is reported per cyclic strongly connected
    component; the component membership is reported alongside it.  This is
    bounded and deterministic rather than enumerating every simple cycle.
    """
    parents, children = _adjacency(dag)
    node_ids = sorted(node.id for node in dag.nodes)

    # Iterative Kosaraju: finishing order on the graph, then components on the
    # reversed graph.  Iterative because a DAG may be deeper than the default
    # Python recursion limit.
    visited: set[str] = set()
    order: list[str] = []
    for start in node_ids:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, Any]] = [(start, iter(children[start]))]
        while stack:
            current, iterator = stack[-1]
            advanced = False
            for child in iterator:
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(children[child])))
                    advanced = True
                    break
            if not advanced:
                order.append(current)
                stack.pop()

    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(order):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[str] = []
        pending = [start]
        while pending:
            current = pending.pop()
            component.append(current)
            for parent in parents[current]:
                if parent not in assigned:
                    assigned.add(parent)
                    pending.append(parent)
        components.append(tuple(sorted(component)))

    cyclic: list[tuple[str, ...]] = []
    for component in components:
        if len(component) > 1 or any(
                node in children[node] for node in component):
            cyclic.append(component)
    cyclic.sort()

    cycles: list[tuple[str, ...]] = []
    for component in cyclic:
        cycle = _representative_cycle(set(component), children)
        cycles.append(cycle)
    cycles.sort()
    return tuple(cycles), tuple(cyclic)


def _representative_cycle(component: set[str],
                          children: Mapping[str, tuple[str, ...]]
                          ) -> tuple[str, ...]:
    start = min(component)
    path = [start]
    positions = {start: 0}
    stack: list[tuple[str, Any]] = [(start, iter(children[start]))]
    while stack:
        node, iterator = stack[-1]
        advanced = False
        for child in iterator:
            if child == start:
                return tuple(path)
            if child in component and child not in positions:
                positions[child] = len(path)
                path.append(child)
                stack.append((child, iter(children[child])))
                advanced = True
                break
        if not advanced:
            stack.pop()
            if path:
                positions.pop(path.pop(), None)
    # Defensive: an SCC must contain a cycle, but never invent one silently.
    return tuple(sorted(component))


def topological_order(dag: Dag) -> tuple[str, ...]:
    """Return a deterministic topological order or raise :class:`CycleError`."""
    _parents, children = _adjacency(dag)
    indegree = {node.id: 0 for node in dag.nodes}
    for edge in dag.edges:
        indegree[edge.child] += 1
    heap = [node_id for node_id, count in indegree.items() if count == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        node_id = heapq.heappop(heap)
        order.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, child)
    if len(order) != len(dag.nodes):
        cycles, components = find_cycles(dag)
        raise CycleError(cycles, components, nodes=len(dag.nodes),
                         edges=len(dag.edges))
    return tuple(order)


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------


def _run_status(nodes: Iterable[DagNode], *, unknown_timing: bool) -> str:
    values = [node.outcome for node in nodes]
    if not values:
        return "unknown"
    if any(value == "cancelled" for value in values):
        return "cancelled"
    if any(value == "failed" for value in values):
        return "failed"
    if all(value == "skipped" for value in values):
        return "skipped"
    if unknown_timing:
        return "incomplete"
    if any(value in ("pending", "running", "unknown") for value in values):
        return "incomplete"
    return "completed"


def _descendants(start: str, children: Mapping[str, tuple[str, ...]]) -> set[str]:
    found: set[str] = set()
    pending = list(children[start])
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children[current])
    return found


def critical_path(dag: Dag, *, on_cycle: str = "raise",
                  include_schedule: bool = False) -> dict[str, Any]:
    """Compute the longest explicit dependency path through ``dag``.

    ``duration_ms`` is the exact longest path only when every duration and
    dependency is known.  Otherwise it is ``"unknown"`` and
    ``lower_bound_ms`` carries the labelled lower bound.  ``path`` is the
    deterministic candidate path (longest lower-bound finish, then smallest
    node ID) and ``path_is_critical`` says whether that candidate is proven.

    A terminal outcome (``failed``, ``cancelled`` or ``skipped``) is reported
    in ``terminal_nodes`` and its downstream nodes in ``blocked_descendants``.
    The measured durations are still included, but the run is never reported
    complete.
    """
    if on_cycle not in ("raise", "report"):
        raise CriticalPathError("on_cycle must be 'raise' or 'report'")
    if include_schedule is not True and include_schedule is not False:
        raise CriticalPathError("include_schedule must be a bool")
    try:
        order = topological_order(dag)
    except CycleError as error:
        if on_cycle == "raise":
            raise
        return cycle_report(error)

    node_map = dag.node_map
    parents, children = _adjacency(dag)
    starts: dict[str, int] = {}
    finishes: dict[str, int | None] = {}
    starts_lb: dict[str, int] = {}
    finishes_lb: dict[str, int] = {}

    for node_id in order:
        node = node_map[node_id]
        node_parents = parents[node_id]
        parent_lb = max((finishes_lb[parent] for parent in node_parents),
                        default=0)
        starts_lb[node_id] = parent_lb
        start_known = (not node.parents_unknown
                       and all(finishes[parent] is not None
                               for parent in node_parents))
        if start_known:
            starts[node_id] = max(
                (finishes[parent] for parent in node_parents
                 if finishes[parent] is not None), default=0)
        duration = node.duration_ms
        if duration is None:
            finishes[node_id] = None
            finishes_lb[node_id] = parent_lb
        elif start_known:
            finishes[node_id] = starts[node_id] + duration
            finishes_lb[node_id] = finishes[node_id]
        else:
            finishes[node_id] = None
            finishes_lb[node_id] = parent_lb + duration

    node_ids = sorted(node_map)
    unknown_duration = [node_id for node_id in node_ids
                        if node_map[node_id].duration_ms is None]
    unknown_dependency = [node_id for node_id in node_ids
                          if node_map[node_id].parents_unknown]
    unknown_timing = bool(unknown_duration or unknown_dependency)
    all_known = not unknown_timing

    end = (min(node_ids, key=lambda node_id: (
        -finishes_lb[node_id], node_id)) if node_ids else None)
    path: list[str] = []
    if end is not None:
        current: str | None = end
        while current is not None:
            path.append(current)
            node = node_map[current]
            siblings = parents[current]
            if node.parents_unknown or not siblings:
                break
            current = min(siblings, key=lambda parent: (
                -finishes_lb[parent], parent))
        path.reverse()

    path_known_ms = sum(
        (node_map[node_id].duration_ms or 0) for node_id in path)
    unknown_on_path = [node_id for node_id in path
                       if node_map[node_id].duration_ms is None
                       or node_map[node_id].parents_unknown]
    if not node_ids:
        duration_ms: int | str = 0
    elif all_known:
        duration_ms = finishes[end] if end is not None else 0
    else:
        duration_ms = UNKNOWN
    lower_bound_ms = finishes_lb[end] if end is not None else 0

    terminal_nodes = sorted(node_id for node_id in node_ids
                            if node_map[node_id].outcome
                            in TERMINAL_OUTCOMES)
    blocked: set[str] = set()
    for node_id in terminal_nodes:
        blocked.update(_descendants(node_id, children))
    blocked.difference_update(terminal_nodes)

    anomalies: list[dict[str, Any]] = []
    for node_id in terminal_nodes:
        downstream = sorted(_descendants(node_id, children))
        if downstream:
            anomalies.append({
                "kind": "terminal_with_descendants",
                "node": node_id,
                "descendants": downstream,
            })
        if node_id in path:
            anomalies.append({"kind": "terminal_on_path", "node": node_id})
    anomalies.sort(key=lambda item: (item["kind"], item["node"]))

    ties: list[str] = []
    for node_id in node_ids:
        node_parents = parents[node_id]
        if len(node_parents) < 2:
            continue
        values = [finishes_lb[parent] for parent in node_parents]
        best = max(values)
        if values.count(best) > 1:
            ties.append(node_id)

    total_work_ms = sum(node.duration_ms or 0 for node in dag.nodes)
    blocking = [
        {
            "id": node_id,
            "kind": node_map[node_id].kind,
            "outcome": node_map[node_id].outcome,
            "duration_ms": (UNKNOWN if node_map[node_id].duration_ms is None
                            else node_map[node_id].duration_ms),
        }
        for node_id in path
    ]
    run_status = _run_status(dag.nodes, unknown_timing=unknown_timing)
    report: dict[str, Any] = {
        "schema": schema_ref(SCHEMA_NAMESPACE, SCHEMA_VERSION),
        "status": "complete" if all_known else "incomplete",
        "run_status": run_status,
        "complete": run_status == "completed" and all_known,
        "path": path,
        "duration_ms": duration_ms,
        "lower_bound_ms": lower_bound_ms,
        "path_is_critical": all_known,
        "unknown_nodes": sorted(set(unknown_duration)
                                | set(unknown_dependency)
                                | set(unknown_on_path)),
        "unknown_duration_nodes": unknown_duration,
        "unknown_dependency_nodes": unknown_dependency,
        "nodes": len(node_ids),
        "edges": len(dag.edges),
        "blocking": blocking,
        "work_ms": total_work_ms,
        "work_is_lower_bound": bool(unknown_duration),
        "overlap_ms": total_work_ms - path_known_ms,
        "terminal_nodes": terminal_nodes,
        "blocked_descendants": sorted(blocked),
        "anomalies": anomalies,
        "ties": ties,
    }
    if include_schedule:
        report["starts"] = {
            node_id: starts.get(node_id, UNKNOWN) for node_id in node_ids
        }
        report["finishes"] = {
            node_id: finishes.get(node_id, UNKNOWN) for node_id in node_ids
        }
    return report


# ---------------------------------------------------------------------------
# What-if schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DurationEstimate:
    """Resolved duration for one node under one profile."""

    duration_ms: int | None
    source: str
    assumptions: tuple[str, ...] = ()
    lower_bound: bool = False
    reason: str | None = None


def resolve_duration_ms(node: DagNode, profile: ScheduleProfile, *,
                        estimate: ModelCallEstimate | Mapping[str, Any] | None = None,
                        unknown_policy: str = "unknown") -> DurationEstimate:
    """Resolve one node's modelled duration without inventing evidence.

    The formula is ``startup + queue + prefill + generation`` where prefill
    covers uncached input tokens and generation covers output tokens.  A
    measured ``node.duration_ms`` is used when no estimate is supplied; an
    estimate deliberately replaces it because this is a what-if run.
    """
    if unknown_policy not in ("unknown", "lower_bound"):
        raise CriticalPathError(
            "unknown_policy must be 'unknown' or 'lower_bound'")
    if estimate is not None:
        estimate = ModelCallEstimate.from_mapping(estimate)
    else:
        estimate = node.estimate
    if estimate is None:
        if node.duration_ms is not None:
            return DurationEstimate(node.duration_ms, "measured")
        return DurationEstimate(
            None, "unknown",
            reason="missing duration and no model-call estimate")

    assumptions: list[str] = []
    lower_bound = False
    if estimate.input_tokens is None:
        return DurationEstimate(
            None, "unknown", reason="missing input_tokens evidence")
    if estimate.output_tokens is None:
        return DurationEstimate(
            None, "unknown", reason="missing output_tokens evidence")
    cached = estimate.cached_input_tokens
    if cached is None:
        cached = 0
        assumptions.append("cached_input_tokens assumed 0")
    prefill_rate = (estimate.prefill_rate if estimate.prefill_rate is not None
                    else profile.prefill_rate)
    if prefill_rate is None:
        return DurationEstimate(
            None, "unknown", reason="missing prefill rate evidence")
    generation_rate = (
        estimate.generation_rate if estimate.generation_rate is not None
        else profile.generation_rate)
    if generation_rate is None:
        return DurationEstimate(
            None, "unknown", reason="missing generation rate evidence")

    queue_wait = (estimate.queue_wait_ms if estimate.queue_wait_ms is not None
                  else profile.queue_wait_ms)
    if queue_wait is None:
        if unknown_policy == "lower_bound":
            queue_wait = 0
            lower_bound = True
            assumptions.append("queue_wait_ms=0 (labelled lower bound)")
        else:
            return DurationEstimate(
                None, "unknown", reason="missing queue_wait_ms evidence")
    startup = (estimate.startup_ms if estimate.startup_ms is not None
               else profile.startup_ms)
    if startup is None:
        if unknown_policy == "lower_bound":
            startup = 0
            lower_bound = True
            assumptions.append("startup_ms=0 (labelled lower bound)")
        else:
            return DurationEstimate(
                None, "unknown", reason="missing startup_ms evidence")

    uncached = estimate.input_tokens - cached
    prefill_ms = _ceil_fraction(Fraction(uncached * 1000) / prefill_rate)
    generation_ms = _ceil_fraction(
        Fraction(estimate.output_tokens * 1000) / generation_rate)
    if node.duration_ms is not None:
        assumptions.append("token estimate replaces measured duration")
    return DurationEstimate(
        startup + queue_wait + prefill_ms + generation_ms,
        "estimated",
        tuple(sorted(set(assumptions))),
        lower_bound,
    )


def _can_start(active: list[tuple[int, str, str]],
               profile: ScheduleProfile, resource_class: str) -> bool:
    if (profile.max_concurrency is not None
            and len(active) >= profile.max_concurrency):
        return False
    limit = profile.limit_for(resource_class)
    if limit is None:
        return True
    running = sum(1 for _finish, _node, name in active
                  if name == resource_class)
    return running < limit


def _simulate(dag: Dag, profile: ScheduleProfile,
              durations: Mapping[str, int], schedulable: set[str]
              ) -> tuple[dict[str, int], dict[str, int]]:
    node_map = dag.node_map
    parents, children = _adjacency(dag)
    indegree = {
        node_id: sum(1 for parent in parents[node_id]
                     if parent in schedulable)
        for node_id in schedulable
    }
    ready = {node_id for node_id in schedulable if indegree[node_id] == 0}
    active: list[tuple[int, str, str]] = []
    starts: dict[str, int] = {}
    finishes: dict[str, int] = {}
    now = 0

    def release_children(node_id: str) -> None:
        for child in children[node_id]:
            if child not in schedulable:
                continue
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.add(child)

    while ready or active:
        released = sorted(
            (item for item in active if item[0] <= now),
            key=lambda item: (item[0], item[1]))
        for finish, node_id, resource_class in released:
            active.remove((finish, node_id, resource_class))
            finishes[node_id] = finish
            release_children(node_id)

        scheduled_any = False
        for node_id in sorted(ready):
            resource_class = node_map[node_id].resource_class
            if not _can_start(active, profile, resource_class):
                continue
            ready.remove(node_id)
            starts[node_id] = now
            duration = durations[node_id]
            if duration == 0:
                finishes[node_id] = now
                release_children(node_id)
            else:
                active.append((now + duration, node_id, resource_class))
            scheduled_any = True

        if not scheduled_any:
            if active:
                now = min(item[0] for item in active)
            elif ready:
                raise CriticalPathError(
                    "no resource slot is available for ready nodes; "
                    "concurrency limits must be positive")
            else:
                break
        elif not ready and active:
            now = min(item[0] for item in active)
    return starts, finishes


def _driving_path(dag: Dag, finishes: Mapping[str, int]) -> list[str]:
    parents, _children = _adjacency(dag)
    if not finishes:
        return []
    current = min(finishes, key=lambda node_id: (
        -finishes[node_id], node_id))
    path: list[str] = []
    while True:
        path.append(current)
        scheduled_parents = [parent for parent in parents[current]
                             if parent in finishes]
        if not scheduled_parents:
            break
        current = min(scheduled_parents, key=lambda parent: (
            -finishes[parent], parent))
    path.reverse()
    return path


def _what_if_cycle_report(error: CycleError, profile: ScheduleProfile,
                          unknown_policy: str) -> dict[str, Any]:
    return {
        "schema": schema_ref(WHAT_IF_NAMESPACE, WHAT_IF_VERSION),
        "status": "cycle",
        "complete": False,
        "makespan_ms": UNKNOWN,
        "lower_bound_ms": 0,
        "dependency_lower_bound_ms": UNKNOWN,
        "critical_path": [],
        "dependency_critical_path": [],
        "critical_path_changed": False,
        "per_stage": [],
        "unresolved": [],
        "unknown_nodes": [],
        "resource_wait_total_ms": 0,
        "bottlenecks": [],
        "assumptions": [],
        "profile": profile.to_dict(),
        "unknown_policy": unknown_policy,
        "lower_bound": False,
        "confidence": UNKNOWN,
        "nodes": error.nodes,
        "edges": error.edges,
        "cycles": [list(cycle) for cycle in error.cycles],
    }


def what_if(dag: Dag, profile: ScheduleProfile, *,
            estimates: Mapping[str, Any] | None = None,
            unknown_policy: str = "unknown",
            max_nodes: int = DEFAULT_MAX_SCHEDULE_NODES,
            on_cycle: str = "raise") -> dict[str, Any]:
    """Re-schedule the recorded dependency DAG under one profile.

    Every node is resolved from the caller's estimate (or the node's own
    estimate), then scheduled by a deterministic greedy list scheduler with
    the profile's global and per-resource concurrency limits.  Unknown
    durations block their descendants rather than being treated as zero.
    """
    if not isinstance(dag, Dag):
        raise CriticalPathError("what_if expects a Dag")
    if not isinstance(profile, ScheduleProfile):
        raise CriticalPathError("what_if expects a ScheduleProfile")
    if unknown_policy not in ("unknown", "lower_bound"):
        raise CriticalPathError(
            "unknown_policy must be 'unknown' or 'lower_bound'")
    if on_cycle not in ("raise", "report"):
        raise CriticalPathError("on_cycle must be 'raise' or 'report'")
    max_nodes = _required_int(max_nodes, "max_nodes")
    if len(dag.nodes) > max_nodes:
        raise CriticalPathBoundsError(
            f"schedule has {len(dag.nodes)} nodes; max_nodes is {max_nodes}")

    try:
        order = topological_order(dag)
    except CycleError as error:
        if on_cycle == "raise":
            raise
        return _what_if_cycle_report(error, profile, unknown_policy)

    supplied = dict(estimates or {})
    node_map = dag.node_map
    unknown_ids = [node_id for node_id in supplied if node_id not in node_map]
    if unknown_ids:
        raise UnknownNodeError(
            f"estimates refer to unknown nodes: {sorted(unknown_ids)}")

    resolutions: dict[str, DurationEstimate] = {}
    assumptions: set[str] = set()
    for node_id in order:
        node = node_map[node_id]
        raw_estimate = supplied.get(node_id)
        resolution = resolve_duration_ms(
            node, profile, estimate=raw_estimate,
            unknown_policy=unknown_policy)
        resolutions[node_id] = resolution
        for assumption in resolution.assumptions:
            assumptions.add(f"{node_id}: {assumption}")
    assumptions.add("greedy list schedule; makespan is simulated, not optimal")
    assumptions.add("resource limits are caller-supplied, not observed")
    assumptions.add(
        f"profile {profile.name} is caller-supplied, not a measured guarantee")

    parents, _children = _adjacency(dag)
    unresolved: dict[str, str] = {}
    durations: dict[str, int] = {}
    for node_id in order:
        node = node_map[node_id]
        if node.parents_unknown:
            unresolved[node_id] = "unknown dependency information"
            continue
        blocked = next((parent for parent in parents[node_id]
                        if parent in unresolved), None)
        if blocked is not None:
            unresolved[node_id] = f"blocked by unresolved parent {blocked}"
            continue
        resolution = resolutions[node_id]
        if resolution.duration_ms is None:
            unresolved[node_id] = resolution.reason or "unknown duration"
            continue
        durations[node_id] = resolution.duration_ms

    schedulable = set(durations)
    starts, finishes = _simulate(dag, profile, durations, schedulable)
    unresolved_ids = sorted(unresolved)
    makespan_known = not unresolved_ids
    known_makespan = max(finishes.values()) if finishes else 0
    makespan_ms: int | str = known_makespan if makespan_known else UNKNOWN

    waits: dict[str, int] = {}
    for node_id in starts:
        parent_finish = max(
            (finishes[parent] for parent in parents[node_id]
             if parent in finishes), default=0)
        waits[node_id] = starts[node_id] - parent_finish
    driving_path = _driving_path(dag, finishes)

    resolved_nodes = tuple(
        DagNode(
            id=node.id,
            duration_ms=(resolutions[node.id].duration_ms
                         if resolutions[node.id].duration_ms is not None
                         else None),
            outcome=node.outcome,
            resource_class=node.resource_class,
            kind=node.kind,
            parents_unknown=node.parents_unknown,
        )
        for node in dag.nodes
    )
    dependency = critical_path(Dag(resolved_nodes, dag.edges),
                               on_cycle="raise")
    # Keep the numeric dependency-only lower bound even when exact durations
    # are incomplete. critical_path() reports exactness separately in
    # duration_ms and preserves the zero-for-unknown lower bound explicitly.
    dependency_lower_bound_ms = dependency["lower_bound_ms"]

    path_set = set(driving_path)
    per_stage = [
        {
            "id": node_id,
            "kind": node_map[node_id].kind,
            "outcome": node_map[node_id].outcome,
            "resource_class": node_map[node_id].resource_class,
            "start_ms": starts[node_id],
            "finish_ms": finishes[node_id],
            "duration_ms": durations[node_id],
            "resource_wait_ms": waits[node_id],
            "on_driving_path": node_id in path_set,
        }
        for node_id in sorted(starts, key=lambda item: (starts[item], item))
    ]
    bottlenecks = sorted(
        (
            {
                "id": node_id,
                "resource_class": node_map[node_id].resource_class,
                "resource_wait_ms": waits[node_id],
                "duration_ms": durations[node_id],
            }
            for node_id in waits if waits[node_id] > 0
        ),
        key=lambda item: (-item["resource_wait_ms"], item["id"]),
    )[:MAX_BOTTLENECKS]

    any_lower_bound = bool(unresolved_ids) or any(
        resolution.lower_bound for resolution in resolutions.values())
    if not makespan_known:
        status = "incomplete"
    elif any_lower_bound:
        status = "lower_bound"
    else:
        status = "complete"
    sources = {resolution.source for resolution in resolutions.values()}
    if not makespan_known or not sources:
        confidence = UNKNOWN
    elif sources == {"measured"}:
        confidence = "measured"
    elif sources == {"estimated"}:
        confidence = "modelled"
    else:
        confidence = "mixed"
    report: dict[str, Any] = {
        "schema": schema_ref(WHAT_IF_NAMESPACE, WHAT_IF_VERSION),
        "status": status,
        "complete": status == "complete",
        "confidence": confidence,
        "makespan_ms": makespan_ms,
        "lower_bound_ms": known_makespan if not makespan_known else makespan_ms,
        "dependency_lower_bound_ms": dependency_lower_bound_ms,
        "critical_path": driving_path,
        "dependency_critical_path": dependency["path"],
        "critical_path_changed": driving_path != dependency["path"],
        "per_stage": per_stage,
        "unresolved": [
            {"id": node_id, "reason": unresolved[node_id]}
            for node_id in unresolved_ids
        ],
        "unknown_nodes": unresolved_ids,
        "resource_wait_total_ms": sum(waits.values()),
        "bottlenecks": bottlenecks,
        "assumptions": sorted(assumptions),
        "profile": profile.to_dict(),
        "unknown_policy": unknown_policy,
        "lower_bound": any_lower_bound,
        "nodes": len(dag.nodes),
        "edges": len(dag.edges),
    }
    return report


def compare_profiles(dag: Dag, *, fast: ScheduleProfile,
                     resource_limited: ScheduleProfile,
                     estimates: Mapping[str, Any] | None = None,
                     unknown_policy: str = "unknown",
                     max_nodes: int = DEFAULT_MAX_SCHEDULE_NODES,
                     on_cycle: str = "raise") -> dict[str, Any]:
    """Run :func:`what_if` under two profiles and compare the schedules."""
    fast_report = what_if(
        dag, fast, estimates=estimates, unknown_policy=unknown_policy,
        max_nodes=max_nodes, on_cycle=on_cycle)
    limited_report = what_if(
        dag, resource_limited, estimates=estimates,
        unknown_policy=unknown_policy, max_nodes=max_nodes, on_cycle=on_cycle)

    fast_makespan = fast_report["makespan_ms"]
    limited_makespan = limited_report["makespan_ms"]
    if isinstance(fast_makespan, int) and isinstance(limited_makespan, int):
        delta_ms: int | str = limited_makespan - fast_makespan
    else:
        delta_ms = UNKNOWN
    if fast_report["status"] == "complete" and limited_report["status"] == "complete":
        status = "complete"
    elif isinstance(fast_makespan, int) and isinstance(limited_makespan, int):
        status = "lower_bound"
    else:
        status = "incomplete"
    return {
        "schema": schema_ref(COMPARISON_NAMESPACE, COMPARISON_VERSION),
        "status": status,
        "summary": {
            "fast_makespan_ms": fast_makespan,
            "resource_limited_makespan_ms": limited_makespan,
            "resource_limited_delta_ms": delta_ms,
            "fast_critical_path": fast_report["critical_path"],
            "resource_limited_critical_path": limited_report["critical_path"],
            "critical_path_changed": (
                fast_report["critical_path"]
                != limited_report["critical_path"]),
        },
        "fast": fast_report,
        "resource_limited": limited_report,
        "bottlenecks": limited_report["bottlenecks"],
        "assumptions": sorted(set(fast_report["assumptions"])
                              | set(limited_report["assumptions"])),
    }


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------


def report_json(report: Mapping[str, Any]) -> str:
    """Return any timing report as canonical JSON."""
    return canonical_json(dict(report))


def report_hash(report: Mapping[str, Any]) -> str:
    """Return the SHA-256 content hash of a timing report."""
    return content_hash(dict(report))


__all__ = [
    "COMPARISON_NAMESPACE", "COMPARISON_SCHEMA", "COMPARISON_VERSION",
    "CRITICAL_PATH_ERROR_CODES", "CRITICAL_PATH_SCHEMA", "SCHEMA_NAMESPACE",
    "SCHEMA_VERSION", "CriticalPathBoundsError", "CriticalPathError",
    "CycleError", "DEFAULT_MAX_SCHEDULE_NODES", "Dag", "DagEdge", "DagNode",
    "DurationEstimate", "EDGE_KINDS", "InvalidNodeError", "MAX_BOTTLENECKS",
    "MAX_DAG_EDGES", "MAX_DAG_NODES", "MAX_NODE_ID", "ModelCallEstimate",
    "NODE_KINDS", "OUTCOMES", "RESOURCE_CLASSES", "ScheduleProfile",
    "TERMINAL_OUTCOMES", "UNKNOWN", "UnknownNodeError", "WHAT_IF_NAMESPACE",
    "WHAT_IF_SCHEMA", "WHAT_IF_VERSION", "compare_profiles", "critical_path",
    "cycle_report", "find_cycles", "report_hash", "report_json",
    "resolve_duration_ms", "schema_ref", "topological_order",
    "validate_node_id", "what_if",
]
