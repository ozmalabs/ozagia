# Task profiles and timing DAGs (WP3)

Status: offline primitives, 2026-11-17. This document describes
`src/ozagia/core/task_profile.py`, `src/ozagia/core/critical_path.py`,
`tests/test_task_profile.py` and `tests/test_critical_path.py`.

This is the WP3 slice of M2a: profile assembly, a fake-clock timing DAG,
critical paths, what-if scheduling and bounded report primitives over
**injected synthetic records**. It does not read a task, goal, daemon,
session or graph store, does not read a clock, does not call a model, does not
import a target project, does not start a daemon and does not write a file.
There is no CLI, MCP tool, daemon operation or prefetch planner in this change.
WP4 owns the adapters to real records and the user-facing report.

## Why two modules

`core/task_profile.py` owns the versioned `ozagia.task_profile` v1.0 instance:
span records, explicit parent edges, coverage, integrity, bounds and hashes.
`core/critical_path.py` owns the graph algorithms and the what-if scheduler.
The profile module imports the graph module; the graph module never imports a
task store or the profile module. Both are standard-library only.

The separation is deliberate:

- a profile is a durable-shaped record that can be regenerated and hashed;
- a critical-path or what-if report is an analysis over that record;
- a timing edge is a declared dependency, not an observation of order.

## Profile instance

A profile is an instance record in the frozen `core/contracts.py` sense:

```json
{
  "schema": {"namespace": "ozagia.task_profile", "version": "1.0"},
  "scope": {"task": "task:demo", "run": "run:demo"},
  "snapshot": "unknown",
  "inputs": [],
  "provenance": {"source": "injected", "assembler": "ozagia.core.task_profile"},
  "data": {"profile_version": "1.0", "clock": "injected", "time_origin": "unknown",
           "spans": [], "edges": [], "coverage": {}, "integrity": {}},
  "omissions": {"truncated": false, "omitted": 0,
                "next_cursor": null, "reason": null},
  "created_at": 0
}
```

`created_at` and `trace_id` are optional and are excluded from
`profile_hash`. The `x-` extension fields allowed by `contracts.py` are
included in the hash unless the caller uses `contracts.instance_hash`
directly with `transport_only`. `snapshot` defaults to the explicit
`"unknown"` placeholder; a caller with a real snapshot ID should pass it.
`scope`, `provenance` and `inputs` are caller-supplied and never inferred from
the records. The assembler default provenance says `source: "injected"`.

`profile_schema()` returns the normalized schema record and
`profile_schema_fingerprint()` returns its SHA-256 fingerprint. The namespace
and version are fixed; a different namespace, a different major version or a
different `data.profile_version` is rejected.

### Span fields

Every span has a closed set of fields. Optional fields are `None` in the API
and `"unknown"` in the report.

| Field | Meaning |
| --- | --- |
| `id` | Local profile span ID: lowercase ASCII, `[a-z0-9._:-]`, 1–128 chars, starts with a letter or digit. Compared exactly; no case folding, Unicode normalization or escaping. Not a self-graph `run:`/`goal:` identity. |
| `kind` | `model_call`, `tool_call`, `graph_prepare`, `index_prepare`, `queue_wait`, `readiness_wait`, `check`, `human_gate`, `prefetch`, `action`, `other`. |
| `parents` | Explicit finish-to-start parents. `[]` declares a root; `"unknown"` says dependency information was not supplied. |
| `start_ms`, `end_ms` | Injected monotonic milliseconds, or unknown. |
| `duration_ms` | Elapsed milliseconds, or unknown. Derivable from start/end only for `monotonic`/`injected` clocks. |
| `clock` | `system`, `monotonic`, `injected` or `unknown` from `contracts.CLOCKS`. |
| `outcome` | `completed`, `failed`, `cancelled`, `pending`, `running`, `skipped`, `unknown`. |
| `result` | A `contracts.RESULTS` value (`declared`, `pending`, `passed`, `failed`, `unknown`, `invalid`, `not_assessed`) or unknown. |
| `evidence` | A `contracts.EVIDENCE` value (`declared`, `inferred`, `observed`, `verified`) or unknown. |
| `measurement` | `measured`, `estimated` or `unknown`. Planned is not measured. |
| `attempt` | Positive attempt number, or unknown. |
| `resource_class` | `model`, `tool`, `cpu`, `io`, `graph`, `index`, `human`, `check` or `unknown`. |
| `queue_wait_ms` | Time waiting in a queue, when measured, or unknown. It is not assumed to be zero. |
| `readiness_wait_ms` | Time waiting for a resource or readiness gate, or unknown. |
| `input_tokens`, `cached_input_tokens`, `output_tokens` | Token evidence, or unknown. Cached input cannot exceed input. |
| `prefill_ms`, `generation_ms` | Measured model-call stage durations, or unknown. Their sum cannot exceed a known duration. |
| `cost_micros` | Integer micro-units of the caller's cost currency, or unknown. No float is accepted. |

