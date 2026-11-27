# Intent-to-outcomes Stage 0 interface inventory and gap map

Read-only interface inventory for the intent-to-outcomes plan.
Date: 2026-11-27. Scope: `src/ozagia`, its tests, and the two governing
documents (`docs/intent_to_outcomes_spec.md`,
`docs/plans/2026-11-27_intent_to_outcomes_delivery.md`). No code, spec or plan
file was modified; no target checkout was written; no model, network or daemon
was started by the analysis.

Method: read the implementation and its module/function docstrings, traced the
call sites, and ran read-only tests:

```
.venv/bin/python -m pytest -q tests/test_tasks.py tests/test_session.py \
  tests/test_evidence_store.py tests/test_actions.py \
  tests/test_resource_grants.py tests/test_task_preflight.py
# 191 passed in 12.25s

.venv/bin/python -m pytest -q tests/test_mesh.py tests/test_mesh_links.py \
  tests/test_code_review.py tests/test_repo_review.py \
  tests/test_graph_engine.py tests/test_task_profile.py
# 250 passed, 2 warnings in 33.96s
```

Labels used below: **implemented** means the contract area is exercised by
code and tests; **partial** means a narrower or differently-scoped surface
exists; **absent** means no implementation of the required guarantee. Stage 1
recommendations use **REUSE** (call it directly), **WRAP** (narrow adapter over
it), or **NEW** (a new typed record/module). Statements not established by a
read or test are marked **(inferred)**.

---

## 1. Summary

Ozagia already has strong primitives for *bounded single runs*, *deterministic
evidence joins*, *review packets*, *resource claims*, *advisory mesh
coordination*, *typed settings* and *graph indexing*. It does not have the
durable outcome lifecycle the specification requires: there is no typed Intent
or Intent revision, no WorkItem/Assignment/Attempt lifecycle bound to a
revision, no decision record bound to an exact action and base state, no effect
receipt/reconciliation journal, no budget reservation ledger, no general event
stream with a cursor, no project link/backend, and no expertise versioning.

The most important architectural finding is that **Ozagia has no general
durable typed graph store**. The current graph is a derived JSON snapshot for
code knowledge (`memory.DevMemory`), and the only append-only, event-sourced,
replay-validated graph is the research-domain `research.graph.ResearchGraph`.
The delivery plan's reference to Ocelot's Rust-backed `substrate_rs` storage is
not implemented in this checkout: `integrations/ocelot` installs/validates an
external Ocelot checkout and edits its workspace boot files; it does not expose
a graph store. Stage 1 must either (a) define one new durable outcome-record
store using the `evidence_store`/`ResearchGraph` patterns, or (b) first build a
narrow supported interface over an external substrate. It must not create
several parallel work stores.

Strong reuse candidates for Stage 1:

- `core/tasks.py` `TaskRunner` — bounded step execution, budgets, gates,
  artifact hashes, refusal-by-default execution.
- `core/evidence_store.py` — the only durable journal with idempotency keys,
  conflict detection, torn-tail recovery, locking and retention.
- `core/contracts.py` — canonical JSON, content/instance hashing, typed
  identities, truncation envelopes.
- `core/critical_path.py` `Dag` — explicit-edge DAG validation, cycle
  detection, topological order.
- `core/goals.py` — machine-checkable criteria and criterion evaluation.
- `core/session.py` `DevSession` — typed receipt graph and presentation-level
  decision/gate vocabulary.
- `edit/txn.py` — base-hash-checked atomic write with backup.
- `core/code_review.py` / `core/repo_review.py` — deterministic review
  packets and stable finding identities.
- `core/resource_grants.py` + `core/grant_lifecycle.py` — a persistent
  claim/release ledger with TTL and host approval flags.
- `core/settings.py` — typed settings registry with precedence, dry-run,
  atomic write and a read-verify before replace.
- `core/mesh.py` — advisory leases, links, delegations and bounded audit.

This inventory is consistent with the independent Stage 0 baseline report
(`docs/results/intent_to_outcomes_stage0_baseline_2026-11-27.md`): it also
recommends graph-native workflow and treats Ocelot's `substrate_rs` as an
optional store implementation behind a boundary, not a Stage 1 dependency.

---

## 2. Contract-to-surface map

The table covers the specification's "Logical contracts" and the specialized
sections that change the Stage 1 interface. The "partial/missing" column names
the guarantee that is *not* present, not merely a smaller implementation.

