"""Offline, bounded, read-only prefetch planning and accounting.

A prefetch is speculative preparation of context that the next turn *may*
need.  This module plans and accounts for such work; it does not execute it.
There is no file, graph, network, model, test, install or checkout access here:
candidates are supplied by a caller (or built from task/goal/profile mappings),
the plan is a bounded value, and a ledger records injected prepare/use events.

Safety
------

* Only read-only candidate kinds are accepted.  ``model_call``, ``test``,
  ``install``, ``network``, ``write``, ``edit``, ``shell`` and checkout
  mutation are refused at candidate construction.
* A candidate must be explicitly ``authorized`` and ``trusted`` before it is
  selected.  A plan does not grant permission.
* Nothing is started speculatively: the planner is pure.  A caller that later
  performs the read is responsible for the existing owner's authorization.
* A cache hit is not proof of current state.  ``use`` rechecks the plan
  cancellation, scope hash, source hash, trust, authorization, freshness and
  expiry.  Stale, absent, expired, scope-changed, unauthorized, untrusted,
  errored and cancelled entries are never reported as current.

Accounting
----------

A :class:`PrefetchLedger` records the injected prepare cost and use of each
item.  Its report separates hits from unused/wasted preparation and overhead,
and keeps estimated savings explicitly labelled as estimates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Iterable, Mapping, Sequence

from ozagia.core import contracts as C

PLAN_NAMESPACE = "ozagia.prefetch_plan"
PLAN_VERSION = "1.0"
LEDGER_NAMESPACE = "ozagia.prefetch_ledger"
LEDGER_VERSION = "1.0"

READ_ONLY_KINDS = (
    "affected_tests",
    "environment_readiness",
    "graph_neighborhood",
    "linked_repo_freshness",
    "outline",
    "source_metadata",
)
FORBIDDEN_KINDS = (
    "checkout", "edit", "install", "model_call", "network", "shell", "test",
    "write",
)
PRIORITY = {
    "affected_tests": 10,
    "environment_readiness": 20,
    "graph_neighborhood": 30,
    "outline": 40,
    "linked_repo_freshness": 50,
    "source_metadata": 60,
}

MAX_CANDIDATES = 256
HARD_MAX_CANDIDATES = 1024
MAX_SELECTED = 64
HARD_MAX_SELECTED = 256
MAX_CONCURRENCY = 16
HARD_MAX_CONCURRENCY = 32
DEFAULT_MAX_TOTAL_MS = 2000
HARD_MAX_TOTAL_MS = 600_000
DEFAULT_MAX_IO_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_MEMORY_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
HARD_MAX_BYTES = 256 * 1024 * 1024
MAX_ITEMS = 256
MAX_LABEL = 200
MAX_TEXT = 512

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)


class PrefetchError(ValueError):
    """A malformed prefetch candidate, plan or ledger operation."""

    def __init__(self, message: str, *,
                 code: str = "prefetch.invalid_input") -> None:
        self.code = code
        super().__init__(_text(message, 500))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return " ".join(text.split())[:limit]


def _int(value: Any, name: str, *, minimum: int = 0,
         maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrefetchError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise PrefetchError(f"{name} must be {bound}")
    return value


def _bounded_text(value: Any, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PrefetchError(f"{name} must be a non-empty string")
    text = _text(value, limit)
    if not text:
        raise PrefetchError(f"{name} must be a non-empty string")
    return text


def _validate_id(value: Any, name: str = "candidate id") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PrefetchError(
            f"{name} {value!r} must match {_ID_RE.pattern}")
    return value


@dataclass(frozen=True)
class PrefetchCandidate:
    """One bounded, read-only prefetch candidate.

    ``priority`` is explicit and lower runs first; the planner never guesses a
    priority from a candidate kind.  ``source_hash`` is the hash the caller
    believes the item has; ``use`` must be given the current hash to serve it.
    ``freshness`` uses the contracts vocabulary.  An unknown hash or freshness
    can be planned, but it is never served as current.
    """

    id: str
    kind: str
    source: str
    source_id: str
    priority: int
    scope_hash: str
    source_hash: str = "unknown"
    expected_saving_ms: int = 0
    cost_ms: int = 0
    io_bytes: int = 0
    memory_bytes: int = 0
    payload_bytes: int = 0
    authorized: bool = False
    trusted: bool = False
    read_only: bool = True
    freshness: str = "unknown"
    expires_at_ms: int | None = None
    parents: tuple[str, ...] = ()
    label: str = ""

    def __post_init__(self) -> None:
        _validate_id(self.id)
        if self.kind in FORBIDDEN_KINDS:
            raise PrefetchError(
                f"prefetch kind {self.kind!r} is side-effecting and is never "
                "planned speculatively", code="prefetch.forbidden_kind")
        if self.kind not in READ_ONLY_KINDS:
            raise PrefetchError(
                f"unknown prefetch kind {self.kind!r}; expected one of "
                f"{', '.join(READ_ONLY_KINDS)}", code="prefetch.invalid_kind")
        object.__setattr__(self, "source",
                           _bounded_text(self.source, "source", 120))
        object.__setattr__(self, "source_id",
                           _bounded_text(self.source_id, "source_id", 200))
        object.__setattr__(self, "scope_hash",
                           _bounded_text(self.scope_hash, "scope_hash", 128))
        object.__setattr__(self, "source_hash",
                           _bounded_text(self.source_hash, "source_hash", 128))
        object.__setattr__(self, "priority",
                           _int(self.priority, "priority", maximum=1000))
        for name in ("expected_saving_ms", "cost_ms", "io_bytes",
                     "memory_bytes", "payload_bytes"):
            object.__setattr__(self, name, _int(getattr(self, name), name))
        if not isinstance(self.authorized, bool) \
                or not isinstance(self.trusted, bool) \
                or not isinstance(self.read_only, bool):
            raise PrefetchError(
                "authorized, trusted and read_only must be booleans")
        if self.read_only is not True:
            raise PrefetchError(
                "a prefetch candidate must be read-only",
                code="prefetch.forbidden_kind")
        if self.freshness not in C.FRESHNESS:
            raise PrefetchError(
                f"freshness must be one of {', '.join(C.FRESHNESS)}")
        if self.expires_at_ms is not None:
            object.__setattr__(self, "expires_at_ms",
                               _int(self.expires_at_ms, "expires_at_ms"))
        parents = tuple(self.parents or ())
        for parent in parents:
            _validate_id(parent, "candidate parent")
        if len(set(parents)) != len(parents):
            raise PrefetchError("candidate parents must be unique")
        object.__setattr__(self, "parents", tuple(sorted(parents)))
        object.__setattr__(self, "label", _text(self.label, MAX_LABEL))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "source_id": self.source_id,
            "priority": self.priority,
            "source_hash": self.source_hash,
            "expected_saving_ms": self.expected_saving_ms,
            "cost_ms": self.cost_ms,
            "io_bytes": self.io_bytes,
            "memory_bytes": self.memory_bytes,
            "payload_bytes": self.payload_bytes,
            "authorized": self.authorized,
            "trusted": self.trusted,
            "read_only": self.read_only,
            "freshness": self.freshness,
            "expires_at_ms": self.expires_at_ms,
            "parents": list(self.parents),
            "label": self.label,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "PrefetchCandidate":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise PrefetchError("a prefetch candidate must be a mapping")
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise PrefetchError(f"unknown prefetch candidate fields: {unknown}")
        missing = [name for name in ("id", "kind", "source", "source_id",
                                     "priority", "scope_hash")
                   if name not in value]
        if missing:
            raise PrefetchError(
                f"prefetch candidate is missing fields: {missing}")
        kwargs = dict(value)
        if isinstance(kwargs.get("parents"), list):
            kwargs["parents"] = tuple(kwargs["parents"])
        return cls(**kwargs)


@dataclass(frozen=True)
class PrefetchPolicy:
    """Caller-owned caps; every default is deliberately small."""

    max_candidates: int = 64
    max_selected: int = 32
    max_concurrency: int = 4
    max_total_ms: int = DEFAULT_MAX_TOTAL_MS
    max_io_bytes: int = DEFAULT_MAX_IO_BYTES
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_candidates", _int(
            self.max_candidates, "max_candidates", minimum=1,
            maximum=HARD_MAX_CANDIDATES))
        object.__setattr__(self, "max_selected", _int(
            self.max_selected, "max_selected", minimum=0,
            maximum=HARD_MAX_SELECTED))
        object.__setattr__(self, "max_concurrency", _int(
            self.max_concurrency, "max_concurrency", minimum=1,
            maximum=HARD_MAX_CONCURRENCY))
        object.__setattr__(self, "max_total_ms", _int(
            self.max_total_ms, "max_total_ms", minimum=0,
            maximum=HARD_MAX_TOTAL_MS))
        for name in ("max_io_bytes", "max_memory_bytes", "max_payload_bytes"):
            object.__setattr__(self, name, _int(
                getattr(self, name), name, minimum=0,
                maximum=HARD_MAX_BYTES))

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


def _topological(candidates: Sequence[PrefetchCandidate]
                 ) -> tuple[PrefetchCandidate, ...]:
    by_id: dict[str, PrefetchCandidate] = {}
    for candidate in candidates:
        if candidate.id in by_id:
            raise PrefetchError(
                f"duplicate prefetch candidate {candidate.id!r}",
                code="prefetch.duplicate_id")
        by_id[candidate.id] = candidate
    children: dict[str, list[str]] = {candidate_id: []
                                      for candidate_id in by_id}
    indegree: dict[str, int] = {candidate_id: 0 for candidate_id in by_id}
    for candidate in candidates:
        for parent in candidate.parents:
            if parent not in by_id:
                raise PrefetchError(
                    f"candidate {candidate.id!r} names unknown parent "
                    f"{parent!r}", code="prefetch.unknown_parent")
            children[parent].append(candidate.id)
            indegree[candidate.id] += 1
    ready = sorted((by_id[candidate_id] for candidate_id, count
                    in indegree.items() if count == 0),
                   key=lambda item: (item.priority, -item.expected_saving_ms,
                                     item.id))
    ordered: list[PrefetchCandidate] = []
    while ready:
        candidate = ready.pop(0)
        ordered.append(candidate)
        for child in sorted(children[candidate.id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(by_id[child])
        ready.sort(key=lambda item: (item.priority, -item.expected_saving_ms,
                                     item.id))
    if len(ordered) != len(candidates):
        raise PrefetchError("prefetch candidate dependencies contain a cycle",
                            code="prefetch.cycle")
    return tuple(ordered)


def _scope_value(scope: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(scope, Mapping):
        raise PrefetchError("scope must be a mapping")
    value = dict(scope)
    return value, C.content_hash(value)


def _plan_hash(plan: Mapping[str, Any]) -> str:
    return C.content_hash({key: value for key, value in plan.items()
                           if key != "hash"})


def build_prefetch_plan(candidates: Iterable[Any], *, scope: Mapping[str, Any],
                        policy: PrefetchPolicy | None = None,
                        now_ms: int = 0, cancelled: bool = False,
                        cancelled_reason: str | None = None
                        ) -> dict[str, Any]:
    """Return a bounded, deterministic plan; nothing is executed."""
    checked_policy = policy or PrefetchPolicy()
    if not isinstance(checked_policy, PrefetchPolicy):
        checked_policy = PrefetchPolicy(**dict(checked_policy))
    now = _int(now_ms, "now_ms")
    if not isinstance(cancelled, bool):
        raise PrefetchError("cancelled must be a bool")
    scope_value, scope_hash = _scope_value(scope)
    supplied = [PrefetchCandidate.from_mapping(item) for item in candidates]
    if len(supplied) > checked_policy.max_candidates:
        raise PrefetchError(
            f"{len(supplied)} candidates exceed the bound of "
            f"{checked_policy.max_candidates}", code="prefetch.bounds_exceeded")
    ordered = _topological(supplied)
    refused: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    selected: list[PrefetchCandidate] = []
    selected_ids: set[str] = set()
    totals = {"cost_ms": 0, "io_bytes": 0, "memory_bytes": 0,
              "payload_bytes": 0}

    if cancelled:
        return _finish_plan(
            scope_value, scope_hash, checked_policy, now, "cancelled", [],
            [{"id": candidate.id, "reason": "cancelled"}
             for candidate in ordered],
            [], {"cost_ms": 0, "io_bytes": 0, "memory_bytes": 0,
                 "payload_bytes": 0},
            cancelled_reason or "cancelled before planning")

    for candidate in ordered:
        reason: str | None = None
        if candidate.scope_hash != scope_hash:
            reason = "scope_mismatch"
        elif not candidate.authorized:
            reason = "unauthorized"
        elif not candidate.trusted:
            reason = "untrusted"
        elif candidate.freshness in ("stale", "absent"):
            reason = candidate.freshness
        elif candidate.expires_at_ms is not None \
                and candidate.expires_at_ms <= now:
            reason = "expired"
        elif any(parent not in selected_ids for parent in candidate.parents):
            reason = "parent_not_selected"
        elif len(selected) >= checked_policy.max_selected:
            reason = "max_selected"
        elif totals["cost_ms"] + candidate.cost_ms \
                > checked_policy.max_total_ms:
            reason = "time_budget"
        elif totals["io_bytes"] + candidate.io_bytes \
                > checked_policy.max_io_bytes:
            reason = "io_budget"
        elif totals["memory_bytes"] + candidate.memory_bytes \
                > checked_policy.max_memory_bytes:
            reason = "memory_budget"
        elif totals["payload_bytes"] + candidate.payload_bytes \
                > checked_policy.max_payload_bytes:
            reason = "byte_budget"
        if reason is not None:
            (refused if reason in ("scope_mismatch", "unauthorized", "untrusted",
                                   "stale", "absent", "expired",
                                   "parent_not_selected") else deferred).append(
                {"id": candidate.id, "reason": reason})
            continue
        selected.append(candidate)
        selected_ids.add(candidate.id)
        totals["cost_ms"] += candidate.cost_ms
        totals["io_bytes"] += candidate.io_bytes
        totals["memory_bytes"] += candidate.memory_bytes
        totals["payload_bytes"] += candidate.payload_bytes

    waves = _waves(selected, checked_policy.max_concurrency)
    selected_rows = []
    wave_start: dict[int, int] = {}
    cumulative = 0
    for wave in sorted(set(waves.values())):
        wave_start[wave] = cumulative
        cumulative += max((candidate.cost_ms for candidate in selected
                           if waves[candidate.id] == wave), default=0)
    for candidate in selected:
        row = candidate.to_dict()
        row["wave"] = waves[candidate.id]
        row["start_offset_ms"] = wave_start[waves[candidate.id]]
        selected_rows.append(row)
    selected_rows.sort(key=lambda row: (row["wave"], row["priority"], row["id"]))
    status = "planned" if selected_rows else "empty"
    return _finish_plan(
        scope_value, scope_hash, checked_policy, now, status, selected_rows,
        refused, deferred, totals, None, waves=waves)


def _waves(selected: Sequence[PrefetchCandidate],
           concurrency: int) -> dict[str, int]:
    by_id = {candidate.id: candidate for candidate in selected}
    waves: dict[str, int] = {}
    occupancy: dict[int, int] = {}
    for candidate in _topological(selected):
        base = max((waves[parent] for parent in candidate.parents
                    if parent in by_id), default=-1) + 1
        wave = base
        while occupancy.get(wave, 0) >= concurrency:
            wave += 1
        waves[candidate.id] = wave
        occupancy[wave] = occupancy.get(wave, 0) + 1
    return waves


def _finish_plan(scope: Mapping[str, Any], scope_hash: str,
                 policy: PrefetchPolicy, now_ms: int, status: str,
                 selected: Sequence[Mapping[str, Any]],
                 refused: Sequence[Mapping[str, Any]],
                 deferred: Sequence[Mapping[str, Any]],
                 totals: Mapping[str, int],
                 cancelled_reason: str | None,
                 *, waves: Mapping[str, int] | None = None) -> dict[str, Any]:
    total_cost = int(totals.get("cost_ms", 0))
    estimated_ms = 0
    if waves and selected:
        for wave in sorted(set(waves.values())):
            estimated_ms += max((row["cost_ms"] for row in selected
                                 if row["wave"] == wave), default=0)
    elif selected:
        estimated_ms = total_cost
    plan: dict[str, Any] = {
        "schema": {"namespace": PLAN_NAMESPACE, "version": PLAN_VERSION},
        "plan_version": PLAN_VERSION,
        "status": status,
        "scope": dict(scope),
        "scope_hash": scope_hash,
        "policy": policy.to_dict(),
        "now_ms": now_ms,
        "selected": list(selected),
        "refused": list(refused)[:MAX_ITEMS],
        "deferred": list(deferred)[:MAX_ITEMS],
        "estimated_ms": estimated_ms,
        "estimated_io_bytes": int(totals.get("io_bytes", 0)),
        "estimated_memory_bytes": int(totals.get("memory_bytes", 0)),
        "estimated_payload_bytes": int(totals.get("payload_bytes", 0)),
        "wave_count": len(set(waves.values())) if waves else 0,
        "cancelled_reason": _text(cancelled_reason, 200)
        if cancelled_reason else None,
    }
    plan["hash"] = _plan_hash(plan)
    return validate_prefetch_plan(plan)


def validate_prefetch_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a plan's version, required fields and content hash."""
    if not isinstance(plan, Mapping):
        raise PrefetchError("prefetch plan must be a mapping")
    value = dict(plan)
    schema = value.get("schema") if isinstance(value.get("schema"),
                                               Mapping) else {}
    if schema.get("namespace") != PLAN_NAMESPACE \
            or schema.get("version") != PLAN_VERSION:
        raise PrefetchError(
            f"prefetch plan schema must be {PLAN_NAMESPACE} v{PLAN_VERSION}")
    if value.get("plan_version") != PLAN_VERSION:
        raise PrefetchError(f"plan_version must be {PLAN_VERSION!r}")
    for key in ("status", "scope", "scope_hash", "policy", "selected",
                "refused", "deferred", "estimated_ms", "hash"):
        if key not in value:
            raise PrefetchError(f"prefetch plan is missing {key!r}")
    if value.get("status") not in ("planned", "empty", "cancelled"):
        raise PrefetchError("prefetch plan status is invalid")
    if value.get("hash") != _plan_hash(value):
        raise PrefetchError("prefetch plan hash does not match its content")
    C.canonical_json(value)
    return value