A span is validated on construction. Unknown keys, duplicate parents, a
missing `id`/`kind`, negative values, an end before a start, a contradictory
duration and an oversized token cache are all explicit errors. `attempt` is
at least 1. A span's `parents` are sorted and deduplicated as a set; the
ordering carries no meaning.

### Explicit edges

`data.edges` is exactly the finish-to-start edge list derived from the spans'
`parents`:

```json
{"from": "root", "to": "child", "kind": "finish_to_start"}
```

The profile validator recomputes the edge list and rejects a profile whose
edges do not match the declared parents. It also rejects a parent that is not
present in the kept span set. `edges_from_spans(spans)` exposes the same
derivation for WP4 adapters. There is no API that reads `start_ms`/`end_ms`
and creates an edge from their order.

If a declared edge contradicts the timestamps, the edge is kept and the
contradiction is reported in `data.integrity.time_order_anomalies`:

```json
{"from": "a", "to": "b", "parent_end_ms": 200, "child_start_ms": 50}
```

Only same-clock `monotonic`/`injected` spans are compared. Wall-clock and
unknown-clock spans are never used to prove order or duration.

### Coverage and integrity

`data.coverage` is a recomputed summary, not caller-supplied truth:

- `complete`: no truncation, no unknown duration and no unknown dependency;
- `timing_complete`, `evidence_complete`: separate completeness axes;
- `truncated`, `spans_included`, `spans_total`, `edges`;
- `unknown_dependencies`, `unknown_duration`, `unknown_queue_wait`,
  `unknown_readiness_wait`, `unknown_tokens`, `unknown_cost`,
  `unknown_evidence`;
- `measured`, `estimated`, `unknown_measurement`;
- `by_kind`, `by_outcome`, `by_resource_class`.

`data.integrity` contains `unknown_dependencies`, `unknown_durations`,
`time_order_anomalies`, `terminal_with_descendants` and `scope_declared`.
A terminal span with downstream spans is reported rather than silently
treated as a successful dependency. A profile validator that finds a tampered
edge, coverage count, clock or integrity section raises an explicit
`task_profile.inconsistent_*` error.

## Clock rules

Clocks are injected:

- `system`: wall clock. It is acceptable for cross-process ordering, but a
  start/end difference is **not** accepted as a duration, because the clock
  can jump.
- `monotonic`: a monotonic elapsed-time clock. Start/end differences are
  valid durations.
- `injected`: a test or caller-supplied monotonic clock. Treated like
  `monotonic`.
- `unknown`: no duration is derived.

`FakeClock` is the helper for fixtures:

```python
clock = FakeClock(now_ms=0, step_ms=2)
clock.advance(5)   # 5
clock.tick()       # 7
```

It never reads the host clock, cannot move backwards and its `kind` is fixed
to `injected`. The profile's `clock` is the common span clock only when every
span has the same value; mixed clocks are reported as `unknown`. A profile
with no spans also has `clock: "unknown"`.

A duration can be supplied directly even when the clock is unknown; that is
caller evidence, not a derivation. When `duration_ms` is supplied together
with `start_ms`/`end_ms` on a monotonic or injected clock, the three must
agree.

## Bounds and truncation

Defaults and hard caps:

| Bound | Default | Hard cap |
| --- | --- | --- |
| spans | 512 | 4096 |
| edges | 2048 | 16384 |
| canonical bytes | 128 KiB | 1 MiB |

`build_profile(..., max_spans=, max_edges=, max_bytes=)` accepts callers
lowering or raising the default within the hard cap. More input spans than the
hard cap is an error, not a silently truncated input.