| Spec area | Reusable implemented surface | Partial / missing guarantee | Stage 1 call |
| --- | --- | --- | --- |
| CORE-1: conversational, harness, MCP, CLI, library; shared IDs/policy/evidence | MCP `DevMemoryServer` and tool table (`integrations/mcp/server.py:1570`, tools at `:85-1503`); Unix-socket daemon protocol (`daemon/protocol.py:1-160`); CLI modules (`tasks_cli.py`, `goals_cli.py`, `mesh_cli.py`, `actions_cli.py`); library modules (`core/tasks.py`, `core/goals.py`, etc.) | No shared intent/work/attempt IDs across entry points. Tasks and goals are separate CLI processes; MCP exposes goals/mesh/sessions but not `TaskRunner`; the daemon has jobs but not task runs. Evidence is shared only for goals/runs/tests. No schema negotiation for the work contract. | **NEW** thin service module (e.g. `core/outcomes.py`) that all entry points call; **REUSE** existing modules behind it. Do not reimplement graph/goal/task logic in MCP or CLI. |
| Context and expertise: typed entity refs, provenance, applicability, freshness, capability records | `memory.DevMemory` typed nodes/edges with stable keys (`core/memory.py:1988`; `core/keys.py:31-73`); `research.graph.ResearchGraph` typed append-only graph (`research/graph.py:360`); `capabilities.Capability` declaration record (`core/capabilities.py:226`, catalogue `:2524`); apertures as data-defined projections (`core/apertures.py:136`) | DevMemory is a code index rebuilt from the repo, not a provenance store; edges carry no applicability/freshness. ResearchGraph is research-only, single-writer JSON. Capabilities are Ozagia's own feature declarations, not user expertise. No capability input/output types, execution location, effects, cost or verification options. | **WRAP** DevMemory/ResearchGraph behind one entity-reference interface; **NEW** expertise/capability records only in Stage 3. Do not overload `capabilities.py`. |
| Intent: stable ID, principal, conversation ref, requested outcome, context refs, optional primary project, criteria, interpretation, version; observations separate from interpretations; steering creates a version | Closest existing records: `goals` goal (`core/goals.py:15-26`) with title/why/targets/scope/criteria/status/history/claims; `TaskSpec.objective` (`core/tasks.py:302`); `DevSession.task` text (`core/session.py:352`); `ResearchGraph.Question` node | Goal is a repo-level work item, not an intent: no principal, conversation reference, requested-outcome field, context references, nullable project, interpretation, or version chain. No separation of observation from interpretation. `TaskSpec.version` versions a task class, not an intent. | **NEW** Intent and IntentRevision as typed records; **REUSE** goal ids as criteria/evaluator attachments. Do not repurpose `goals` as the intent store. |
| Project, Project links, Project backend: nullable project, multiple links, independent access, backend mapping, revision preconditions | `.ozagia.toml` / `<root>/ozagia/config.toml` project configuration (`config.py:755,804,1878`); declared cross-checkout links in `.ozagia/federation.json` with `source`/`target`/`relation`/`provenance` (`core/federation.py:543`); Ocelot workspace adapter edits an external workspace (`integrations/ocelot/workspace*.py`) | No Project record with purpose/owner/visibility/state; no intent→project links, primary-link preference, provisional grouping, merge/detach/archive. Federation links are repo-knowledge relations, not durable work links, and are read-only. No backend mapping/authority/stale-write handling. | **NEW** Project, ProjectLink and ProjectBackend records in Stage 2; **REUSE** `contracts.Identity` (`project:` is an ID kind at `core/contracts.py:608-618`; `parse_id` at `:830`) and federation's provenance shape as a design precedent. |
| Work and attempt: bounded unit pointing to intent version/criteria; attempt with work context, baseline, worker, capability/expertise/policy versions, limits, lease generation, status, evidence; retries create distinct attempts | `TaskSpec` (`core/tasks.py:296`) with steps/tools/write scope/worktree/resources/budget/gates/output schema; `TaskRunner` (`:1673`) writes a run directory with `events.jsonl`, `manifest.json`, `artifacts.json`, `output.json` (`:40-45`, `:2806`); `DevSession` Attempt/Edit/Verification nodes (`core/session.py:581-640`); `task_profile.TaskRecords` joins task/goal/test/session/daemon/eval records (`core/task_profile.py:1067,2880`) | No WorkItem instance bound to an intent revision. `TaskRunner` is synchronous and executor-injected; no queue, worker identity, lease generation, capability pin, resume, external cancel, or reconciliation state. A `run_id` collision is refused (`:2486-2488`) but there is no payload-sensitive idempotency key/conflict. DevSession attempts are receipts with pass/fail verification, not execution leases. | **WRAP** `TaskRunner` as the Stage 1 attempt executor; **NEW** WorkItem and Attempt records that reference `run_id`, spec digest and lease generation. Keep `DevSession` as a projection/receipt graph, not canonical work state. |
| Grant: principal+worker+operation+scoped targets+intent version+time+budget; host intersects caller/target/worker/task permissions at dispatch and effect; plans/capability/mesh messages are inputs, never authority | `resource_grants.ResourceGrant` + `GrantBudget` (`core/resource_grants.py:287,217`); `GrantLease` preflight/claim/release (`core/grant_lifecycle.py:110,145,226,265`); `actions` preview→one-time token→apply (`core/actions.py:1897,1926`); mesh delegations (`core/mesh.py:1738`) | Grants bind a resource, task id, session label, reason, budget and TTL; approval is a host boolean, not a principal/role. No operation name, effect-target list, intent revision, or effect-time intersection. No mid-run revocation; a claim is released, not invalidated. Actions tokens bind action/args/root/TTL but not actor, intent, base state or policy version. | **NEW** a typed Grant that references existing resource-grant ids and action names; **REUSE** `GrantLease`/`resource_grants.claim` for resource entitlement and `actions._load_token` for exact-request binding. Do not duplicate grant config. |
| Event and evidence: schema version, actor, correlation IDs, sequence, timestamp; evidence adds source identity, artifact digest, validation method, coverage, freshness; artifact access checked at retrieval; no secrets | `evidence_store` durable journal (`core/evidence_store.py:1-68,1197`) with record_version/source/observed_at/created_at/refs/hash; `evidence.collect_evidence` read-only join (`core/evidence.py:412`); task `_EventLog` with `schema`, `seq`, `ts`, `run_id`, `type` (`core/tasks.py:1365`); job events with seq/cursor (`daemon/jobs.py:440,1727`); mesh bounded redacted audit (`core/mesh.py:2946`); research graph event log with replay validation (`research/graph.py:478`) | Evidence schema is goal/run/test-specific; no generic artifact/evidence record. Task events are per-run, size-bounded and truncatable, with no actor/correlation/causation and no cross-run cursor. Job events only report job ends and are in-memory. Mesh audit is bounded and redacted. No retrieval-time artifact authorization. | **WRAP** `evidence_store` for durable evidence (add a typed outcome-evidence adapter or record kind); **NEW** one generic event stream with actor/correlation/sequence/cursor. Reuse task event sequence and job cursor semantics; do not create two event logs. |
| Client and worker operations: submit, steer, inspect, follow events from cursor, decide, cancel, retrieve evidence; worker register/claim/heartbeat/report/ack; idempotency keys; schema/capability negotiation before dispatch | Daemon request/response protocol (`daemon/protocol.py:12-160`, socket `:484`); MCP JSON-RPC server (`integrations/mcp/server.py:3397`); mesh `register`/`heartbeat`/`unregister` (`core/mesh.py:1088,1169,1195`); `JobTable` start/wait/log/stop/events (`daemon/jobs.py:1096,1564,1531,1764,1727`); graph engine `hello` capability negotiation (`core/graph_engine.py:1182,1251`) | No intent submit/steer/decide/cancel operations. No worker claim/report/ack for assignments; mesh registration is a liveness record, not a lease. Idempotency keys exist for mesh links/delegations (`core/mesh.py:1389-1429`) but not for task runs, goals, actions or settings. No worker capability negotiation against a work contract. | **NEW** service operations; **REUSE** daemon transport, mesh identity/liveness, and graph-engine `hello` negotiation pattern. Add idempotency keys following mesh link semantics. |
| Durable request lifecycle: accepted→running→waiting→satisfied/partial/failed/cancelled; attempts queued→leased→running→terminal plus reconciliation; satisfied requires recorded evidence | Task run statuses `ok`/`refused`/`stopped`/`failed` (`_run_status` `core/tasks.py:3006`); goal statuses (`open`/`met`/`regressed`/`blocked`/`dropped`, `core/goals.py:120`); DevSession `open`/`closed` and step statuses (`core/session.py:63,752`) | None of these is an intent lifecycle. No accepted/waiting/satisfied/partial/cancelled vocabulary, no explicit reconciliation state, no rule that "satisfied" requires evidence. Goal `met` is criteria-only; a task `ok` is executor-reported. | **NEW** explicit intent and attempt state machines in the outcome store; adapt task/session statuses into attempt outcomes. Do not overload goal status. |
| Decision (gate): refers to intent version, exact action parameters/target/expected base state; scope or state drift requires reevaluation; evaluator/input versions recorded | `DevSession` Decision node with `choice`, optional `gate` (`wait`/`approve`/`clear`) and summary (`core/session.py:642-651`, gate derivation `:789`); goals `criterion.last` result (`core/goals.py:21`); review findings and `exit_code` (`code_review.py:2655`); settings preview/apply re-reads the file before replace (`core/settings.py:2499-2503`) | DevSession gate is presentation-level and has no enforcement. Goal results are not decisions about an action. Review verdicts are packet-level, not bound to a delivery action. No principal, intent version, action hash, target, base hash, policy version, expiry or drift check. | **NEW** Gate/Decision record; **REUSE** `actions` token binding for exact action/args and `edit/txn.py` base hash for expected base state. Keep DevSession decisions as a rendering source. |
| Assignments and portable recovery: required skills/expertise, resource bounds, execution window, checkpoint formats, allowed inference routes; durable checkpoints; restore/restart/wait on uncertainty; fence stale attempts | `TaskSpec` declares tools, write scope, worktree, resources, budget (`core/tasks.py:296`); `task_profile` span tree and measurements (`core/task_profile.py:490,1637`); `eval/agent.py` workdirs/drivers/validation (`eval/agent.py:2161-2473`); `resource_grants` claims and mesh leases | No Assignment record: no skills/expertise ids, execution window, checkpoint formats, allowed provider/model/data-location route. No durable checkpoint schema or export. No worker replacement/resume protocol. | **NEW** Assignment and Checkpoint records in Stage 5; **REUSE** TaskSpec as the bounded payload and task_profile spans as measurement. Do not advertise portable recovery before effect reconciliation exists. |
| Worker capacity and provisioning: capacity request, provider options/cost/lead time, provision/inspect/drain/release, owned vs shared vs enrolled, reservations, ambiguous create reconciliation | `resource_grants` kinds `vm`/`cloud`/`host`/`service`/`container` with provider, image, region, constraints, budget, cleanup (`core/resource_grants.py:43-59,287`); `relay` local/SSH/SSM transports (`core/relay.py:110,1485`); `daemon/jobs` admission/cgroup/rlimit (`daemon/jobs.py:51-73,1096`); mesh enrollment (`core/mesh.py:1088`) | Grants declare entitlement and readiness but do not provision, inspect, drain or release capacity. No provider adapter contract, no operation key before a provider call, no ambiguous-create reconciliation, no owned/shared/enrolled distinction in a lifecycle. | **NEW** Stage 6 capacity provider adapter; **REUSE** grants for policy/budget, relay for access, jobs for local resource enforcement. Do not add provider calls to `resource_grants`. |
| Workflow DAGs and gates: versioned DAG, typed inputs/outputs, human/model/tool nodes, edges/conditions/joins, persisted node results and gate decisions, timeout vs stop vs revocation, recovery/compensation | `critical_path.Dag`/`DagNode`/`DagEdge` with explicit finish-to-start edges, cycle detection and topological order (`core/critical_path.py:576,628,647,733,826`); `TaskSpec.steps` + before/after gates (`core/tasks.py:279,260,86-103`); code/repo review packets as deterministic checks; DevSession Decision | `TaskSpec` is a linear step list with named gate checks, not a DAG with branches, joins, parallel work, fallback or compensation. `critical_path` is an in-memory analysis library: no process definition, run state, node results, gate decisions or recovery. | **REUSE** `critical_path.Dag` for validation/topology and **WRAP** `TaskRunner` for leaf steps; **NEW** persisted process/DAG-run/node-result/gate records. Do not add a separate DAG engine. |
| Expertise graph: stable ID, immutable version, provenance, applicability, prerequisites, relationships, procedures, checks; candidate/active/superseded/revoked; activation scope; pins; revocation stops downstream actions | `capabilities.Capability` has id, domain, purpose, owner, side effect, exposure, impl, surfaces, prerequisites, limits, authorization, tests, docs (`core/capabilities.py:226`); `ResearchGraph` has typed nodes/edges and `amend` (`research/graph.py:544,580`) | Capabilities are a hand-written catalogue of Ozagia's own declarations with one catalogue version (`CAPABILITY_VERSION`), not user expertise entries with immutable per-entry versions. No activation scope, supersession/revocation, dependency resolution or attempt pin. | **NEW** expertise entry/version/activation records in Stage 3; reuse `contracts` identities and graph edge vocabulary. Do not overload `capabilities.py`. |
| Budget/reservation: per-attempt, cumulative intent, recurring allowance; reserve before dispatch; bounded sublimits; commit once; release unused; carry unresolved; multi-limit atomic admission; no partial reservation | `tasks.Budget`/`StepBudget`/`_BudgetLedger` arithmetic and stop conditions (`core/tasks.py:198,212,1403,1463`); `resource_grants.GrantBudget` + TTL and duplicate-active-claim refusal (`core/resource_grants.py:217,1683`); admission checks (`core/resources.py`) | Task budgets are in-memory, self-reported by the executor, and never reserved. Resource grant claims reserve a *resource*, not token/allowance capacity. No multi-limit transaction, committed usage/late receipt, carryover or idempotent charge. | **NEW** reservation ledger in Stage 1/3; **REUSE** `_BudgetLedger` arithmetic and the `resource_grants` claim/release ledger pattern. Do not call a task budget a reservation. |
| Ozma inference request: typed request ID/schema/question/graph refs/result shape/freshness/deadline/limits; graph-only/model-backed/hybrid policy; capability/routing; typed result with mode/evidence/uncertainty/usage; status/cancel/replay/redaction | `ozma_mesh` config/identity/readiness (`core/ozma_mesh.py:122,422,661,1011,1099`); `ozma_bridge` typed local envelope, dedupe, outbox/inbox, ack, claims, handoffs (`core/ozma_bridge.py:1-24,751,1233,1496,1591,1768`); graph engine `/v1/ozma/context` client (`engine_client.py:37-39`); `model_api` scoped maps (`model_api/project.py`) | The bridge is explicitly a *closed local schema* with `CONFORMANCE = "declared"` and no approved Ozma shim/upstream envelope (`ozma_bridge.py:9-14,43-47`). No inference request/response type, fulfillment-mode policy, zero-model guarantee, typed partial/unavailable/denied result, or provider-side limit enforcement. | **NEW** Stage 5 wire contract + two-peer conformance fixture; **REUSE** `ozma_bridge` transport/dedupe and `model_api` scoped projection. Do not claim conformance. |
| Model-facing configuration: canonical registry; inspect/propose/apply; types/units/defaults/precedence/dependencies/writeability; credentials as refs; proposal/version binding; atomic multi-setting apply; conflicts/rollback | `settings.schema`/`values`/`get`/`set_values`/`set_value` (`core/settings.py:2090,2126,2189,2387,2436`); five-layer precedence and source reporting (`values`); dry-run default and atomic write with pre-write re-read (`_prepare_values`, `set_value`); `model_api.HostPolicy` for scoped model access (`model_api/policy.py:74`) | No configuration skill/tool. No config version identity or proposal hash; approvals are not bound to a proposal. `set_values` is atomic for one file but has no cross-setting constraints, policy gate, bundled-action authorization, conflict display beyond text diff, or rollback. | **WRAP** the settings registry in Stage 2; add proposal/version binding and policy checks at the service layer. Do not create a second settings store. |
| Adaptive provider allowance estimates: versioned estimator per provider/quota/model/window; join readings and metered logs; cache mix; overlapping windows; drift; uncertainty; reserve against all limits | Metered usage exists in `session_profile` token/timing metadata (`integrations/session_profile.py:1-18`, `session_profile_cli.py:89`) and task `_BudgetLedger` (`core/tasks.py:1403`). `eval` has matched-run measurement (`eval/agent.py`, `eval/compare.py`). | No provider allowance reading, quota-pool model, estimator segment, cache-ratio calculation, drift tracking, or multi-limit reservation. No distinction between provider-enforced/estimated/unsupported. | **NEW** Stage 3 estimator; reuse session/task usage as inputs. Label estimates separately from measured facts. |
| Workflow experience and progress: outcome + evidence first; plan/attempts for diagnosis; blockers and next action; deterministic progress from events/criteria; optional model summary with same facts/access | `status.py` read-only repo/component snapshot (`core/status.py:1-15`); `working_set` session brief (`core/working_set.py`; daemon op `daemon/server.py:2782`); `goals.brief_lines` (`core/goals.py:1926`); `view.py`/`apertures.py` deterministic graph projection (`core/view.py:1-25`); `view_html.py` | Status is component-oriented, not outcome-oriented. No list of active work/decisions/verified outcomes; no deterministic progress derive from event+criteria; no stable view contract for available operations/evidence refs. `view.py` projects graph bundles, not work records. | **NEW** Stage 1 outcome list/status projection; **REUSE** `view.py`/apertures for generic extension rendering. UI requests must go through the same service/aperture path. |
| Messaging and ChatOps | None. The `actions` layer can notify nothing; no inbound/outbound adapter, sender mapping, dedupe or intake limits. | Absent. | **NEW** Stage 4 adapters against the same service operations. |
| Calendars and scheduled events | None. No reference calendar, event identity/revision, recurrence, availability or trigger evaluation. | Absent. | **NEW** Stage 4 calendar adapter. |
| Home and agent activity | `status.py`, `working_set`, `view.py`/apertures; `session_profile` activity metadata. No assignment/outcome graph, no live-vs-stale distinction for work. | Absent as a product surface. | **NEW** projection in Stage 2; reuse view/aperture rendering. |
| Revealed preferences | `session_profile` and `eval` are measurement surfaces, not preference observation over UI choices. | Absent. | Stage 2/UX instrumentation, local only. |
| Attention and unattended operation: ask/notify/batch/silent; advisory vs required gates; deadlines/fallbacks; indefinite service; restart; stop/steer | Daemon idle timeout/autostart (`daemon/autostart.py`), job detach/shutdown/restart reconciliation (`daemon/jobs.py:97-105,465-525`), mesh heartbeat/liveness (`core/mesh.py:682`), resource claim TTL. | No consultation deadlines, required gate enforcement, standing policy, budget renewal, or return-to-work summary. Daemon recovery covers processes/jobs, not intent decisions. | **NEW** Stage 1/4 policy and deadline records; reuse daemon lifecycle. |
| Outcome efficiency and model comparison | `task_profile` spans/report with measurements, cost/tokens/waits (`core/task_profile.py:2344`); `eval/agent` matched runs and validation; `eval/compare`; `status` session profiles. | No per-intent Outcome record, criteria-satisfied/partial/quality/rubric, cost per verified outcome, human effort, or comparison by workflow/model/configuration. `task_profile` is generic timing/measurement, not an outcome judgment. | **WRAP** task_profile/eval in Stage 3; **NEW** Outcome record. Keep unknown distinct from zero. |
| Forecasting from observed work | `task_profile`/`eval` historical measurements; `critical_path.what_if` deterministic schedule simulation (`core/critical_path.py:1275`). | No forecast record, comparable-run cohort, verification-probability model, baseline/backtest or calendar inputs. `what_if` is simulation, not a forecast from observed runs. | **NEW** Stage 3 forecast records; reuse task_profile/eval data and `what_if` for schedule arithmetic. |
| Optional telemetry and pooled estimates | None. | Absent. | **NEW** Stage 7. |

