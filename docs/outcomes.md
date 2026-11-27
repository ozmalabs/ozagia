# Durable outcome journal

Status: Stage 1 complete for the local slice (S1-A store, S1-B
effects/validation/gates, S1-C view/CLI, S1-D integration). This documents the
library contract and the disposable-Git exit proof of
`ozagia.core.outcomes`. It is not the product specification; the governing
documents remain [`intent_to_outcomes_spec.md`](intent_to_outcomes_spec.md)
and the [Stage 1 plan](plans/2026-11-27_intent_to_outcomes_stage1.md).

The journal is the canonical, durable record of intent revisions, work,
attempts, gates, decisions, effect receipts, evidence references, outcomes and
events for the Stage 1 slice. It is deliberately separate from:

- `core/memory.py` / `graph.json`, which is a rebuildable code index;
- `core/session.py`, which is a presentation receipt graph;
- `core/evidence_store.py`, which owns goal/run/test evidence;
- `core/goals.py`, which owns criterion evaluation.

The journal itself never runs a task, edits a file, evaluates a gate or calls a
model. Effects go through `EffectAdapter` and `ozagia.edit.txn`; the only
target write is the requested one, and all other durable files stay under the
state directory.

## Location

`OutcomeStore(root)` uses `config.state_path(root, "outcomes")` by default, so
the store is `<state>/outcomes/` (`<root>/.ozagia/outcomes/` unless
`OZAGIA_STATE_DIR` or an explicit `state_dir=` overrides it).

```
<state>/outcomes/
  journal.jsonl   append-only canonical JSON lines
  .lock           fcntl lock shared by writers
```

`OutcomeStore(root, state_dir=...)` accepts an explicit directory. Reads do not
create the directory; the first successful append or `recover()` does.

## Journal envelope

Every line is the canonical JSON form of one record. The envelope schema is
`ozagia.outcomes.record.v1`:

| Field | Meaning |
| --- | --- |
| `schema` | `ozagia.outcomes.record.v1` |
| `seq` | Contiguous sequence starting at 1 |
| `record_id` | Deterministic `or-<seq>-<hash-prefix>` |
| `event` | Typed event, for example `intent.revised` |
| `intent_id` | Stable intent identity |
| `actor` | Bounded actor label |
| `timestamp` | Integer Unix milliseconds |
| `correlation_id` | Optional caller correlation reference |
| `causation_id` | Optional causal record reference |
| `payload` | Typed payload; includes its own `schema` and a `request_hash` |
| `idempotency_key` | Explicit caller key, or an `auto-` content key |
| `prev_hash` | Previous record's `record_hash`; `null` at sequence 1 |
| `record_hash` | SHA-256 of every field except `record_hash` |

`record_hash` uses `core.contracts.content_hash`, which hashes
`core.contracts.canonical_json`. The chain binds sequence, payload, actor,
timestamp, correlation/causation and the idempotency key. Replay rejects a
non-canonical line, a malformed interior line, a record-hash mismatch, a
sequence gap or a broken `prev_hash`. A line that is not terminated by `\n` is
a torn tail; it is reported but never served.

## Operations

The class is the primary API. Module-level functions with the same names except
`inspect_intent` construct a default store and delegate.