def plan_json(plan: Mapping[str, Any]) -> str:
    """Canonical JSON for a validated prefetch plan."""
    return C.canonical_json(validate_prefetch_plan(plan))


@dataclass(frozen=True)
class _Prepared:
    candidate: PrefetchCandidate
    prepared_at_ms: int
    prepared_ms: int
    payload_bytes: int
    value_hash: str
    expires_at_ms: int | None
    error: str | None = None


class PrefetchLedger:
    """Injected prepare/use accounting for one validated plan."""

    def __init__(self, plan: Mapping[str, Any], *, now_ms: int = 0) -> None:
        self.plan = validate_prefetch_plan(plan)
        self.now_ms = _int(now_ms, "now_ms")
        self.cancelled_reason: str | None = (
            str(self.plan.get("cancelled_reason"))
            if self.plan.get("cancelled_reason") else None)
        self.prepared: dict[str, _Prepared] = {}
        self.prepared_total = 0
        self.prepared_ms_total = 0
        self.prepared_bytes_total = 0
        self.used: set[str] = set()
        self.used_ms_total = 0
        self.used_bytes_total = 0
        self.saved_ms_total = 0
        self.error_count = 0
        self.discarded = 0
        self.served_count = 0
        self.rejected: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []
        self.overhead_ms = 0
        self._selected = {}
        for row in self.plan.get("selected") or []:
            if not isinstance(row, Mapping) or not isinstance(row.get("id"),
                                                             str):
                raise PrefetchError(
                    "every selected plan row must have a string id",
                    code="prefetch.invalid_plan")
            data = {key: value for key, value in row.items()
                    if key in {item.name for item in fields(PrefetchCandidate)}}
            data["scope_hash"] = self.plan["scope_hash"]
            candidate = PrefetchCandidate.from_mapping(data)
            if candidate.id in self._selected:
                raise PrefetchError(
                    f"duplicate selected candidate {candidate.id!r}",
                    code="prefetch.duplicate_id")
            self._selected[candidate.id] = candidate
        self._scope_hash = str(self.plan["scope_hash"])

    # -- lifecycle -------------------------------------------------------- #
    def cancel(self, reason: str = "cancelled") -> None:
        """Cancel the plan; prepared and future items are never served."""
        self.cancelled_reason = _text(reason, 200) or "cancelled"
        self.events.append({"event": "cancel", "reason": self.cancelled_reason})

    def changed_scope(self, new_scope: Mapping[str, Any]) -> str:
        """Cancel on a scope change and return the new scope hash."""
        _value, new_hash = _scope_value(new_scope)
        if new_hash == self._scope_hash:
            return new_hash
        self.cancel("scope_changed")
        for candidate_id in sorted(self.prepared):
            self.discard(candidate_id, "scope_changed")
        return new_hash

    def record_overhead_ms(self, value: int) -> None:
        self.overhead_ms += _int(value, "overhead_ms")

    def record_prepared(self, candidate_id: str, *, prepared_at_ms: int,
                        prepared_ms: int, payload_bytes: int, value_hash: str,
                        expires_at_ms: int | None = None,
                        error: str | None = None) -> dict[str, Any]:
        """Record an injected preparation result; no payload is stored."""
        _validate_id(candidate_id)
        if candidate_id not in self._selected:
            raise PrefetchError(
                f"candidate {candidate_id!r} is not selected by this plan",
                code="prefetch.not_selected")
        if candidate_id in self.prepared:
            raise PrefetchError(
                f"candidate {candidate_id!r} was already prepared",
                code="prefetch.duplicate_prepare")
        at = _int(prepared_at_ms, "prepared_at_ms")
        cost = _int(prepared_ms, "prepared_ms")
        payload = _int(payload_bytes, "payload_bytes")
        if not isinstance(value_hash, str) or not value_hash \
                or len(value_hash) > 128:
            raise PrefetchError("value_hash must be a bounded non-empty string")
        if expires_at_ms is not None:
            expires_at_ms = _int(expires_at_ms, "expires_at_ms")
        candidate = self._selected[candidate_id]
        entry = _Prepared(candidate, at, cost, payload, _text(value_hash, 128),
                          expires_at_ms, _text(error, 300) if error else None)
        self.prepared[candidate_id] = entry
        self.prepared_total += 1
        self.prepared_ms_total += cost
        self.prepared_bytes_total += payload
        if entry.error:
            self.error_count += 1
        self.events.append({"event": "prepared", "id": candidate_id,
                            "at_ms": at, "prepared_ms": cost,
                            "payload_bytes": payload,
                            "error": entry.error})
        return {"id": candidate_id, "state": "error" if entry.error else "prepared"}

    def discard(self, candidate_id: str, reason: str = "discarded") -> None:
        """Forget a prepared item; unused preparation remains waste."""
        _validate_id(candidate_id)
        if self.prepared.pop(candidate_id, None) is not None:
            self.discarded += 1
            self.events.append({"event": "discard", "id": candidate_id,
                                "reason": _text(reason, 120)})

    # -- use -------------------------------------------------------------- #
    def use(self, candidate_id: str, *, at_ms: int,
            current_scope_hash: str | None = None,
            source_hash: str | None = None,
            freshness: str | None = None,
            authorized: bool = True, trusted: bool = True,
            expires_at_ms: int | None = None) -> dict[str, Any]:
        """Try to serve one prepared item, rechecking every current fact.

        The returned ``current`` flag is true only when every check passed and
        the supplied freshness is ``current``.  A served item with unknown
        freshness has ``served=True`` and ``current=False``.
        """
        at = _int(at_ms, "at_ms")
        _validate_id(candidate_id)
        if not isinstance(authorized, bool) or not isinstance(trusted, bool):
            raise PrefetchError("authorized and trusted must be booleans")
        if current_scope_hash is not None:
            _bounded_text(current_scope_hash, "current_scope_hash", 128)
        if source_hash is not None:
            _bounded_text(source_hash, "source_hash", 128)
        if freshness is not None and freshness not in C.FRESHNESS:
            raise PrefetchError(
                f"freshness must be one of {', '.join(C.FRESHNESS)}")
        status, served, current = self._use_status(
            candidate_id, at=at, current_scope_hash=current_scope_hash,
            source_hash=source_hash, freshness=freshness,
            authorized=authorized, trusted=trusted, expires_at_ms=expires_at_ms)
        if served:
            if candidate_id not in self.used:
                entry = self.prepared.get(candidate_id)
                if entry is not None:
                    self.used_ms_total += entry.prepared_ms
                    self.used_bytes_total += entry.payload_bytes
                    self.saved_ms_total += entry.candidate.expected_saving_ms
            self.used.add(candidate_id)
            self.served_count += 1
        elif status not in ("already_used",):
            self.rejected[status] = self.rejected.get(status, 0) + 1
        self.events.append({"event": "use", "id": candidate_id, "at_ms": at,
                            "status": status, "served": served,
                            "current": current})
        return {"id": candidate_id, "status": status, "served": served,
                "current": current}

    def _use_status(self, candidate_id: str, *, at: int,
                    current_scope_hash: str | None,
                    source_hash: str | None,
                    freshness: str | None,
                    authorized: bool, trusted: bool,
                    expires_at_ms: int | None) -> tuple[str, bool, bool]:
        if self.cancelled_reason:
            return "cancelled", False, False
        if current_scope_hash is not None \
                and current_scope_hash != self._scope_hash:
            return "scope_changed", False, False
        entry = self.prepared.get(candidate_id)
        if entry is None:
            return "not_prepared", False, False
        if entry.error:
            return "error", False, False
        if not authorized:
            return "unauthorized", False, False
        if not trusted:
            return "untrusted", False, False
        if source_hash is not None and source_hash != entry.candidate.source_hash:
            return "source_changed", False, False
        if entry.candidate.source_hash == "unknown":
            return "hit_unknown", True, False
        resolved_freshness = freshness or entry.candidate.freshness
        if resolved_freshness in ("stale", "absent"):
            return resolved_freshness, False, False
        expiry = expires_at_ms if expires_at_ms is not None \
            else entry.expires_at_ms
        if expiry is not None and expiry <= at:
            return "expired", False, False
        if current_scope_hash is None:
            return "hit_unknown", True, False
        if candidate_id in self.used:
            return "already_used", True, resolved_freshness == "current"
        if resolved_freshness == "unknown":
            return "hit_unknown", True, False
        return "hit", True, True

    # -- accounting ------------------------------------------------------- #
    def report(self) -> dict[str, Any]:
        """Return the bounded ``ozagia.prefetch_ledger`` v1.0 report."""
        prepared_ids = set(self.prepared)
        unused = max(0, self.prepared_total - len(self.used))
        errors = sum(1 for entry in self.prepared.values() if entry.error)
        errors = max(errors, self.error_count)
        wasted_ms = max(0, self.prepared_ms_total - self.used_ms_total)
        wasted_bytes = max(0, self.prepared_bytes_total - self.used_bytes_total)
        overhead = self.prepared_ms_total + self.overhead_ms
        items = []
        for candidate_id in sorted(prepared_ids):
            entry = self.prepared[candidate_id]
            items.append({
                "id": candidate_id,
                "kind": entry.candidate.kind,
                "source": entry.candidate.source,
                "source_id": entry.candidate.source_id,
                "state": ("error" if entry.error else
                          "used" if candidate_id in self.used else "unused"),
                "prepared_ms": entry.prepared_ms,
                "payload_bytes": entry.payload_bytes,
                "expected_saving_ms": entry.candidate.expected_saving_ms,
                "error": entry.error,
            })
        items = items[:MAX_ITEMS]
        rejected = dict(sorted((key, value) for key, value in self.rejected.items()
                              if value))
        for row in self.plan.get("refused") or []:
            if isinstance(row, Mapping) and isinstance(row.get("reason"), str):
                key = str(row["reason"])
                rejected[key] = rejected.get(key, 0) + 1
        for row in self.plan.get("deferred") or []:
            if isinstance(row, Mapping) and isinstance(row.get("reason"), str):
                key = str(row["reason"])
                rejected[key] = rejected.get(key, 0) + 1
        report: dict[str, Any] = {
            "schema": {"namespace": LEDGER_NAMESPACE, "version": LEDGER_VERSION},
            "ledger_version": LEDGER_VERSION,
            "plan_hash": self.plan["hash"],
            "status": "cancelled" if self.cancelled_reason else "active",
            "cancelled_reason": self.cancelled_reason,
            "prepared": self.prepared_total,
            "used": len(self.used),
            "hits": len(self.used),
            "served": self.served_count,
            "unused": unused,
            "discarded": self.discarded,
            "errors": errors,
            "wasted_ms": wasted_ms,
            "wasted_bytes": wasted_bytes,
            "overhead_ms": overhead,
            "estimated_saved_ms": self.saved_ms_total,
            "net_estimated_ms": self.saved_ms_total - overhead,
            "rejected": rejected,
            "items": items,
            "events": len(self.events),
            "bounds": {"items": MAX_ITEMS, "candidates":
                       self.plan.get("policy", {}).get("max_candidates")},
        }
        report["hash"] = C.content_hash(
            {key: value for key, value in report.items() if key != "hash"})
        return report