### Acceptance scenarios most at risk

These are the scenarios that current surfaces cannot pass without the Stage 1
records. The full 41-scenario table is in the specification; this list is the
minimum Stage 1 exit coverage.

| Scenario | Current coverage | Missing for a pass |
| --- | --- | --- |
| Standalone report resumes after restart, no project | Task run manifests persist under `.ozagia/tasks/`; goals persist; DevSession persists | No Intent/standalone work record; no resume of a waiting/partial intent; no service-owned dispatch state |
| "Just investigate" invalidates a queued change | `TaskRunner` stop conditions; no queue | Revisioned intent, queued work cancellation, effect reconciliation disclosure |
| Two isolated workspaces bind graph/edit/test/report to their own workspace | `TaskRunner` root + worktree checks; `task_exec.LocalExecutor` pins root; `edit/txn` root-relative | No per-attempt workspace/baseline record; no capture-mode visibility; no cross-workspace isolation test |
| Remote worker disconnects after attempting a write | Mesh lease expiry/takeover; no effect fencing | Effect key/receipt, lease generation, reconciliation state, stale-lease refusal |
| Peer requests graph-only inference with zero budget | `model_api` graph projection; ozma bridge messages | Typed inference request/result, zero-model enforcement, replay/revocation |
| Same expertise process with human approval, model review, no gate | DevSession Decision node; review packets | Persisted DAG run, gate decisions bound to action/base, restart recovery |
| Revoke expertise while an attempt runs | None | Expertise pin, revocation scope, stop-before-next-effect, recovery |
| Two peers share remaining budget | Resource grant claim; no token budget | Reservation ledger with atomic multi-limit admission and late-receipt settlement |
| Link one intent to two projects and race a backend write | None | ProjectLink record, backend revision preconditions, conflict preservation |
| Waiter needs decision; progress update preserves facts/access | `status`/`working_set`/`goals` brief | Outcome+decision view contract, deterministic progress, evidence links |
| Stop an intent while compensation is queued | Job stop kills a process group | Intent stop admission closure, queued-effect suppression, in-flight reconciliation |
| Renew allowance with worker receipt outstanding | None | Reservation carryover and idempotent late receipt |
| Queue work needing unavailable capacity | Resource grants readiness; no provisioning | Capacity request/provider adapter/enrollment verification |
| Replace unavailable worker with compatible one | Mesh takeover of advisory lease | Assignment/checkpoint, compatible worker check, effect reconciliation |

