# Intent-to-outcomes Stage 1: local verified outcome with one gate

Status: Stage 1 local slice complete 2026-11-27. This is an
integration/exit proof, not production readiness. Governing documents:
[product specification](../intent_to_outcomes_spec.md),
[staged delivery plan](2026-11-27_intent_to_outcomes_delivery.md),
[Stage 0 execution](2026-11-27_intent_to_outcomes_stage0.md), and the Stage 0
[inventory](../results/intent_to_outcomes_inventory_2026-11-27.md) /
[baseline](../results/intent_to_outcomes_stage0_baseline_2026-11-27.md).

## Scope

Prove one local, deterministic, verified outcome with one human gate and restart
recovery. Use the selected Dev Manager task shape (one-file docstring change,
`git diff --check`, Python parse, operator review) on a disposable local Git
repository. No OpenCode, Dev Manager cutover, Ocelot store adoption, network,
model calls or remote workers.

## Architecture decision

Graph-native workflow, with one new durable **outcome journal** as the canonical
record of intent revisions, work, attempts, gates, decisions, effects, evidence
references and events. The journal is not `DevMemory` (a rebuildable code index),
not `DevSession` (a receipt/presentation graph), and not a second DAG engine.
It follows the `research.graph` event-replay pattern and the `evidence_store`
idempotency, locking and torn-tail recovery semantics. The Stage 0 baseline
suggested reusing `DevSession` or a thin extension; the inventory shows that
`DevSession` is a lock-free receipt/presentation graph, so Stage 1 uses the new
journal as canonical state and may project into `DevSession` later. This keeps
one session state machine rather than turning the receipt graph into an
executor. `critical_path.Dag` may validate a process shape; `TaskRunner` remains
the bounded executor for later attempts.

## Stage 1 record store

- Location: `<state>/outcomes/` with an append-only journal and lock. One
  canonical journal for the pilot, or one journal per intent with a bounded
  index; the implementation may choose, but it must expose one logical store.
- Canonical JSON lines with schema version, record id, event type, intent id,
  correlation/causation ids, actor, timestamp, sequence, payload and content
  hash.
- Idempotency: same key + same payload is a no-op; same key + different payload
  is a typed conflict. Torn tail is truncated on recovery; malformed interior
  fails closed.
- Records required for the slice: Intent, IntentRevision, WorkItem, Attempt,
  Gate/Decision, EffectReceipt, EvidenceReference, Outcome, Event. Project links
  and grants may be fields in the pilot, not full Stage 2/3 records.
- State machines:
  - intent: accepted → running → waiting_decision → satisfied/partial/failed/
    cancelled;
  - attempt: queued → running → terminal, with `uncertain` for an unobserved
    effect;
  - effect: intended → applied/uncertain → reconciled/failed.
- `satisfied` requires recorded validation evidence and an approved gate bound
  to the current intent revision, action digest and expected base state.

## Operations

- `submit_intent(...)`, `revise_intent(...)`, `inspect_intent(...)`,
  `list_active(...)`, `follow_events(cursor, intent_id=None)`;
- `add_work(...)`, `start_attempt(...)`, `finish_attempt(...)`;
- `record_effect_intent(...)`, `record_effect_result(...)`,
  `reconcile_effect(...)`;
- `create_gate(...)`, `record_decision(...)`;
- `record_evidence(...)`, `set_outcome(...)`, `cancel_intent(...)`,
  `recover(...)`;
- `view(intent_id)` returning the versioned list-first view contract.

The library is the first surface. A small CLI may follow only if it does not
distract from the store and recovery tests.

## Effect and validation

- Apply the one-file change through `ozagia.edit.txn` with the expected base
  hash; record backup, base/new hashes and the effect key. A stale base refuses
  before writing.
- Validation is deterministic: `git diff --check` and a Python AST parse or
  `py_compile`. Evidence records command, exit code, source identity, artifact
  digest, method, coverage and freshness. A task/worker success response is not
  evidence.
- The gate binds the intent revision, diff/head hashes, action digest, target,
  expected base, policy version and evidence refs. A later revision or changed
  base invalidates it.
- A narrow pilot grant/epoch can be a record checked at effect time; it is not
  a full ACL. Its revocation refuses a new effect and leaves an in-flight
  uncertain effect for reconciliation.

## View contract

