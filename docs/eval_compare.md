# Matched-arm evaluation and comparison (WP8/M6, offline slice)

Status: implemented 2026-11-18 in `src/ozagia/eval/compare.py`, with
`tests/test_eval_compare.py`. This is the **offline** evaluation slice of
M6/WP8: it ingests retained run/result records and reports a matched-arm
comparison. It does not start a live driver, call a model or network, import a
target project, run a test, start a daemon or write a record.

The live-eval gate is unchanged: any live comparison still needs a person's
approval, `--live` and `OZAGIA_EVAL_LIVE=1` in the existing agent driver.
Thresholds for a live go/no-go decision are **not** set here.

## Arms

The comparison vocabulary is fixed:

| Arm | Meaning |
| --- | --- |
| `plain` | Plain baseline. |
| `prompted` | Well-prompted baseline with equivalent context. |
| `ozagia` | Existing Ozagia. |
| `ozagia_map` | Ozagia plus the map/service path. |

The adapter accepts the existing eval arm names through aliases:

```python
DEFAULT_ARM_ALIASES = {
    "baseline": "plain", "control": "plain", "no_graph": "plain",
    "plain": "plain",
    "baseline_context": "prompted", "context_baseline": "prompted",
    "prompted": "prompted", "well_prompted": "prompted",
    "dev_memory": "ozagia", "graph": "ozagia", "ozagia": "ozagia",
    "graph_intent": "ozagia_map", "map": "ozagia_map",
    "map_service": "ozagia_map", "ozagia_map": "ozagia_map",
    "ozagia_service": "ozagia_map", "service": "ozagia_map",
}
```

`graph_intent` maps to `ozagia_map` because it runs the graph-intent path, not
because the mapping is evidence that the map/service path was evaluated. An
unknown arm is an error; it is never silently dropped. A caller can override
aliases with `aliases={"name": "canonical-arm"}`.

## Record input

`build_comparison(records, ...)` accepts a list of explicit records, one record
object, a JSON text for either, or an existing eval report object with a
`runs` list.

### Explicit record

All scalar measures are integers. Unknown evidence is omitted, `null` or
`"unknown"`; it is never zero. The only fields required to identify a record
are `arm` and `task`; `repeat` defaults to 1 and `id` defaults to a deterministic
`<arm>:<task>:<repeat>` label.

```json
{
  "id": "run:demo:plain:1",
  "arm": "plain",
  "task": "task:demo",
  "repeat": 1,
  "acceptance": "sha256:<criteria digest>",
  "revision": "snapshot:<checkout>@<graph hash>",
  "model": "local/qwen",
  "permissions": "sha256:<tool permission digest>",
  "check": "sha256:<independent check digest>",
  "harness": "opencode",
  "prefetch_enabled": false,
  "check_result": "passed",
  "valid": true,
  "self_reported": "passed",
  "timing_ms": {
    "wall": 120000, "setup": 2000, "index_cold": 5000,
    "index_repeat": 1200, "model": 80000, "tool": 20000,
    "other": 8000, "queue_wait": 1000, "readiness_wait": 500,
    "prefetch": 3000, "check": 4000, "operator": 10000
  },
  "time_to_verified_ms": 120000,
  "counts": {
    "tool_calls": 17, "tokens_input": 42000, "tokens_cached": 12000,
    "tokens_output": 3000, "cost_micros": 12500,
    "parse_attempts": 4, "parse_failures": 0, "fallbacks": 1
  },
  "coverage": {"current": 40, "stale": 2, "unknown": 3, "absent": 0},
  "critical_path": {"predicted_ms": 90000, "observed_ms": 120000},
  "prefetch": {
    "prepared": 3, "hits": 2, "served": 2, "used": 1, "unused": 1,
    "discarded": 1, "errors": 0, "wasted_ms": 1200,
    "overhead_ms": 3000, "estimated_saved_ms": 9000
  },
  "source": "retained run record"
}
```

Accepted aliases in a record include `task_id`/`task_key` for `task`,
`run_id`/`record_id` for `id`, `success` (boolean) or `check_returncode` for
`check_result`, `tokens_in`/`tokens_out` for input/output tokens, `cost_usd`
for cost, and `prefetch_ledger` for `prefetch`. `timing` may be a legacy
timing block in **seconds** (`setup.s`, `index.cold_s`, `index.repeat_s`,
`model.s`, `tools.s`, `other.s`, `check.s`) or an explicit `timing_ms` block in
milliseconds. A legacy report's `time_to_verified_ms` is derived only when
`setup`, `index cold`, `wall` and `check` are all known; otherwise it stays
unknown.