---

## 3. Logical-record mapping

Every record below needs a typed representation. "Existing" names the closest
object; "status" says whether it can carry the contract as-is.

| Logical record | Existing representation | Status | Persistence / idempotency / replay notes | Stage 1 recommendation |
| --- | --- | --- | --- | --- |
| **Intent** | `goals` goal (`core/goals.py:15-26`) is the closest durable work record: id, title, why, targets, scope, criteria, status, `claimed_by`, `history`, `source`. `TaskSpec.objective` and `DevSession.task` are task/conversation text. | **Partial — not an intent.** No principal, conversation reference, requested outcome, context refs, optional project, interpretation, or revision. | Goal store is one JSON document at `<state>/goals/goals.json`, atomic write + `fcntl` lock (`core/goals.py:429`). Goal ids are local (`g<n>`), not globally stable. Goal status is criteria-only. | **NEW** Intent record. Reuse goal ids as the criteria attach point; do not put intent fields into the goal store. |
| **Intent revision** | No revision chain. `ResearchGraph.amend` records `node_amended` events with before/after fields (`research/graph.py:580`); `TaskSpec.version`/`digest` version a task class, not an intent. | **Absent.** | ResearchGraph's event log validates replay against materialized state (`research/graph.py:478-532`) and is a good pattern, but it is research-only and single-writer. | **NEW** immutable IntentRevision records with `revision`, `hash`, `parent`, `created_by`, `reason`, and a link to affected WorkItems. Workers pin the revision hash; new steering appends, never mutates. |
| **Project link** | No intent-project link. `federation.json` declared links (`core/federation.py:543`) have source/target/relation/provenance but are repo-knowledge links, read-only, exposed only when both endpoints resolve. `.ozagia.toml` is one repository configuration, not a project entity. | **Absent.** | Federation manifest is consumer-owned and versioned (`{"version":1,"links":[...]}`), but has no ACL or write path. | **NEW** ProjectLink record `{intent_id, project_id, primary, provenance, access_scope, state}`. Keep link ownership in Ozagia; do not derive it from a repo checkout. |
| **WorkItem** | Goal (`core/goals.py:15`) with targets/scope/criteria is the closest. `TaskSpec` (`core/tasks.py:296`) defines a reusable bounded task class. `critical_path.DagNode` (`core/critical_path.py:576`) can represent a unit in a DAG. | **Partial.** Goal has criteria/status/history but no intent revision, assignment, attempt list or project link. TaskSpec is a class, not an instance. | Goal store is atomic/locked. No idempotency key; duplicate goals are allowed. Goal criteria evaluation is read-only and can be invoked repeatedly. | **WRAP** goal as the criteria evaluator; **NEW** WorkItem record `{id, intent_id, intent_revision, criteria_ref, project_links, state}`. Do not create a second criteria engine. |
| **Attempt** | `TaskRunner` run (`core/tasks.py:2469-2803`) is the closest execution instance: `run_id`, spec digest, preflight, events, steps, artifacts, output, budget. `DevSession` Attempt/Edit/Verification nodes (`core/session.py:581-640`) are receipts. | **Partial.** Task run has no worker, capability/expertise pin, lease generation, assignment, reconciliation state or resume. DevSession attempt has pass/fail verification but no execution identity. | Run directory existence refuses reuse of a `run_id` (`core/tasks.py:2486-2488`); no payload-sensitive idempotency key. Events are append-only JSONL at `<run>/events.jsonl` with seq (`:1365`), but no torn-tail recovery. No lock across the run directory. | **WRAP** TaskRunner; **NEW** Attempt record referencing `run_id`, spec digest, worker, lease generation and status. Keep task events as the execution trace; copy required refs into the attempt record. |
| **Assignment** | No assignment. TaskSpec + `GrantLease` + mesh delegation are separate inputs. `eval/agent` has driver/arm/workdir but not a durable assignment. | **Absent.** | — | **NEW** Assignment record with skills/expertise ids, resource bounds, execution window, checkpoint formats, allowed inference routes and current lease. |
| **Gate / Decision** | `DevSession` Decision node (`core/session.py:642`) has `choice`, optional `gate` (`wait`/`approve`/`clear`), `summary`, and is projected into the next-action table. Goal `criterion.last` records pass/fail/stale/unknown. Review `exit_code` (`code_review.py:2655`) is a packet verdict. `actions` token binds action/args/root/TTL (`core/actions.py:459-520`). | **Partial.** Decisions are not bound to an intent revision, exact action hash, target, expected base state or policy version. DevSession gate has no enforcement. Review verdicts are not delivery decisions. | DevSession is one JSON document per session, atomic replace (`core/session.py:140-157`); no lock, no event cursor. Action tokens are one-time files under `<state>/actions/pending`, consumed by rename (`core/actions.py:523`). Settings apply re-reads the file before replace (`core/settings.py:2499`). | **NEW** Gate/Decision record `{id, intent_id, intent_revision, action_digest, target, expected_base, policy_version, evaluator, decision, expires_at, recheck}`. Reuse action-token binding and base-hash checks. |
| **Effect receipt** | No typed receipt. The edit journal records `base_sha256`, `new_sha256`, `backup`, `files`, `ops` for applied edits (`core/memory.py:4172`, `edit/txn.py:174-229`). `actions` audit records action/state/ok/error/target/token digest (`core/actions.py:551`). `resource_grants.release` records observed cost/time/cleanup (`core/resource_grants.py:1863`). Task artifact manifest has `sha256`/`size` (`core/tasks.py:2312-2351`). | **Partial.** These are observations, not an effect state machine. No effect key, no `intended/attempted/observed/confirmed/uncertain/reconciled` state, no safe-retry classification, no lease/grant fencing declaration. | Edit journal is append-only JSONL, no lock/recovery. Actions audit is bounded and rewritten atomically, keeping the last 200 records. Resource claims are one JSON file per claim, lock-protected, never auto-pruned. | **NEW** effect receipt/journal with `effect_key`, `attempt_id`, `lease_generation`, `grant_id`, `target`, `expected_base`, `observed_receipt`, `state`, `fencing`. Reuse `edit/txn` for the actual write and its base hash; reuse action audit for the effect intent/result envelope. |
| **Evidence** | `evidence_store` durable records: `observation`, `assessment`, `declaration`; record has idempotency key, source, observed/created times, evidence word (`declared`/`observed`/`verified`), freshness, refs and hash (`core/evidence_store.py:10-68,1197`). `evidence.collect_evidence` is a read-only join (`core/evidence.py:412`). Review packets carry findings/evidence/coverage. `task_profile` spans carry evidence/measurement. | **Partial.** The schema is goal/run/test-specific; no generic artifact/evidence record for an arbitrary outcome; no actor, validation method, coverage/freshness per arbitrary evidence item; no retrieval-time authorization. | Strong: `fcntl` lock, canonical JSONL append, idempotency-key duplicate/conflict, torn-tail recovery, retention/compaction, `record_hash`. Tests pin all of these (`tests/test_evidence_store.py:351,380,428,544,568`). | **WRAP** `evidence_store` with a typed outcome-evidence adapter (new record kind or a new namespace) that stores refs/digests, not duplicate content. Reuse its idempotency/recovery semantics. Do not create a second evidence journal. |
| **Event** | Task `_EventLog` (`core/tasks.py:1365`): `schema`, `seq`, `ts`, `run_id`, `type` + fields, 8 MiB cap. Job `events` with `seq`/cursor (`daemon/jobs.py:440,1727`). Mesh audit (`core/mesh.py:2946`). ResearchGraph event log (`research/graph.py:478`). `session_profile` metadata events (`integrations/session_profile.py`). | **Partial.** Task events are run-local and can truncate; no actor/correlation/causation. Job events are in-memory and only job ends. Mesh audit is bounded/redacted. No cross-work-stream cursor or replay. | Task JSONL is append-only; `_EventLog.write` silently returns on OSError and sets `truncated` at the byte cap. Job events are not persisted. ResearchGraph validates exact event replay on load. EvidenceStore recovers a torn tail. | **NEW** generic event stream (or one event table in the outcome store) with `event_id`, `schema`, `actor`, `correlation_id`, `causation_id`, `seq`, `ts`, `type`, `payload_ref`, `redaction`. Reuse task event ordering and job cursor semantics. |
| **Budget / reservation** | `tasks.Budget`/`StepBudget`/`_BudgetLedger` (`core/tasks.py:198,212,1403`) tracks tokens/model calls/cost/wall/memory/disk/pids in memory and stops on exceed. `resource_grants.GrantBudget` + `claim`/`release` (`core/resource_grants.py:217,1683,1863`) persists a resource claim with max USD/minutes/instances/TTL. | **Partial.** Task budgets are self-reported and not reserved; no committed usage or late receipt. Grant claims are resource-centric, not token/allowance, and do not reconcile a model provider's usage. No atomic multi-limit admission. | Task ledger is per-run memory only; it is serialized into the manifest but cannot be resumed. Grant ledger is one file per claim with lock, TTL/expiry, explicit release and caller-supplied observed values; cleanup is not provider-verified. | **NEW** reservation ledger with idempotent reserve/commit/release, carryover and multi-limit admission. Reuse `_BudgetLedger` arithmetic and the grant ledger's file/lock pattern. |
| **Aperture** | `apertures.py` defines versioned *view* projections (`core/apertures.py:136`): resolves node/edge types, grain, collapse, render, limits. `model_api.HostPolicy` (`model_api/policy.py:74`) is a read-scope policy: root, session/model labels, allowed maps, path prefixes, budgets. `mesh._scope_guard` scopes mesh records to repo/mesh. `actions`/`resource_grants` are point checks. | **Partial and easily misread.** `apertures.py` is not authorization. `HostPolicy` is read-only map access, not effect authorization. No single object combines visibility + operation permission + intent revision + project scope + capability + approval. | Apertures are TOML/JSON data, built-ins plus repo overrides (`core/apertures.py:459,477`). `HostPolicy` is constructed by the host and never by the model (`model_api/policy.py:1-12`). No persisted aperture decision. | **NEW** `EffectiveAperture` resolver at the service boundary that composes `HostPolicy`, resource grants, action tokens and mesh scope. **REUSE** `apertures.py` only for the view projection. Record the resolved aperture/decision as evidence. Do not create a second permission store. |
| **Worker lease** | `mesh` lease: one JSON record per lease with holder, resource, mode, TTL, expiry, queue, renew/takeover, `_lease_effective` and advisory-only semantics (`core/mesh.py:2439,2555,2657,2707`). `resource_grants` claim is a resource entitlement lease, not a worker lease. | **Partial.** Mesh leases are explicitly advisory and confer no authorization (`core/mesh.py:1-8`). No lease generation, no attempt/assignment binding, no effect fencing, no finalization check. Takeover reuses the record and has no generation increment. | Leases under `<state>/mesh/<mesh_id>/leases/`, one JSON per lease, `flock` on `leases/.lock`, atomic replace; expiry and holder liveness are computed on read; history is capped. | **WRAP** mesh lease for local coordination; **NEW** execution lease with `generation`, `attempt_id`, `assignment_id`, `expires_at`, `fencing` declaration. Reject stale generations at effect time. Do not treat a mesh lease as authority. |