Truncation is deterministic and ancestor-closed. Spans are put in a
topological order with ties broken by span ID; the kept set is a prefix of
that order. Because every parent precedes its child, every kept span's
parents are kept. A span/edge limit truncates with reason `limit`. If the
canonical instance is still over `max_bytes`, the assembler finds the largest
byte-safe prefix and reports reason `bytes`. The `omissions` envelope always
has `truncated`, `omitted`, `next_cursor` and `reason`; `next_cursor` is the
last kept span ID (or absent when no span is kept). `max_spans=0` is a valid
empty view of a non-empty input.

The final instance is validated with `contracts.validate_instance`, so every
value is canonical JSON: no floats, tuples, sets, bytes or lone surrogates.

## Critical path

`critical_path(dag)` accepts a `Dag` of `DagNode`s and explicit `DagEdge`s.
`profile_dag(profile)` and `profile_critical_path(profile)` convert a
validated profile.

The algorithm is the standard longest path in a DAG with non-negative
durations, not a sum of parallel spans:

1. Topologically order the nodes deterministically (Kahn's algorithm, min-heap
   tie-break by node ID).
2. For each node in that order, `start = max(finish[parent])` (or 0 for a
   root) and `finish = start + duration`.
3. The critical path is the path ending at the node with the largest finish;
   it is reconstructed by following the parent with the largest finish,
   breaking ties by node ID.
4. `work_ms` is the sum of known durations (labelled a lower bound when a
   duration is unknown); `overlap_ms = work_ms - path_work_ms` shows work that
   is not on the selected path.

Two parallel 1000 ms spans have a 1000 ms critical path, not 2000 ms. Multiple
parents wait for the longest parent, not the sum.

### Unknown durations and dependencies

A missing duration is never zero:

- `duration_ms` is `"unknown"` if any node has an unknown duration or unknown
  dependency information;
- `lower_bound_ms` is the longest path with unknown durations contributing
  zero, explicitly labelled as a lower bound;
- `path_is_critical` is `false` for an incomplete report; `path` is the
  deterministic candidate path, not a claim that another unknown branch could
  not be longer;
- `unknown_duration_nodes`, `unknown_dependency_nodes` and `unknown_nodes`
  name the missing evidence.

`DagNode.parents_unknown=True` means the node's dependency information is
unknown, which is not the same as a root with no parents. Its start is unknown
even if its own duration is known.

### Cycles

A cycle is invalid input. `critical_path(dag)` raises `CycleError` with:

- `code == "critical_path.cycle"`;
- `cycles`: one deterministic representative cycle per cyclic strongly
  connected component;
- `components`: the cyclic SCC membership;
- `report()`: a canonical report with `status: "cycle"`,
  `run_status: "invalid"`, empty path and `duration_ms: "unknown"`.

`critical_path(dag, on_cycle="report")` returns that report instead of raising.
Self-loops and disconnected cycles are both detected. Cycle enumeration is
bounded to one representative per SCC, not an exponential list of all simple
cycles.

### Cancellation, failure and skipped work

Outcomes do not rewrite measured durations:

- `failed`, `cancelled` and `skipped` are terminal outcomes; they are listed
  in `terminal_nodes`;
- descendants reachable from a terminal node are listed in
  `blocked_descendants` and reported as `terminal_with_descendants`
  anomalies;
- `run_status` is `cancelled`, `failed`, `skipped`, `incomplete`,
  `completed` or `unknown`;
- `complete` is true only when the run is `completed` **and** the timing is
  complete. A cancelled run can still have a known measured duration and
  `status: "complete"` for timing, but never `run_status: "completed"` or
  `complete: true`.

`skipped` is terminal rather than a zero-duration success. Pending/running
spans make the run `incomplete`.

### Critical-path report fields

`schema`, `status`, `run_status`, `complete`, `path`, `duration_ms`,
`lower_bound_ms`, `path_is_critical`, `unknown_nodes`,
`unknown_duration_nodes`, `unknown_dependency_nodes`, `nodes`, `edges`,
`blocking`, `work_ms`, `work_is_lower_bound`, `overlap_ms`, `terminal_nodes`,
`blocked_descendants`, `anomalies`, `ties`; with
`include_schedule=True`, `starts` and `finishes` maps are added. Every map is
sorted and every value is canonical JSON. `report_json` and `report_hash`
provide canonical text and SHA-256.

## What-if model

`what_if(dag, profile, estimates=...)` re-schedules the same explicit DAG
under a caller-supplied estimate and resource profile. It never calls a model
and never treats a simulation as measured.

### Model-call estimate