`cost_usd` is converted to integer micro-units with a deterministic
`Decimal(str(value))` conversion and half-up rounding. The explicit schema
should supply `cost_micros`; the output never contains a float.

`self_reported` is recorded for inspection only; it is normalized to a bounded
label (`passed`, `failed`, `invalid`, `unknown`, `not_assessed` or `claimed`)
and free-form model text is never stored. It is never the acceptance result. A
run counts as a **verified success** only when `valid` is `true` and the
independent `check_result` is `passed`.

### Existing eval report

`records_from_report(report, defaults=..., aliases=...)` reads a report with a
`runs` list. The report's `base_commit`/`base` becomes the default revision,
`backend.model` the default model and `driver` the default harness. A run
record wins over those defaults.

A report does not normally state the acceptance-criteria digest, tool
permissions or check identity. Those dimensions therefore stay unknown and the
comparison refuses to aggregate unless the caller supplies them as defaults:

```python
build_comparison(
    report,
    defaults={"acceptance": "sha256:a", "permissions": "sha256:p",
              "check": "sha256:c"},
)
```

Supplying a default is an assertion by the caller. The module does not read a
task file to verify it.

## Compatibility rules

A comparison cell is `(task, repeat)`. Within a cell every compared arm must
agree on all of these **dimensions**, and every value must be known:

- `task`;
- `acceptance` (criteria digest);
- `revision` (repository revision or snapshot);
- `model`;
- `permissions` (tool-permission digest);
- `check` (independent check identity).

Across cells, `revision`, `model`, `permissions` and `check` must also be
homogeneous. `acceptance` is allowed to differ across different tasks because
each task has its own criteria.

The result is deliberately strict:

- any different value is a `mismatch`;
- any missing or `"unknown"` value is `unknown`;
- either makes the whole comparison `incompatible` and **no aggregate or
  delta is produced**;
- a cell missing one or more arms is unpaired, not a match;
- if no cell has every compared arm, the status is `unpaired` and there is no
  aggregate;
- if some cells are complete and others are not, the status is `partial` and
  the aggregate covers only the complete cells.

Statuses:

| Status | Meaning | Aggregate |
| --- | --- | --- |
| `empty` | No records. | refused |
| `incomplete` | Fewer than two arms, or a required arm is missing. | refused |
| `unpaired` | No cell has every compared arm. | refused |
| `incompatible` | A compatibility dimension mismatched or was unknown. | refused |
| `partial` | Compatible, but only some cells have every arm. | complete cells only |
| `compatible` | Every cell has every arm and all dimensions match. | all cells |

`compatible` is `true` only for `compatible` and `partial`; `partial` is not
presented as a complete matched set.

## Measures

The aggregate is per arm over the complete cells. It includes:

| Measure | Output |
| --- | --- |
| Task success and validity | `verified_successes`, `valid_runs`, `invalid_runs`, `unknown_validity_runs`, `decisive_runs`, `unknown_check_runs`, `invalid_check_runs`, `check_counts`, `success_rate_per_mille`. The success rate uses `decisive_runs` (valid and check passed/failed) as its denominator. |
| Time to verified outcome | `time_to_verified.total_ms`, `known_runs`, `unknown_runs`, `mean_ms`. Only verified successes with a known time contribute. |
| Verified tasks per hour | `verified_tasks_per_hour_milli`: `successes * 3_600_000_000 / summed_time_to_verified_ms`, rounded half-up. It is a throughput proxy over the included runs, not a cohort clock. |
| Wall-clock components | `wall_ms` and `timing_ms` for `wall`, `setup`, `index_cold`, `index_repeat`, `model`, `tool`, `other`, `queue_wait`, `readiness_wait`, `prefetch`, `check` and `operator`. Each has `total`, `known_runs` and `unknown_runs`. |
| Tool calls | `tool_calls.total`, `known_runs`, `unknown_runs`, `mean_milli` (mean × 1000). |
| Tokens | `tokens.input`, `tokens.cached`, `tokens.output`, `tokens.total`; cached input is not added to the total. |
| Cost | `cost_micros.total`, `known_runs`, `unknown_runs`. |
| Parse and fallback | `parse.attempts`, `parse.failures`, `parse.failure_per_mille`; `fallbacks.total`, `runs_with_fallbacks`, `run_per_mille`. |
| Stale/unknown coverage | `coverage.current/stale/unknown/absent`, `known_runs`, `unknown_runs`, `complete`, and one `*_per_mille` rate per state. If no included run supplied coverage, the four counts are `"unknown"` rather than zero. If only some runs supplied it, the counts are partial sums with `complete=false` and no rate. |
| Predicted vs observed critical path | `critical_path.predicted_ms`, `observed_ms`, `delta_ms`, `mean_absolute_error_ms`, `pairs`, `unknown_pairs`. Only runs with both values contribute. |
| Prefetch hit/use/waste/overhead | `prefetch.prepared/hits/served/used/unused/discarded/errors`, `wasted_ms`, `overhead_ms`, `estimated_saved_ms`, `net_estimated_ms` and `use_per_mille`. Each metric has a total and known/unknown run counts. `net_estimated_ms` is an **estimate**, not a measured saving. |