| Operation | Result |
| --- | --- |
| `submit_intent(requested_outcome, ...)` | Intent in `accepted`, revision 1 |
| `revise_intent(intent_id, requested_outcome, ...)` | New immutable revision; queued work staled |
| `add_work(intent_id, title, ...)` | `WorkItem` bound to the current revision |
| `queue_attempt(intent_id, work_id, ...)` | Attempt in `queued` |
| `start_attempt(intent_id, work_id, ...)` | Attempt in `running`; a newly created attempt starts in one record |
| `finish_attempt(intent_id, attempt_id, outcome, ...)` | Attempt in `terminal`; work follows |
| `transition_intent(intent_id, state, ...)` | Explicit intent transition |
| `transition_work(intent_id, work_id, state, ...)` | Explicit work transition |
| `cancel_intent(intent_id, ...)` | Intent in `cancelled` |
| `mark_queued_work_stale(intent_id, ...)` | Appends `work.stale`; never deletes |
| `append_event(intent_id, event_type, payload, ...)` | Generic event with a cursor |
| `record_gate(...)`, `record_decision(...)` | Gate and exact decision records (`gates.py`) |
| `record_evidence(...)` | Validation evidence reference (`validation.py`) |
| `record_effect(...)` | Effect receipt (`effects.py`) |
| `record_outcome(...)` | Outcome record (`gates.py`) |
| `follow(after_seq, intent_id=None, limit=...)` | Ordered, bounded cursor page |
| `inspect(intent_id, events_limit=...)` | Materialized intent, revisions, work, attempts, recent events and typed records |
| `list_active(limit=...)` | Bounded, deterministic active-intent rows |
| `recover()` | Truncate a torn tail and validate the complete journal |
| `materialize()` | Return the validated `OutcomeState` |

`follow` is exclusive of `after_seq`. It returns `records`, `cursor` /
`next_cursor`, `more`, `truncated` (limit cut), `gap` (a missing leading prefix,
when discovered), `tail_truncated` and `torn_bytes`. A malformed interior is
not a gap: it fails closed.

`inspect` returns the current intent summary, every immutable revision, work,
attempts, the most recent `events` (bounded by `events_limit`), `event_count`,
`events_truncated`, and the gate/decision/evidence/effect/outcome records.
`outcome` is the latest outcome bound to the current revision;
`latest_outcome` is the latest record for any revision.

`list_active` includes `accepted`, `running` and `waiting_decision` intents,
ordered by `last_seq` descending then `intent_id`, and reports `count`,
`total` and `truncated`.

## Lifecycle

### Intent

Allowed states:

```
accepted -> running | waiting_decision | satisfied | partial | failed | cancelled
running -> waiting_decision | satisfied | partial | failed | cancelled
waiting_decision -> running | satisfied | partial | failed | cancelled
satisfied | partial | failed | cancelled -> (terminal)
```

`start_attempt` moves `accepted` to `running`. `waiting_decision`, terminal
states and cancellation are explicit. A `satisfied` transition is refused unless
the store holds:

1. an `Outcome` for the **current** revision with status `satisfied` and at
   least one evidence reference; and
2. the outcome's `decision_ref` resolves to an `approved` `Decision` for the
   same intent and revision.

`record_outcome`, `record_gate` and `record_decision` write these records;
`finalize_outcome` records the outcome and then calls
`transition_intent(..., "satisfied", outcome_ref=...)`. The decision must be
recorded before the outcome that references it, because a decision reference is
an immutable journal record id. Gate binding, base/action drift checks and
evidence freshness are implemented by `gates.py` and `validation.py`.

### Revisions and steering

Submitting creates revision 1. Every `revise_intent` appends a new immutable
revision with:

- contiguous `revision`;
- `parent` = the previous revision hash;
- `revision_hash` = canonical hash of the requested outcome, criteria, context
  references, optional project, interpretation, principal, conversation, parent
  and reason;
- a new `request_hash` for idempotency.

Prior revision records are never rewritten. `revise_intent` marks every
`queued` WorkItem at an older revision as `stale` by appending a `work.stale`
record with a reason and the new revision reference. Running work and attempts
keep their original revision for audit; they are not deleted or silently
retargeted. `mark_queued_work_stale` exposes the same append-only helper
directly. A revision change does not by itself cancel a running attempt;
Stage 1 leaves it for the caller to finish or cancel.

### Work

States: `queued`, `running`, `waiting`, `completed`, `failed`, `cancelled`,
`stale` (`stale` is terminal for the item). A new WorkItem must bind the current
intent revision. Attempt outcomes map work as follows: `succeeded -> completed`,
`failed -> failed`, `cancelled -> cancelled`, `uncertain -> waiting`. Only
queued work is staled by steering; stale work cannot accept an attempt.