### Record persistence summary

| Record | Stage 1 persistence | Idempotency / replay | Recovery |
| --- | --- | --- | --- |
| Intent / IntentRevision | New outcome store (one journal or graph namespace) | revision hash as natural key; duplicate append with same hash = no-op; different payload = conflict | reload latest revision; never rewrite a prior revision |
| WorkItem | Outcome store; references goal id | work item id + intent revision unique | re-open from records; goal evaluation is re-runnable |
| Attempt | Outcome store + existing task run directory | `(work_item, attempt_no)` unique; task `run_id` remains the execution key | read task manifest/events; reconciliation state explicit |
| Assignment | Outcome store | assignment id + intent revision | re-place only after lease/effect reconciliation |
| Gate/Decision | Outcome store | decision id + action digest; repeated identical decision = duplicate | expired/stale decision requires re-evaluation |
| Effect receipt | Outcome store/effect journal | `effect_key`; same key+same payload = duplicate; same key+different payload = conflict | `uncertain` until observed/reconciled; never silently retry |
| Evidence | Reuse `evidence_store` via adapter | existing idempotency key/content hash | existing torn-tail recovery |
| Event | Outcome store event table/stream | `event_id`/sequence; no duplicate sequence | replay from cursor; detect gaps |
| Budget/reservation | New reservation ledger | reservation id + idempotency key; duplicate charge at most once | carry unresolved reservations across restart |
| Aperture decision | Outcome store evidence | decision id + policy version + intent revision | re-resolve on revision/policy change |
| Worker lease | New execution lease, optionally mirrored to mesh | `(attempt, generation)` unique | expiry permits reassignment only if fenced/reconciled |

### Control notes by record

- **Intent / IntentRevision.** Steering appends a new revision with a parent
  hash; it never mutates the accepted revision. The revision records the
  principal, conversation ref, requested outcome, context refs, criteria and
  optional primary project. Access checks happen on intent read, project link
  resolution and evidence retrieval; a link cannot widen access. An old worker
  may keep auditing its pinned revision, but a new claim must cite the current
  revision or be refused.
- **WorkItem.** Points at one intent revision and a criteria snapshot/reference.
  A revision change marks dependent queued work stale; completed work keeps its
  original revision for audit. Criteria evaluation reuses `goals` and records
  the evaluated revision/input hashes.
- **Attempt.** Binds work item, worker, assignment, capability/expertise pin,
  policy version, lease generation and baseline. Cancellation/steering first
  closes admission and stops new grants, then signals the worker; finalization
  rejects a stale lease generation. A worker loss enters `uncertain` until the
  effect is reconciled; retry creates a new attempt, never reuses the old
  attempt id.
- **Assignment.** Binds required skills/expertise, resource bounds, execution
  window, checkpoint formats and allowed inference routes. A fallback/route
  change rechecks policy, budget and checkpoint compatibility before dispatch.
- **Gate/Decision.** Binds intent revision, evaluator, exact action digest,
  target, expected base state and policy version. A scope/state drift or
  expired decision requires re-evaluation; an approval cannot authorize a
  different action or a later revision.
- **Effect receipt.** Uses an effect key per logical effect (not per retry).
  Same key+same payload is a duplicate; same key+different payload is a
  conflict. A confirmed-safe operation may retry; an uncertain result is held
  for reconciliation; a fenced target rejects a stale lease/grant. A permitted
  unfenced effect blocks conflicting reassignment until reconciled.
- **Evidence.** Carries source identity, artifact digest, validation method,
  coverage and freshness. Retrieval checks the caller's access to the artifact,
  not just the evidence record. A declared statement is never upgraded to
  observed/verified without the owner record.
- **Event.** Uses one monotonic sequence per stream with actor and correlation
  IDs. A client follows from a cursor; a gap or truncated stream is explicit.
  Events exclude secret values; payloads by reference where possible.
- **Budget/reservation.** Reserve all applicable limits in one admission; commit
  actual usage once per attempt; release unused capacity; carry unresolved
  reservations across renewal; charge a late/duplicate receipt at most once.
- **Aperture.** Resolve at each requested operation and again at effect time
  from identity, project links, intent revision, assignment capability,
  approvals and target policy. A revision/revocation invalidates a prior
  resolution; record the resolved decision and inputs. The UI may request, never
  authorize.
- **Worker lease.** A lease is coordination, not authority. A takeover or
  reassignment increments a generation; the target refuses stale generations.
  Expiry permits reassignment only when the effect can be fenced, replayed or
  reconciled; otherwise the attempt stays `uncertain`.

---

## 4. Cross-cutting semantics

| Concern | What exists now | Missing Stage 1 guarantee | Useful anchor |
| --- | --- | --- | --- |
| **Persistence** | Task run dirs and goals are durable; evidence_store is durable and locked; mesh/grants are file-per-record; DevSession is one JSON doc; graph is a derived snapshot. | No single durable work record with intent/revision/attempt/decision/effect. No transaction spanning "record decision" and "apply effect." | `evidence_store._locked`/`_append_line` (`:1090,1079`); `goals.locked_store` (`:429`); `research.graph._save` (`:534`) |
| **Idempotency** | EvidenceStore has content-hash idempotency + explicit-key conflict. Mesh links/delegations have keys. Task run id collision refuses. Action tokens are one-time. | No idempotency for intent submission, work creation, attempts, decisions, effects, settings proposals or budget commits. No payload-sensitive conflict rule outside mesh/evidence. | `evidence_store.append_record` (`:1228-1242`); `mesh.link_request` (`:1416-1429`); `actions._consume_token` (`:523`) |
| **Replay / reconciliation** | ResearchGraph replays its event log and refuses a mismatch. EvidenceStore serves earlier complete records after a torn tail. JobTable reload reconciles process groups. | No replay of an outcome stream; no explicit uncertain-effect state; no "attempt was lost after dispatch" reconciliation; no duplicate late receipt handling. | `research.graph._validate_event_log` (`:478`); `daemon/jobs.JobTable._load` (`:465`); `resource_grants.release` (`:1863`) |
| **Cancellation / steering** | TaskRunner stops on internal conditions only; `actions` can stop services/tunnels/daemon; jobs `stop` kills a process group; mesh can release/revoke links; grants can release. | No intent cancel/steer operation, no stop-admission-first ordering, no signal-active-worker, no "cancellation pending until acknowledged/reconciled", no supersession of pending steps. | `tasks.TaskRunner._run` stop loop (`:2599-2803`); `daemon.jobs.JobTable.stop` (`:1764`); `mesh.lease_release` (`:2657`) |
| **Revision binding** | TaskSpec has version+digest and records the digest in the manifest. Code review pins base/head commits; repo review pins a working-tree fingerprint (`tree`). Goals `review_resolved` pins packet generated_at/head/sha. Edit txn pins a base hash. Settings apply re-reads the file. | No intent revision binding for work/attempt/decision/effect. No revocation of authority when revision changes. No "attempt accepted revision X" record. | `tasks.TaskSpec.digest` (`:325`); `code_review._packet` (`:1773`); `repo_review.finding_state` (`:2434`); `edit.txn.check_base` (`:156`) |
| **Access checks** | Actions token binds root/action/args/TTL. Resource grants require host approval flags. Mesh scope guard checks repo/mesh and live session. HostPolicy checks root/session/model/maps/paths/budgets. Secrets are scoped to harness/session. | No principals/ACL, no project-level access, no intersection of caller/target/worker/task permissions, no revocation invalidation, no artifact retrieval authorization. | `actions._load_token` (`:490`); `resource_grants._require_claim_approval` (`:1649`); `mesh._scope_guard` (`:393`); `model_api.policy.HostPolicy` (`:74`) |
| **Recovery** | Daemon restarts and reconciles jobs; task run dirs survive; goals/evidence/sessions/mesh survive; graph rebuilds from source. | No resume of a partially executed intent or DAG; no checkpoint; no safe restart step; no distinction "worker lost" vs "effect uncertain"; no stale attempt fence. | `daemon/jobs.py:23-31,97-105`; `tasks._finalize` (`:2806`); `evidence_store._recover_locked` (`:1122`) |

