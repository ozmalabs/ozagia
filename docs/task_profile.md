# Task profiles, timing DAGs and post-run reports (WP3/WP4)

Status: WP3 offline primitives implemented 2026-11-17; the WP4 adapters, report
and CLI described below were added on 2026-11-18. This document describes
`src/ozagia/core/task_profile.py`, `src/ozagia/core/critical_path.py`,
`src/ozagia/core/prefetch.py`, `src/ozagia/tasks_cli.py`,
`tests/test_task_profile.py`, `tests/test_critical_path.py` and
`tests/test_prefetch.py`.

The WP3 slice is profile assembly, a fake-clock timing DAG, critical paths,
what-if scheduling and bounded report primitives over **injected synthetic
records**. It does not read a task, goal, daemon, session or graph store, does
not read a clock, does not call a model, does not import a target project, does
not start a daemon and does not write a file.

WP4 adds read-only adapters over retained task/goal/test/session/daemon/eval
records, a bounded regenerable `ozagia.task_report` v1.0 document with a
readable rendering, and the `ozagia-tasks report` CLI. It still does not run a
test, refresh a graph, start a daemon, call a model or write a record.

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
  durations and no resource limits. Unknown durations contribute zero to this
  numeric lower bound; `status` and `unresolved` still show that the schedule
  is incomplete. This matches `critical_path.lower_bound_ms`.
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

## WP4 adapters: retained records to spans

`TaskRecords` is the injected record set:

| Field | Source | Adapter |
| --- | --- | --- |
| `run` | `tasks.show_run` (or a bare manifest) | `adapt_task_run` |
| `goal` | a goal record | `adapt_goal_record` |
| `test_record` | `working_set.load_test_record` | `adapt_test_record` |
| `session` | a `DevSession` data mapping | `adapt_session_record` |
| `daemon` | daemon journal/response timing mappings | `adapt_daemon_evidence` |
| `evaluation` | an eval run record | `adapt_evaluation_record` |
| `prefetch` | a `ozagia.prefetch_ledger` report | passed through to the report |

`task_profile_spans(records)` combines them; `task_execution_spans(records)`
keeps only the run, daemon and evaluation work that has measured timing.

A task run with no manifest (a partial or interrupted run directory) becomes a
single unknown root span and a report with `coverage.run="incomplete"`; no
step, duration or outcome is fabricated.

Every span ID is produced by `source_span_id(source, *parts)`: a readable
sanitized prefix plus a short SHA-256 suffix. The original source ID and schema
version are kept in the report's `sources` section; they are not guessed from
the span ID. The readable part passes through the same redactor as free-text
labels, and the digest is not reversible. Facts that are absent from a record
stay absent, never zero.

### Source clocks and edges

| Source | Clock | Duration | Edges |
| --- | --- | --- | --- |
| task run | `system` for the ISO start/finish pair; the supplied `duration_s` is the run total | measured; missing stays unknown | run root is separate; steps are chained in the runner's declared sequential manifest order |
| task step | `monotonic` when `elapsed_s` exists (the runner measured it with `time.monotonic`) | measured or unknown | previous step → next step; no root-containment edge |
| goal | unknown | none | goal root → criterion |
| test record | `system` (epoch seconds) | none | a test key links to the deciding run only when the record names it |
| session | `system` (ISO wall clock) | none | only the session's explicit graph edges (`has_*`, `attempt`, `edit`, `verification`, `decision`, `next`) |
| daemon | `monotonic` when `ms`/`duration_ms` exists, else `system` timestamp | measured or unknown | none |
| evaluation | unknown clock; supplied timing components are measured durations | measured where present | each timing component is a separate root because components can overlap; no finish-to-start edge from timestamp or containment |

The runner's step order is a declared property of its loop, not an edge
inferred from elapsed timestamps. Two spans that overlap or finish in a
particular order are still independent unless a record declares a parent.

The full profile includes evidence spans with no measured duration, so its
critical path is often `unknown` with a labelled lower bound. The report also
computes `execution_critical_path` over the measured run/daemon/eval subset;
that is the observed execution path, not a claim about evidence spans.

## Post-run task report

`build_task_report(records, ...)` returns the bounded `ozagia.task_report`
v1.0 document. `build_task_report_from_root(root, run_id, ...)` first collects
retained records with `TaskSources` defaults; `collect_task_records` exposes
that read-only join separately.

The report carries:

- `outcome`: run status, criteria status, whether every criterion is current,
  per-criterion result/freshness/evidence/test join, and a `complete` flag that
  is true only for a completed run, every criterion current and passed, a
  non-truncated report, and a timing-complete execution profile. The full
  profile's evidence spans usually have unknown durations; `coverage` reports
  that separately as `evidence_timing`.