`ModelCallEstimate` carries `input_tokens`, `cached_input_tokens`,
`output_tokens`, `prefill_rate`, `generation_rate`, `queue_wait_ms` and
`startup_ms`. Rates are exact tokens per second as `int`, `Fraction` or a
decimal string; floats are rejected because they are not exact and would make
the schedule non-deterministic.

For a model call:

```text
uncached = input_tokens - cached_input_tokens
prefill_ms = ceil(uncached * 1000 / prefill_rate)
generation_ms = ceil(output_tokens * 1000 / generation_rate)
duration_ms = startup_ms + queue_wait_ms + prefill_ms + generation_ms
```

A missing `cached_input_tokens` is recorded as an explicit assumption
`cached_input_tokens=0`, because no cache is the default; it is not silently
counted as a measured cache hit. A measured `node.duration_ms` is replaced by
the estimate in a what-if run, and that replacement is listed in
`assumptions`.

### Profiles and resources

`ScheduleProfile` carries a name, optional default rates, optional queue and
startup waits, an optional global `max_concurrency` and per-resource-class
concurrency limits. `ScheduleProfile.fast()` and
`ScheduleProfile.resource_limited()` are **assumed** starting points, not
provider measurements. They are labelled as assumptions in every report. The
WP4 or evaluation layer must validate a speed allowance per provider/profile;
a faster allowance is not a guarantee of the same output or execution path.

### Two different numbers

The report keeps the dependency-only lower bound and the simulated schedule
separate. A schedule is capped at 512 stages by default (`max_nodes` in
`what_if`); a caller that needs a larger schedule must raise that bound
explicitly rather than receive a silently partial one.

- `dependency_lower_bound_ms`: longest dependency path with the resolved
  durations and no resource limits. This is the same quantity as
  `critical_path`.
- `makespan_ms`: a deterministic greedy list schedule with the profile's
  global and per-class concurrency limits. It is a simulation, not an optimum.
- `resource_wait_total_ms` and `bottlenecks`: time spent ready but waiting for
  a slot, and the top resource-waiting stages.
- `critical_path`: the driving path after rescheduling; it can differ from
  `dependency_critical_path` when a resource constraint serialises another
  branch.
- `per_stage`: start, finish, duration, resource wait and driving-path marker
  for each scheduled stage.

Statuses:

- `complete`: every duration and dependency is known and no lower-bound
  allowance was used;
- `lower_bound`: the schedule is complete but a missing queue/startup wait was
  replaced by zero under `unknown_policy="lower_bound"`;
- `incomplete`: at least one node is unresolved (missing token/rate/duration,
  or blocked by an unresolved parent);
- `cycle`: a dependency cycle was returned in report mode.

`complete` is false for `lower_bound`, `incomplete` and `cycle`.

`confidence` summarizes the duration evidence actually used: `measured` when
every scheduled duration was measured, `modelled` when every duration came
from a token/rate estimate, `mixed` when both were used, and `unknown` when
the schedule is incomplete or empty. Ranges, batching and provider-specific
queueing are not modelled here; a caller can represent a queue observation as
`queue_wait_ms` and validate a rate profile later.

### Missing evidence

The default policy is `unknown`:

- missing `input_tokens`, `output_tokens`, prefill rate, generation rate,
  queue wait or startup wait leaves the duration unknown;
- an unknown duration blocks its descendants; independent branches can still
  be scheduled, and `lower_bound_ms` is the maximum finish of the scheduled
  work (a labelled lower bound);
- `unknown_policy="lower_bound"` may replace a missing queue or startup wait
  with zero and labels the result `lower_bound`; it does not replace missing
  tokens or rates, because zero cannot bound that work.

No missing value becomes a factual zero.

### Comparison

`compare_profiles(dag, fast=..., resource_limited=..., estimates=...)` runs
`what_if` twice and returns the two full reports plus a `summary` with the
makespans, the `resource_limited_delta_ms`, the two critical paths and whether
the path changed. The comparison is `complete`, `lower_bound` or `incomplete`
with the same rules. A delta is only an estimate under the declared
assumptions.

## Determinism

- Spans are topologically ordered with ID tie-breaks; truncation is a prefix
  of that order.
- Critical-path ties choose the smallest node ID.
- What-if readiness is ordered by start then ID; releases and active tasks
  are ordered; bottlenecks and assumptions are sorted.
- No map iteration order, set order, clock or random value enters a report.
- Reports use only integers and strings; there are no floats.
- Tests run the profile and schedule builders in subprocesses under
  `PYTHONHASHSEED=0` and `PYTHONHASHSEED=1` and compare the bytes.

