# Durable evidence store (WP2, M2)

Status: implemented in `src/ozagia/core/evidence_store.py`, 2026-11-18. It is
the additive write side of the WP2 evidence spine. The read-only collector in
`src/ozagia/core/evidence.py` and the read-only task preflight in
`src/ozagia/core/task_preflight.py` are unchanged: the collector still writes
nothing, and the preflight still does not call the store.

The store answers one question: *which goal, task run, test-record outcome and
interpreter candidate were recorded together, and how strong is that link?*
It does not decide that the work is correct and it never runs a test.

## Layout and ownership

```
<root>/.ozagia/evidence/
  records.jsonl   one canonical-JSON record per line
  .lock           fcntl.flock writer lock
```

(The state directory is `$OZAGIA_STATE_DIR` when set, as for the goal, task
and working-set stores; the default is `<root>/.ozagia/`.)

- Writes go only to the repository state directory (`config.state_path`), the
  same directory as `goals/goals.json`, `tasks/<run>/` and
  `working_set/tests.json`.
- The store never writes the goal, task, session, graph or working-set stores.
- `evidence_store.store_dir(root)` and `journal_path(root)` expose the paths.
- The journal file is created with mode `0600`; compaction uses a
  same-directory `mkstemp` temporary, `fsync` and `os.replace`, so a reader
  sees either the old or the new complete file.

## Record schema (version 1)

Namespace `ozagia.evidence.store`, `record_version: 1`. A record is a strict
allowlist; unknown fields, a different record version or a mismatched content
hash fail closed.

```json
{
  "schema": "ozagia.evidence.store",
  "record_version": 1,
  "record_id": "ev-<24 hex from the idempotency key>",
  "idempotency_key": "<caller key or SHA-256 of the body>",
  "kind": "observation | assessment | declaration",
  "source": "ozagia.core.evidence.collect_evidence",
  "observed_at": 1700000000000,
  "created_at": 1700000000000,
  "evidence": "declared | observed | verified",
  "freshness": "current | stale | unknown | absent",
  "complete": true,
  "reason": null,
  "subject": "observation:goal:goal:g1",
  "goal": {"id": "goal:g1", "status": "open", "state": "present"},
  "criteria": [{"id": "c1", "kind": "test_passes", "result": "failed",
                "freshness": "current", "evidence": "observed",
                "test_key": "test:tests/test_core.py::test_run",
                "recorded_status": "fail"}],
  "runs": [{"run_id": "run-1", "state": "present", "status": "failed",
            "digest": "<sha256>", "output_digest": "<sha256>"}],
  "tests": [{"key": "test:tests/test_core.py::test_run",
             "outcome": "failed", "recorded_at": 1700000000000,
             "run": {"complete": true, "filtered": false,
                     "source": "captured"}}],
  "environments": [{"path": "/repo/.venv/bin/python",
                    "source": "root_venv", "project": true,
                    "trusted": true, "exists": true,
                    "freshness": "current"}],
  "assessment": null,
  "declaration": null,
  "verification": {"state": "unverified",
                   "method": "recorded_test_goal_join",
                   "checks": [{"name": "criterion_c1_result",
                                "state": "failed"}],
                   "reasons": ["criterion_c1_result"]},
  "refs": {"report_hash": "<sha256>",
           "test_record_digest": "<sha256>"},
  "record_hash": "<sha256>"
}
```

`assessment` records replace the observation sections with:

```json
{"status": "failed | green | unknown", "green": false,
 "dimensions": {"goal": {"readiness": false, "reason": "..."},
                "graph": {"readiness": true, "reason": "..."},
                "capabilities": {"readiness": true, "reason": "..."},
                "environment": {"readiness": true,
                                "reason": "selected interpreter ..."},
                "resources": {"readiness": true, "reason": "..."},
                "tests": {"readiness": true, "reason": "..."}},
 "criteria": {"total": 1, "counts": {"failed": 1}},
 "reasons": [{"dimension": "goal", "readiness": false, "reason": "..."}],
 "environment": {"selected": "/repo/.venv/bin/python", "candidates": 1,
                 "readiness": true, "reason": "selected interpreter ..."}}
```

`declaration` records carry `{"statement": "<bounded, redacted text>"}` and
only neutral `goal`/`runs`/`tests` references (state/outcome `unknown`, no
digest). A declaration cannot carry criteria, environments or observations.

## Declared, observed and verified

| Word | Meaning in this store | May be `current`? |
| --- | --- | --- |
| `declared` | A caller/model claim such as "the tests pass" or a plan. No result is attached. | Never. Also `freshness="unknown"`. |
| `observed` | Read from an existing owner: goal store, task manifest, working-set test record, interpreter discovery or a host preflight report. It records what exists, not correctness. | Yes, when complete and fresh. |
| `verified` | The observer re-read the owners and the recorded joins agree: the test outcome matches the criterion direction, the test-record run is complete and unfiltered, the criterion is current, and a trusted existing interpreter was found. | Yes. |