### Attempts

`Attempt.state` is exactly `queued`, `running` or `terminal`. Terminal attempts
carry one `outcome`: `succeeded`, `failed`, `cancelled` or `uncertain`. Records
carry `worker_ref`, `assignment_ref`, `baseline_ref`, `lease_generation` and
`lease_ref`.

- `queue_attempt` requires a claimable work item at the current revision.
- `start_attempt` requires a queued attempt or creates one in the same journal
  record (`previous_state: null`); the intent must be `accepted`/`running` and
  no other attempt may be running for the work item.
- `finish_attempt` requires a running attempt. Passing a `lease_generation`
  different from the running attempt's generation raises
  `OutcomeStaleError`; the payload always records the generation that finished.
  A completed effect is not implied by an attempt: effects and evidence are
  recorded separately.

## Effects and reconciliation

`EffectAdapter(store).apply(EffectRequest(...))` is the Stage 1 write path for
one target file. It calls `ozagia.edit.txn` (`read_source`, `write_source`), so
the base-hash check, backup and atomic replace are the edit transaction's, not
a text patch. A request names the intent, a running attempt, one target inside
the root, the expected base SHA-256, the exact new text, an optional effect key,
lease generation and pilot grant epoch. The action digest covers the target,
expected base and new-text hash.

An effect is an append-only sequence under one derived `effect_key`:

| State | Meaning |
| --- | --- |
| `intended` | Written before the target is read or touched |
| `applied` | Transaction returned; target digest equals the intended digest |
| `failed` | Refused before replace (stale base, unreadable target, encode/write refusal) |
| `uncertain` | The replace may have happened; explicit reconciliation is required |
| `reconciled` | Operator resolution of an `intended`/`uncertain` effect as `applied` or `failed` |

A stale base is refused before writing and records `intended` + `failed` with
the observed base. Retrying an applied or failed effect with the same key
returns the stored result (`duplicate: true`) without writing again. An
`intended` or `uncertain` effect is never retried: `apply` raises
`EffectUncertainError`; `reconcile(..., "applied"|"failed")` appends the
resolution and never writes the target. Reconciliation remains available after
revocation.

The pilot grant is a `grant.revoked` event with an epoch. An effect whose
`grant_epoch` is at or below a covering revocation is refused before an
`intended` record; a later epoch is not covered. This is not the Stage 2 ACL.

## Validation evidence

`validate_effect(store, effect)` is deterministic and local. It runs
`git --no-pager diff --check -- <target>`, parses the target with Python's
`ast`, and hashes the target; the evidence is `current` only if that digest
equals the effect's intended new digest.

The durable record is written to the existing evidence store
(`ozagia.core.evidence_store`) with kind `validation` first, then an
`EvidenceReference` is appended to the outcome journal. The evidence record
carries source `ozagia.outcomes.validation`, command and exit code, target,
method, coverage, checks, artifact digest, expected/observed base and the
evidence-store reference. `read_validation_record(root, record_id)` and
`validation_record_for_ref(root, ref)` resolve it.

A failed validation is recorded but cannot become gate evidence. A worker or
task success response is never validation evidence. Stage 1 coverage is
whitespace (`git diff --check`) plus syntax; it is not a sandbox and does not
run the target's tests.

## Gates and decisions

`create_gate(...)` binds the current intent revision, action digest, target,
expected base, policy version and evidence refs. A required gate needs all of
those plus at least one resolvable, current, passed validation record whose
artifact digest matches the target. It can hold the intent in
`waiting_decision`. `pending_gate` returns the newest current-revision pending
gate; `gate_status` derives `pending`, `approved`, `rejected`, `expired` or
`stale` from the journal and current target.