def ledger_json(report: Mapping[str, Any]) -> str:
    """Canonical JSON for a prefetch ledger report."""
    return C.canonical_json(dict(report))


# ---------------------------------------------------------------------------
# Bounded candidate builders (read-only inputs only)
# ---------------------------------------------------------------------------


def _candidate(source: str, source_id: str, kind: str, scope_hash: str, *,
               source_hash: str = "unknown", priority: int | None = None,
               expected_saving_ms: int = 0, cost_ms: int = 0,
               io_bytes: int = 0, memory_bytes: int = 0,
               payload_bytes: int = 0, authorized: bool = False,
               trusted: bool = False, freshness: str = "unknown",
               expires_at_ms: int | None = None, label: str = "",
               suffix: str = "") -> PrefetchCandidate:
    raw_id = f"{kind}:{source_id}:{suffix}" if suffix else f"{kind}:{source_id}"
    candidate_id = re.sub(r"[^a-z0-9._:-]+", "-", raw_id.lower()).strip("-")
    if len(candidate_id) > 128:
        import hashlib

        candidate_id = (candidate_id[:110] + "-"
                        + hashlib.sha256(raw_id.encode()).hexdigest()[:12])
    if not candidate_id or not candidate_id[0].isalnum():
        candidate_id = f"cand-{candidate_id}"
    return PrefetchCandidate(
        id=candidate_id, kind=kind, source=source, source_id=source_id,
        priority=PRIORITY[kind] if priority is None else priority,
        scope_hash=scope_hash, source_hash=source_hash,
        expected_saving_ms=expected_saving_ms, cost_ms=cost_ms,
        io_bytes=io_bytes, memory_bytes=memory_bytes,
        payload_bytes=payload_bytes, authorized=authorized, trusted=trusted,
        freshness=freshness, expires_at_ms=expires_at_ms, label=label)


