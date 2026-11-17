# Task preflight and evidence join (WP2)

Status: implemented in `src/ozagia/core/task_preflight.py` and
`src/ozagia/core/evidence.py`, with the read-only CLI
`ozagia task-preflight`, 2026-11-17. It is the M2 task preflight: a bounded,
read-only composition of existing owners. It does not run tests, refresh the
graph, start a daemon, call a model, import a target project or write a
goal/task/session/graph record.

## What it answers

For one goal and one or more selected checkouts:

- Are the goal criteria currently passing, failing or unknown?
- Is the graph snapshot current, stale, unknown or absent?
- Are the required capabilities declared and observed, with no audit error?
- Is there a trusted interpreter candidate?
- Does resource admission accept the task point-in-time?
- Is there a current affected-test plan, and was any test actually run?

The last point is deliberately two facts. A plan is not a result. The preflight
never turns a planned or selected test into a passed test.

## Inputs

| Input | Default | Rule |
| --- | --- | --- |
| goal | required | `g1` or `goal:g1`; a bare id is qualified with `goal:` |
| root | CLI: repository root | the primary checkout; must exist |
| checkouts | `[root]` | at most 8 distinct roots; optional `repo`, `checkout`, `snapshot` contract identities |
| scope | the goal's recorded scope | at most 32 bounded entries; `--scope` overrides the request field |
| changed | `None` (let the test planner read git) | bounded root-relative paths |
| base | `HEAD` | revision for changed-file discovery |
| required capabilities | `cap:goals.inspect`, `cap:test.scoped`, `cap:environment.interpreters` | canonical `cap:` ids |
| capability trust | unobserved | explicit `True`, `False` or `"unknown"` per required capability |
| timeout | 30 s; max 600 s | caller-owned; checked before each dimension; no provider is started after expiry |
| limit / cursor | 25; max 200 | paginates the criteria display; the assessment still considers every criterion it read |
| max facts | 256 | a hard bound on returned facts |

## Dimensions

Every fact is a `contracts.Fact`: subject, value, source, injected
`observed_at`, contract freshness (`current`, `stale`, `unknown`, `absent`)
and evidence kind (`declared`, `inferred`, `observed`, `verified`).
Per-dimension readiness is `true`, `false` or `"unknown"`.

| Dimension | Owner | `true` | `false` | `unknown` |
| --- | --- | --- | --- | --- |
| `goal` | `core.goals` | every criterion passed and is current | a criterion failed, or the goal is blocked/dropped | a criterion is unknown/not assessed, no criteria, or more criteria than the bound |
| `graph` | `core.memory.DevMemory.staleness` | snapshot root, HEAD and tree digest match | no snapshot | stale, unreadable, or verification errors |
| `capabilities` | `core.self_graph` + the WP1 catalogue | every required capability is declared and its surfaces observed | a required capability is undeclared, untrusted (`False`), or a declared surface is missing / has an audit error | a surface/audit state is unknown, trust is `"unknown"`, or an internal capability has no observable surface |
| `environment` | `core.environments.discover` | a non-project candidate exists and is executable; or a project candidate is trusted | no candidate | only untrusted project-shipped candidates |
| `resources` | `core.resources.check_admission` | admission `ok`, or checks disabled | admission refused | the host sample could not be read |
| `tests` | `core.testing.plan` | a plan is available and current (`executed=false`) | the policy refused to select tests | the graph is stale/unknown, or a fallback left affected coverage unassessed |

The `graph` dimension reads an existing snapshot. It does not call
`DevMemory.refresh`, `reingest` or `build_index`. The `goal` dimension
re-evaluates only the safe criterion kinds read-only (`test_passes`,
`test_fails`, `file_exists`, `file_absent`, `unchanged`, `symbol_exists`,
`symbol_absent`, `ops_has`, `review_resolved`). `calls`, `not_calls`,
`no_callers` and `lint_clean` would build a tag cache or symbol graph, so they
are reported from their recorded result as `declared` evidence with
`unknown` freshness. A stale or absent graph makes graph-backed criterion kinds
unknown; it never upgrades a recorded pass.