`verified` never means the test was re-executed by this store. It also does not
prove the interpreter in the environment list is the one that ran the test; the
working-set test record does not record that. A plan, a model statement or a
green-looking outer status is never promoted: declarations are stored only as
`declared`, and a green assessment is refused unless every required preflight
dimension is `true` (see below).

For an observation, `record.evidence` is `verified` only when
`verification.state == "verified"`: the goal is present, every criterion is a
`test_passes`/`test_fails` criterion, each criterion result is `passed` and
current, each recorded test outcome matches the criterion direction, each
recorded test run is complete and unfiltered, and at least one existing
trusted interpreter candidate is present. A mixed goal or a failed join stays
`observed` with `verification.checks` naming exactly which check failed
(`criterion_c1_result`, `test_..._outcome`, and so on).

## Joins to existing records

- **Goal / criterion** — goal id, goal status/state, criterion id, kind,
  mapped result, recorded status, freshness and evidence. Free text
  (title, why, objective, reason) is not copied; only bounded/redacted fields
  are kept.
- **Task run** — run id, state, status, manifest digest and output digest.
  A missing run is stored as `state="absent"`; an unreadable manifest as
  `invalid`; an incomplete run as `incomplete`. None is upgraded.
- **Test record** — test key, outcome, recorded time and the recorded run's
  `complete`/`filtered`/`source`. The test record does not carry the
  interpreter or a task run id, so the collector states: "the interpreter link
  is unknown; a task run or test record does not name the interpreter it used."
- **Interpreter / environment** — path, discovery source, project-shipped and
  trust flags, existence and freshness. Discovery does not probe imports or
  run the interpreter.
- **Assessment** — goal, status/green, all six required preflight dimension
  readiness values, criteria counts, reasons and the selected interpreter from
  the primary checkout. A report hash is checked when present.

## Idempotency

- The default key is `contracts.content_hash` of the record body without the
  local `record_id`, `created_at`, `idempotency_key` and `record_hash`.
  Re-observing the same unchanged owners therefore produces the same key.
- A caller may supply an explicit key matching
  `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`. The same key with the same event
  returns the stored record (`state="duplicate"`) and appends nothing.
- The same key with different content is refused as `conflict`; the existing
  record is never overwritten.
- `created_at` is monotonic within the journal: a write with an older injected
  clock is bumped past the newest record. Event time belongs in `observed_at`.

## Bounds and retention

| Bound | Value |
| --- | --- |
| Record | 16 KiB canonical JSON; `budget_exceeded` above it |
| Journal | 8 MiB; a larger file is `invalid` and fails closed |
| Records | 500 |
| Age | 30 days (eviction only; `max_age_ms=0` disables age eviction) |
| Lock wait | 30 s, then `unavailable`; no unlocked write |
| Criteria / runs / tests / environments per record | 50 / 32 / 64 / 16 |
| Reasons / verification checks | 20 / 16 |

Appending compacts when any bound is exceeded: records older than the age
limit are dropped first, then the oldest records above the count limit. The
just-appended record is retained. `compact_store(root, max_records=...,
max_age_ms=..., now_ms=...)` performs the same eviction explicitly under the
lock. Eviction is deliberate data loss; the store does not claim to retain all
history.

## Crash, partial runs and fail-closed reads

- A crash during an append can leave a torn final line (invalid JSON, or valid
  JSON without the newline terminator). `read_store` reports
  `state="partial"`, lists the tear under `partial`, and never serves it as a
  record or as current. Earlier complete records are still available.
- The next `append_record` recovers the tail under the lock: a complete JSON
  tail without its newline is kept and re-framed; an incomplete tail is
  dropped. Recovery is atomic, and the append result says
  `recovered: true`.
- An explicitly incomplete record (`record_partial`, `complete=false`) is
  durable and inspectable but is listed only under `partial`, has
  `freshness="unknown"`, `current=false`, and never satisfies
  `latest_record(current_only=True)`.
- A newer partial record with the same subject also stops an older complete
  record with that subject from being `current`: the latest outcome is
  unknown, so the older record is not served as the latest.
- Malformed interior JSON, an unknown field, a wrong `record_version`, a bad
  content hash, a non-UTF-8 journal or an oversized journal make the whole
  store `invalid`/`unsupported`; reads return no records and appends refuse
  (`invalid_state`). Records are not silently skipped.
- An absent journal is `state="absent"`, not an empty success.

## Redaction and read scope

- The record schema is an allowlist. Prompts, goals' free text, task
  summaries, event messages, raw logs, command lines, output values and file
  contents are never accepted. Unknown input fields are rejected.