def candidates_from_goal(goal: Mapping[str, Any], *, scope_hash: str,
                         authorized: bool = False, trusted: bool = False,
                         max_candidates: int = 32) -> list[PrefetchCandidate]:
    """Build bounded read-only candidates from a goal record.

    Test criteria become affected-test-plan candidates and scope/target keys
    become graph-neighborhood candidates.  Authorization and trust default to
    false; the caller must opt in explicitly.
    """
    if not isinstance(goal, Mapping):
        raise PrefetchError("goal must be a mapping")
    limit = _int(max_candidates, "max_candidates", maximum=MAX_CANDIDATES)
    candidates: list[PrefetchCandidate] = []
    seen: set[str] = set()
    criteria = [item for item in goal.get("criteria") or []
                if isinstance(item, Mapping)]
    for item in criteria:
        args = item.get("args") if isinstance(item.get("args"), Mapping) else {}
        key = args.get("key")
        if item.get("kind") in ("test_passes", "test_fails") \
                and isinstance(key, str) and key.startswith("test:"):
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_candidate(
                "ozagia.core.goals", key, "affected_tests", scope_hash,
                authorized=authorized, trusted=trusted,
                expected_saving_ms=500, cost_ms=250, io_bytes=16 * 1024,
                payload_bytes=4 * 1024, label=key))
        if len(candidates) >= limit:
            return candidates
    for key in [*(goal.get("targets") or []), *(goal.get("scope") or [])]:
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        candidates.append(_candidate(
            "ozagia.core.goals", key, "graph_neighborhood", scope_hash,
            authorized=authorized, trusted=trusted,
            expected_saving_ms=300, cost_ms=150, io_bytes=32 * 1024,
            payload_bytes=8 * 1024, label=key))
        if len(candidates) >= limit:
            break
    candidates.sort(key=lambda item: (item.priority, item.id))
    return candidates


