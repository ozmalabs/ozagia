# Intent-to-outcomes Stage 0 baseline: selected Dev Manager workflow and architecture decision

Date: 2026-11-27. Status: Stage 0 baseline, read-only. This report selects one
change-and-review workflow, records its usual-process baseline and pins the
revisions and interfaces seen at analysis time. It does not run Dev Manager,
Ocelot, Ozma, Docker or any live service, and it makes no target-repository
writes, network calls, live model calls or commits.

Evidence was read from the checkouts and from read-only SQLite queries against
the live review database. The live review service was running during this
analysis; it was observed, not exercised.

## 0. Verdict

1. **Selected workflow:** Dev Manager review-sandbox task 262, "Add a module
   docstring to app/base.py". It is the clearest complete change-and-review
   record: a passing validation recipe, an operator verdict and decision, a
   branch delivery and a draft PR. A second accepted record, task 259,
   delivered a branch only; task 262 was chosen because it is a code-file
   change with an explicit PR-delivery trial. It is small and its state machine
   is the same one used for production work.
2. **Production analogue, not the selected workflow:** task 257, "document the
   replay window in replay.rs", is real Ozma work with a measured agent run and
   a passing `cargo test -p ozma-crypto --locked` recipe, but it has no review
   verdict or decision. It is used below for timing, recovery and
   revision-binding evidence.
3. **Architecture:** recommend **graph-native workflow** over a separate DAG
   runner, with a narrow internal scheduler/evaluator over typed graph records.
   Do not introduce a second canonical workflow store. Treat Ocelot's
   `substrate_rs` as an optional store implementation to prove behind a store
   boundary, not as a Stage 1 dependency.
4. **Main gaps:** Ocelot has a proven typed graph, snapshot persistence, CoW
   session checkpoints, and an opt-in request-scoped at-sink action gate
   (authorization, same-epoch stop/revocation, sink receipt, no retry after a
   failed sink) for its own world/reflex actions. It has no general host
   authorization aperture, no external Git/OpenCode/PR effect journal, no
   grant/intent-level revocation and no workflow executor. Ozagia has reusable
   contracts, goals, session, task, evidence/evidence-store, action, grant,
   critical-path and view modules, but no durable effect journal or
   intent-revision binding.
5. **Blocking evidence gap:** the running Dev Manager source is an unpinned
   overlay. The live `app-src` differs from `MANIFEST.sha256` in 40 of 227 app
   files (for example `app/main.py` and `app/services/review_decision.py`), and
   the checkout HEAD is separately dirty. Exact deployed-code reproduction is
   therefore not established.

## 1. Selected workflow

### 1.1 Identity and source

| Item | Value |
| --- | --- |
| Project | `sandbox` — "Review sandbox", a throwaway repository used to exercise the review build end to end |
| Task | `task_queue.id = 262`, "Add a module docstring to app/base.py" |
| Task description | "PR delivery trial: one-line docstring, no behaviour change." |
| Contract | `local_task_contract_revisions` row `task_id=262, revision=1`, created `2026-11-12T20:31:21.714035+00:00` |
| Repository | `/applications/data/ozma-review/repos/sandbox` |
| Base commit | `4143f1bbc9f6bd65a9eaa41089e377d7b97dbca1` |
| Attempt | `a2c6e9af-a722-4de2-b818-2d5678b0f011` |
| Harness | `opencode` (the attempt record does not store a version; current observed OpenCode is v2.3.0), profile `qwen-local` (current profile config: provider `vllm`, model `qwen3.8-27b`; the attempt record stores only the profile id) |
| Review verdict | `0282dca0-6169-41f5-bad5-975158a38503`, `pass`, reviewer `operator` |
| Review decision | `ed522a6e-9434-40f2-baea-a7f874a4cfab`, `accept`, reviewer `operator` |
| Delivery | Branch `devmgr/task-262-a2c6e9af` at `0565adac2c2ba195f99958073d84b38badb602bc`; draft PR `https://github.com/hardwarebob/devmgr-sandbox/pull/1` |

The task contract records the objective and acceptance criteria explicitly:

- Objective: "Add a one-line module docstring at the top of `app/base.py` that
  says what the module provides. Do not change any code."
- Acceptance criterion 1: "`app/base.py` starts with a one-line module
  docstring".
- Acceptance criterion 2: "`base()` is unchanged and still returns 1".
- Allowed path: `app/base.py`.
- Contract validation command: `git diff --check`.
- Timeout: 1800 seconds; maximum attempts: 2.

The frozen project snapshot used by the attempt (project revision 5) contained
one component recipe named `syntax`:

```text
python3 -c "import ast,sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]" app/base.py
```

and the frozen review policy:

```text
low:    [gates, human]
normal: [gates, human]
high:   [gates, human, human-second-look, agent:qwen-local]
```

The deliverable is a 209-byte tracked diff for one file. The workspace also
contains five valid `.opencode/goals/...` harness-state paths, which finalization
classifies separately from the deliverable (`harness_state_paths`); the
scope-violation count is zero. The diff is:

```diff
+"""Base module providing the base() helper function."""
+
+
 def base():
     return 1
```

Diff SHA-256: `3c43bfa565e6fc6664b32cfa405347c535c33ef148aac9b740ec0ed346f5ef6f`.
Head SHA-256: `8310f675620b6ecb9bce73063b177d0c68017b383a90f754b3c5f66951873999`.

### 1.2 Verification recipe and review artifact

The attempt has two layers of verification:

1. **Contract validation** (`git diff --check`) run by
   `app/services/validation_runner.py` through the allowlist. It is not a
   sandbox; it can read host files and open network connections. The result
   recorded `validation.status = passed`.
2. **Component recipe** `syntax`, run by `app/services/recipe_runner.py` after
   finalization. It parses `app/base.py` with Python's `ast`. The result
   recorded `passed`, exit code 0, recipe duration 0.019 seconds (0.09 seconds
   between the start/finish events, including orchestration). Recipe execution
   has the same limitation: the environment is scrubbed, but it is not a
   sandbox.

The review artifact consists of:

- the bounded diff and its SHA-256, recorded in
  `execution_attempts.result_json.review_evidence`;
- the changed-path list and scope check (`scope_violation_count = 0`);
- the validation and recipe results;
- the operator verdict `pass` with note "Docstring only; base() unchanged. PR
  delivery trial.";
- the decision `accept` with note "PR trial", bound to the same
  `diff_sha256`, `head_sha256` and `finalized_at_ms`;
- the branch and draft-PR delivery records.

The mechanical gate is computed in
`app/services/review_decision.py:mechanical_gate` and includes: attempt is
`ready_for_review`, finalization state `passed`, diff and head hashes present,
diff not truncated or omitted, scope verified with zero violations, validation
`passed`, and a non-empty change. `submit_verdict` and `submit_decision` bind
the decision to the evidence; `is_stale` refuses a verdict or decision whose
binding no longer matches. This is the strongest existing revision-binding
mechanism in the baseline.

### 1.3 The meaningful steering point

The selected workflow has one real judgment point: **the human review gate**.

At that point the operator could see a passing syntax parse and `git diff
--check`, but no behavioral test that `base()` still returns 1. The operator
could:

1. accept the docstring-only diff and deliver it (the recorded choice);
2. require a behavioral test or a stronger contract before accepting, which
   would create new evidence and delay delivery;
3. reject or return it for a different change.

Continuing to accept produces a branch and a draft PR with only syntax
evidence. Requiring a behavior check would change the verification recipe and
the decision frame. An observation that should change the choice is a diff
touching `base()`, a changed base revision, a failing syntax/parse result, or a
reviewer finding that the docstring is not accurate.

This is a meaningful steering point because it is exactly the gate Stage 1 must
make durable and auditable. The reviewer's contemporaneous internal belief was
not recorded beyond the note; the three options above are inferred from the
contract, diff and note, and should be recorded explicitly in Stage 1.

### 1.4 Why this workflow is representative

It exercises the complete Dev Manager state machine, not a mock:

- contract revision and frozen project policy;
- exact-commit workspace creation;
- OpenCode session creation and prompt submission;
- observation and finalization;
- allowlisted validation plus a component recipe;
- risk-based review policy and evidence-bound operator decision;
- branch export and draft-PR delivery.

It is low-risk and has one deliverable file, so it isolates workflow behavior
from domain complexity. It is one of two accepted records in the current review
database and the only one with both a branch delivery and a draft-PR delivery,
which makes it a usable baseline for the Stage 1 vertical slice.

**Limit:** the repository is a throwaway review sandbox. It is a real change
and a real PR, but it is not production project work. The production analogue
in §1.5 is real work but stops before review. Stage 1 should re-run the same
shape against a real checkout in an isolated workspace before claiming
production representativeness.

### 1.5 Production analogue: task 257 (not the selected baseline)

Task 257, "ozma-crypto: document the replay window in replay.rs", is a real
comment-only change with a clear recipe:

- Objective: "Document the replay window in `ozma-crypto/src/replay.rs` without
  changing behaviour."
- Acceptance criteria: "only the allowed paths change"; "the component recipes
  pass".
- Contract validation: `git diff --check`.
- Component recipe: `cargo test -p ozma-crypto --locked`
  (recipe id `cargo-test-crypto`).
- Attempt: `1c3290c6-cb03-4ed6-92b6-1259164beaa3`; the preceding attempt
  `bbfe1bb9-a22a-490d-a060-58faac6c7e8a` ended `needs_reconciliation` with no
  events.

The recipe passed; `REVIEW_PROCESS_PLAN.md` records that the relevant file was
unchanged at current project HEAD and that the cargo recipe passed 26 tests. No
review verdict or decision exists for this attempt. The current Ozma checkout
`ecb763ef` is a descendant of the task's base commit `79b41c73`, and
`git diff 79b41c73..HEAD -- ozma-crypto/src/replay.rs` is empty, so the
documentation target has not drifted. The review authority is still missing.

This analogue shows the real production path and its current stopping point:
validation can pass while review and delivery remain pending. Keep task 262 as
the Stage 0 workflow baseline because it has the review artifact; use task 257
as the production applicability check and collect its review as the first
Stage 1 integration run.

| Candidate | Change | Recipe | Review | Delivery | Role |
| --- | --- | --- | --- | --- | --- |
| 262 | `app/base.py` docstring | `syntax` passed | Operator pass + accept | Branch + draft PR | **Selected** |
| 259 | `docs/NOTES.md` | `syntax` passed | Operator pass + accept | Branch only | Comparable accepted case |
| 257 | `ozma-crypto/src/replay.rs` comment | `cargo-test-crypto` passed | None | None | Production analogue |
| 261 | `docs/FILES.md` | `syntax` passed | Operator pass, no decision | None | Negative/evidence case |

## 2. Pinned revisions and environment

| Component | Pinned revision / version | Evidence and caveat |
| --- | --- | --- |
| Ozagia | `eb9bcb0b3b0d665a170221d19461d6a351c51f14` | `git rev-parse HEAD`; the three Stage 0 planning/spec documents were untracked at analysis start, and this report is a new untracked file. Package `0.1.5.dev0`; Python 3.14.8. |
| Ocelot | `b7fda1c9dcc7a0c86f259f4083f66f65260605aa` | `git rev-parse HEAD`; 316 dirty entries. `substrate_rs` engine SHA-256 `3ceb5431062c788528f566579a176567082d916ab4c713d5669624b0c9f80983` from `runners/dsl/python/substrate_rs/_native.build.json`; `_native.abi3.so` is 11,608,592 bytes. |
| Ozma | `ecb763ef01bd878184075daae285499537284f83` | `git rev-parse HEAD`; 33 dirty entries. |
| Dev Manager checkout | `0e7877a71900a18d0d5e5f5daf7f70878871e42c` | `/home/matt/ozmadev/internal` HEAD; 111 dirty entries in the worktree. This is the source checkout, not the running service. |
| Dev Manager live review source | `SOURCE_HEAD.txt` = `fb12fbdadbf566a5c8398ba97048e002f29d9fe9` (2026-11-12 snapshot) | `/applications/data/ozma-review/app-src`; not a Git checkout. 40 of 227 `app/` files differ from `MANIFEST.sha256`; examples: `app/main.py` manifest `f7113aed...` vs live `c8c4e14d...`; `app/services/review_decision.py` manifest `cfbdf0c6...` vs live `d1848837...`. **Exact deployed revision is not pinned.** |
| OpenCode | current observed `v2.3.0` | `opencode --version`; smoke record `OZAGIA_OPENCODE_SMOKE_2026-11-27.md` also records v2.3.0. The 2026-11-12 task 262 record stores only `harness=opencode`, not a version. |
| Node | `v20.5.1` | `node --version` on the host. |
| Python, Ozagia | `3.14.8` | `/home/matt/ozmadev/ozagia/.venv/bin/python --version`. |
| Python, live review service | `3.12.15` | `/applications/data/ozma-review/venv/bin/python --version`. |
| Worker profile, selected task | `qwen-local`; current profile config `vllm` / `qwen3.8-27b` | `execution_profiles` row `qwen-local` (current config; the attempt stores only the profile id); attempt usage evidence provenance `provider_reported_via_opencode`, cost 0.0 for the local profile. |