## Assessment rules

1. A dimension's provider claim is capped by its facts. A `true` claim with a
   stale, absent or unknown fact becomes `unknown`; only `false` is a definite
   negative. This is why a provider cannot fake green with a stale fact.
2. A dimension is `false` if any checkout is false; otherwise `true` if every
   checkout is true; otherwise `unknown`.
3. The overall status is:
   - `failed` if any required dimension is false;
   - `unknown` if no dimension is false but any is not true;
   - `green` only when **every** required dimension is `true` and every fact
     it used is `current`.
4. `assessment.green` is a boolean convenience; `assessment.reasons` names
   every non-true dimension and each checkout's reason.
5. `assessment.notes` states that a test plan is not an executed test and that
   the graph was not refreshed.

A missing interpreter, an absent graph, a failing/absent required capability
and a failing criterion are `false` (failed). A stale graph, an unknown
capability surface, an untrusted project interpreter, unknown criterion
evidence and unassessed affected coverage are `unknown`. Neither can be green.

## CLI

```text
ozagia task-preflight GOAL [--json] [--limit N] [--cursor C] [--root R]
                        [--checkout DIR ...] [--scope ENTRY ...]
                        [--base REV] [--timeout-s S]
```

The existing IaC command `ozagia preflight plan` is unchanged; this is a
separate top-level command. The command surface is declared as
`cap:preflight.task` in the WP1 capability catalogue.

- Exit `0`: assessment `green` (every required dimension current and true).
- Exit `1`: assessment `unknown` or `failed`.
- Exit `2`: invalid input or refusal, including an absent goal, an invalid
  goal id, an invalid capability id/trust entry, a bad cursor/limit or a
  duplicate checkout.

With `--json` the report is deterministic JSON (sorted keys, no floats). The
human form prints the assessment, each dimension and reason, the criteria
page, and the explicit unknowns. `--limit`/`--cursor` paginate only the
criteria display; the assessment is computed before truncation. A truncated
page carries the WP0 `{truncated, omitted, next_cursor, reason}` envelope.
The top-level `truncated` is the union of that page and any fact-display
omission; `criteria.truncated` is the page-specific envelope and
`bounds.facts_omitted` counts the fact omission.

## Report

```json
{
  "schema": {"namespace": "ozagia.task_preflight", "version": "1.0"},
  "report_version": "1.0",
  "goal": {"id": "goal:g1", "local_id": "g1", "status": "open", "...": "..."},
  "scope": [],
  "checkouts": [{"label": "checkout[0]", "root": "...",
                 "dimensions": {"graph": {"readiness": true,
                                          "reason": "...",
                                          "facts": [{"subject": "...",
                                                     "source": "...",
                                                     "freshness": "current",
                                                     "evidence": "observed"}]}}}],
  "dimensions": {"graph": {"readiness": true, "reason": "...",
                           "checkouts": ["..."]}},
  "criteria": {"total": 2, "counts": {"passed": 1, "failed": 1},
               "items": ["..."], "truncated": {"...": "..."}},
  "assessment": {"status": "failed", "green": false, "reasons": ["..."]},
  "not_assessed": ["..."],
  "deferred": ["..."],
  "bounds": {"...": "..."},
  "started_at": 1700000000000,
  "duration_ms": 12,
  "hash": "sha256:..."
}
```

Each criterion item carries its mapped result (`declared`, `pending`,
`passed`, `failed`, `unknown`, `invalid`, `not_assessed` — a goal store's
`pass`/`fail`/`stale`/`unknown`/`unchecked` is mapped into that vocabulary),
freshness, evidence, source, observed time, a redacted reason and, for test
criteria, the joined test record outcome and an explicit
`interpreter.state="not_recorded"`.