def candidates_from_profile(profile: Mapping[str, Any], *, scope_hash: str,
                            authorized: bool = False, trusted: bool = False,
                            max_candidates: int = 32
                            ) -> list[PrefetchCandidate]:
    """Build candidates from measured spans in a validated task profile."""
    data = profile.get("data") if isinstance(profile.get("data"),
                                             Mapping) else {}
    spans = [item for item in data.get("spans") or [] if isinstance(item,
                                                                     Mapping)]
    limit = _int(max_candidates, "max_candidates", maximum=MAX_CANDIDATES)
    candidates: list[PrefetchCandidate] = []
    seen: set[str] = set()
    for span in spans:
        span_id = span.get("id")
        if not isinstance(span_id, str) or span_id in seen:
            continue
        seen.add(span_id)
        kind = span.get("kind")
        if kind == "check":
            candidate_kind = "affected_tests"
        elif kind in ("graph_prepare", "tool_call") \
                and span.get("resource_class") == "graph":
            candidate_kind = "graph_neighborhood"
        elif kind in ("index_prepare", "tool_call", "model_call"):
            candidate_kind = "source_metadata"
        else:
            continue
        candidates.append(_candidate(
            "ozagia.core.task_profile", span_id, candidate_kind, scope_hash,
            authorized=authorized, trusted=trusted,
            expected_saving_ms=200, cost_ms=100, io_bytes=8 * 1024,
            payload_bytes=2 * 1024, label=span_id))
        if len(candidates) >= limit:
            break
    candidates.sort(key=lambda item: (item.priority, item.id))
    return candidates


