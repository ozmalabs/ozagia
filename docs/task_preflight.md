# Task preflight and evidence join (WP2)

Status: implemented in `src/ozagia/core/task_preflight.py` and
`src/ozagia/core/evidence.py`, with the read-only CLI
`ozagia task-preflight`, 2026-11-17. The optional read-only `linked`,
`research` and `actions` joins were added 2026-11-18. It is the M2 task
preflight: a bounded, read-only composition of existing owners. It does not run
tests, refresh the graph, start a daemon, call a model, import a target project
or write a goal/task/session/graph record.

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
| linked checkouts | not selected | at most 8 explicit `{root, label?, repo?, checkout?, snapshot?, authorized?, read_scope?}` entries; a path is not an identity or permission |
| research sessions | not selected | at most 8 retained `.ozagia/research/<id>.json` ids; an explicit empty list selects none |
| action readiness | not selected | at most 16 explicit `{action, name?}` service/tunnel operations; `action` must be a declared `service.*`/`tunnel.*` action; an explicit empty list selects none |
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

`linked`, `research` and `actions` are optional, primary-scope dimensions.
They are present only when the caller selects `linked_checkouts`,
`research_sessions` or `action_readiness`; without those inputs the report
keeps exactly the six base dimensions, the same schema/version and the same
assessment. When selected, an unknown, absent, stale or unauthorized result is
a required dimension and cannot yield `green`.

| Dimension | Owner | `true` | `false` | `unknown` |
| --- | --- | --- | --- | --- |
| `linked` | `core.memory` snapshot read + `core.federation.reference_status` cache | every selected member is explicitly authorized, has a declared read scope, is in its own `.ozagia` state, and its snapshot is fresh | the caller marked a selected member unauthorized | a member is absent, stale, has no snapshot, lacks authorization/scope, or reference-sync state is not verified |
| `research` | `core.research_projection` over retained research graphs | a selected session is readable and asserts no claim, assumption or evidence | a selected session is unreadable and the error is an authorization failure | the session is absent/invalid, or any recorded claim/assumption/evidence exists (they are declarations, not verified evidence) |
| `actions` | `core.action_readiness` over `actions.ACTION_SPECS` | every selected service/tunnel operation is `available` and current | a selected service or tunnel is `absent` | a selected operation is `blocked` or `unknown`, or no operation was selected |

The `linked` dimension reads an existing member snapshot through
`DevMemory.staleness()` and the anchor's cached federation reference status.
It never calls `federation.discover`/`sync_references`, never fetches, never
refreshes a member graph and never imports or executes a linked project.
`research` reads retained graph JSON and selected local source files; it never
fetches a URL, calls a model/OCR/parser or writes a research record. Ocelot is
not connected: the report carries `research.engines.ocelot = {"state":
"unknown", "reason": "..."}`, and no Ocelot module is imported or executed.

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
   The required set is the six base dimensions plus `linked`/`research`/
   `actions` when the caller selected them. None of the optional dimensions can
   be omitted from the assessment to make a selected member, claim or action
   look green.
4. `assessment.green` is a boolean convenience; `assessment.reasons` names
   every non-true dimension and each checkout's reason.
5. `assessment.notes` states that a test plan is not an executed test and that
   the graph was not refreshed.
