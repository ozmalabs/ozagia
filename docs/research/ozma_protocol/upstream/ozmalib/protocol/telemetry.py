"""
telemetry.py — Shim-call telemetry sinks.

The shim already computes rich per-call telemetry inside `intercept_response`
(level, ack_found, cap_mismatches, edit/plan/claim counts, reasoning choice,
reasoning-token usage) but returns it to the caller and drops it. Most
deployments don't catch it, so the signal is lost.

This module gives the shim an optional sink. When wired, every
`intercept_response` emits a ShimCallRecord to the sink. Query later for
ack regressions, reasoning-budget convergence, route-level comparisons.

Design mirrors StatsStore / SessionStore:

  - TelemetrySink Protocol — structural, so arbitrary sinks plug in
  - MemoryTelemetrySink — list-backed, for tests and dev
  - SQLiteTelemetrySink — persistent, default path ~/.ozma/shim_runs.db

Wiring is opt-in via OzmaShim(telemetry_sink=...). Default None = no-op.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable


@dataclass
class ShimCallRecord:
    """One intercept_request/intercept_response trace event.

    `phase` distinguishes:
      - "decision"  — emitted at intercept_request. Carries everything
                      knowable before the upstream replies: model, level,
                      reasoning choice. Outcome fields remain None.
      - "completed" — emitted at intercept_response. All fields populated.

    A call that times out or errors before intercept_response leaves only a
    decision row behind, which is how the sink surfaces failed calls.

    Timestamps are UTC ISO8601 strings (set by the sink on record if not
    supplied). Reasoning fields are None for models without a reasoning_knob.
    """
    session_id: str
    model_id: str
    level: str
    phase: str = "completed"        # "decision" | "completed"
    task_class: str | None = None
    reasoning_level_idx: int | None = None
    reasoning_value: str | None = None
    reasoning_tokens: int | None = None
    latency_ms: float | None = None
    ack_found: bool = False
    cap_mismatches: list[str] = field(default_factory=list)
    edit_count: int = 0
    plan_count: int = 0
    claim_count: int = 0
    memory_write_count: int = 0
    ts: str | None = None      # filled at record() time if None


@runtime_checkable
class TelemetrySink(Protocol):
    """Structural interface for telemetry sinks."""

    def record(self, call: ShimCallRecord) -> None: ...
    def query(
        self,
        model_id: str | None = None,
        task_class: str | None = None,
        phase: str | None = None,
        limit: int = 100,
    ) -> list[ShimCallRecord]: ...
    def record_counters(
        self,
        component: str,
        counters: dict[str, Any],
        component_id: str | None = None,
    ) -> None: ...
    def query_counters(
        self,
        component: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# MemoryTelemetrySink
# ---------------------------------------------------------------------------


class MemoryTelemetrySink:
    """In-memory sink. No persistence, for tests and short-lived processes."""

    def __init__(self) -> None:
        self._records: list[ShimCallRecord] = []
        self._counters: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, call: ShimCallRecord) -> None:
        if call.ts is None:
            call.ts = _now_iso()
        with self._lock:
            self._records.append(call)

    def query(
        self,
        model_id: str | None = None,
        task_class: str | None = None,
        phase: str | None = None,
        limit: int = 100,
    ) -> list[ShimCallRecord]:
        with self._lock:
            out: list[ShimCallRecord] = []
            # newest-first
            for r in reversed(self._records):
                if model_id is not None and r.model_id != model_id:
                    continue
                if task_class is not None and r.task_class != task_class:
                    continue
                if phase is not None and r.phase != phase:
                    continue
                out.append(r)
                if len(out) >= limit:
                    break
            return out

    def all(self) -> list[ShimCallRecord]:
        """Return every record (test helper)."""
        with self._lock:
            return list(self._records)

    def record_counters(
        self,
        component: str,
        counters: dict[str, Any],
        component_id: str | None = None,
    ) -> None:
        ts = _now_iso()
        with self._lock:
            for name, v in counters.items():
                self._counters.append({
                    "ts": ts,
                    "component": component,
                    "component_id": component_id,
                    "counter_name": name,
                    "value": float(v),
                })

    def query_counters(
        self,
        component: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for row in reversed(self._counters):
                if component is not None and row["component"] != component:
                    continue
                out.append(dict(row))
                if len(out) >= limit:
                    break
            return out


# ---------------------------------------------------------------------------
# SQLiteTelemetrySink
# ---------------------------------------------------------------------------


_DEFAULT_TELEMETRY_DB_PATH = Path.home() / ".ozma" / "shim_runs.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS shim_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    level               TEXT NOT NULL,
    phase               TEXT NOT NULL DEFAULT 'completed',
    task_class          TEXT,
    reasoning_level_idx INTEGER,
    reasoning_value     TEXT,
    reasoning_tokens    INTEGER,
    latency_ms          REAL,
    ack_found           INTEGER NOT NULL DEFAULT 0,
    cap_mismatches      TEXT,        -- JSON array, NULL if empty
    edit_count          INTEGER NOT NULL DEFAULT 0,
    plan_count          INTEGER NOT NULL DEFAULT 0,
    claim_count         INTEGER NOT NULL DEFAULT 0,
    memory_write_count  INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_INDEX_MODEL = (
    "CREATE INDEX IF NOT EXISTS ix_shim_runs_model ON shim_runs(model_id, ts)"
)
_CREATE_INDEX_TASK = (
    "CREATE INDEX IF NOT EXISTS ix_shim_runs_task ON shim_runs(task_class, ts)"
)
_CREATE_INDEX_SESSION_PHASE = (
    "CREATE INDEX IF NOT EXISTS ix_shim_runs_session_phase "
    "ON shim_runs(session_id, phase)"
)

_CREATE_COUNTERS_TABLE = """
CREATE TABLE IF NOT EXISTS shim_counters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    component    TEXT NOT NULL,         -- reasoning_persist|think_act|schedule|...
    component_id TEXT,                  -- optional instance qualifier (session/workunit)
    counter_name TEXT NOT NULL,
    value        REAL NOT NULL
)
"""

_CREATE_INDEX_COUNTERS = (
    "CREATE INDEX IF NOT EXISTS ix_shim_counters_component "
    "ON shim_counters(component, ts)"
)


class SQLiteTelemetrySink:
    """SQLite-backed telemetry sink. Sync-write on every record().

    Thread-safe via per-instance lock. Pass ":memory:" for ephemeral.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = str(path) if path is not None else str(_DEFAULT_TELEMETRY_DB_PATH)
        self._lock = threading.Lock()

        if self._path != ":memory:":
            os.makedirs(os.path.dirname(self._path), exist_ok=True)

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_CREATE_TABLE)
            self._conn.execute(_CREATE_INDEX_MODEL)
            self._conn.execute(_CREATE_INDEX_TASK)
            self._conn.execute(_CREATE_INDEX_SESSION_PHASE)
            self._conn.execute(_CREATE_COUNTERS_TABLE)
            self._conn.execute(_CREATE_INDEX_COUNTERS)
            self._conn.commit()

    def record(self, call: ShimCallRecord) -> None:
        ts = call.ts or _now_iso()
        caps_json = json.dumps(call.cap_mismatches) if call.cap_mismatches else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO shim_runs ("
                "ts, session_id, model_id, level, phase, task_class,"
                " reasoning_level_idx, reasoning_value, reasoning_tokens,"
                " latency_ms, ack_found, cap_mismatches,"
                " edit_count, plan_count, claim_count, memory_write_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, call.session_id, call.model_id, call.level, call.phase,
                    call.task_class,
                    call.reasoning_level_idx, call.reasoning_value,
                    call.reasoning_tokens,
                    call.latency_ms, int(call.ack_found), caps_json,
                    call.edit_count, call.plan_count, call.claim_count,
                    call.memory_write_count,
                ),
            )
            self._conn.commit()

    def record_counters(
        self,
        component: str,
        counters: dict[str, Any],
        component_id: str | None = None,
    ) -> None:
        """Snapshot an accumulator's counter dict into shim_counters."""
        ts = _now_iso()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO shim_counters "
                "(ts, component, component_id, counter_name, value) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (ts, component, component_id, name, float(v))
                    for name, v in counters.items()
                ],
            )
            self._conn.commit()

    def query(
        self,
        model_id: str | None = None,
        task_class: str | None = None,
        phase: str | None = None,
        limit: int = 100,
    ) -> list[ShimCallRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if model_id is not None:
            clauses.append("model_id = ?")
            params.append(model_id)
        if task_class is not None:
            clauses.append("task_class = ?")
            params.append(task_class)
        if phase is not None:
            clauses.append("phase = ?")
            params.append(phase)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM shim_runs" + where +
            " ORDER BY id DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def query_failed(self, limit: int = 100) -> list[ShimCallRecord]:
        """Sessions with a decision record but no completion — truncated/errored.

        A decision with no matching completed row for the same session means
        the upstream call didn't round-trip through intercept_response.
        """
        sql = (
            "SELECT d.* FROM shim_runs d "
            "WHERE d.phase = 'decision' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM shim_runs c "
            "  WHERE c.session_id = d.session_id AND c.phase = 'completed' "
            "  AND c.id > d.id"
            ") "
            "ORDER BY d.id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [_row_to_record(r) for r in rows]

    def query_counters(
        self,
        component: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent counter snapshots as flat dicts."""
        sql = "SELECT ts, component, component_id, counter_name, value FROM shim_counters"
        params: list[Any] = []
        if component is not None:
            sql += " WHERE component = ?"
            params.append(component)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _row_to_record(row: sqlite3.Row) -> ShimCallRecord:
    caps = json.loads(row["cap_mismatches"]) if row["cap_mismatches"] else []
    return ShimCallRecord(
        ts=row["ts"],
        session_id=row["session_id"],
        model_id=row["model_id"],
        level=row["level"],
        phase=row["phase"],
        task_class=row["task_class"],
        reasoning_level_idx=row["reasoning_level_idx"],
        reasoning_value=row["reasoning_value"],
        reasoning_tokens=row["reasoning_tokens"],
        latency_ms=row["latency_ms"],
        ack_found=bool(row["ack_found"]),
        cap_mismatches=caps,
        edit_count=row["edit_count"],
        plan_count=row["plan_count"],
        claim_count=row["claim_count"],
        memory_write_count=row["memory_write_count"],
    )