The report hash is `contracts.content_hash` of the report without its `hash`
field. `task_preflight.preflight_envelope(report)` wraps it in the versioned
service envelope with `side_effect="read"`.

## Evidence collector

`core.evidence.collect_evidence(root, goal_id, run_ids=..., test_keys=...,
sources=..., observed_at=...)` is the first-stage join:

- goal → criteria → recorded results;
- goal/test criterion ↔ working-set test record ↔ recorded outcome per test
  key;
- requested task runs → manifest metadata, artifact hashes, output digest and
  allowlisted evidence labels;
- interpreter/environment candidates.

It stores hashes and redacted references only:

- no goal title/why/objective free text;
- no task step summary, stop detail, event payload, command line or raw log;
- no task output value — only its canonical SHA-256 (or the manifest digest);
- no prompt or secret value;
- labels are bounded and passed through a best-effort secret-pattern redactor.

A requested run that is not on disk is `state="absent"` with a reason and
`freshness="absent"`; it is never dropped and never current. An unreadable
manifest is `invalid`; an incomplete run is `incomplete`. `coverage` reports
counts and `complete: false`, and `not_assessed` names what the collector did
not establish. The test record does not carry the interpreter that ran a test,
so the join reports `not_recorded` rather than guessing.

## Injection seams and tests

`task_preflight.Providers` is a frozen set of six callables
(`goal`, `graph`, `capabilities`, `environment`, `resources`, `tests`). Each
receives a `ProviderContext` with the checkout, deadline, goal, scope, changed
paths, required capabilities, trust map and injected observation time, and
returns an `Observation(readiness, facts, data, reason, error, truncated)`.
`default_providers()` returns the read-only defaults; individual providers are
also callable directly.

Tests use fakes for every assessment rule and a temporary git project for the
integration path (stale graph, missing interpreter, failing test, missing or
untrusted capability, unknown criteria). No test executes a target-project
test or probe. `PYTHONHASHSEED` subprocess runs prove deterministic output.

## Explicit unknowns

The report's `not_assessed` list includes, as applicable:

- interpreter importability and package availability were not probed;
- capability trust is only assessed when the caller supplies it;
- resource admission is a point-in-time host sample;
- the graph snapshot was not refreshed;
- no test was executed;
- the self-graph is a bounded static/runtime join, not an exhaustive inventory;
- a selected interpreter is a candidate, not proof that a test used it;
- the test record does not record the interpreter;
- only runs named by the caller are joined.

## Deferred durable writes

This package is read-only. The preflight does not write and does not call the
durable store. Durable writes now live in the additive
`src/ozagia/core/evidence_store.py` (see [evidence.md](evidence.md)): versioned
observation/assessment/declaration records, idempotency keys, bounded
retention, atomic compaction and crash/partial-record recovery. The report's
`deferred` list still names what this preflight itself does not do; the
collector's optional `evidence.attach_store(report, root)` join reads the store
without writing it.

## Limits

- Multiple checkouts are assessed independently; the aggregate is the worst
  readiness. `dimensions[*].data` is the primary checkout's data
  (`data_checkout` names it); every checkout's full data is under
  `checkouts`. Checkout identities are optional and stay `unknown` unless the
  caller supplies them; a path is not treated as an identity or permission.
- The default providers are synchronous. The deadline prevents starting new
  work but cannot interrupt a provider that is already blocked; the underlying
  owner's own timeout remains authoritative.
- Fact display is capped round-robin across dimensions; the bound never
  changes a dimension's readiness, and `bounds.facts_omitted` says what was
  omitted.
- A report over 192 KiB is trimmed deterministically and labelled
  `reason="bytes"`. Reports are kept below the service envelope's 192 KiB
  `data` bound so `preflight_envelope` never has to drop the report.
- The preflight does not prove a criterion's evidence is still current beyond
  the read-only evaluator's fingerprint checks; the evidence collector
  explicitly does not re-evaluate staleness.