- `work`: requested task/goal/criteria, planned steps, executed/failed/skipped
  counts, changed files with hashes and sizes, and verified items;
- `selections`: recorded model(s), harness(es), tools and environment; no
  reason is invented when a record does not provide one;
- `snapshot`: the manifest preflight snapshot state mapped to contract
  freshness, with only allowlisted details;
- `failures`, `fallbacks`, `blocked`, `skipped`: stop, preflight, step and
  test-plan records;
- `measurements`: executor-reported token/cost/resource counters, per-step and
  evaluation stage timing, and explicit measurement state;
- `critical_path` and `execution_critical_path`;
- `prefetch`: hits, unused/wasted preparation and overhead from a supplied
  ledger, or `state="not_recorded"`;
- `sources`, `coverage`, `not_assessed`, `bounds`, `truncated` and `hash`.

`task_report_json` returns canonical JSON. `task_report_hash` is SHA-256 over
the report without `hash` and `created_at`, so a caller-injected build time does
not change the identity of the regenerated document. `validate_task_report`
checks the namespace, version, required fields, canonical form and hash.
`format_task_report` is a bounded readable rendering.

Bounds: 50 criteria, 100 changed files, 32 sources, 128 KiB by default (1 MiB
hard). The byte trim drops list items deterministically and changes
`truncated.reason` to `bytes`; a profile that was already truncated is marked
at the report level too.

### Redaction and regeneration

The report is assembled from an allowlist: labels, source IDs, hashes, counters
and short redacted reasons. It contains no prompts, tool inputs/outputs, file
contents or raw logs. A best-effort secret-pattern redactor runs over the few
free-text fields; tests include a secret canary and assert it never appears in
the JSON or the text rendering.

The same `TaskRecords` always produce the same report bytes, and
`build_task_report_from_root` rereads the same retained manifest, goal, test,
session, daemon and evaluation records. A report is therefore regenerable
without a persisted profile store.

## CLI

```text
ozagia-tasks report [RUN] [--root R] [--goal G] [--session S]
                    [--evaluation E] [--snapshot ID]
                    [--criterion-freshness ID=STATE ...] [--max-bytes N]
                    [--json]
```

`RUN` defaults to the newest recorded run. Exit `0` means the report outcome is
`passed`; exit `1` means the report was produced but the outcome is failed,
cancelled, incomplete or unknown; exit `2` means invalid input or a missing
run. `--json` prints the canonical report text; the default prints the readable
rendering. The command reads only existing records; it does not execute the
run, tests or a model. Existing `validate`, `run`, `list` and `show` behavior
is unchanged.

## WP4 public API (additive)

`core/task_profile.py` adds:

- `TaskRecords`, `TaskSources`, `TaskReportError`;
- `adapt_task_run`, `adapt_goal_record`, `adapt_test_record`,
  `adapt_session_record`, `adapt_daemon_evidence`,
  `adapt_evaluation_record`, `task_profile_spans`,
  `task_execution_spans`, `source_span_id`, `redact_task_text`;
- `build_task_profile_from_records`, `collect_task_records`,
  `build_task_profile_from_root`, `build_task_report`,
  `build_task_report_from_root`, `validate_task_report`,
  `task_report_json`, `task_report_hash`, `task_report_schema`,
  `task_report_schema_fingerprint`, `format_task_report`;
- `default_run_source`, `default_goal_source`, `default_test_record_source`,
  `default_session_source`, `default_daemon_source`,
  `default_evaluation_source`.

Existing WP3 names and behavior are unchanged.

## Remaining WP4 boundaries

- No persisted profile/report store, retention or cursor. The report is
  regenerated from retained owner records.
- No run-to-run comparison; compatibility rules for comparing two reports are
  not implemented.
- No live prefetch delivery into a model turn; `core/prefetch.py` plans and
  accounts only. See [prefetch](prefetch.md).
- No evaluation owner store: the default evaluation source reads
  `.ozagia/eval/<id>.json` if a caller names one, and otherwise the report says
  the evaluation was not recorded.
- Daemon records are joined only when a journal record names the run or
  session. The journal does not carry every harness action.
- Session timestamps are wall clock, so a session contributes no duration.
- The report does not re-evaluate goal criteria or prove a snapshot current;
  `criterion_freshness` is an explicit caller injection.
- Interpreter selection is not recorded by a task run or a test record; the
  report says so instead of guessing.
- What-if/alternate-profile scheduling remains the WP3 API; WP4 does not route
  work or change model selection.