The selected task ran on 2026-11-12 against the then-current review service and
source snapshot. The current checkout has newer review defaults (new projects
use agent review at low/normal risk; see `README.md` "Modernisation in
progress"). The Stage 1 consumer must not assume the historical policy or code.

## 3. Baseline of the usual process

All times below are **measured** from SQLite event rows unless marked
**estimated** or **inferred**. The review DB records event timestamps, not UI
interaction timestamps.

| Baseline item | Status |
| --- | --- |
| Task creation → prompt, agent run, finalize, recipe, review, branch/PR | Measured from event/row timestamps |
| Operator task-authoring time | Not recorded |
| Interruptions and retries | Measured for the selected task (none); task 257 comparison measured |
| Context-regain effort | Not measured; low-effort estimate only |
| Memory / process count for the integrated workflow | Not measured; ambient observations only |
| Downstream use, merge or outcome value | Not recorded |

### 3.1 Preparation

The usual preparation for task 262 was:

1. Create the task and its contract revision; freeze the project snapshot,
   allowed paths, validation commands, recipes, review policy and delivery
   policy (`local_task_contract_revisions`; `app/task_contract_store.py`).
2. Resolve the repository at the exact base commit and create a detached
   worktree (`app/services/workspaces.py:create_workspace`).
3. Create an OpenCode session in that workspace and submit the prompt
   (`execution_engine`, attempt `a2c6e9af...`, event `prompt_submitted`).
4. Wait for the agent to finish; observe and finalize
   (`app/services/review_finalizer.py:finalize_attempt`).
5. Run the component recipe (`app/services/recipe_runner.py`).
6. Review and decide (`app/services/review_decision.py`).
7. Deliver by branch and optionally PR (`app/services/delivery.py`).

Observed setup time from task creation to prompt submission: **1.344 seconds**
(20:31:22.123 → 20:31:23.468). This is machine/setup time, not operator
preparation time; the operator's task-authoring time is not recorded.

### 3.2 Inputs

| Input | Observed value |
| --- | --- |
| Task title / description | "Add a module docstring to app/base.py"; "PR delivery trial: one-line docstring, no behaviour change." |
| Contract revision | 1 |
| Base commit | `4143f1bbc9f6bd65a9eaa41089e377d7b97dbca1` |
| Allowed paths | `app/base.py` |
| Contract validation | `git diff --check` |
| Component recipe | `syntax` (`python3 -c ... app/base.py`) |
| Timeout / attempts | 1800 s / 2 |
| Prompt | SHA-256 `98ab1cadf8e4e9b7019b59ec15fe49abcbf1e74c48b3b22d60c7d84b0be66046`; 395 characters; prompt text not stored |
| Context pack | None recorded. Unlike later attempts, this result has no `context_sha256` and no capability pack. It is a low-context, single-file task. |
| Model | Profile `qwen-local`; current profile config is `vllm` / `qwen3.8-27b`. The attempt result stores the selected profile id, not the provider/model string. |
| Usage | 4,671 input tokens, 331 output tokens, 25,088 cache-read tokens, cost 0.0 (local; provider-reported via OpenCode) |

### 3.3 Outcome criteria and observed result

| Criterion | Evidence | Result |
| --- | --- | --- |
| `app/base.py` starts with a one-line module docstring | Diff shows `+"""Base module providing the base() helper function."""`; changed path is `app/base.py` | Pass |
| `base()` unchanged and still returns 1 | Operator note "Docstring only; base() unchanged"; diff does not touch the function body | Pass by inspection; **not independently executed** |
| Only the allowed deliverable path changed | Tracked diff path `app/base.py`; five valid `.opencode/goals/...` harness-state paths classified separately; `scope_violation_count = 0` | Pass |
| `git diff --check` | `validation.status = passed` | Pass |
| Component recipe `syntax` | `recipe_result` status `passed`, exit code 0 | Pass |
| Review gate | Verdict `pass`; decision `accept`; evidence binding current | Pass |
| Delivery | Branch exported at `0565ada...`; draft PR #1 open | Branch created; PR open/draft, not merged; no CI checks recorded (`checks_total = 0`, `state = none`) |

**Outcome quality:** the artifact is a real draft PR, but the baseline does not
record downstream use, merge or review of the PR. Anticipated usefulness was
not measured; there is no evidence that the PR was merged or used. The
behavioral criterion relies on human inspection, not an independent check. This
is the exact weakness Stage 1's gate/evidence contract should expose.

### 3.4 Timing evidence

Measured event timeline (UTC, 2026-11-12):

| Step | Timestamp | Delta |
| --- | --- | --- |
| Task created | 20:31:22.123 | — |
| Workspace created | 20:31:22.396 | +0.273 s |
| OpenCode session created | 20:31:23.058 | +0.662 s |
| Prompt submitted | 20:31:23.468 | +0.410 s |
| Agent reconciled idle/succeeded | 20:31:30.701 | +7.233 s agent run |
| Attempt finalized | 20:31:45.218 | +14.517 s gap to finalize (unattributed) |
| Recipe validation started | 20:32:27.415 | +42.197 s gap to recipe (unattributed) |
| Recipe passed | 20:32:27.505 | +0.090 s |
| Review verdict | 20:32:39.153 | +11.648 s review |
| Decision accept | 20:32:39.377 | +0.224 s |
| Branch exported | 20:32:39.799 | +0.422 s |
| Draft PR opened | 20:32:50.349 | +10.550 s |

**Task creation to branch:** 77.68 seconds.
**Task creation to draft PR:** 88.23 seconds.
**Agent work:** 7.23 seconds.
**Unattributed gaps:** 14.52 s after the agent, 42.20 s before the recipe,
11.65 s before the verdict. These are not machine execution time, but the
review DB has no UI/click timestamps, so they cannot be split into operator
latency and scheduler/worker delay.

The sub-0.1-second recipe is misleading as an effort measure: the recipe itself
is trivial, and the 42-second gap before it is not accounted for. A comparable
production task (257) took 2 minutes 02 seconds of agent time and 15.2 seconds
of cargo test, with no review.

### 3.5 Interruptions and context-regain effort

- **Interruptions:** none recorded in task 262. There was no cancellation,
  retry or reconciliation. There were four recorded attention points:
  finalize, start recipe validation, review, and delivery; whether each was a
  human click or a scheduler action is not recorded.
- **Review point:** verdict at 20:32:39.153; accept at 20:32:39.377; branch at
  20:32:39.799.
- **Context-regain effort:** **not measured.** No UI-open, focus-change or
  interruption timestamps exist. The review note and the 209-byte single-file
  diff suggest low effort, but that is an **estimate**, not a measurement.
  Exact missing measurement: time from the operator's previous context to the
  point where they can state the diff, the validation status and the next
  action; and the number of re-opened artifacts/links needed.
- **Comparable recovery evidence (task 257):** the first attempt was
  `needs_reconciliation` with no events; a second attempt was dispatched
  13 minutes 46 seconds later. This is measured recovery/retry cost, not a
  selected-workflow interruption.

### 3.6 Comparable case

Task 261, "Add docs/FILES.md listing the repository files", is a comparable
docs-only change in the same sandbox repository. Attempt
`403eaed9-d9e7-45f4-b04e-0692c24fda87` has an operator `pass` verdict but
remained `ready_for_review` with no decision; its recorded review evidence
includes untracked `.opencode/goals/...` paths and a zero-byte tracked diff.
This shows that a passing review verdict alone is not a terminal outcome and
that harness-state/untracked evidence can block acceptance. It is a useful
negative fixture for Stage 1's generic workspace-evidence rules, but the
reason for the missing decision is not recorded, so no causal claim is made.

## 4. Ownership map

The pilot must have one owner per lifecycle decision. The proposal below keeps
Dev Manager authoritative for the pilot's execution, validation, review and
delivery while Ozagia becomes authoritative for the new intent and workflow
graph records.

| Concern | Baseline owner | Pilot owner | What is recorded / enforced |
| --- | --- | --- | --- |
| Intent lifecycle | Dev Manager task contract + queue row; no Ozagia intent ID | **Ozagia** for intent ID, intent revision, requested outcome, acceptance criteria and steering history; Dev Manager contract is linked and remains the frozen execution contract | Ozagia records `Intent` and immutable `IntentRevision`; a steering change creates a new revision. For the pilot, a conflict with the frozen Dev Manager contract is surfaced, and the frozen contract wins for execution. |
| Work / attempt | Dev Manager `execution_attempts`, `dispatch_queue` and legacy `task_queue` | **Dev Manager** for task queue/scheduling/admission; **Ozagia** for the linked workflow attempt graph | Dev Manager keeps leases, dispatch claims, attempt status and workspace ownership. Ozagia records attempts/events/evidence referencing the Dev Manager attempt ID. No dual scheduling. |
| Graph state | Dev Manager review DB plus Ozagia graph/session and evidence stores | **Ozagia** for workflow/evidence graph state; Dev Manager review DB stays authoritative for its attempt/review/delivery records until cutover | Ozagia graph nodes/edges are canonical for intent, revisions, dependencies, gates, decisions, effects and evidence references. `evidence_store` is the durable evidence journal; Ocelot `substrate_rs` is an optional storage implementation, not a second semantic model. |
| Aperture resolution | Dev Manager project gate, profile allowlist, review policy; Ozagia resource grants/actions | **Ozagia** resolves the effective graph aperture for each requested operation; **Dev Manager** remains the source of project/profile/permission facts | The effective aperture is the intersection of visibility and permitted operations, scoped by identity, linked project, intent revision, capability and approval. Resolve at effect time; record the resolution. Dev Manager policy cannot be expanded by Ozagia. |
| Effect execution | Dev Manager OpenCode execution + worktree + Git | **Dev Manager** during the pilot | Dev Manager owns workspace creation, agent execution, branch/PR operations and cleanup. Ozagia may propose or record an effect; it does not execute a Dev Manager effect. Ozagia's `edit/txn.py` is used only through an explicit adapter for edits Ozagia itself is authorized to make. |
| Validation | Dev Manager `validation_runner` and `recipe_runner` | **Dev Manager** | Recipes and allowlisted commands remain authoritative. Ozagia imports evidence with source identity and freshness; it never substitutes a plan or model finding for a passing check. |
| Review | Dev Manager `review_policy` + `review_decision` + `AttemptReviewStore` | **Dev Manager** for the decision; **Ozagia** records the linked decision and evidence | The reviewer verdict and accept/reject decision remain Dev Manager's. Ozagia records the decision binding (intent revision, diff/head/base hashes, validation evidence) and never turns advice into a verdict. |
| Delivery | Dev Manager `delivery.py` | **Dev Manager** | Branch, patch and draft-PR delivery remain Dev Manager's. Ozagia records the delivery receipt and links it to the outcome. No merge, push or PR-ready action from Ozagia. |
| Secrets / credentials / quotas | Dev Manager | **Dev Manager** | Ozagia receives references, not secret values. Budget/usage policy remains Dev Manager's. |

**What Dev Manager keeps during the pilot:** task contracts and revisions,
project/repository configuration, admission and scheduling, execution profiles
and routing, workspace ownership, OpenCode lifecycle, validation recipes,
review policy and decisions, delivery, secrets, usage/quota and the existing
UI. Ozagia gets a scoped read/write adapter for intent, graph, evidence and
aperture records. There is no cutover and no duplicate scheduling or review
authority.

The single-owner rule matters because the spec requires one owner per
lifecycle. A dual-write of intent or review state would recreate the stale
evidence problem the pilot is meant to remove.

## 5. Reuse map

### 5.1 Ocelot substrate, session, aperture and action guarantees

Status vocabulary: **proven** means implemented and covered by in-tree code
and tests or a documented working subsystem in this checkout; **partial**
means a component exists but does not cover the workflow contract; **absent**
means no interface found. No Ocelot tests were run in Stage 0, so these labels
describe the in-tree implementation and test evidence, not a fresh pass.

| Guarantee | Status | Evidence | Consequence for Ozagia |
| --- | --- | --- | --- |
| Typed graph storage | **Proven** | `substrate/graph.py:TypedGraph`; `runners/dsl/src/py.rs` `add_node` (2360), `add_edge` (2389), `set_attr` (2582); arbitrary node/edge types and attrs; exercised by graph tests such as `tests/test_composer_graph_pilot.py` | Good storage primitive. It is a graph, not a workflow schema; Ozagia must define node types, invariants and lifecycle. |
| Whole-graph persistence | **Proven** | `substrate/persistence.py:graph_to_dict` (77), `graph_from_dict` (84); `runners/dsl/src/persist.rs:graph_to_value` (255); node IDs preserved; round-trip exercised by `tests/test_composer_graph_pilot.py` | Good checkpoint primitive. It is a full snapshot with `next_id`, no schema version field and no event history. |
| Session checkpoint/restart recovery | **Proven for CoW sessions** | `domains/cow_sessions.py:CowSessionManager` (201), `_write_revision_files` (293), `_persist_session` (338): immutable revision pair, fsync, atomic metadata publication, restore on load; covered by `tests/test_cow_sessions.py` | Useful model for checkpointing and digesting. It is per-principal conversational session state, not a workflow attempt/lease/gate model. |
| Multi-writer / transactional workflow state | **Absent / unproven** | `Substrate` mutation methods take a per-instance mutex; no cross-process transaction; `graph_to_dict` is whole-state | A single-writer service boundary would be needed if multiple processes share the graph. This is a real design constraint, not a detail. |
| Scoped aperture (authorization) | **Absent** | Ocelot's "aperture" is a cognitive/attention frame: `domains/conversation_aperture.py:attended_turns` (57), `docs/research/engine_substrate/APERTURE_PATHOLOGIES.md`, `seeds/aperture_frame.json` | Do not reuse Ocelot aperture as the host authorization aperture. It answers "what enters the agent's representation", not "what may this principal do at this effect". |
| Action / commitment policy | **Partial** | `domains/commitment_gate.py:classify_detail` (146), `commit_gate` (293); frame data for reversible/committing; audit `Commitment`; covered by `tests/test_commitment_gate.py` | A useful policy precedent, not a general enforcement path. The module says it is not wired to `install_proposal`/`auto_approve` or the other effect paths and is not a sandbox. |
| At-sink action authorization / receipt | **Proven for native world/reflex sinks** | `runners/dsl/src/py.rs:authorize_reflex_emission` (7003), `authorize_action_emission` (7151) and `complete_action_emission` (7289) implement an opt-in request-scoped gate: it checks action key, sink route and source epoch, consumes exactly one pending authorization, refuses on same-epoch `ActionStopEvent` (`stop_before_commit`), and writes `sink_receipt_id` or `emission_failed`. `substrate/world_adapter.py:_apply_world_action_at_sink` (16720) and `fire_reflexes` (17638) call it. Tests: `tests/test_response_inhibition_migrated.py` (`test_native_stop_withholds_and_revokes_authorization` 121, `test_native_sink_never_receives_stopped_action` 193, `test_sink_idempotence` 210, `test_sink_failure_consumes_authorization_without_retry` 220). | Strong graph-native precedent for Stage 1: action request + authorization + stop/epoch + sink receipt. It is opt-in (`ResponseInhibitionPolicy.enabled`, default-off parity), applies to Ocelot's own world/reflex sinks, and is not a sandbox or a host/project authorization aperture. An evaluation-only broker (`tests/evaluation/test_action_broker.py`) separately proves default-deny for malformed/unknown requests, but is not the production substrate gate. |
| External-effect journal / replay | **Partial** | The native sink gate writes a receipt and terminal status for world/reflex actions, and a failed sink consumes the authorization without retry (`test_sink_failure_consumes_authorization_without_retry`). There is no Git/OpenCode/validation/PR effect journal, no generic effect key and no reconciliation state. `runners/dsl/src/effect_audit.rs` is only about failed *graph* inserts; `graph.rs` experiment journals are in-graph before-image records. | Reuse the request/authorization/receipt/terminal-state pattern, not the world-specific implementation. Ozagia still needs an effect record and adapter-level idempotency for Dev Manager effects. |
| Revocation | **Partial** | Same-epoch `ActionStopEvent` sets the request `withheld`, `terminal_reason=stop_before_commit`, and the pending `ActionAuthorization` `revoked` before the sink; a late stop records `already_emitted`/`effect_already_committed`; a stale-epoch stop is ignored (`tests/test_response_inhibition_migrated.py:121,131,162`). No grant/intent-level revocation, no reassignment fence and no external effect revocation found. | Reuse the epoch/stop/revoked pattern. Add intent-revision and grant-generation binding for host effects; Ocelot's request-scoped revocation is not sufficient by itself. |
| Intent revision binding | **Absent** | Sessions have a numeric checkpoint revision and graph digest, not an intent-revision ID | Add explicit `IntentRevision` nodes/edges. A session checkpoint counter is not a substitute. |
| Graph schema migration | **Partial / unproven** | `graph_to_dict`/`from_dict` round-trip a graph but carry no schema-version envelope; Ocelot code changes are not accompanied by a graph migration layer in this path | Prefer an Ozagia-owned envelope with a version and explicit migrations before adopting Ocelot as the store. |
| Workflow/DAG executor | **Absent for a general process DAG** | Ocelot is a rule engine over a typed graph; `run_rules` is a monotone fixpoint. It has graph-selected host actions (`HostActionRequest` in `scripts/jabberwock_daemon.py`) and an at-sink action gate, but no versioned process definition with typed inputs/outputs, joins, retries, compensation or gate nodes | Do not claim Ocelot is a workflow runner. Reuse the host-action and at-sink patterns; Ozagia must add the process/DAG lifecycle and recovery semantics. |

**What Ocelot does simplify:** typed graph storage, whole-graph snapshots,
digestable checkpoints, CoW session isolation, in-process rule evaluation, and
the request/authorization/stop/receipt shape for an at-sink action gate.
**What it does not simplify:** intent lifecycle, the host/project authorization
aperture, review policy, external Dev Manager effects, delivery or migration.

### 5.2 Ozagia interfaces that can be reused

These are implemented in the current checkout; S0-A's inventory is the
authoritative interface map. Status here is about Stage 1 reuse.

| Ozagia interface | Status | Reuse for Stage 1 |
| --- | --- | --- |
| `src/ozagia/core/contracts.py` | Implemented, versioned (`SUPPORTED_MAJOR=1`, `CURRENT_VERSION="1.0"`); canonical JSON, IDs, schema/instance hashing, freshness/evidence vocabularies | Use for the versioned view/record envelope and evidence references. Do not invent a second serialization contract. |
| `src/ozagia/core/goals.py` | Implemented; criteria, scope, history, `check_goals`; `test_passes`/`test_fails`/`symbol_*`/`review_resolved` etc. | Reuse criteria evaluation and the "never trust an agent claim" stance. It is a repo-level goal store, not an intent-revision/DAG store. |
| `src/ozagia/core/session.py` | Implemented; typed append-only receipt graph (`Session`, `Task`, `Plan`, `Step`, `Attempt`, `Edit`, `Verification`, `Decision`), atomic JSON, `load_session`, workflow table with `gate=wait/approve/clear` | **Primary receipt/state shape.** Extend with `Effect`, `Intent`/`IntentRevision` and a recovery/lease field; keep the declarative workflow table. Do not make the session JSON the only durable evidence journal. |
| `src/ozagia/core/evidence_store.py` | Implemented durable JSONL journal (`observation`, `assessment`, `declaration`) with idempotency key, duplicate/conflict detection, torn-tail recovery, locking, retention and record hash; tests in `tests/test_evidence_store.py` | **Primary durable evidence journal.** Wrap it with an outcome-evidence record kind; do not create a second evidence store. It does not by itself provide an effect receipt/idempotency state machine. |
| `src/ozagia/research/graph.py` | Implemented append-only, event-sourced research graph with replay validation (`ResearchGraph`, `amend`, event log) | **Pattern worth borrowing** for intent revisions and replayable events. It is research-only and single-writer; do not reuse it as the general outcome store. |
| `src/ozagia/core/tasks.py` | Implemented; bounded task spec, typed steps, gates, stop conditions, budgets, evidence log and manifest | Reuse the gate/evidence/budget vocabulary and validation patterns; it is not wired into Dev Manager and has no real executor by default. |
| `src/ozagia/core/critical_path.py` | Implemented `Dag`/`DagNode`/`DagEdge` with explicit finish-to-start edges, cycle detection and topological order | Reuse for dependency validation/topology. It is an in-memory analysis library, not a persisted workflow run or scheduler. |
| `src/ozagia/core/evidence.py` | Implemented read-only joins; explicitly defers durable evidence, idempotency, retention and crash recovery (`DEFERRED`, line 67) | Reuse the report/join shape, but write durable evidence through `evidence_store.py`. Do not mistake the read-only collector for a ledger. |
| `src/ozagia/core/actions.py` | Implemented preview-first action layer with one-time token, precondition re-check, bounded audit (`preview` 1897, `apply` 1926) | Reuse the preview/apply/audit shape for effect authorization. It is a process/connection action layer, not a generic Git/edit effect adapter. |
| `src/ozagia/core/grant_lifecycle.py` | Implemented `GrantLease` (110); preflight/claim/release; exercised indirectly through `tests/test_tasks.py`; no dedicated test module found | Reuse grant IDs and release semantics; add revocation/epoch and effect-time checks. |
| `src/ozagia/core/apertures.py` + `core/view.py` | Implemented; data-defined projection, unknown types become `other` and are never dropped; `build_view` at 338; `omitted` reasons | Reuse as the **UI projection** base. It is not an authorization aperture and its current output is graph-layout, not a work list. |
| `src/ozagia/edit/txn.py` | Implemented; read once, base-hash check, atomic write, backup (`read_source` 107, `write_source` 174) | Reuse as the only text-edit path. It prevents stale-base writes but is not an idempotency key or effect receipt. |
| `src/ozagia/core/mesh.py` | Implemented local advisory mesh with filesystem records, leases and messages; explicitly not an authorization boundary | Keep out of Stage 1; useful later for local coordination. Leases are advisory. |
| `src/ozagia/integrations/mcp/` | Implemented MCP surface | Expose the Stage 1 view/operations there after the core contract is stable; no Dev Manager dependency. |
| Ocelot integration (`src/ozagia/integrations/ocelot/`) | Implemented as an optional adapter for working *on* Ocelot; it analyses the checkout as text and does not import Ocelot or provide a `substrate_rs` graph store | Do not cite it as a store integration. A real `substrate_rs` store adapter is new work. |

### 5.3 What a Rust worker/store boundary would and would not simplify

This is an architectural assessment. No Rust/Python benchmark or integrated
workflow measurement was run, so the performance claims below are expected
effects, not measured results.

**Expected to simplify (if a single Rust store owns the graph):**

- typed graph mutation and whole-graph snapshot/checkpoint performance;
- one memory owner for large graphs and one serialized mutation stream;
- deterministic rule evaluation if workflow rules are expressed in the Rust
  DSL;
- a possible shared store between Ocelot and Ozagia instead of two graph
  models.

Ocelot's at-sink action gate is already native Rust (`authorize_action_emission`
and `complete_action_emission`), so a new Rust boundary would not add that
capability; it would only decide where the graph lives and how Python reaches
it.