---

## 5. Graph persistence and query surfaces

| Store | Location / format | Write / locking | Query surface | Suitability for outcome records |
| --- | --- | --- | --- | --- |
| DevMemory snapshot | `<state>/graph.json`, version 2; nodes/edges/keys/side tables (`core/memory.py:1457-1473,2207`) | `write_snapshot` temp+replace (`:1732`); reingest rewrites whole snapshot; no cross-process lock observed **(inferred)** | `DevMemory.search`, `context`, `file`, `symbol`, `tests_for`, `impact`, `symbol_graph`, `brief`, `repo_map` (`:2288,3040,2816,2845,2919,3655,3663,3273,3594`); MCP `dev_*`; graph engine | **No.** Derived from source; a rebuild discards non-code records. Use only for context/expertise references, never as the canonical work store. |
| Edit journal | `<state>/journal.jsonl`, one JSON per edit (`core/memory.py:4172`) | Append; no lock/torn-tail recovery observed **(inferred)** | `journal_recent`/`journal_decisions` (`:4196,4223`); owners `who` (`core/owners.py:454`) | **Partial.** Good precedent for an append-only effect log; not typed enough for receipts/reconciliation. |
| Evidence store | `<state>/evidence/records.jsonl`, canonical JSON lines, record hash (`core/evidence_store.py:44-67,939`) | `fcntl` lock, temp+replace compaction, torn-tail recovery | `read_store`, `latest_record`, `store_summary` (`:1699,1778,1823`) | **Yes, as a component.** Reuse its durability; extend the record schema through a typed adapter. |
| Task run store | `<state>/tasks/<run-id>/` with `events.jsonl`, `manifest.json`, `artifacts.json`, `output.json` (`core/tasks.py:40-45,2899-2929`) | Append log; manifest/artifacts/output atomic JSON writes (`_write_json` `:1227`); run id collision refuses | `list_runs`, `show_run`, `TaskReport` (`:1252,1305`; `task_profile.build_task_report` `:2344`) | **Yes, as an attempt payload.** Keep run dir as execution trace; put canonical attempt state in the outcome store. |
| Goal store | `<state>/goals/goals.json`, one document (`core/goals.py:9-26,345`) | `fcntl` lock, temp+replace, thread cache | `read_goals`, `get_goal`, `list_goals`, `check_goals` (`:383,505,509,1674`); CLI/MCP goal tools | **No.** Reuse as criteria evaluator; do not store intent revisions there. |
| DevSession store | `<state>/sessions/<id>.json`, one document (`core/session.py:25-28,59`) | Atomic temp+replace; no lock observed **(inferred)** | `load_session`, `list_sessions`, `state`, `next_action` (`:884,910,455,493`) | **Partial.** Reuse the typed receipt graph and next-action table as a projection; do not make it the executor. |
| ResearchGraph | `<state>/research/<id>.json`, nodes/edges/events (`research/graph.py:360-408`) | Atomic temp+replace; no file lock observed; single-writer assumption | `record`, `amend`, `link`, `state`, `inspect`, `check`, `export` (`:544,580,619,669,672,704,924`) | **Pattern only.** Best existing example of event-sourced replay; domain-specific and not multi-writer. |
| Mesh / Ozma stores | `<state>/mesh/<mesh_id>/{sessions,leases,links,delegations,mail,audit}`, ozma outbox/inbox/claims under `<state>/mesh/ozma/` (`core/mesh.py:10-26`; `core/ozma_bridge.py:49-57`) | `flock` on `leases/.lock`; atomic JSON writes; scope guard | `peers`, `status`, lease/link/delegation/message APIs; `ozma_bridge` outbox/inbox/claims/flight | **Partial.** Reuse advisory liveness/leases and dedupe; do not treat as authority or canonical work. |
| Review packets | `<state>/reviews/repo_review-*.json` + `latest.json` (`core/repo_review.py:2930-2969`); code review packet returned/written by CLI | Atomic write; prune to newest N; no lock | `review_repo`, `load_packet`, `compare_packets`, `finding_state`, `brief` | **Yes, as gate evidence.** Reuse packets/stable finding ids; add a Decision record that references a packet id/hash. |
| Daemon job table | `<state>/jobs/jobs.json` + per-job logs (`daemon/jobs.py:154-155,527`) | Atomic table write under `cond`; process-group verification | `JobTable.start/wait/log/stop/events`; daemon ops; MCP job tools | **Partial.** Reuse job lifecycle for process execution; not an intent/work record. |
| Settings/config | `<root>/ozagia/config.toml`, user config, env (`core/settings.py:5-18,2126`) | Atomic write; re-read before replace; dry-run default | `schema/values/get/set_value/set_values`; CLI settings | **Yes, as configuration.** Reuse for Stage 1 execution policy; add proposal/version binding at service layer. |
| Graph engine | In-process `GraphEngine` sessions over DevMemory (`core/graph_engine.py:1127`) | Read-only; freshness guard withholds stale results (`:1332`) | `handle` (`:1182`) over `find`/`outline`/`expand`/`impact`/`tests`/`context`/`shape`/`fetch`; MCP `graph` tool | **No.** A read view; no writes. |

The important separation: **DevMemory is an index, not a record store.** The
delivery plan's "graph is the canonical record" needs a new durable record
layer; otherwise a source rebuild would erase work records. The new layer can
follow `ResearchGraph`'s event-sourced shape and `evidence_store`'s idempotency
and recovery, but it must not be rebuilt by `build_index`.

### Read-only assessment and task-class surfaces

| Surface | Entry point | Behaviour | Stage 1 use |
| --- | --- | --- | --- |
| Task preflight | `core/task_preflight.py:844` `build_preflight`; dimensions `:63`; providers `:595`; deferred list `:127`; action join `:2522` | Read-only six-dimension assessment (goal, graph, capabilities, environment, resources, tests) plus optional linked/research/actions dimensions. A green assessment requires every required dimension `True` and every fact `current`. It never runs a test, refreshes the graph, starts a daemon or writes a record. | **WRAP** as the pre-dispatch readiness check for an Attempt; store its report hash as evidence, not as a pass/fail verdict. |
| Local executor | `core/task_exec.py:169` `LocalExecutor`; policy `:83`; supported tools `:53` | Read-only `read`/`grep`/`glob`/`graph_search`/`graph_impact`/`test`; mutating/model tools return `blocked`; context root must match. | **REUSE** for read-only attempts; do not add edit/write to it. A real edit is a separate effect adapter through `edit/txn.py`. |
| Task profile adapters | `core/task_profile.py:1186` `adapt_task_run`, `:1259` `adapt_goal_record`, `:1400` `adapt_session_record`, `:1465` `adapt_daemon_evidence`, `:1637` `build_task_profile_from_records`, `:2344` `build_task_report` | Joins retained task/goal/test/session/daemon/eval records into a bounded span DAG with explicit edges, unknown values and evidence labels. Read-only; no model or test execution. | **WRAP** for Stage 1 measurement/progress; do not use it as the canonical attempt store. |
| Task library | `core/task_library.py:311` `load_library`, `:371` `catalog`, `:476` `copy_task`; `LibraryTask` `:76` | Built-in task classes plus repo overrides under `ozagia/tasks`; version/digest metadata; copy previews unless applied; no execution. | **REUSE** as the named workflow/task-class catalogue; an Intent/WorkItem can reference a library task id/version. |

---

## 6. Daemon, session, event and settings surfaces