A gate becomes stale when the intent is revised, the target digest no longer
matches the validated artifact, evidence is missing or not current, the
decision fields do not match the gate, or the gate expires before a decision.
`decide_gate` / `approve_gate` / `reject_gate` require exact
revision/action/target/base/policy/evidence binding; approval re-checks the
validation evidence. A repeated identical decision returns `duplicate: true`;
a different decision for the same gate is refused. Decisions are immutable
records and are written before any outcome that references them.

## Finalization

`finalize_outcome(store, intent_id, status="satisfied", gate_id=...,
decision_ref=...)` records an `Outcome` and applies the matching intent
transition. `satisfied` requires an approved current-revision gate, the exact
decision reference, the gate's evidence refs, current passed validation and the
current target digest. A repeat with the same approved decision returns
`duplicate: true` and adds no records. `waiting_decision` requires a pending
gate; `partial` and `failed` record the explicit status and transition.
Satisfaction is never inferred from an attempt's success response.

## Idempotency

Every write accepts `idempotency_key`. If present and already stored:

- the same request fingerprint is a no-op that returns the stored record with
  `duplicate: true`;
- a different request fingerprint raises `OutcomeConflictError`.

The fingerprint is the canonical hash of the event, intent and caller request
fields (`request_hash` inside the payload). It excludes generated record
metadata, sequence, timestamp and the resulting revision number, so retrying
`revise_intent` with the same key and same steering request is a safe no-op.

Most writes are also idempotent without an explicit key when the request and
ids are identical, because the default `auto-` key hashes the final record
payload. Callers should still supply an explicit key when retrying an
operation that generates ids (for example a bare `revise_intent` or `add_work`
without a supplied work id).

## Recovery and errors

Writes take a process thread lock plus `fcntl.flock` on `.lock`, append under
the lock and `fsync`. A write failure can leave a torn final line;
`recover()` or the next write truncates only the bytes after the final complete
newline, then revalidates the whole journal. A malformed complete line,
non-canonical JSON, hash mismatch or broken chain fails closed. No interior
record is silently skipped.

Multi-record operations (a steering revision plus its `work.stale` records) are
appended in one write, but a process death can still retain a complete prefix
of that write. Recovery validates that prefix; an operator or a later recovery
path should run `mark_queued_work_stale` when a revision is present but its
stale records are not. A retry with the same idempotency key returns the
already-stored revision as a duplicate rather than appending a second one.

Error hierarchy (`OutcomeError` base, stable `code`):

| Class | Code | Use |
| --- | --- | --- |
| `OutcomeValidationError` | `outcomes.invalid_request` | Missing or wrongly typed field |
| `OutcomeBoundsError` | `outcomes.bounds` | Field/list/payload exceeds a bound |
| `OutcomeConflictError` | `outcomes.conflict` | Idempotency or natural-key conflict |
| `OutcomeTransitionError` | `outcomes.invalid_state` | Disallowed lifecycle transition |
| `OutcomeStaleError` | `outcomes.stale` | Stale revision, expected state or lease |
| `OutcomeNotFoundError` | `outcomes.not_found` | Unknown intent/work/attempt/record |
| `OutcomeIntegrityError` | `outcomes.corrupt` | Damaged, non-canonical or broken chain |
| `OutcomeUnsupportedError` | `outcomes.unsupported` | Unknown schema/event |
| `OutcomeLockError` / `OutcomeStoreError` | `outcomes.unavailable` | Lock or filesystem failure |

Bounds include 32 criteria, 32 context refs, 64 evidence refs, 16 KiB canonical
payload, 32 KiB record line, 4,000-character requested outcome, 128-character
ids, 1,000 follow records, 500 inspected events and 500 active intents.

## Record schemas

Append these through the store so every record keeps the same envelope, chain,
lock and idempotency behavior.

| Schema | Producer |
| --- | --- |
| `ozagia.outcomes.gate.v1` | `record_gate` (`gates.py`) |
| `ozagia.outcomes.decision.v1` | `record_decision` (`gates.py`) |
| `ozagia.outcomes.effect_receipt.v1` | `record_effect` (`effects.py`) |
| `ozagia.outcomes.evidence_reference.v1` | `record_evidence` (`validation.py`) |
| `ozagia.outcomes.outcome.v1` | `record_outcome` (`gates.py`) |