**Would not simplify:**

- intent lifecycle, intent revisions, project binding and policy;
- effective aperture resolution, which needs identity, project, task, review
  and grant facts from Dev Manager and Ozagia;
- external effects (workspace creation, OpenCode, validation subprocesses, Git
  branch/PR), effect idempotency, fencing or reconciliation;
- review policy/decisions and delivery ownership;
- graph schema migration across Ocelot snapshots and Ozagia records;
- the UI projection and generic fallback;
- Dev Manager integration, secrets, quotas or audit access.

**What it would add:** a Python↔Rust boundary, serialization/versioning of every
record, a second deployment artifact, two-language debugging, and a new
migration surface. It only pays off if the Rust store is the **single writer**
and the graph is the canonical workflow record. A Rust "worker" that runs
alongside Python effect adapters would not reduce the dominant memory footprint
(OpenCode and validation processes) and would duplicate the adapter/policy
model.

**Recommendation:** do not make a Rust worker/store boundary a Stage 1
dependency. Keep the record contract language-neutral and add a store interface;
prove a `substrate_rs` implementation later if the measured memory or
single-writer benefit is real.

## 6. Architecture decision: graph-native workflow vs separate DAG runner

### 6.1 Decision

**Accept graph-native workflow.** Use typed graph records as the canonical
state for intent revisions, work, attempts, dependencies, gates, decisions,
effects and evidence. Execute through a small deterministic evaluator over that
graph (the existing `session.py`/`dev_session_workflow.json` pattern). Do not
introduce a second DAG engine or a second canonical workflow model.