`ozagia.outcomes.view.v1` is a compact list-first projection: outcome and
criteria, verified/pending evidence, current work/attempt, blocker, pending
decision, available operations and evidence references. Unknown record types
render generically and are never dropped. Presentation state is not stored in
the canonical journal.

## Acceptance tests

1. Happy path: submit → work → effect → validation → gate pending → approve →
   satisfied.
2. Restart after effect, before gate: reload and resume at the pending gate;
   the effect is not applied twice.
3. Restart after decision: no re-ask and no duplicate delivery.
4. Stale base: change the target after intent but before write; the effect is
   refused and the failure is evidence.
5. Revision change: revision 2 stales a pending revision-1 gate/decision.
6. Revocation: a revoked pilot grant refuses a new effect.
7. Unknown type: the view renders a generic row.
8. No model, network, Ocelot, Dev Manager or OpenCode is used.

## Workstreams

- **S1-A (core store):** `src/ozagia/core/outcomes.py` (or a small package),
  typed records, journal, replay, idempotency/conflict/recovery, state
  transitions, intent/revision/work/attempt/event operations. Tests for store
  and lifecycle. No UI/effect adapter yet.
- **S1-B (effect, gate, evidence):** edit-transaction adapter, validation
  evidence, effect receipts/reconciliation, gate/decision binding, revision and
  revocation checks. Tests for stale base, replay, revision and revocation.
- **S1-C (view/CLI):** versioned view projection, list-first fields, generic
  unknown-type rendering, and a small CLI/library surface. Tests for
  graph/view agreement and restart.
- **S1-D (integration):** end-to-end disposable Git fixture and recovery tests;
  docs and capability entries.

S1-A lands first. S1-B and S1-C may run in parallel after the record API is
reviewed; S1-D follows.

## Out of scope

Real Dev Manager cutover, shared-server OpenCode capture, Ocelot
`substrate_rs` adoption, remote workers, mesh/Ozma inference, model-authored
progress, PR push/merge, project backends, expertise versioning, calendars,
messaging, telemetry and capacity provisioning.

## Delivered

- `src/ozagia/core/outcomes/`: journal store, typed records/state/replay,
  `effects.py`, `validation.py`, `gates.py`, `view.py` and typed errors.
- `src/ozagia/outcomes_cli.py` and top-level `ozagia outcomes` registration in
  `src/ozagia/cli.py`; capability entry in `src/ozagia/core/capabilities.py`.
- `tests/test_outcomes.py`, `tests/test_outcomes_effects.py`,
  `tests/test_outcomes_view.py` and `tests/test_outcomes_e2e.py`.
- `examples/outcomes_local.py`: self-contained temporary-Git exit example.
- `docs/outcomes.md`: effects/reconciliation, validation evidence, gates,
  finalization, view schema, CLI/example and Stage 1 limits.
- Minimal S1-C fix found by the integration test: between approval and
  finalization the view now reports a `finalize` blocker with
  `next_operation: "outcome.record"` instead of claiming no pending gate means
  another decision is needed.

## Verification

Run from the repository root on 2026-11-27:

```
.venv/bin/python -m pytest -q tests/test_outcomes_e2e.py tests/test_outcomes.py \
  tests/test_outcomes_effects.py tests/test_outcomes_view.py \
  tests/test_cli.py tests/test_capabilities.py
# 90 passed in 5.19s

.venv/bin/ruff check src/ozagia/core/outcomes/view.py \
  tests/test_outcomes_e2e.py examples/outcomes_local.py
# All checks passed!

.venv/bin/python examples/outcomes_local.py
# exit 0; printed ozagia.outcomes.view.v1 with status satisfied,
# outcome status satisfied and criteria 2/2 satisfied
```

The full repository suite passed on the Stage 1 code batch: 7,370 passed,
27 skipped. Ruff passed across source, tests, addins, evaluation, examples and
the docs generator.

The host has no bare `python` command, so the example was run with the
declared virtualenv interpreter. `tests/test_outcomes_e2e.py` covers the
disposable Git fixture, restart at the pending gate and after the decision,
stale-base refusal, revision invalidation of pending and approved gates,
revocation refusal, uncertain-effect reconciliation, read-only CLI JSON/text
and byte stability, generic unknown-type rendering, view/list agreement with
the journal, and confinement to the temporary repository/state.
