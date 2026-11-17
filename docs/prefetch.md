# Offline prefetch planning and accounting (WP4)

Status: implemented in `src/ozagia/core/prefetch.py`, 2026-11-18, with
`tests/test_prefetch.py`. This is the WP4 slice of the M2a prefetch planner:
bounded candidate selection, explicit priority, safety checks, cancellation,
and a ledger for hits, unused work and overhead.

It is **offline and read-only**. The module has no file, graph, daemon,
network, model, test, install or checkout operation. Candidates are supplied as
values (or built from task/goal/profile/neighborhood mappings); a plan is a
value; prepare and use events are injected by a caller. It is not wired into a
live model turn, and it does not inject anything into a prompt.

## Plan

`build_prefetch_plan(candidates, scope=..., policy=..., now_ms=...)` returns
the `ozagia.prefetch_plan` v1.0 document:

```json
{
  "schema": {"namespace": "ozagia.prefetch_plan", "version": "1.0"},
  "status": "planned",
  "scope": {"task": "task:demo"},
  "scope_hash": "sha256:...",
  "policy": {"max_candidates": 64, "max_selected": 32,
             "max_concurrency": 4, "max_total_ms": 2000,
             "max_io_bytes": 8388608, "max_memory_bytes": 8388608,
             "max_payload_bytes": 65536},
  "now_ms": 0,
  "selected": [{"id": "...", "kind": "affected_tests", "priority": 10,
                "source_hash": "sha256:...", "wave": 0,
                "start_offset_ms": 0, "...": "..."}],
  "refused": [{"id": "...", "reason": "untrusted"}],
  "deferred": [{"id": "...", "reason": "time_budget"}],
  "estimated_ms": 250,
  "estimated_io_bytes": 16384,
  "estimated_memory_bytes": 0,
  "estimated_payload_bytes": 4096,
  "wave_count": 1,
  "cancelled_reason": null,
  "hash": "sha256:..."
}
```

`status` is `planned`, `empty` or `cancelled`. The plan is deterministic: the
same candidates and scope produce the same canonical bytes regardless of input
order or hash seed. `plan_json` returns canonical JSON and validates the
schema, required fields and content hash.

### Candidate

`PrefetchCandidate` carries a local `id`, one of the read-only `kind` values,
source/source identity, an explicit `priority` (lower first), the scope hash,
a source hash, expected saving, preparation cost, IO/memory/payload estimates,
authorization and trust flags, contract freshness, an optional expiry, parent
candidate IDs and a bounded redacted label.

Read-only kinds:

- `affected_tests` — a test plan, never a test execution;
- `environment_readiness` — recorded readiness, never a probe that installs;
- `graph_neighborhood` — graph context, never a graph refresh;
- `linked_repo_freshness` — an already-authorized linked checkout;
- `outline` — a caller-provided file outline;
- `source_metadata` — cached metadata, not a network fetch.

`model_call`, `test`, `install`, `network`, `write`, `edit`, `shell` and
checkout mutation are rejected at construction with
`prefetch.forbidden_kind`. `read_only=False` is also rejected. A plan does not
grant permission: both `authorized` and `trusted` must be true or the candidate
is refused.

### Selection rules

1. Candidates are validated and topologically ordered by declared parents;
   priority then expected saving then ID break ties. A cycle or unknown parent
   is an explicit error.
2. A candidate is refused when its scope hash differs, it is unauthorized or
   untrusted, its freshness is `stale` or `absent`, or it has expired at
   `now_ms`.
3. Otherwise candidates are selected in order until `max_selected`,
   `max_total_ms`, `max_io_bytes`, `max_memory_bytes` or
   `max_payload_bytes` would be exceeded. Refusals carry the reason
   (`scope_mismatch`, `unauthorized`, `untrusted`, `stale`, `absent`,
   `expired`, `parent_not_selected`); cap failures are `deferred`
   (`max_selected`, `time_budget`, `io_budget`, `memory_budget`,
   `byte_budget`).
4. A child is selected only after every parent. Waves place independent work
   concurrently up to `max_concurrency`; a child's wave is after its parent's
   wave. `estimated_ms` is the sum of the per-wave maximum preparation cost.
   It is an estimate, not an observation.
5. More candidates than `max_candidates` is an error, not a silent truncation.

`cancelled=True` returns a cancelled plan with no selected candidates and one
refusal per candidate. There is no speculative start in any mode.

### Bounds

| Bound | Default | Hard cap |
| --- | --- | --- |
| candidates | 64 | 1024 |
| selected | 32 | 256 |
| concurrency | 4 | 32 |
| preparation time | 2 s | 600 s |
| IO bytes | 8 MiB | 256 MiB |
| memory bytes | 8 MiB | 256 MiB |
| payload bytes | 64 KiB | 256 MiB |

## Candidate builders

These read only the mapping they are given; none opens a graph, store or file:

