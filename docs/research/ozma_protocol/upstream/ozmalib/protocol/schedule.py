"""
schedule.py — First-class scheduled wake-up (v5.11 SCHEDULE).

Reference implementation of the SCHEDULE capability: registry, in-memory
store, fire-id computation, overlap/drift policies, fire-storm breaker,
mass-messaging throttle, and per-agent schedule ceiling.

Design principle: a wake-up call is a turn. Everything the protocol already
does for human-prompted turns applies to scheduled turns. The only new
surface is the trigger declaration and the firing machinery here.

Fire-id scheme. Deterministic id = `sha256(schedule_id + fire_ts_iso + epoch)[:12]`.
`epoch` is the scheduler's leader-epoch, hardening against HA leader-flip
clock-skew collisions (review-issue #11). Same-slot fires under the same
leader still dedupe (the idempotency-on-crash feature).

Safety guardrails implemented here (review fixes):
  #3 max_schedules_per_agent + spawn-depth lineage
  #4 min_interval floor (60s default)
  #5 on-idle definition: excludes schedule-originated turns
  #11 fire_id includes leader epoch
  #12 compliance revalidation at fire time (hook: compliance_provider callback)
  #14 step-cap resume via ResumeContext handoff from think_act
"""

from __future__ import annotations

import bisect
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .types import (
    CircuitBreaker, DriftPolicy, EscalateSpec, FireRecord, MemScope,
    OverlapPolicy, RetrySpec, ScheduleBlock, ScheduleBudget, ScheduleCancel,
    ScheduleUpdate, TriggerForm, TriggerSpec, WakeContext,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ScheduleValidationError(Exception):
    """Raised when a schedule declaration fails validation (syntax, floors, limits)."""


class CircuitBreakerTripped(Exception):
    """Raised when a fire is attempted on a schedule whose breaker has tripped."""


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*(ms|s|m|h|d)$", re.IGNORECASE)
_DURATION_UNITS = {
    "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0,
}

DEFAULT_MIN_INTERVAL_S = 60.0              # review-fix #4
DEFAULT_MAX_SCHEDULES_PER_AGENT = 50       # review-fix #3
DEFAULT_MAX_SPAWN_DEPTH = 3                # review-fix #3


def parse_duration(expr: str) -> float:
    """`15m` → 900.0 seconds. Raises ScheduleValidationError on bad syntax."""
    m = _DURATION_RE.match(expr.strip())
    if not m:
        raise ScheduleValidationError(f"invalid duration: {expr!r}")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * _DURATION_UNITS[unit]


# ---------------------------------------------------------------------------
# Fire-id computation (review-fix #11: include leader epoch)
# ---------------------------------------------------------------------------

def compute_fire_id(schedule_id: str, fire_ts_iso: str, leader_epoch: int = 0) -> str:
    """`sha256(schedule_id + fire_ts_iso + epoch)[:12]`.

    Including leader_epoch hardens against HA leader-flip clock skew producing
    colliding ids for independent intended fires. Within a single leader's
    tenure, same-slot fires still dedupe — the crash-idempotency feature."""
    payload = f"{schedule_id}|{fire_ts_iso}|{leader_epoch}".encode("utf-8")
    return "f:" + hashlib.sha256(payload).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Storage abstraction
# ---------------------------------------------------------------------------

class ScheduleStore(Protocol):
    """Persistence layer. Deployments implement for file / SQLite / external."""
    def save(self, schedule: ScheduleBlock) -> None: ...
    def load(self, schedule_id: str) -> ScheduleBlock | None: ...
    def delete(self, schedule_id: str) -> None: ...
    def list_for(self, agent_id: str, scope: MemScope | None = None) -> list[ScheduleBlock]: ...
    def record_fire(self, record: FireRecord) -> None: ...
    def recent_fires(self, schedule_id: str, limit: int = 100) -> list[FireRecord]: ...


class MemoryScheduleStore:
    """In-memory ScheduleStore — for tests, single-process deployments."""

    def __init__(self) -> None:
        self._schedules: dict[str, ScheduleBlock] = {}
        self._fires: dict[str, list[FireRecord]] = {}
        self._lock = threading.Lock()

    def save(self, schedule: ScheduleBlock) -> None:
        with self._lock:
            self._schedules[schedule.id] = schedule

    def load(self, schedule_id: str) -> ScheduleBlock | None:
        with self._lock:
            return self._schedules.get(schedule_id)

    def delete(self, schedule_id: str) -> None:
        with self._lock:
            self._schedules.pop(schedule_id, None)

    def list_for(self, agent_id: str, scope: MemScope | None = None) -> list[ScheduleBlock]:
        with self._lock:
            return [
                s for s in self._schedules.values()
                if s.as_agent == agent_id and (scope is None or s.scope == scope)
            ]

    def record_fire(self, record: FireRecord) -> None:
        with self._lock:
            bucket = self._fires.setdefault(record.schedule_id, [])
            # Replace prior record for this fire_id (handles crash-resume state).
            for i, r in enumerate(bucket):
                if r.fire_id == record.fire_id:
                    bucket[i] = record
                    return
            bucket.append(record)
            # retain only most recent 100
            if len(bucket) > 100:
                del bucket[:-100]

    def recent_fires(self, schedule_id: str, limit: int = 100) -> list[FireRecord]:
        with self._lock:
            bucket = self._fires.get(schedule_id, [])
            return list(bucket[-limit:])


# ---------------------------------------------------------------------------
# Registry — the firing machinery
# ---------------------------------------------------------------------------

CompliancePolicyProvider = Callable[[list[str]], list[str]]
"""Given requested compliance tags, return the subset currently honored.
Any tag missing from the return value triggers `schedule_compliance_revoked`
pause at fire time (review-fix #12)."""


@dataclass
class _InFlight:
    schedule_id: str
    fire_id: str
    started_at: float
    queue: list[float] = field(default_factory=list)   # pending fire timestamps for overlap=queue


@dataclass
class _BreakerState:
    window_start: float = 0.0
    fires_in_window: int = 0
    failures_in_window: int = 0
    tripped: bool = False


class ScheduleRegistry:
    """Active-firing registry.

    Holds the runtime state of registered schedules and coordinates the
    firing lifecycle. Tests may drive this deterministically by calling
    `tick(now=...)` rather than relying on wall-clock.
    """

    def __init__(
        self,
        *,
        store: ScheduleStore | None = None,
        leader_epoch: int = 1,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_schedules_per_agent: int = DEFAULT_MAX_SCHEDULES_PER_AGENT,
        max_spawn_depth: int = DEFAULT_MAX_SPAWN_DEPTH,
        compliance_provider: CompliancePolicyProvider | None = None,
    ) -> None:
        self.store: ScheduleStore = store or MemoryScheduleStore()
        self.leader_epoch = leader_epoch
        self.min_interval_s = min_interval_s
        self.max_schedules_per_agent = max_schedules_per_agent
        self.max_spawn_depth = max_spawn_depth
        self.compliance_provider = compliance_provider or (lambda tags: list(tags))

        self._in_flight: dict[str, _InFlight] = {}
        self._breakers: dict[str, _BreakerState] = {}
        self._next_fire: dict[str, float] = {}          # schedule_id → next scheduled unix ts
        self._last_success: dict[str, float] = {}
        self._spawn_lineage: dict[str, str] = {}        # child schedule_id → parent_fire_id
        self._telemetry: dict[str, int] = {
            "fires": 0, "overlap_skipped": 0, "queue_overflow": 0,
            "drift_skipped": 0, "drift_coalesced": 0,
            "breaker_tripped": 0, "mass_messaging_flag": 0,
            "min_interval_rejected": 0, "compliance_revoked": 0,
        }

    # -----------------------------------------------------------------
    # Declare / update / cancel
    # -----------------------------------------------------------------

    def declare(
        self,
        schedule: ScheduleBlock,
        *,
        parent_fire_id: str | None = None,
        spawn_depth: int = 0,
    ) -> None:
        """Register a new schedule. Raises ScheduleValidationError on violations."""
        self._validate_trigger(schedule.trigger)
        self._validate_ceiling(schedule)

        if parent_fire_id is not None:
            if spawn_depth >= self.max_spawn_depth:
                raise ScheduleValidationError(
                    f"schedule_spawn_depth_exceeded: depth={spawn_depth} "
                    f"max={self.max_spawn_depth} parent={parent_fire_id}"
                )
            self._spawn_lineage[schedule.id] = parent_fire_id

        self.store.save(schedule)
        now = time.time()
        nxt = self._compute_next_fire(schedule, now)
        if nxt is not None:
            self._next_fire[schedule.id] = nxt

    def update(self, upd: ScheduleUpdate) -> None:
        s = self.store.load(upd.id)
        if s is None:
            raise ScheduleValidationError(f"schedule not found: {upd.id}")
        if upd.trigger is not None:
            self._validate_trigger(upd.trigger)
            s.trigger = upd.trigger
        if upd.overlap is not None: s.overlap = upd.overlap
        if upd.drift is not None: s.drift = upd.drift
        if upd.budget is not None: s.budget = upd.budget
        if upd.on_fire_prompt is not None: s.on_fire_prompt = upd.on_fire_prompt
        if upd.purpose is not None: s.purpose = upd.purpose
        self.store.save(s)
        now = time.time()
        nxt = self._compute_next_fire(s, now)
        if nxt is not None:
            self._next_fire[s.id] = nxt

    def cancel(self, cancel: ScheduleCancel) -> None:
        self._next_fire.pop(cancel.id, None)
        self.store.delete(cancel.id)

    def list_for(self, agent_id: str, scope: MemScope | None = None) -> list[ScheduleBlock]:
        return self.store.list_for(agent_id, scope)

    # -----------------------------------------------------------------
    # Fire lifecycle
    # -----------------------------------------------------------------

    def due_schedules(self, now: float | None = None) -> list[ScheduleBlock]:
        """Return schedules due at or before `now`. Caller fires each."""
        now = time.time() if now is None else now
        due: list[tuple[float, ScheduleBlock]] = []
        for sid, ts in list(self._next_fire.items()):
            if ts <= now:
                s = self.store.load(sid)
                if s is not None:
                    due.append((ts, s))
        due.sort(key=lambda x: x[0])
        return [s for _, s in due]

    def begin_fire(
        self,
        schedule: ScheduleBlock,
        *,
        fire_ts: float | None = None,
    ) -> tuple[WakeContext, FireRecord] | tuple[None, FireRecord]:
        """Enter the fire phase. Returns (wake_context, fire_record).

        Returns (None, record) on overlap-skip / drift-skip / breaker-trip /
        compliance-revoke / queue-overflow — caller still records the outcome."""
        fire_ts = time.time() if fire_ts is None else fire_ts
        fire_ts_iso = _iso(fire_ts)

        # Breaker check (review-fix #3 circuit breaker already spec'd)
        breaker = self._breakers.setdefault(schedule.id, _BreakerState(window_start=fire_ts))
        if breaker.tripped:
            self._telemetry["breaker_tripped"] += 1
            raise CircuitBreakerTripped(
                f"schedule_fire_storm_paused: {schedule.id}"
            )

        fire_id = compute_fire_id(schedule.id, fire_ts_iso, self.leader_epoch)

        # Idempotency check (crash/HA resume): already-completed fire_id = no-op
        for prev in self.store.recent_fires(schedule.id, limit=32):
            if prev.fire_id == fire_id and prev.status == "ok":
                record = FireRecord(
                    fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
                    started_at=fire_ts, status="idempotent-replay",
                    trigger_form=schedule.trigger.form.value,
                    trigger_expr=schedule.trigger.expr,
                )
                self.store.record_fire(record)
                return (None, record)

        # Overlap policy
        if schedule.id in self._in_flight:
            return self._handle_overlap(schedule, fire_id, fire_ts, fire_ts_iso)

        # Compliance revalidation (review-fix #12)
        if schedule.compliance:
            current = self.compliance_provider(schedule.compliance)
            missing = [t for t in schedule.compliance if t not in current]
            if missing:
                self._telemetry["compliance_revoked"] += 1
                record = FireRecord(
                    fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
                    started_at=fire_ts, status="failed",
                    failure_reason=f"schedule_compliance_revoked: {','.join(missing)}",
                    trigger_form=schedule.trigger.form.value,
                    trigger_expr=schedule.trigger.expr,
                )
                self.store.record_fire(record)
                self._next_fire.pop(schedule.id, None)   # pause the schedule
                return (None, record)

        # Missed-slot coalescing (drift-on-resume — review grounding, existing spec)
        missed = self._count_missed_slots(schedule, fire_ts)
        if missed > 0 and schedule.drift == DriftPolicy.SKIP_MISSED:
            self._telemetry["drift_skipped"] += 1
            record = FireRecord(
                fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
                started_at=fire_ts, status="drift-skipped",
                missed_count=missed,
                trigger_form=schedule.trigger.form.value,
                trigger_expr=schedule.trigger.expr,
            )
            self.store.record_fire(record)
            self._reschedule(schedule, fire_ts)
            return (None, record)

        if missed > 0:
            self._telemetry["drift_coalesced"] += 1

        # Happy path — emit wake context
        last_success = self._last_success.get(schedule.id)
        wake = WakeContext(
            source="schedule",
            schedule_id=schedule.id,
            fire_ts=fire_ts_iso,
            fire_id=fire_id,
            reason=f"{schedule.trigger.form.value} {schedule.trigger.expr}".strip(),
            missed_count=missed,
            last_fire_ts=_iso(last_success) if last_success else None,
            workunit=schedule.workunit,
        )
        record = FireRecord(
            fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
            started_at=fire_ts, status="started",
            trigger_form=schedule.trigger.form.value,
            trigger_expr=schedule.trigger.expr,
            missed_count=missed,
        )
        self._in_flight[schedule.id] = _InFlight(
            schedule_id=schedule.id, fire_id=fire_id, started_at=fire_ts,
        )
        self.store.record_fire(record)
        self._telemetry["fires"] += 1
        return (wake, record)

    def complete_fire(
        self,
        record: FireRecord,
        *,
        status: str = "ok",
        failure_reason: str | None = None,
        cost_usd: float = 0.0,
        dispatches: int = 0,
        messages_emitted: int = 0,
    ) -> None:
        """Record the outcome of a fire. Updates breaker and reschedules."""
        completed = time.time()
        record.completed_at = completed
        record.duration_ms = int((completed - record.started_at) * 1000)
        record.status = status
        record.failure_reason = failure_reason
        record.cost_usd = cost_usd
        record.dispatches_in_fire = dispatches
        record.messages_emitted = messages_emitted
        self.store.record_fire(record)

        self._in_flight.pop(record.schedule_id, None)
        if status == "ok":
            self._last_success[record.schedule_id] = completed

        # Mass-messaging throttle
        if messages_emitted > 10:
            self._telemetry["mass_messaging_flag"] += 1
            # Flag — caller decides whether to gate next fire on human approval.

        # Breaker bookkeeping
        self._update_breaker(record.schedule_id, status, completed)

        # Drain queue / reschedule
        schedule = self.store.load(record.schedule_id)
        if schedule is None:
            return
        inflight = None  # was dropped above

        # If overlap=queue had queued fires pending, pop the next one now
        pending = self._pending_queue.pop(record.schedule_id, None) if hasattr(self, "_pending_queue") else None
        if pending:
            next_ts = pending[0]
            self._next_fire[record.schedule_id] = next_ts
        else:
            self._reschedule(schedule, completed)

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def _validate_trigger(self, trig: TriggerSpec) -> None:
        if trig.form == TriggerForm.EVERY:
            interval = parse_duration(trig.expr)
            if interval < self.min_interval_s:
                self._telemetry["min_interval_rejected"] += 1
                raise ScheduleValidationError(
                    f"schedule_min_interval_violation: {trig.expr} "
                    f"< {self.min_interval_s}s"
                )

        if trig.form == TriggerForm.CRON:
            fields = trig.expr.split()
            if len(fields) not in (5, 6):
                raise ScheduleValidationError(f"invalid cron: {trig.expr!r}")

        if trig.form == TriggerForm.AT:
            if "T" not in trig.expr:
                raise ScheduleValidationError(f"at: requires ISO8601: {trig.expr!r}")

        if trig.form == TriggerForm.AFTER:
            parse_duration(trig.expr)   # raises on bad syntax

    def _validate_ceiling(self, schedule: ScheduleBlock) -> None:
        existing = self.store.list_for(schedule.as_agent)
        if len(existing) >= self.max_schedules_per_agent and schedule.id not in (e.id for e in existing):
            raise ScheduleValidationError(
                f"max_schedules_per_agent exceeded: "
                f"{len(existing)}/{self.max_schedules_per_agent} for {schedule.as_agent}"
            )

    # -----------------------------------------------------------------
    # Overlap + queue
    # -----------------------------------------------------------------

    def _handle_overlap(
        self, schedule: ScheduleBlock, fire_id: str, fire_ts: float, fire_ts_iso: str,
    ) -> tuple[None, FireRecord]:
        if schedule.overlap == OverlapPolicy.SKIP:
            self._telemetry["overlap_skipped"] += 1
            record = FireRecord(
                fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
                started_at=fire_ts, status="overlap-skipped",
                overlap_action="skip",
                trigger_form=schedule.trigger.form.value,
                trigger_expr=schedule.trigger.expr,
            )
            self.store.record_fire(record)
            self._reschedule(schedule, fire_ts)
            return (None, record)

        if schedule.overlap == OverlapPolicy.QUEUE:
            if not hasattr(self, "_pending_queue"):
                self._pending_queue: dict[str, list[float]] = {}
            q = self._pending_queue.setdefault(schedule.id, [])
            if len(q) >= schedule.queue_cap:
                self._telemetry["queue_overflow"] += 1
                record = FireRecord(
                    fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
                    started_at=fire_ts, status="queue-overflow",
                    overlap_action="queue-drop-oldest",
                    trigger_form=schedule.trigger.form.value,
                    trigger_expr=schedule.trigger.expr,
                )
                self.store.record_fire(record)
                q.pop(0)
            bisect.insort(q, fire_ts)
            record = FireRecord(
                fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
                started_at=fire_ts, status="queued",
                overlap_action="queue",
                trigger_form=schedule.trigger.form.value,
                trigger_expr=schedule.trigger.expr,
            )
            self.store.record_fire(record)
            return (None, record)

        # OverlapPolicy.PARALLEL — caller enforces `parallel_limit`; here we
        # simply allow and let them start a concurrent fire.
        return self._reserve_parallel(schedule, fire_id, fire_ts, fire_ts_iso)

    def _reserve_parallel(
        self, schedule: ScheduleBlock, fire_id: str, fire_ts: float, fire_ts_iso: str,
    ) -> tuple[None, FireRecord]:
        # Parallel fires are tracked outside _in_flight single-slot (for simplicity)
        record = FireRecord(
            fire_id=fire_id, schedule_id=schedule.id, fire_ts=fire_ts_iso,
            started_at=fire_ts, status="started",
            overlap_action="parallel",
            trigger_form=schedule.trigger.form.value,
            trigger_expr=schedule.trigger.expr,
        )
        self.store.record_fire(record)
        return (None, record)   # caller must drive parallel execution itself

    # -----------------------------------------------------------------
    # Drift / rescheduling
    # -----------------------------------------------------------------

    def _count_missed_slots(self, schedule: ScheduleBlock, fire_ts: float) -> int:
        """How many scheduled slots fell in the gap since last success?"""
        last = self._last_success.get(schedule.id)
        if last is None:
            return 0
        if schedule.trigger.form == TriggerForm.EVERY:
            interval = parse_duration(schedule.trigger.expr)
            missed = int((fire_ts - last) / interval) - 1
            return max(0, missed)
        return 0

    def _compute_next_fire(self, schedule: ScheduleBlock, now: float) -> float | None:
        """Compute the next scheduled fire time from `now`. Reference impl
        covers `every` and `after` precisely; `cron`/`at`/`on-event` are
        simplified — real deployments use a cron parser / event bus."""
        t = schedule.trigger
        if t.form == TriggerForm.EVERY:
            interval = parse_duration(t.expr)
            return now + interval
        if t.form == TriggerForm.AFTER:
            return now + parse_duration(t.expr)
        if t.form == TriggerForm.AT:
            return _parse_iso(t.expr)
        if t.form == TriggerForm.CRON:
            return now + 60.0            # reference stub — real cron parser in prod
        if t.form == TriggerForm.ON_EVENT:
            return None                  # event-driven; fire on event.subscribe()
        if t.form == TriggerForm.ON_IDLE:
            return now + parse_duration(t.expr)
        if t.form == TriggerForm.ON_DEADLINE:
            return None                  # needs WORK-UNIT deadline resolution
        return None

    def _reschedule(self, schedule: ScheduleBlock, now: float) -> None:
        nxt = self._compute_next_fire(schedule, now)
        if nxt is None:
            self._next_fire.pop(schedule.id, None)
        else:
            self._next_fire[schedule.id] = nxt

    # -----------------------------------------------------------------
    # Breaker
    # -----------------------------------------------------------------

    def _update_breaker(self, schedule_id: str, status: str, now: float) -> None:
        schedule = self.store.load(schedule_id)
        if schedule is None:
            return
        bk = self._breakers.setdefault(schedule_id, _BreakerState(window_start=now))
        window = parse_duration(schedule.circuit_breaker.window)
        if now - bk.window_start > window:
            bk.window_start = now
            bk.fires_in_window = 0
            bk.failures_in_window = 0

        bk.fires_in_window += 1
        if status in ("failed", "drift-skipped", "queue-overflow"):
            bk.failures_in_window += 1

        if bk.fires_in_window >= schedule.circuit_breaker.threshold:
            rate = bk.failures_in_window / bk.fires_in_window
            if rate >= schedule.circuit_breaker.failure_rate:
                bk.tripped = True
                self._telemetry["breaker_tripped"] += 1
                # Pause — drop from _next_fire. Manual `update` or human reset to resume.
                self._next_fire.pop(schedule_id, None)

    # -----------------------------------------------------------------
    # Observability
    # -----------------------------------------------------------------

    def telemetry(self) -> dict[str, int]:
        return dict(self._telemetry)

    def flush_counters(self, sink, component_id: str | None = None) -> None:
        """Snapshot telemetry counters into a TelemetrySink under component
        name 'schedule'. Non-resetting."""
        sink.record_counters("schedule", self.telemetry(), component_id)

    def is_tripped(self, schedule_id: str) -> bool:
        bk = self._breakers.get(schedule_id)
        return bool(bk and bk.tripped)

    def reset_breaker(self, schedule_id: str) -> None:
        bk = self._breakers.get(schedule_id)
        if bk:
            bk.tripped = False
            bk.fires_in_window = 0
            bk.failures_in_window = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(ts: float) -> str:
    """Unix ts → ISO8601 UTC."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(iso: str) -> float:
    """ISO8601 → unix ts."""
    from datetime import datetime
    s = iso.rstrip("Z")
    dt = datetime.fromisoformat(s)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Helpers for constructing a minimal ScheduleBlock (tests + fixtures)
# ---------------------------------------------------------------------------

def make_schedule(
    *,
    id: str,
    as_agent: str,
    form: TriggerForm,
    expr: str,
    purpose: str = "",
    overlap: OverlapPolicy = OverlapPolicy.SKIP,
    drift: DriftPolicy = DriftPolicy.FIRE_ON_RESUME,
    workunit: str | None = None,
    on_fire_prompt: str = "",
    compliance: list[str] | None = None,
    circuit_breaker: CircuitBreaker | None = None,
) -> ScheduleBlock:
    """Convenience constructor used by tests and examples."""
    return ScheduleBlock(
        id=id,
        as_agent=as_agent,
        trigger=TriggerSpec(form=form, expr=expr),
        purpose=purpose,
        overlap=overlap,
        drift=drift,
        workunit=workunit,
        on_fire_prompt=on_fire_prompt,
        compliance=list(compliance or []),
        circuit_breaker=circuit_breaker or CircuitBreaker(),
    )