6. The report also carries a bounded top-level `rules` list with the same
   assessment rules in canonical words:
   - "green requires every required dimension is true and every fact it used
     is current";
   - "a plan is not a result; a test plan is not an executed test";
   - "a stale graph or missing interpreter cannot be green";
   - "the preflight does not refresh the graph".
   `rules` is descriptive only: it is not an input, does not change a
   dimension or the assessment, and is covered by the report hash.

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
`cap:preflight.task` in the WP1 capability catalogue. The CLI does not expose
the `linked_checkouts`/`research_sessions` API inputs in this slice; they are
available to Python callers (and to a later reviewable surface) without adding
a new write or a new network action.

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
  "rules": [
    "green requires every required dimension is true and every fact it used is current",
    "a plan is not a result; a test plan is not an executed test",
    "a stale graph or missing interpreter cannot be green",
    "the preflight does not refresh the graph"
  ],
  "not_assessed": ["..."],
  "deferred": ["..."],
  "linked": {"schema": {"...": "..."}, "readiness": "unknown",
             "members": [{"label": "linked[0]", "identity": {"...": "..."},
                          "authorization": {"...": "..."},
                          "reference": {"...": "..."},
                          "index": {"...": "..."}}],
             "counts": {"...": "..."}, "reference": {"...": "..."},
             "provenance": {"reader": "ozagia.core.task_preflight.linked"},
             "not_assessed": ["..."], "omissions": [],
             "truncated": {"...": "..."}},
  "research": {"schema": {"...": "..."}, "readiness": "unknown",
               "claims": [{"id": "claim-0001", "evidence": "declared",
                           "truth_assessed": false, "trust": {"...": "..."}}],
               "sources": [{"id": "source-0001", "hash_state": "match",
                            "freshness": "current"}],
               "engines": {"ocelot": {"state": "unknown", "reason": "..."}},
               "not_assessed": ["..."], "omissions": [],
               "truncated": {"...": "..."}},
  "actions": {"schema": {"namespace": "ozagia.action_readiness",
                         "version": "1.0"},
              "selection": "explicit", "state": "blocked",
              "readiness": "unknown", "reason": "...",
              "targets": [{"kind": "service", "name": "api",
                           "configured": true, "mode": "background",
                           "process": {"state": "configured",
                                       "verified": false},
                           "environment": {"keys": ["API_TOKEN"],
                                           "references": [{"key": "API_TOKEN",
                                                           "reference": "secret://env/API_TOKEN"}],
                                           "resolved": false},
                           "readiness": "unknown"}],
              "operations": [{"action": "service.start", "target": "api",
                              "readiness": "unknown",
                              "reason": "environment secret references "
                                        "were not resolved",
                              "requirements": ["..."]}],
              "counts": {"operations": {"available": 0, "blocked": 0,
                                        "unknown": 1, "absent": 0,
                                        "operations": 1}},
              "provenance": {"reader": "ozagia.core.action_readiness",
                             "secrets_resolved": false},
              "not_assessed": ["..."], "deferred": ["..."],
              "truncated": {"...": "..."}},
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

## Linked checkout join

`build_preflight(..., linked_checkouts=[...])` adds the optional `linked`
dimension and a top-level `linked` section. Each mapping accepts only `root`,
`label`, `repo`, `checkout`, `snapshot`, `authorized` and `read_scope`; unknown
fields are refused. `authorized` is `True`, `False` or `"unknown"` (default) and
`read_scope` is a bounded list of paths/globs. Both are caller declarations:
a readable path, a manifest entry or a successful snapshot read never grants
authorization.

Each member row reports:

- `identity`: caller contract IDs when supplied, plus the snapshot-recorded
  name/root/head/generated time. Contract IDs stay `unknown` when the caller
  does not supply them; a path is not promoted to a `repo:`/`checkout:` id.
- `authorization`: declared state, read scope, freshness and reason.
- `reference`: `member_only`/`not_declared`, or the cached
  `current`/`pinned`/`updated`/`error`/`pending`/`not_checked` state with pin,
  checked time and an explicit `synced: false` summary.
- `index`: member-local `.ozagia/graph.json` freshness (`fresh`, `stale`,
  `absent`, `error`, `unknown`), snapshot/HEAD, generated time and verification
  errors.

Readiness is `true` only for an authorized, scoped, fresh, currently
reference-verified member. Unauthorized is `false`; missing, stale, absent,
unverified or unknown state is `unknown`. A missing member is retained in the
report, not dropped. Member facts are bounded by `max_facts` like every other
fact.

The join reads the anchor's existing federation manifest and reference cache
with `federation.reference_status`. It does not call `federation.discover`,
`sync_references`, `request_reference_sync` or Git fetch; it does not refresh
or rebuild a member snapshot. Reference sync is a separate explicit operation.

## Research join

`build_preflight(..., research_sessions=[...])` adds the optional `research`
dimension and a top-level `research` section. It calls
`core.research_projection.build_projection`, then joins a compact view
(`preflight_join`) and bounded `contracts.Fact` records. The projection reads
retained research graph state and selected local source hashes only; it never
fetches, calls a model/OCR/parser, refreshes the graph or writes.

Claims, assumptions and evidence are always `evidence="declared"` and
`truth_assessed=false`. A recorded `supports`, `opposes` or `derives` edge is
authored provenance, not observed evidence. A source hash match is
`observed`/`current`; a mismatch is `stale`; an absent file or unverified
remote source stays `absent`/`unknown`. Claims with no source stay `unknown`.
The `research` dimension is `unknown` whenever a selected session contains a
recorded claim, assumption or evidence; a research assertion cannot make the
assessment green.

`research.engines.ocelot` is always an explicit `unknown` with the reason that
the optional Ocelot adapter is deferred in this read-only slice and was not
imported or executed. The report also carries `deferred`, `not_assessed`,
`omissions` and its own `truncated` envelope; the global `truncated` envelope
includes those display omissions.

See [research_projection.md](research_projection.md) for the standalone schema,
source-hash rules and limits.

## Action readiness join

`build_preflight(..., action_readiness=[...])` adds the optional `actions`
dimension and a top-level `actions` section. Each selection is a bounded
mapping:

```python
{"action": "service.start", "name": "api"}
{"action": "tunnel.start", "name": "database"}
{"action": "service.list"}          # no name
```

`action` must be a `service.*` or `tunnel.*` action declared in
`core.actions.ACTION_SPECS`; unknown fields, unsupported actions, duplicate
selections, a missing target name (except `service.list`/`tunnel.list`) and
more than 16 selections are refused. `action_readiness=[]` selects the
dimension explicitly but observes no operation, so it is `unknown`.

The join calls `core.action_readiness.readiness` and maps the four-state
result into the preflight vocabulary:

- `available` -> `true`, only when every selected operation is available;
- `absent` -> `false`;
- `blocked` or `unknown` -> `"unknown"`.

Therefore an unresolved secret reference, a live-gated tunnel, a stale or
unverified process/lease, a mode/relay mismatch and a missing target can never
make the assessment green. The target/operation reason is retained; the
dimension is not collapsed to a bare boolean.

Each selected operation contributes a bounded `contracts.Fact` under
`action.<operation>.<target>`, with freshness `current` only for `available`,
`absent` for `absent` and `unknown` otherwise. A summary fact
`preflight.actions` records the state and counts. The top-level section carries
the target observations and operation rows; the dimension `data` carries a
compact copy so the report stays within the dimension byte bound.

The join reads only:

- `ozagia.config.load_project_config` (configured/absent, mode, relay, env key
  names and `secret://...` references);
- `ozagia.core.runtime.runtime_status` (existing process/lease state);
- `RuntimeStore(create=False)` log metadata (presence/size, never content).

It does not call `actions.preview`/`actions.apply`, create a confirmation
token, audit or runtime record, resolve a secret, construct a mutating manager,
start/stop/signal a process, open a tunnel or test relay/target reachability.
`tunnel.start` is always `blocked` in this join because live/network gates are
explicit apply inputs, not configuration. A service with any unresolved
environment reference is never `available`; a `secret://session/...` reference
is `blocked` because a runtime service cannot resolve it at spawn.

`linked`, `research` and `actions` are independent selectors. When
`action_readiness` is omitted, the report has the same six dimensions, schema,
bounds and assessment as before this join. The CLI does not expose
`action_readiness` in this slice; it is available to Python callers.

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

`task_preflight.Providers` is a frozen set of six required callables
(`goal`, `graph`, `capabilities`, `environment`, `resources`, `tests`) plus
three optional primary-scope callables (`linked`, `research`, `actions`). Each
receives a `ProviderContext` with the checkout, deadline, goal, scope, changed
paths, required capabilities, trust map, selected linked members/research
sessions/action selections and injected observation time, and returns an
`Observation(readiness, facts, data, reason, error, truncated)`.
`default_providers()` returns the read-only defaults (including the linked,
research and action observations); individual providers are also callable
directly. Existing callers that construct `Providers` with only the six
required callables keep working.

Tests use fakes for every assessment rule and a temporary git project for the
integration path (stale graph, missing interpreter, failing test, missing or
untrusted capability, unknown criteria, two linked checkouts with
fresh/stale/unauthorized/absent states, and a retained research fixture). No
test executes a target-project test, fetches a URL, runs reference sync or
probes a linked project. `PYTHONHASHSEED` subprocess runs prove deterministic
output.

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
- only runs named by the caller are joined;
- linked checkout authorization and read scope are caller declarations, not an
  observed ACL;
- reference-sync state is the existing cached status; no sync/fetch was
  performed;
- linked snapshots were not refreshed or reindexed, and cross-repository
  semantic/API checks were not run;
- retained research claims, assumptions and evidence are declarations, not
  observed or verified facts;
- a research source is verified only when it is a selected local file with a
  recorded SHA-256; remote sources were not fetched;
- the Ocelot adapter is deferred and represented as an explicit unknown;
- action readiness did not resolve secret references, check provider
  availability or validate credentials;
- action readiness did not start, stop, signal, preview or apply anything, and
  did not test relay/target reachability or remote authorization;
- live/network gates are never granted by preflight; apply still requires the
  explicit flags and a one-time confirmation token;
- action readiness reads log presence/size only, not log content.

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
  `checkouts`. For the optional `linked`/`research` dimensions, the dimension
  `data` is a compact summary and the top-level `linked`/`research` section
  carries the bounded records. Checkout identities are optional and stay
  `unknown` unless the caller supplies them; a path is not treated as an
  identity or permission.
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
- Action readiness keeps at most 16 explicit selections, 32 target rows and 64
  environment key names per service. It reports references and counts, not
  environment values or log content. Over-limit bytes are trimmed
  deterministically and named in `actions.truncated`.