- Every stored string passes the collector's best-effort secret-pattern
  redactor (`evidence.redact_text`), and lengths are bounded. This is a second
  defence, not a guarantee against every novel secret shape.
- `read_store(root, limit=..., kind=..., goal=..., run=...,
  include_partial=False)` returns a bounded view:
  `state`, `path`, `observed_at`, newest-first `records` (with a view-only
  `current` flag), `partial`, counts, truncation and explicit `unknowns`.
- `latest_record(...)` defaults to current records only;
  `include_partial=True` is required to see an incomplete record, and it is
  returned with `current=false`.
- `attach_store(report, root)` filters to the report's goal id (when present)
  and returns the newest records up to `limit`; it does not widen scope.
- A store under another root/state directory is not read. There is no
  cross-repository or cross-checkout merge in this slice.

## API

Write:

```python
from ozagia.core import evidence_store as ES

ES.record_observation(root, "g1", run_ids=("run-1",), test_keys=(key,))
ES.record_assessment(root, preflight_report)
ES.record_declaration(root, source="model", statement="tests pass",
                      goal_id="g1")
ES.record_partial(root, reason="interrupted", goal_id="g1", run_id="run-1")
ES.append_record(root, record)
```

Read:

```python
view = ES.read_store(root, limit=10, goal="g1")
row = ES.latest_record(root, kind="assessment")
status = ES.store_summary(root)
```

Additive collector join (explicit; not the default):

```python
from ozagia.core import evidence as E
report = E.collect_evidence(root, "g1", run_ids=("run-1",))
joined = E.attach_store(report, root)
joined["durable"]  # state, records, partial, counts, unknowns
```

`collect_evidence` is unchanged and still never touches the store. The
report's `deferred` list still describes the collector; `attach_store` adds
`durable.writes = "separate store; this collector remains read-only"`.

## Assessment rules exercised by the store

- A green preflight record is accepted only when `assessment.green` is true,
  `status == "green"`, every one of `goal`, `graph`, `capabilities`,
  `environment`, `resources`, `tests` is ready `true`, and there are no
  non-true reason entries. A missing interpreter, stale graph or fabricated
  outer status is refused (`invalid_state`).
- A `failed` assessment is `current` evidence of failure.
- An `unknown` assessment has `freshness="unknown"` and is never `current`.
  Neither can be read as green.

## Explicit unknowns and deferred items

- The store records references; it does not re-run tests, refresh the graph,
  probe imports, install tools, start a daemon or call a model.
- The test record does not name the interpreter that ran a test, and a
  working-set test run is not linked to a task run id. Both links remain
  unknown unless the owners add them. The collector's `not_assessed` and
  `coverage.unknowns` carry the canonical sentence: "the interpreter link is
  unknown; a task run or test record does not name the interpreter it used."
  When a record does not name its interpreter, those links stay unknown.
- `verified` means the recorded joins agree, not that the interpreter is
  importable, the package set is complete or the result is reproducible.
- Only runs named by the observer are joined; unrelated runs are not scanned.
- `record_observation(..., sources=...)` is an injection seam for tests and
  host adapters. The store does not authenticate a custom `Sources`
  implementation; production callers must use the default read-only sources.
  The default sources read the owner stores and never execute target code.
- There is no cryptographic signing, authorization model beyond filesystem
  access, cross-repository merge, CLI/MCP surface, daemon integration or
  automatic recording from the task runner in this slice.
- Retention evicts old records; an `absent` record may mean "never recorded"
  or "evicted", and the view does not distinguish those causes.
- The default clock is local wall time in milliseconds; `created_at` is
  monotonic per journal, not a globally ordered event time.
- The journal is a single file; throughput is bounded by the lock and the
  scan/compaction cost. No benchmark has been run.
- `docs/task_preflight.md`, `docs/goals.md`, `docs/tasks.md` and
  `docs/session.md` keep their existing read-only contracts; this store is an
  additive companion, not a new format for those stores.

## Tests

```sh
PYTHONPATH=$PWD/src /home/matt/ozmadev/ozagia/.venv/bin/python -m pytest -q \
  tests/test_evidence.py tests/test_evidence_store.py
/home/matt/ozmadev/ozagia/.venv/bin/ruff check src tests
```
`tests/test_evidence_store.py` covers the goal → criterion → test → run →
environment join, a failing test traced through interpreter selection to a
failed goal assessment, stale-graph/missing-interpreter cases that cannot
become green, declaration vs evidence, the secret canary, repeated writes,
torn tails and recovery, explicit partial records, malformed/unsupported/
oversized journals, retention eviction and concurrent writers.
`tests/test_evidence.py` covers the opt-in collector join and the partial
store view.