| Surface | Entry point | What it owns | Gap for the plan |
| --- | --- | --- | --- |
| Daemon protocol | `daemon/protocol.py:1-160`; socket `:484`; dispatch `daemon/server.py:766` | `ping`, `tool`, `after`, `hello`, `stats`, `shutdown`, `session_event`, `profile_event`, `read_check`, `path_check`, `owner_check`, `who`, `working_set`, `goals`, `structural`, `hint`, `secrets`, `toolchain`, `tmp`, `mesh` | Request/response only; no submit/steer/decide/cancel; no event cursor for work; no authentication beyond local socket/plugin envelope **(inferred)**. |
| Jobs | `daemon/jobs.py:400` `JobTable` | Opt-in process jobs, logs, waits, cursors, events, stop, resource limits, shutdown/reload reconciliation | Process-level execution only. No assignment/attempt/effect semantics. Logs are not a work event stream. |
| Host sessions | `integrations/session_profile.py`; `session_profile_cli.py:89` | Metadata-only token/timing/tool lifecycle aggregates under `<state>/session_profiles` | Not a durable conversation/work record; no actor/principal; no event replay cursor for work. |
| Mesh | `core/mesh.py`; `mesh_cli.py:81`; daemon `mesh_op` (`daemon/server.py:2611`) | Session registration, liveness, advisory leases, links, delegations, mailbox, audit | Advisory only. No worker claim/dispatch/ack; no lease generation; no authority. |
| Ozma bridge | `core/ozma_mesh.py`; `core/ozma_bridge.py`; `mesh_cli.py` ozma commands | Identity mapping, typed local envelope, outbox/inbox/ack, claims, handoffs | Declared local schema; not an Ozma inference contract. No wire conformance. |
| Settings | `core/settings.py`; `settings_cli.py` | Typed registry, precedence, dry-run/apply, source reporting | No config version/proposal/rollback; no model-facing skill; no cross-setting constraints. |
| Config / paths | `config.py:226-235,245,262,271,755,804,1878` | `OZAGIA_STATE_DIR`, `.ozagia` state, `.ozagia.toml`, `ozagia/config.toml`, project config | No project identity/ACL/backend; one repo config is not a project record. |
| Jobs opt-in | `daemon/jobs.jobs_enabled` (`:285`), `[jobs] enabled` / `OZAGIA_JOBS` (`:10-16`) | Gate for `job_start` | Not a work-policy gate. |
| Mesh opt-in | `core/mesh._mesh_values`/`mesh_enabled` (`:274,294`), `[mesh] id/enabled/model` | Gate for mesh coordination | Not an execution authorization. |

---

## 7. MCP, CLI and library entry points

| Surface | Entry | Coverage |
| --- | --- | --- |
| MCP | `integrations/mcp/server.py:1570` `DevMemoryServer`; `dev_context` (`:87`), `dev_query` (`:253`), `dev_session_start/record/state/next/finish/list` (`:533-581`), goal tools (`:1363-1451`), `dev_mesh_status`/`dev_mesh_inbox` (`:989,1003`), `dev_resource_grants` (`:1340`), `dev_review_repo` (`:1137`), `dev_deep_review` (`:1164`), `dev_maps` (`:1458`) | Graph/session/goal/mesh/review access, plus the existing `dev_edit`/`dev_span_edit`/`dev_key_edit`/`dev_replace`/`dev_refresh`/`dev_secrets` tools. No task runner, intent, attempt, decision or effect operation. |
| Daemon ops | `daemon/server.py:766` | Graph/tool interception, working set, goals, mesh, secrets, jobs, temp space. No work lifecycle. |
| CLI — tasks | `tasks_cli.py:49` (`validate`, `run`, `report`); `main:149` | Task preview/run/evidence report. `run` is preview unless `--allow-exec`; executor injected or `--executor local`. |
| CLI — goals | `goals_cli.py:28` (`add/list/show/check/claim/drop/block/from-review`) | Goal lifecycle and criteria evaluation. |
| CLI — task preflight | `task_preflight_cli.py:30` | Read-only preflight report. |
| CLI — task library | `task_library_cli.py:164` | Browse/copy task classes; copy previews unless `--apply`. |
| CLI — mesh | `mesh_cli.py:81` | Mesh sessions/links/delegations/leases/messages/ozma. |
| CLI — actions | `actions_cli.py:176` | Preview/apply operational actions. |
| CLI — reviews | `code_review_cli.py:72`; `cli.py:1805` review-repo; `cli.py:1839` deep-review | Deterministic review packets. |
| Library | `core/tasks.py`, `core/goals.py`, `core/session.py`, `core/evidence*.py`, `core/actions.py`, `core/mesh.py`, `core/code_review.py`, `core/repo_review.py`, `core/settings.py` | Direct Python calls; no shared service facade. Stage 1 should add one. |

Notes:

- `TaskRunner.run` is preview by default and refuses to execute without an
  injected executor (`core/tasks.py:2454-2542`). The built-in `LocalExecutor`
  is read-only and blocks mutating/model tools (`core/task_exec.py:53-58,169`).
  This is a strong safety default for Stage 1: a real edit effect must be an
  explicit adapter, not an implicit runner capability.
- The MCP server and the daemon are separate surfaces with overlapping graph
  tools; neither is a work service. Stage 1 should expose one service module to
  both rather than adding intent logic to each.

---

## 8. Tests that pin behavior

| Surface | Tests (examples) | What they pin |
| --- | --- | --- |
| Task spec/runner | `tests/test_tasks.py:298,326,354,369,403,425,543,579,948,982,1014,1051` | Preview/refusal, injected executor, budget/step/resource stops, no-progress/repeated-state, approval, schema gate, policy gate, graph freshness, grant claim/release order. |
| Local executor | `tests/test_task_exec.py` (17 tests) | Read-only tools, root/scope containment, graph freshness, blocked mutate/model tools, planned pytest, no content leakage. |
| Task profile/report | `tests/test_task_profile.py` | Span/edge integrity, unknown values, deterministic hashing, report coverage. |
| Task preflight | `tests/test_task_preflight.py` | Read-only dimensions, green only when all required facts current, no durable writes, stale/missing evidence stays unknown. |
| Goals | `tests/test_goals.py:832,890,962,1031,1135,1189,1212,1291` | Claim by harness/session, scope globs, daemon hints, captured test runs, brief caps, atomic/locked store, MCP goal tools. |
| DevSession | `tests/test_session.py:39,77,104,165,189,270` | Start/plan/step/attempt/edit/verification sequence, failed verification diagnosis, max attempts, persistence reload, honest unknown transition, human gate. |
| Evidence store | `tests/test_evidence_store.py:351,380,428,544,568,610` | Idempotent repeat, conflict, torn-tail recovery, malformed interior fails closed, retention, concurrent writers, green assessment requires all dimensions. |
| Actions | `tests/test_actions.py:215,238,394,476,941` | Preview read-only + exact operation/token, one-time/expiry/token mismatch, bounded audit without argv/secrets, no real process/network, secret canary. |
| Action readiness | `tests/test_action_readiness.py:114,154,172,197,234,390` | Available only when observed prerequisites hold; absent/blocked/unknown; live gate; stale identity; unresolved secret; unreadable config. |
| Resource grants | `tests/test_resource_grants.py:600,633,720,802,908,948` | Claim only when ready/approved, private ledger, double-claim refusal, expiry/orphans, release preview/apply, concurrent one-winner, CLI/MCP. |
| Mesh | `tests/test_mesh.py`; `tests/test_mesh_links.py:195,264,331,355,515` | Idempotent registration, liveness never guessed dead, bounded/redacted messages, link idempotency/conflict, super role confers no lease/mailbox authority, delegation scope, merge-on-behalf is a reference only. |
| Ozma | `tests/test_ozma_mesh.py`; `tests/test_ozma_bridge.py` | Config validation, identity/readiness, dedupe, outbox/inbox/ack, claims, no network with unavailable shim. |
| Code review | `tests/test_code_review.py:386,479,588,857,898,1196` | Packet without/fresh/stale graph, test plan without execution, injected reviewer is unverified, multi-reviewer agreement annotated not truth, injected test result. |
| Repo review | `tests/test_repo_review.py:57,339,562,586` | Packet shape/stable ids, compare round trip, suggested goals satisfy goal contract, review_resolved refused in a section. |
| Deep review | `tests/test_deep_review*.py` | Stable finding ids, contract/baseline validation, report artifacts, write safety, model-pass closed gate. |
| Graph engine | `tests/test_graph_engine.py:222,253,353,499,552` | Minimal hello, bounded sessions, paging, stable hashes, op-scoped known, strict budget. |
| Settings | `tests/test_settings.py:123,213,267,292,320,437` | Five-layer precedence, dry-run default, idempotent set, comment preservation, type/range errors. |
| Daemon jobs | `tests/test_daemon_jobs.py:199,290,406,423,446,610,1320` | Start/wait/log, deadline, eviction, shutdown/detach, job-end events, admission refusal, load reconciliation. |

The two test runs above passed (191 + 250). They cover the main surfaces cited
in this report but not the later-stage product areas (messaging, calendar,
telemetry, capacity), which have no implementation.