A separate DAG runner may later be used **behind an adapter** for durable
timers/retries if measured needs justify it, but it must not become the system
of record. This matches the delivery plan's architecture direction and the
spec's "graph is canonical" requirement. No external DAG runner was installed
or run in Stage 0; the comparison below is against its documented contract and
the observed Dev Manager/Ozagi recovery surfaces.

### 6.2 Evaluation against the required criteria

| Criterion | Graph-native | Separate DAG runner | Why graph-native wins / what remains |
| --- | --- | --- | --- |
| Crash recovery | One graph/session store plus an effect journal. Ozagia already has atomic session JSON and `load_session`; Ocelot has whole-graph snapshots. Missing: effect journal and restart tests. | A runner has durable history and retries, but it is a second store. The graph and runner must be reconciled after a crash; the external effect still needs idempotency. | One recovery point is simpler and auditable. The graph-native approach still has to add an effect journal; that is Stage 1 work either way. |
| Revision binding | A gate/decision/effect points directly to `IntentRevision`, base/diff/head hashes and evidence. Ozagia can query current revision at effect time. | The runner must mirror the intent revision and call back for authority; stale revisions can be dispatched before the graph notices. | Graph-native is the natural binding. Dev Manager already proves evidence-hash binding (`review_decision.is_stale`); Ocelot's action gate binds `action_key`/`sink_route`/`source_epoch` but not an intent revision. Extend the same shape with an intent-revision hash. |
| Revocation | A `Grant`/epoch node is queryable at effect time; pending gates can be invalidated by revision or revocation. | The runner may have already queued or started work; it needs a callback and a cancellation/reconciliation path. | Graph-native reduces the window. Ocelot proves same-epoch stop-before-commit and authorization revocation at its own sinks; the pilot still needs grant and intent-revision revocation. A graph alone does not enforce revocation. |
| Effect replay | An `Effect` node records an idempotency key, target, before-state, intent revision, attempt and receipt. Replay checks the key and the current grant. | The runner has mature retry policies, but external effects still need the same key/receipt ledger; otherwise a retry duplicates. | Ocelot proves sink idempotence and consumes the authorization on failure without retrying, but only for its own world/reflex sinks. For Dev Manager effects, use one graph effect record and adapter-level idempotency; a second effect ledger is a liability. |
| Graph migration | One versioned graph schema; migrations can rewrite nodes/edges and preserve IDs. Ozagia already has snapshot v2 with v1 key derivation; `contracts.py` has a major/minor policy. | Two schemas: workflow runner state and graph state. Both need migration and a compatibility test. | Graph-native wins on migration count. Ocelot's graph dict has no schema envelope yet; add one before adoption. |
| Operational maturity | Small, local, deterministic evaluator; easy to inspect and replay. No external engine to operate. | Mature scheduling, timers, parallelism, retries and observability if the chosen engine is production-grade. | The pilot's needs are small and bounded. A runner is justified only after measured concurrency/timer needs; otherwise it is premature complexity. |