All values are integers or explicit strings. Ratios are integer per-mille
(thousandths); means are rounded half-up. A missing value is `"unknown"`, with
its own `unknown_runs` count where the measure is accumulated.

`deltas` compare each non-reference arm with the reference arm (default
`plain`). They cover success rate, mean time to verified outcome, verified
tasks per hour, cost, tool calls and total tokens. A delta is `"unknown"` when
either side is unknown.

`paired_time` compares the reference and candidate on the same complete cells,
using only cells where both runs were verified successes with known times. It
reports the pair count, the two totals, the delta and the candidate's
improvement per-mille. A positive improvement means the candidate was faster.

### Prefetch on/off

`aggregate.prefetch_pairing` compares runs of the same task and arm where
`prefetch_enabled` is explicitly `true` versus `false`. If both states have
verified successes with known times, it reports the mean for each state and
`on - off` delta. A negative delta means prefetch-on was faster. `faster_on`,
`faster_off`, `equal` and `unknown` count the pairs. Multiple repeats are
averaged; a pair with unknown time or incompatible dimensions is counted as
unknown, not as zero.

## Document

The result is an `ozagia.eval_comparison` v1.0 document. Top-level fields:

```text
schema, comparison_version, scope, status, compatible, reasons, arms,
required_arms, compatibility, cells, aggregate, runs, bounds, omissions,
not_assessed, thresholds, verdict, created_at (optional), hash
```

`compatibility` contains the rules, the per-cell dimension rows, the set-level
rows, the issues and their reasons. `runs` contains normalized per-run rows
with an `included` flag; rows for arms outside the compared set are retained
but not aggregated. Each run's result, timing, counts, coverage, critical path
and prefetch are normalized, with `"unknown"` for missing evidence.

`comparison_json(document)` is canonical JSON. `comparison_hash(document)` is
SHA-256 over the canonical document without `hash` and `created_at`.
`validate_comparison(document)` checks the namespace/version, required fields,
status, canonical form, hash, and that an incompatible/unpaired document has
no aggregate while a compatible/partial one does. `format_comparison(document)`
returns a bounded readable rendering.

## Bounds and truncation

| Bound | Default | Hard cap |
| --- | --- | --- |
| records | 512 | 4096 |
| canonical bytes | 128 KiB | 1 MiB |
| arms | 4 | 4 |

`max_records` lower than the input keeps a deterministic prefix sorted by
`(task, repeat, arm, id)` and reports `omissions.omitted_runs`, `reason="limit"`
and `next_cursor`. It does not aggregate a hidden tail. More than the hard cap
is an error, not a silent truncation.

If the document is still over `max_bytes`, detailed `runs`, then compatibility
cell rows, then `not_assessed` entries are dropped in that order and counted
in `omissions.omitted_details`, `omitted_compatibility_cells` and
`omitted_not_assessed`. The reason is `limit` when the record cap was also
applied, otherwise `bytes`; the counters always show which trim happened. The
aggregate is built before the detail trim and is retained. If even the minimal
document cannot fit in the requested budget, the builder raises
`compare.bounds_exceeded` rather than exceeding the bound. The smallest
accepted `max_bytes` is 1024, but a practical aggregate needs more; the default
is recommended.

## Thresholds and verdict

Thresholds are caller-supplied. With no thresholds the document has
`thresholds: null` and:

```json
{"status": "not_evaluated", "reason": "thresholds_unresolved", "checks": []}
```

No go/no-go numbers are built in, and none are suggested here. When supplied,
all of these keys are required:

- `reference_arm`;
- `candidate_arm`;
- `min_verified_runs`;
- `max_success_drop_per_mille`;
- `min_time_to_verified_improvement_per_mille`.

`max_prefetch_overhead_ms` is optional. The values are the caller's decision
for that task set; this module only evaluates them. The checks are:

- each arm has at least `min_verified_runs`;
- the candidate's success-rate drop is at most `max_success_drop_per_mille`;
- the candidate's paired time improvement is at least
  `min_time_to_verified_improvement_per_mille`;