---

## 9. Stage 1 gap list, prioritized

### P0 — required for the first vertical slice

| ID | Gap | Concrete Stage 1 deliverable | Acceptance check |
| --- | --- | --- | --- |
| G1 | No canonical durable outcome store | One typed record store (journal or graph namespace) for Intent, IntentRevision, WorkItem, Attempt, Assignment, Decision, EffectReceipt, Event, BudgetReservation, WorkerLease. Prefer one module and one state layout; reuse `evidence_store` idempotency/recovery and `ResearchGraph` event replay. | Append same record twice = one stored record; same key/different payload = conflict; kill during append = recover earlier records; restart reloads all records. |
| G2 | No intent/revision binding | Intent + immutable revisions; WorkItem/Attempt/Decision carry the accepted revision hash. Steering appends a revision and supersedes queued work. | New revision cancels a queued attempt; an old attempt cannot finalize; the operator sees the original request and what changed. |
| G3 | No effective aperture at effect time | Resolver composing `model_api.HostPolicy`, `resource_grants`, `actions` token and mesh scope; record the resolved decision and policy/revision. | Stale revision, revoked grant, wrong project or stale base refuses the effect; UI request cannot bypass the resolver. |
| G4 | No effect receipt/reconciliation | Effect journal with `effect_key`, `attempt_id`, `lease_generation`, `target`, `expected_base`, state, observed receipt. Use `edit/txn.py` for the write. | Replay same effect key does not duplicate; stale base refuses; crash after write leaves `uncertain` until reconciled; no silent retry. |
| G5 | No event stream/cursor/replay for work | One event stream with stable sequence, actor, correlation/causation and a cursor; derive progress from it. Reuse task `seq` and job cursor semantics. | Follow from cursor returns every event once; restart resumes; truncated/gapped stream is explicit. |
| G6 | No outcome view contract | Versioned list/status projection: outcome, verified/pending evidence, current work, blocker, available operations, evidence refs. Reuse `view.py`/apertures for generic rendering. | List matches records after restart; unknown node/operation renders generically; UI request goes through the service/aperture path. |

### P1 — needed for the first real workflow, but after G1–G6

| ID | Gap | Deliverable |
| --- | --- | --- |
| G7 | Decision/gate not bound to exact action/base/policy | Gate/Decision record with action digest, target, expected base, policy version, expiry and re-evaluation. Reuse action-token binding and edit base hash. |
| G8 | No budget reservation | Reservation ledger with atomic multi-limit admission, commit-once, release-unused, carryover and late-receipt settlement. Reuse `_BudgetLedger` arithmetic. |
| G9 | No assignment/execution lease generation | Assignment record + execution lease with generation and fencing; wrap mesh lease for local liveness. |
| G10 | No project link/provisional grouping | ProjectLink record with provenance, primary preference and independent access; no ACL broadening. |
| G11 | No retry/compensation semantics | Explicit attempt retry creation, error/compensation path, stop-vs-timeout-vs-revocation distinction. Reuse `critical_path` topology and task stop conditions. |

### P2 — later stages; do not let them shape the Stage 1 store

| ID | Gap | Stage |
| --- | --- | --- |
| G12 | Project backend mapping/revision preconditions | Stage 2 |
| G13 | Expertise graph/version pins/activation/revocation | Stage 3 |
| G14 | Allowance estimator/forecast/outcome measurement | Stage 3 |
| G15 | Messaging, calendar, Home activity graph | Stage 4 |
| G16 | Capacity provider and portable worker recovery | Stage 5–6 |
| G17 | Ozma inference wire contract/conformance | Stage 5 |
| G18 | Telemetry/pooled estimates | Stage 7 |

### Do not build in Stage 1

- A second DAG engine or a second workflow state model. `critical_path.Dag`
  is the validation/topology primitive; the persisted process/DAG-run records
  should be the only new workflow state.
- A second evidence journal. `evidence_store` already has the hard guarantees
  (idempotency, conflict, recovery, retention).
- A second goals/criteria engine. `goals` already evaluates test/symbol/file/
  ops/review criteria against the graph, working tree and test record.
- A second mesh or worker registry. `mesh` already owns session registration,
  liveness, advisory leases, links and delegations.
- A second action preview/token/audit mechanism. `actions` already has
  preview-first, one-time token, precondition recheck and bounded audit.
- A second settings registry. `settings` already has the typed registry and
  precedence.
- A second review packet/finding identity. `code_review` and `repo_review`
  already produce stable ids and coverage/omission envelopes.
- A permission model hidden in `apertures.py`. Apertures are view projections;
  authorization must be a separate resolver that calls existing checks.

---

## 10. Do not duplicate

The following existing objects are the canonical owner of their concern. Stage 1
should extend or wrap them, not create a parallel model:

1. **Graph/index:** `memory.DevMemory` + `graph_engine.GraphEngine` are the
   code-knowledge index and read-only query view. Do not create a second code
   graph. If outcome records need a graph, put them in a distinct durable
   namespace/store; do not mix them into the rebuildable snapshot.
2. **Event-sourced pattern:** `research.graph.ResearchGraph` is the only
   append-only, replay-validated graph today. Reuse its event/replay *pattern*,
   not its research node vocabulary, for the outcome store.
3. **Task execution:** `tasks.TaskRunner` is the bounded executor. Do not write
   another step runner in the outcome module; wrap it as an attempt executor.
4. **Evidence:** `evidence_store` is the durable evidence journal;
   `evidence.collect_evidence` is the read-only join. Do not add a second
   evidence file format.
5. **Criteria:** `goals` is the criteria evaluator. Do not add a second
   criterion language or status store.
6. **Session receipts:** `session.DevSession` is the typed receipt graph and
   next-action table. Do not create a competing session state machine; project
   its nodes into the outcome view or extend it deliberately.
7. **Mesh:** `mesh` owns session identity, liveness, advisory leases, links,
   delegations and messages. Do not add a second worker registry or mailbox.
8. **Ozma:** `ozma_mesh`/`ozma_bridge` own the local bridge. Do not add a
   second envelope/dedupe store; add the inference contract as a typed
   operation over the existing transport or a new explicitly-versioned module.
9. **Actions:** `actions` owns preview/token/apply/audit for operational
   effects. Do not add another confirmation-token mechanism; reuse its token
   binding and audit envelope.
10. **Grants:** `resource_grants` + `grant_lifecycle` own resource entitlement
    and claim/release. Do not put provider/provisioning calls into them; wrap
    them for assignment reservations.
11. **Settings:** `settings` owns the typed registry and config writes. Do not
    add a second config file format or env map.
12. **Reviews:** `code_review`/`repo_review` own review packets, stable finding
    ids and coverage/omission reporting. Do not create a second finding id or
    packet schema.
13. **Aperture appearance vs authority:** `apertures.py` owns view projection;
    `model_api.HostPolicy`, `mesh._scope_guard`, `actions` and
    `resource_grants` own different checks. Do not call an aperture a
    permission, and do not create a second permission store to avoid composing
    these.

---

## 11. Open questions and inferred limits

1. **Ocelot substrate reuse is not implemented here.** `integrations/ocelot`
   resolves/installs an external checkout and edits its workspace boot files
   (`integrations/ocelot/workspace.py:35-43`,
   `integrations/ocelot/workspace_boot.py:1-18`). A
   repository-wide search found no `substrate_rs` graph-store binding in
   `src/ozagia`. The delivery plan's "reuse Ocelot's Rust-backed `substrate_rs`
   graph storage" therefore needs either an external integration task or an
   explicit fallback to a local durable store in Stage 1. **(inferred from
   source and docs; not tested against an Ocelot checkout.)**
2. **The current graph snapshot is not transactional with the task/goal/
   evidence stores.** `write_snapshot` is atomic per file but the graph,
   journal, goals, sessions and evidence are separate stores. There is no
   cross-store transaction. **(inferred from the absence of a shared lock/
   transaction module.)**
3. **`TaskRunner` does not enforce resources or observe the executor.** Its
   own `_not_assessed` says tokens/cost/memory/disk/pids are self-reported and
   network/executor internals are not observed (`core/tasks.py:2935-2968`).
   Stage 1 must not treat a green task run as proof of resource or effect
   enforcement.
4. **`DevSession` is a receipt graph, not a runtime.** Its module docstring is
   explicit (`core/session.py:1-6`). A Stage 1 attempt must not rely on it for
   dispatch, leases or effect enforcement.
5. **Mesh is advisory by construction.** `core/mesh.py:1-8` says leases are
   advisory and a cooperative peer can ignore them. Do not use a mesh lease as
   an execution grant.
6. **Review packets are evidence, not truth.** `code_review.py:9-18` and
   `repo_review.py:20-22` state `truth_assessed` is always false. A Stage 1
   satisfaction rule must require recorded evidence, not a packet or a worker
   success response.
7. **No network/live verification was performed.** The report does not claim
   live Ozma, remote worker, provider, messaging or calendar behavior. Those
   are absent or declared-only in this checkout.