## View

`ozagia.outcomes.view.v1` is a read-only projection; it is not stored in the
canonical journal. A detail view (`kind: intent`) carries intent and current
revision, status, the current and latest outcome, criteria rows,
verified/pending evidence, current work and attempt, pending decision, blocker,
operations, gates/decisions/effects/outcomes, event records, `other`,
references, the last sequence/cursor, a journal summary and bounded-event
omission information. A list view (`kind: list`) has bounded rows ordered by
last sequence descending then intent id; terminal intents stay visible so a
verified outcome does not disappear. Neither view appends, recovers, truncates
or creates state.

Known event, record and operation types render typed rows. Unknown event types,
unfamiliar payload schemas, unknown record types, unknown operations and extra
`inspect` collections render as `display_type: "other"` rows that keep their
data; an unknown operation is disabled and never inferred. Journal replay fails
closed on an unknown **event**, so forward-compatible rows are injected at the
reader boundary in tests rather than written as an unsupported event.

After approval but before finalization, the view reports a `finalize` blocker
with `next_operation: "outcome.record"`; it does not ask for the same decision
again.

## CLI and example

`ozagia outcomes` is read-only and registered on the top-level CLI:

```sh
ozagia outcomes show INTENT --root R --json
ozagia outcomes list --root R --json
ozagia outcomes events INTENT --after 0 --root R --json
```

Every command accepts `--root`, `--state-dir` and `--json`; text is the
default. The standalone module (`python -m ozagia.outcomes_cli`) has the same
commands. They never append, recover or create the store. Lifecycle operations
remain library calls in Stage 1; a write-capable CLI is deferred until the
effect/gate policy and capability boundary are reviewed.

A runnable example creates its own temporary Git fixture, runs the same manual
flow (submit → work → attempt → effect → validation → pending gate →
restart/replay → approval → restart → satisfaction), prints the final
`ozagia.outcomes.view.v1` and exits non-zero on a failed expectation:

```sh
python examples/outcomes_local.py
```

It writes only under its temp directory and uses no model, network, daemon or
caller checkout.

## Stage 1 limits

- One local canonical journal; no sharding, remote store or multi-writer
  service.
- One-file effects through `edit/txn`; Stage 1 validation supports only Python
  `.py`/`.pyi` targets. No multi-file, non-Python, repository-wide, branch or PR
  delivery.
- Deterministic `git diff --check` + AST validation only; no test suite,
  sandbox, model review or retrieval-time artifact authorization.
- A pilot grant epoch, not a full grant/ACL/worker-identity model; no lease
  renewal or worker claim protocol.
- One human gate/decision per exact action; no approval UI, standing policy,
  consultation deadlines or delegation.
- No DAG runner, parallel work, budgets, retries/compensation, telemetry, mesh,
  remote workers or Dev Manager/OpenCode/Ocelot cutover.
- The outcome journal is separate from the evidence store, code graph and
  `DevSession`; there is no projection into those stores yet.
- `recover()` repairs only a torn tail. A complete prefix of a multi-record
  write (revision plus stale-work records, gate plus hold transition, outcome
  plus transition) is valid journal history and must be completed by an
  explicit retry; Stage 1 does not roll forward automatically.
- Read-only CLI and library only; no write-capable CLI and no production
  readiness claim.

## Checks

```
.venv/bin/python -m pytest -q tests/test_outcomes_e2e.py tests/test_outcomes.py \
  tests/test_outcomes_effects.py tests/test_outcomes_view.py \
  tests/test_cli.py tests/test_capabilities.py
.venv/bin/ruff check src/ozagia/core/outcomes tests/test_outcomes_e2e.py \
  examples/outcomes_local.py
.venv/bin/python examples/outcomes_local.py
```