- `candidates_from_goal(goal, scope_hash=..., authorized=..., trusted=...)`
  turns test criteria into `affected_tests` and target/scope keys into
  `graph_neighborhood` candidates.
- `candidates_from_profile(profile, ...)` maps `check` spans to
  `affected_tests`, graph tool spans to `graph_neighborhood`, and
  index/model/tool spans to `source_metadata`.
- `candidates_from_graph_neighborhood(neighborhood, ...)` accepts an injected
  key → metadata mapping and never traverses a graph.

Authorization and trust default to **false** in every builder. A caller must
opt in explicitly, and the existing owner's permission remains authoritative.

## Ledger and accounting

`PrefetchLedger(plan, now_ms=...)` records injected preparation and use:

```python
ledger = PrefetchLedger(plan, now_ms=0)
ledger.record_prepared(
    "affected_tests:test:tests/test_a.py::test_a:...",
    prepared_at_ms=10, prepared_ms=120, payload_bytes=4096,
    value_hash="sha256:...", expires_at_ms=60_000)
ledger.use("affected_tests:...", at_ms=30,
           current_scope_hash=plan["scope_hash"],
           source_hash="sha256:...", freshness="current")
report = ledger.report()
```

`record_prepared` stores only metadata: preparation time, payload size, value
hash, expiry and an optional bounded error. It does not store the payload, a
prompt, a file body or a log.

`use` rechecks, in order: cancellation, current scope hash, prepared entry,
stored error, authorization, trust, current source hash, freshness and expiry.
The result has `status`, `served` and `current`:

| Situation | `status` | `served` | `current` |
| --- | --- | --- | --- |
| current hash/freshness, all checks pass | `hit` | true | true |
| freshness unknown | `hit_unknown` | true | **false** |
| source hash unknown | `hit_unknown` | true | **false** |
| scope not supplied | `hit_unknown` | true | **false** |
| already used | `already_used` | true | current only if current |
| stale / absent | `stale` / `absent` | false | false |
| source hash changed | `source_changed` | false | false |
| scope changed | `scope_changed` | false | false |
| unauthorized | `unauthorized` | false | false |
| untrusted | `untrusted` | false | false |
| expired | `expired` | false | false |
| prepared error | `error` | false | false |
| plan cancelled | `cancelled` | false | false |
| never prepared | `not_prepared` | false | false |

A cache hit is explicitly **not** proof of current state. `changed_scope`
cancels the ledger and discards prepared items. `cancel(reason)` does the same
without a new scope. A caller that omits `current_scope_hash` can still serve
an item, but the result is `current=False`; only a check against the plan's
scope hash can make it current.

`ledger.report()` is the `ozagia.prefetch_ledger` v1.0 report. It includes:

- `prepared`, `used`, `hits`, `served`, `unused`, `discarded`, `errors`;
- `wasted_ms` and `wasted_bytes` for prepared but unused items, including
  items discarded on a scope change;
- `overhead_ms` = all preparation time plus caller-recorded overhead;
- `estimated_saved_ms` = the expected saving of used items (an estimate);
- `net_estimated_ms` = estimated saving minus overhead;
- `rejected` counts by reason, including plan refusals and deferrals;
- bounded per-item state, `plan_hash`, `status` and a content `hash`.

The task report accepts this ledger as `TaskRecords.prefetch` and exposes its
hits, unused/wasted work and overhead in the `prefetch` section. A report with
no ledger says `state="not_recorded"` rather than zero waste.

## Safety boundaries

- No target-project import, test execution, install, network fetch, graph
  refresh, checkout mutation, daemon start or model call.
- No speculative model or tool start: planning is pure and preparation is an
  injected caller action.
- Existing authorization, trust, source identity, scope and freshness are
  rechecked at use time; a plan or cache entry grants nothing.
- Only bounded metadata and hashes are kept by the ledger; payloads stay with
  the caller's authorized context store.
- Errors and unknown evidence are reported, never converted into a hit.

## Tests

`tests/test_prefetch.py` covers deterministic priority and hashing across
`PYTHONHASHSEED` values, every forbidden kind, unauthorized/untrusted/stale/
absent/expired/scope-mismatched refusals, all caps, dependency waves and
cycles, cancellation, prepare/use accounting, unknown freshness, stale/scope/
cancel/error/untrusted/expired/source-change paths never being current, unused
waste and overhead, and the read-only builders.

## Not implemented here

- No live prefetch delivery into a model turn or automatic "ready at next turn"
  surface. M3 owns the model-facing delivery.
- No scheduler or background worker. A caller decides whether to run a plan.
- No payload cache, persistence, retention or eviction policy; the ledger is a
  value and a caller-owned cache remains authoritative.
- No measurement of actual saved wall-clock time. `estimated_saved_ms` is a
  caller-declared estimate; a later evaluation must compare prefetch-on and
  prefetch-off runs on matched tasks.
- No cross-scope cache reuse: a plan scope hash mismatch is refused.