### 6.3 Reasons

1. **One canonical record.** The spec requires the graph to be the canonical
   record of intent revisions, assignments, dependencies, attempts, effects,
   decisions and evidence. A separate DAG runner would either duplicate that
   record or make the graph a projection, violating the direction.
2. **Revision and revocation are graph queries.** The effective aperture,
   current intent revision, grant generation and evidence freshness are all
   facts in the graph. A runner would need to call back for each, which is the
   same coupling with an extra store.
3. **Recovery is not solved by a runner alone.** External effects (Git,
   OpenCode, tests, PRs) are not transactional with either a graph or a runner.
   The required work is an effect key/receipt and reconciliation state, not a
   second scheduler.
4. **Migration is cheaper with one schema.** The pilot will change its record
   shape repeatedly. Two stores double the migration and compatibility burden.
5. **The existing Ozagia session module already implements the shape.** It is
   an append-only typed graph with atomic persistence, a declarative workflow
   table and gate values. Extending it is the smallest path to the Stage 1
   exit test.
6. **The strongest control precedent is already graph-native.** Ocelot's
   `ActionRequest`/`ActionAuthorization`/`ActionStopEvent`/`sink_receipt_id`
   path proves that same-epoch stop, revocation, exactly-once sink consumption
   and failure-without-retry can be enforced at the effect boundary for its
   own world/reflex sinks. The missing pieces are host aperture, intent
   revision, Git/OpenCode effect adapters and recovery, not a separate DAG
   engine.

### 6.4 Unresolved questions

The companion S0-A inventory independently reaches the same store conclusion:
Ozagia has no general durable typed graph store, and Stage 1 must define one
store or a narrow supported substrate interface rather than several parallel
work stores.

1. **Store authority.** Is the canonical graph Ozagia's JSON/session graph or
   an Ocelot `substrate_rs` store? Current Ozagia has no `substrate_rs`
   dependency; `pyproject.toml` has no runtime dependencies. A store-boundary
   spike must prove a single-writer, versioned, crash-safe implementation
   before adopting it.
2. **Graph schema envelope.** Ocelot's `graph_to_dict` has no schema version.
   Ozagia's snapshot has version 2. Stage 1 must define an envelope and a
   migration test before importing any existing graph.
3. **Effect fencing.** Git worktrees/branches and OpenCode sessions are not
   fenced by the graph. Which effects can be made conditional (`git update-ref`
   with expected old value, branch existence checks, PR idempotency) and which
   must remain "uncertain" until reconciled?
4. **Concurrency.** Can Dev Manager and Ozagia write graph records concurrently
   through one adapter, or is there a single writer? What happens when an
   attempt is cancelled during a graph write?
5. **Aperture inputs.** Which Dev Manager facts are authoritative for the
   effective aperture (project gate, profile allowlist, review policy, task
   scope), and how are they versioned so a later change cannot retroactively
   authorize an attempt?
6. **UI contract ownership.** Does the versioned view contract live in Ozagia
   core (`contracts.py`) or in the host adapter? The list-first UI needs
   status/operations/evidence fields that `build_view` does not yet emit.

## 7. Workspace-to-graph binding

### 7.1 Evidence from the smoke

`OZAGIA_OPENCODE_SMOKE_2026-11-27.md` records:

- OpenCode `v2.3.0` stores the requested `location.directory` per session on one
  long-lived server. Both `POST /api/session` and `GET /api/session/{id}`
  returned the exact requested directory for two temporary roots.
- The installed Ozagia plugin reads `OZAGIA_DAEMON_SOCK` from the OpenCode
  server process at plugin setup. Its documented root default is the server
  root; it does not switch roots per session.
- The smoke made no model or tool call, so it did **not** prove that a graph
  read or capture event reached the matching workspace. `opencode plugin list`
  returned an ID of `-`, which the smoke explicitly rejects as evidence.
- Classification: OpenCode session workspace binding is **available**;
  per-session Ozagia daemon selection and graph capture on a shared server are
  **unsupported/not demonstrated**.

### 7.2 Evidence from the selected task

The selected attempt records:

- `workspace_id = 508b84cca44a4d03a9574ac0840764a6`;
- `workspace_path = /applications/data/ozma-review/worktrees/workspace-508b84cca44a4d03a9574ac0840764a6`;
- `repository = /applications/data/ozma-review/repos/sandbox`;
- base commit `4143f1b...`; head commit is the same (the change is uncommitted
  in the worktree), with `head_sha256` and `diff_sha256`;
- five `.opencode/goals/...` harness-state paths recorded separately from the
  `app/base.py` deliverable.

It records **no Ozagia graph ID, graph snapshot digest, daemon identity or
session-to-graph binding**. The task's contract snapshot has no `context_sha256`
and no `capabilities`; this was a low-context task. Dev Manager binds the
attempt to a Git workspace and evidence hashes, not to a graph.

### 7.3 What remains unverified

1. That a read/capture event from an OpenCode session in root A reaches only
   the Ozagia daemon for root A, with root B isolated, on one shared server.
2. That the graph snapshot is bound to the exact workspace root, base commit,
   dirty-state fingerprint and attempt ID, not just the server launch root.
3. That the binding survives daemon restart, server restart, plugin reload and
   workspace cleanup.
4. That a missing, relative or unregistered root is refused rather than falling
   back to the server root.
5. That the graph used for context is the graph for the same attempt, not a
   stale or shared snapshot.
6. That Dev Manager's `workspace_id`/`commit` and Ozagia's graph identity are
   recorded on the same attempt in both directions.

### 7.4 Proposed contract test (Stage 1/2)

Create two isolated Git roots and two Ozagia daemons with distinct identities.
Start one OpenCode 2.3 server in each root (or prove a supported per-session
selector). For each session, issue a read/capture tool call and assert:

- the event reaches only the matching daemon;
- the daemon records the canonical root, session ID, workspace snapshot and
  dirty-state fingerprint;
- a session in the other root produces no event in the first daemon;
- after killing and restarting a daemon, the next event resumes the correct
  graph without cross-contamination;
- a missing/relative/unregistered root is refused with a typed error.

Until that test passes, do not advertise per-session graph capture on the
shared server.

## 8. Memory and UI baseline

### 8.1 Memory and process count

No integrated workflow was run, so the requested idle/peak baseline for the
selected workflow is **not measured**. Starting Dev Manager, OpenCode and an
Ozagia daemon to measure it would violate the Stage 0 read-only constraint.

The following ambient observations were taken read-only while the live review
service was already running. They are **not** the selected workflow's baseline
and must not be summed as one topology:

| Process / cgroup | RSS | Peak (HWM / cgroup) | Notes |
| --- | --- | --- | --- |
| Dev Manager review service `uvicorn` PID 2736699 | 97,724 kB ≈ 95.4 MiB | VmHWM 97,724 kB; cgroup `memory.peak` 465,584,128 B ≈ 444.0 MiB | One process in the `devmgr-review.service` cgroup; cgroup `memory.current` 157,245,440 B ≈ 150.0 MiB at observation. Peak includes prior activity. |
| Ozagia daemon PID 2447286 | 3,488 kB ≈ 3.4 MiB | VmHWM 71,248 kB ≈ 69.6 MiB | Idle daemon for the Ozagia checkout; no active build. Not part of the selected workflow. |
| OpenCode 2.3.0 `--standalone` PID 1887739 | 305,448 kB ≈ 298.3 MiB | VmHWM 453,704 kB ≈ 443.1 MiB | Ambient process for another session. |
| OpenCode 2.3.0 `serve --stdio` PID 1887816 | 360,180 kB ≈ 351.7 MiB | VmHWM 675,080 kB ≈ 659.3 MiB | Ambient process for another session. |

**Exact missing measurement** (must be taken with the pilot topology):

- idle, before dispatch: sum RSS and PSS for the Dev Manager review service,
  one OpenCode 2.3 server, one Ozagia daemon/MCP process and their persistent
  helpers; count processes and threads;
- peak, from dispatch through PR/delivery: sample all descendant processes at a
  fixed interval (for example 1 Hz), including `git`, Python validation, cargo
  and OpenCode children; record max RSS/PSS and cgroup `memory.peak`;
- cleanup: confirm child processes exit and no orphan worktree/daemon remains;
- repeat at least three times and report median and range, not a single sample.

Without those samples, the memory comparison in the delivery plan cannot be
made and a Rust-worker comparison would be premature.

### 8.2 List-first UI projection sketch

The baseline UI is list-first already: `app/templates/work.html` has a work
summary and filtered list; `app/templates/dispatch_review.html` has a saved
attempt detail with status, changes, actions and review controls; but both read
service APIs/DB rows, not a scoped graph projection.

Sketch one view contract, derived from the graph, with presentation state kept
separate:

```json
{
  "schema": "ozagia.workview/1",
  "generated_at": "2026-11-27T00:00:00Z",
  "scope": {
    "principal": "operator",
    "project": "sandbox",
    "intent_id": "intent:...",
    "intent_revision": 1,
    "aperture": "work-list"
  },
  "items": [
    {
      "id": "work:...",
      "type": "Work",
      "display_type": "work",
      "label": "Add a module docstring to app/base.py",
      "status": "awaiting_review",
      "summary": "validation passed; review required",
      "updated_at": "2026-11-27T00:00:00Z",
      "relations": [
        {"type": "attempted_by", "target": "attempt:...", "label": "attempt"}
      ],
      "operations": [
        {"id": "review.open", "label": "Review diff", "enabled": true},
        {"id": "work.cancel", "label": "Cancel", "enabled": true,
         "requires_gate": false}
      ],
      "evidence": [
        {"kind": "validation", "ref": "evidence:...", "freshness": "current",
         "sha256": "..."}
      ]
    }
  ],
  "groups": [{"id": "awaiting_review", "label": "Awaiting review", "count": 1}],
  "omitted": {"nodes": 0, "edges": 0, "reason": "none"},
  "provenance": {"graph": "ozagia.workflow/1", "snapshot": "..."}
}
```

**List-first behavior:**

- Default order is attention-first: blocked/needs decision, awaiting review,
  running, recently verified, done; not graph layout order.
- Each row shows label, status, next operation and freshest evidence; graph
  exploration is an expansion, not the default.
- Operations are proposals only. The backend resolves the effective aperture at
  request time and records the result. The UI never authorizes.
- The view is derived from recorded state; progress text is generated from
  facts, not a second workflow state machine.

**Generic extension rendering constraint:**

- Every item carries `type` and `display_type`. A known type may use a
  specialized component; an unknown type must render a readable generic row
  using `label`, `status`, `summary`, `relations`, `operations` and `evidence`.
- Unknown node/edge types are preserved, not dropped, and are shown under
  `display_type: "other"` or their raw type with a generic label. This follows
  `core/view.py`'s existing rule: an unresolved type becomes `other` and is
  never dropped; `omitted` reports scope/caps.
- Unknown operations are disabled with a reason; they are never inferred.
- The view has a schema version and a migration path; presentation-only fields
  are not stored in the canonical graph.

## 9. Stage 1 recommendation

### 9.1 Smallest vertical slice

Prove one local, deterministic, verified outcome with **one gate** and
**restart recovery**, without OpenCode, Dev Manager cutover, Ocelot store
adoption or network.

Use the selected workflow's shape:

1. **Intent and work:** create `Intent` (objective, acceptance criteria,
   project/repo link) and `IntentRevision` 1 in one durable outcome store. The
   smallest implementation can extend `DevSession`/its session JSON; the
   record shape should borrow `ResearchGraph`'s append-only revision/event
   pattern. Link a `Work` node to revision 1.
2. **Effect:** apply a bounded one-file change through `edit/txn.py` to a
   temporary local Git repository. Record an `Effect` node with an idempotency
   key, target path, before hash, intent revision, attempt ID, state and
   receipt. Adopt the shape of Ocelot's `ActionRequest` /
   `ActionAuthorization` / `ActionStopEvent` / `sink_receipt_id` records; do not
   copy the world-specific implementation.
3. **Validation:** run `git diff --check` and a deterministic Python parse or
   `py_compile` check (no model). Write `Evidence` through `evidence_store.py`
   with command, exit code, source identity and output hash. Do not treat a plan
   or model statement as evidence.