- if supplied, candidate prefetch overhead is at most
  `max_prefetch_overhead_ms`.

The verdict is `pass` only when every check passes, `fail` when any check
fails, and `not_evaluated` when a required measure is unknown or the comparison
has no compatible aggregate. `thresholds` only records the caller's values;
the module never invents them.

## API

`ozagia.eval.compare` exports:

- `ARM_ORDER`, `DEFAULT_ARM_ALIASES`, `COMPATIBILITY_DIMENSIONS`,
  `SET_DIMENSIONS`, `TIMING_FIELDS`, `COUNT_FIELDS`, `COVERAGE_FIELDS`,
  `PREFETCH_FIELDS`, `CHECK_RESULTS`, `COMPARISON_STATUSES`;
- `ComparisonRecord`, `ComparisonError`, `records_from_report`,
  `build_comparison`, `validate_comparison`, `comparison_json`,
  `comparison_hash`, `comparison_schema`,
  `comparison_schema_fingerprint`, `format_comparison`;
- bounds and schema constants (`DEFAULT_MAX_RECORDS`, `HARD_MAX_RECORDS`,
  `DEFAULT_MAX_BYTES`, `HARD_MAX_BYTES`, `MAX_ARMS`,
  `COMPARISON_NAMESPACE`, `COMPARISON_VERSION`, `COMPARISON_SCHEMA`).

`ComparisonRecord` and `records_from_report` are the ingestion seam.
`build_comparison` is the pure builder.

## Errors

Stable module-local codes:

- `compare.invalid_input` — a bad container, scope, JSON text or bound value;
- `compare.invalid_record` — a malformed record field;
- `compare.unknown_arm` — an arm outside the vocabulary and aliases;
- `compare.duplicate_run` — two records for one `(task, repeat, arm)`;
- `compare.bounds_exceeded` — hard cap, or a byte budget the document cannot fit;
- `compare.invalid_comparison` — a malformed or tampered document;
- `compare.invalid_thresholds` — missing, unknown or unresolved threshold input.

## Tests

`tests/test_eval_compare.py` covers:

- four matched arms and the headline measures;
- deterministic canonical output across input order and `PYTHONHASHSEED`;
- model self-report never accepted as success;
- missing measures remaining `"unknown"` with unknown counts, not zero;
- unknown and mismatched compatibility refusing the aggregate;
- mixed models across tasks refusing the aggregate;
- duplicate, unknown-arm and bad-JSON errors;
- partial pairing and no-shared-cell refusal;
- prefetch on/off pairing, hit/use/waste and overhead;
- predicted vs observed critical paths;
- wall components with known/unknown counts;
- legacy eval-report ingestion and seconds/cost conversion;
- alias mapping (`baseline`/`dev_memory`/`graph_intent`);
- parse/fallback and stale/unknown coverage rates;
- unresolved and caller-supplied thresholds;
- record-limit and byte-bound truncation;
- hash validation, schema fingerprint and readable formatting.

No test invokes a model, driver, network, target project, install, daemon or
test execution.

## Not implemented / still unknown

- **No live runs, no live threshold, no approval.** This slice does not start
  a model or driver, does not set a go/no-go number and does not claim a
  performance or success benefit. The live-eval gate remains M6/WP8.
- **No task-file verification.** An `acceptance` digest is caller-supplied; the
  module does not recompute it from the task definition.
- **No independent check execution.** `check_result` is retained evidence; a
  caller must supply the real independent result. A `self_reported` value is
  never promoted.
- **No operator-preparedness measurement beyond `operator_ms`.** Missing
  operator time stays unknown.
- **No amortized index model.** Per-run `index_cold`/`index_repeat` components
  are reported, but the existing timing framework's amortization over N tasks
  is not recomputed here.
- **No cost-currency normalization.** `cost_micros` is integer micro-units of
  the caller's currency, as supplied.
- **No persistence, retention or web UI.** The document is a value; the caller
  stores or renders it. No `.ozagia/eval` write occurs.
- **No inference-profile what-if or routing.** The WP3 `critical_path.what_if`
  API remains separate; this module compares retained predicted/observed
  durations and does not reschedule work.
- **No statistical significance model.** Counts, totals and integer
  per-mille ratios are descriptive. A caller deciding a live threshold must
  account for repeats, task variation and confidence separately.
- **Prefetch pairing uses same task+arm**, not an explicit randomized
  prefetch experiment. A caller must ensure the on/off assignment is a fair
  comparison; this module only refuses missing or incompatible evidence.