## Errors

Errors are typed and stable for callers/tests:

- `task_profile.invalid_record`, `task_profile.invalid_field`,
  `task_profile.duplicate_id`, `task_profile.unknown_parent`,
  `task_profile.cycle`, `task_profile.inconsistent_edges`,
  `task_profile.inconsistent_data`, `task_profile.bounds_exceeded`,
  `task_profile.invalid_profile`;
- `critical_path.invalid_input`, `critical_path.invalid_node`,
  `critical_path.unknown_node`, `critical_path.cycle`,
  `critical_path.bounds_exceeded`.

These are module-local codes, distinct from the frozen contract and service
vocabularies in `core/contracts.py`. Canonical-form, schema and instance
violations still raise the contract errors (`contract.float_not_allowed`,
`contract.unknown_field`, `contract.unsupported_version`, and so on).

## Public API

`core/task_profile.py`:

- `SpanRecord`, `SpanRecord.from_mapping`, `SpanRecord.to_dict`;
- `edges_from_spans`;
- `FakeClock`;
- `build_profile(records, *, scope, snapshot=..., provenance=...,
  inputs=..., created_at=..., trace_id=..., time_origin=..., max_spans=...,
  max_edges=..., max_bytes=...)`;
- `validate_profile`, `profile_json`, `profile_hash`, `profile_dag`,
  `profile_critical_path`;
- `profile_schema`, `profile_schema_fingerprint`;
- `TaskProfileError`, `TaskProfileCycleError`, `TaskProfileBoundsError`.

`core/critical_path.py`:

- `Dag`, `DagNode`, `DagEdge`;
- `critical_path`, `topological_order`, `find_cycles`;
- `ModelCallEstimate`, `ScheduleProfile`, `DurationEstimate`,
  `resolve_duration_ms`;
- `what_if`, `compare_profiles`;
- `report_json`, `report_hash`;
- `CycleError`, `InvalidNodeError`, `UnknownNodeError`,
  `CriticalPathBoundsError`.

## What WP4 still has to wire

WP3 deliberately stops before the real system. WP4 must:

1. **Map real records to spans.** Join existing task, goal, daemon, session,
   harness, resource and evaluation records into stable run identities and
   spans. Preserve source IDs and schema versions; a missing record is
   `unknown`, not a fabricated event.
2. **Populate scope, snapshot, inputs and provenance.** Use the real task,
   goal, checkout and snapshot identities, with the existing owner stores as
   authority. Do not reinterpret unqualified graph keys.
3. **Classify clocks per source.** Use monotonic deltas where the source
   actually provides them; mark wall-clock-only cross-process ordering as
   `system` and do not derive durations from it.
4. **Respect redaction and permission boundaries.** No prompts, secrets,
   raw logs, tool inputs/outputs or unrestricted paths in a profile. Model
   names and token/cost counters are metadata, not content. Re-check scope
   before serving a retained report.
5. **Persist and regenerate.** Store profiles/reports under the state
   directory with atomic writes, bounded retention and cursors. A report must
   be derivable again from retained permitted records.
6. **Render and expose.** Add the read-only task report surface (CLI and/or
   task action) with bounded JSON and a readable rendering. WP3 provides no
   CLI, MCP tool or daemon operation.
7. **Handle partial, resumed, failed and cancelled runs.** Distinguish a
   repeated run from a resumed run; never merge runs with different scope or
   acceptance criteria. Preserve unknown criteria evidence.
8. **Add the prefetch planner.** Profile and critical path land first; WP4
   can then plan read-only prefetch, measure overlap/waste and report savings.
   Prefetch must use existing authorization, be cancellable and never execute
   a model, test, install or network fetch as speculation.
9. **Compare only compatible runs.** Same task, acceptance criteria,
   revision/snapshot, environment and measurement coverage; otherwise label
   the comparison incompatible rather than comparing apples to oranges.
10. **Validate speed profiles.** A fast/resource-limited profile is an
    assumption to test per provider/model; do not route work or change model
    selection because a simulation recommends it.

## Not implemented here

No live task/goal/daemon/session wiring, no persisted profile store, no CLI or
MCP surface, no daemon operation, no prefetch, no model call, no target
project import, no network call, no install, no snapshot rewrite, no report
rendering and no run comparison. The only inputs are caller-supplied records
and estimates; the only outputs are canonical in-memory values and hashes.