4. **Gate:** persist a `Gate`/`Decision` requiring `approve` (the selected
   workflow's human review gate). Bind it to the intent revision, diff hash,
   head hash, finalization time and validation evidence. `wait`/`approve`/
   `clear` should reuse the existing `DevSession` gate vocabulary.
5. **Delivery:** record an outcome as satisfied only when the gate is approved
   and the evidence is current; produce a local branch or a JSON report (no
   push, no PR) to prove the outcome.
6. **View:** derive a list-first `workview` JSON from the outcome store with the
   fields in §8.2; add a generic fallback for an unknown type. No second UI
   state model.

### 9.2 Acceptance tests

- **Happy path:** change → validation pass → gate pending → approve → outcome
  satisfied; the list view shows the same status and operations as the graph.
- **Restart after effect, before gate:** kill the process, reload the session,
  resume at the pending gate; the effect is not applied twice (idempotency key
  and before-hash check).
- **Restart after gate decision:** reload; do not re-ask the gate and do not
  duplicate delivery; the decision and evidence binding are unchanged.
- **Stale base:** after the attempt records its intended base hash but before
  the write, change the file; the edit path refuses with a typed stale-base
  error and records the failure as evidence.
- **Revision change:** create intent revision 2; pending gate/decision for
  revision 1 becomes stale and cannot authorize the new work.
- **Revocation:** revoke the grant/epoch before effect; the effect is refused
  and no new claim is admitted. Record the revocation and pending state.
- **Unknown type:** add a node type the view does not know; it renders a
  generic row and is not dropped.
- **No model/network:** the test suite runs with no provider, no OpenCode and
  no network.

### 9.3 Explicitly out of scope for Stage 1

- Real Dev Manager cutover or shared-server OpenCode capture.
- Ocelot `substrate_rs` as the canonical store; only a store-interface spike.
- Multiple workers, remote placement or Ozma mesh.
- Model-authored progress text or model review.
- PR push/merge; a local branch or JSON report is enough.
- A second DAG engine.

## 10. Blockers and unresolved evidence

1. **Selected workflow is a sandbox task, not production work.** The production
   analogue task 257 has no review verdict/decision. Stage 1 should re-run the
   selected shape against a real checkout in an isolated workspace and record
   the review outcome.
2. **Live Dev Manager source is not pinned.** `app-src` has 40/227 files
   differing from `MANIFEST.sha256`; `SOURCE_HEAD` is a 2026-11-12 snapshot.
   The exact deployed code cannot be reproduced from the recorded revision.
   Capture a live tree hash/manifest and a deployment revision before Stage 1
   integration.
3. **Ocelot is not integrated as a store, and Ozagia has no general durable
   workflow graph.** Ozagia's code graph is a derived JSON snapshot; the only
   event-sourced, replay-validated graph is research-only
   (`research/graph.py`); `evidence_store` is a durable evidence journal, not a
   workflow/effect graph. Ozagia has no `substrate_rs` dependency and
   `pyproject.toml` runtime dependencies are empty. A store-boundary spike must
   prove graph import/export, schema versioning, single-writer behavior and
   migration before the delivery plan's "reuse the Rust-backed store" can be
   treated as implemented.
4. **No workspace-to-graph binding.** The smoke proves session directory
   storage only. The selected attempt records Git workspace identity but no
   graph/daemon identity.
5. **No memory baseline for the integrated workflow.** Ambient numbers are not a
   substitute; the exact measurement protocol is in §8.1.
6. **No intent-level revision binding or revocation.** Dev Manager proves
   evidence-hash binding. Ocelot proves request-scoped same-epoch stop and
   authorization revocation at its own sinks. Neither system binds a
   gate/effect to an intent revision or a revocable grant generation.
7. **No external-effect journal/fencing for Dev Manager effects.** Ocelot's
   world/reflex sink gate proves a receipt plus failure-without-retry pattern,
   but Git/OpenCode/validation/PR effects in the selected workflow have no
   equivalent generic idempotency receipt or reconciliation state.
8. **The selected workflow's behavioral criterion was not independently
   tested.** `base()` unchanged is supported by the diff and operator note, not
   by an executed behavioral check. Stage 1 should make that evidence explicit.

## 11. Sources, commands and reproducibility

### 11.1 Read-only sources

- Ozagia: `/home/matt/ozmadev/ozagia` at `eb9bcb0b...`; `AGENTS.md`,
  `docs/intent_to_outcomes_spec.md`,
  `docs/plans/2026-11-27_intent_to_outcomes_delivery.md`,
  `docs/plans/2026-11-27_intent_to_outcomes_stage0.md`; companion S0-A
  inventory `docs/results/intent_to_outcomes_inventory_2026-11-27.md`.
- Ozagia code cited: `src/ozagia/core/contracts.py`, `goals.py`, `session.py`,
  `tasks.py`, `evidence.py`, `evidence_store.py`, `actions.py`,
  `grant_lifecycle.py`, `critical_path.py`, `apertures.py`, `view.py`,
  `mesh.py`, `src/ozagia/research/graph.py`, `src/ozagia/edit/txn.py`,
  `src/ozagia/integrations/mcp/`, `src/ozagia/integrations/ocelot/`.
- Dev Manager: `/home/matt/ozmadev/internal/dev-manager` at `0e7877a7...`;
  `README.md`, `OZAGIA_INTEGRATION_PLAN.md`,
  `OZAGIA_OPENCODE_SMOKE_2026-11-27.md`, `REVIEW_PROCESS_PLAN.md`.
- Dev Manager code cited: `app/services/workspaces.py`,
  `execution_engine.py`, `task_dispatch.py`, `recipe_runner.py`,
  `validation_runner.py`, `review_finalizer.py`, `review_decision.py`,
  `delivery.py`, `app/review_policy.py`, `app/task_contract_store.py`,
  `app/templates/work.html`, `app/templates/dispatch_review.html`.
- Live review data (read-only SQLite):
  `/applications/data/ozma-review/review/review.db` and
  `/applications/data/ozma-review/legacy/dev-manager.db`.
- Selected attempt/review rows: `execution_attempts.id =
  a2c6e9af-a722-4de2-b818-2d5678b0f011`; `attempt_review_verdicts.id =
  0282dca0-6169-41f5-bad5-975158a38503`; `attempt_review_decisions.id =
  ed522a6e-9434-40f2-baea-a7f874a4cfab`; `attempt_deliveries.id =
  12703963-2b2a-4798-a454-fa9f834f4fe3` and
  `4d7c00ac-62f3-41d1-a926-c322f42fb129`.
- Completeness note: the attempt's `updated_at` is 2026-11-12; the PR delivery
  row was last updated 2026-11-27 and remains `pr_open`/draft. No merge or
  downstream use is recorded.
- Ocelot: `/home/matt/ozmadev/ocelot` at `b7fda1c9...`; `README.md`,
  `substrate/graph.py`, `substrate/persistence.py`,
  `domains/cow_sessions.py`, `domains/commitment_gate.py`,
  `domains/conversation_aperture.py`, `substrate/world_adapter.py`,
  `scripts/jabberwock_daemon.py`, `runners/dsl/src/py.rs`,
  `runners/dsl/src/effect_audit.rs`, `runners/dsl/src/persist.rs`,
  `runners/dsl/python/substrate_rs/_native.build.json`,
  `tests/test_response_inhibition_migrated.py`,
  `tests/evaluation/test_action_broker.py`,
  `evaluation/process_mastery/linear_p12_v1.py`,
  `docs/research/engine_substrate/decision_making_spike.md`,
  `docs/research/engine_substrate/APERTURE_PATHOLOGIES.md`.
- Ozma: `/home/matt/ozmadev/ozma` at `ecb763ef...`; `README.md`. Ozma mesh is
  network peer/key/discovery/tunnel infrastructure; it is not used in the
  Stage 1 slice. No Ozma service was run.

### 11.2 Read-only commands used

```sh
# revisions and dirty state
git -C /home/matt/ozmadev/ozagia rev-parse HEAD
git -C /home/matt/ozmadev/ocelot rev-parse HEAD
git -C /home/matt/ozmadev/ozma rev-parse HEAD
git -C /home/matt/ozmadev/internal rev-parse HEAD
git -C /home/matt/ozmadev/internal/dev-manager status --porcelain

# versions
/home/matt/ozmadev/ozagia/.venv/bin/python --version
/applications/data/ozma-review/venv/bin/python --version
/home/matt/.ozagia/opencode/2.3.0/opencode --version
node --version

# selected workflow and review rows (read-only)
python3 - <<'PY'
import sqlite3, json
con = sqlite3.connect(
    "file:/applications/data/ozma-review/review/review.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for table, key in (
    ("execution_attempts", "a2c6e9af-a722-4de2-b818-2d5678b0f011"),
    ("attempt_review_verdicts", "0282dca0-6169-41f5-bad5-975158a38503"),
    ("attempt_review_decisions", "ed522a6e-9434-40f2-baea-a7f874a4cfab"),
):
    row = con.execute(f"select * from {table} where id=?", (key,)).fetchone()
    print(table, json.dumps(dict(row), indent=1) if row else "missing")
PY

# live source pin check (read-only)
python3 - <<'PY'
import hashlib, pathlib
root = pathlib.Path("/applications/data/ozma-review")
manifest = {}
for line in (root / "MANIFEST.sha256").read_text().splitlines():
    if "  " in line:
        digest, path = line.split("  ", 1)
        if path.startswith("./app/"):
            manifest[path[2:]] = digest
diff = [p for p, h in manifest.items()
        if hashlib.sha256((root / "app-src" / p).read_bytes()).hexdigest() != h]
print(len(manifest), "app files,", len(diff), "differ")
PY

# ambient memory/process observation (read-only)
grep -E '^(Name|VmRSS|VmHWM|Threads)' /proc/<pid>/status
cat /sys/fs/cgroup/.../memory.current
cat /sys/fs/cgroup/.../memory.peak
```

No service was started, no test was run against the target checkouts, no
network request was made, no live model call was issued and no commit was
made. This task wrote only this report; the governing specification/planning
documents and the companion inventory were read, not written.