def candidates_from_graph_neighborhood(
        neighborhood: Mapping[str, Any], *, scope_hash: str,
        source: str = "ozagia.core.graph", authorized: bool = False,
        trusted: bool = False, max_candidates: int = 32
) -> list[PrefetchCandidate]:
    """Build candidates from an injected neighborhood mapping.

    Each value may carry ``source_hash``, ``freshness``, ``priority``,
    ``expected_saving_ms``, ``cost_ms``, ``io_bytes``, ``payload_bytes``,
    ``expires_at_ms`` and ``label``.  No graph is read here.
    """
    if not isinstance(neighborhood, Mapping):
        raise PrefetchError("neighborhood must be a mapping")
    limit = _int(max_candidates, "max_candidates", maximum=MAX_CANDIDATES)
    candidates: list[PrefetchCandidate] = []
    for key in sorted(neighborhood):
        if not isinstance(key, str):
            continue
        value = neighborhood[key]
        if not isinstance(value, Mapping):
            value = {}
        candidates.append(_candidate(
            source, key, "graph_neighborhood", scope_hash,
            source_hash=str(value.get("source_hash") or "unknown"),
            priority=value.get("priority"),
            expected_saving_ms=value.get("expected_saving_ms", 300),
            cost_ms=value.get("cost_ms", 100),
            io_bytes=value.get("io_bytes", 8 * 1024),
            memory_bytes=value.get("memory_bytes", 0),
            payload_bytes=value.get("payload_bytes", 2 * 1024),
            authorized=authorized, trusted=trusted,
            freshness=str(value.get("freshness") or "unknown"),
            expires_at_ms=value.get("expires_at_ms"),
            label=str(value.get("label") or key)))
        if len(candidates) >= limit:
            break
    return candidates


__all__ = [
    "DEFAULT_MAX_IO_BYTES", "DEFAULT_MAX_MEMORY_BYTES",
    "DEFAULT_MAX_PAYLOAD_BYTES", "DEFAULT_MAX_TOTAL_MS", "FORBIDDEN_KINDS",
    "HARD_MAX_BYTES", "HARD_MAX_CANDIDATES", "HARD_MAX_CONCURRENCY",
    "HARD_MAX_SELECTED", "HARD_MAX_TOTAL_MS", "LEDGER_NAMESPACE",
    "LEDGER_VERSION", "MAX_CANDIDATES", "MAX_CONCURRENCY", "MAX_ITEMS",
    "MAX_SELECTED", "PLAN_NAMESPACE", "PLAN_VERSION", "PRIORITY",
    "PrefetchCandidate", "PrefetchError", "PrefetchLedger", "PrefetchPolicy",
    "READ_ONLY_KINDS", "build_prefetch_plan", "candidates_from_goal",
    "candidates_from_graph_neighborhood", "candidates_from_profile",
    "ledger_json", "plan_json", "validate_prefetch_plan",
]
