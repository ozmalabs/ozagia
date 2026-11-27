# Batch completion: repo relationships, API references, Ocelot workspace

Status: complete 2026-11-27. Phase 1 and Phase 2 are implemented; the
read-only audit remediation is applied; the full suite, Ruff, generated
reference docs and `REPO_SHAPE.md` all pass. Coordinator owns the reviewable
diff; live model evaluation and real-checkout applies remain approval-gated.
The status table below records the original workstreams; the remediation round
is tracked under "Audit remediation" below.

## Status

| Workstream | Status | Evidence |
| --- | --- | --- |
| W1 Ocelot workspace | complete | 126 Ocelot/task-library tests, Ruff clean, CLI/MCP surface checked |
| W2 repo/API relationships | complete | 76 targeted tests, Ruff clean, five new edge-case tests |
| W3 Bigville integration | complete | Bigville adapter/project-invariant tests 10 passed; docs example validated |
| W4 combined verification | complete | 7,318 passed, 27 skipped; Ruff clean; 47 reference pages and REPO_SHAPE current |
| W5 adapter-declared bridges | complete | 50 focused tests, 106 broader tests, Ruff clean, Bigville evidence spot check |
| W6 persisted supergraph | complete | 14 new tests, 47 targeted tests, Ruff clean, CLI smoke |
| W7 family-aware retrieval | complete | 39 core tests + CLI/MCP surface tests (69 in the follow-up), fixture eval recorded, Ruff clean |

## Objective

Finish the current uncommitted batch and the earlier Bigville/Ocelot work to a
green, documented, internally consistent state. "Finished" means the code,
tests, plans, results and generated reference docs agree, and the remaining
work is explicitly approval-gated or a named follow-on rather than an
unrecorded gap.

## Current state

- **Repository relationships and API references** (new, uncommitted):
  `core/repo_relations.py`, `core/api_relationships.py`,
  `cross_repo.check`/`relations`, `federation relations`, tests and docs.
  The feature is implemented; completion is verification, an integration
  example and status reconciliation.
- **Ocelot workspace second slice** (uncommitted): seed editing, boot/registry
  editing, session context and OpenCode hook, offline planner evaluation,
  claim check, Rust/Python parity, task spec, MCP/CLI surfaces, tests, docs and
  results are implemented. The plan header now says so; the live
  agent-workflow/model-task evaluation remains approval-gated.
- **Bigville support** (committed): read-only adapter, project invariants,
  retrieval evaluation, test-coverage audit. The cross-repository Ocelot item
  now points at the declared relationship and API-reference surfaces.
- **Phase 2** (uncommitted): adapter-declared `RepoBridge` nodes, the
  persisted relationship supergraph (`federation build/graph`), and opt-in
  family-aware federation search with a paired fixture evaluation are
  implemented. Live evaluation and a full node-union supergraph remain
  follow-ons.
- **Generated state**: `REPO_SHAPE.md` and `docs/reference/` are stale and
  coordinator-owned; the docs generator also needs deterministic width/path
  normalization before the final regeneration.

## Completion criteria

1. `docs/plans/2026-11-27_repo_relationships.md` and
   `docs/plans/2026-11-26_ocelot_workspace.md` describe what is actually
   implemented, what was verified, and what remains.
2. Bigville/Ocelot integration is demonstrated with an explicit, provenance-
   bearing federation manifest and an API/reference example. No inferred
   dynamic import, name match or runtime call is claimed.
3. No code path added by this batch performs an implicit target import, target
   write, live model call or network access outside the existing opt-in
   reference sync. The Ocelot seed/boot apply tools remain explicit
   preview-first tools requiring `write: true`; real-checkout applies are
   approval-gated and are not exercised in this batch.
4. Target tests, the full repository suite and Ruff pass.
5. `scripts/docs/generate_docs.py --check` and
   `ozagia overview --check REPO_SHAPE.md` pass after regeneration.
6. The final diff is reviewable, with no unrelated edits and no unresolved
   TODO/FIXME in the changed code.

## Workstreams

### W1 — Ocelot workspace completion (agent-owned; Ocelot files only)

- Reconcile the plan header with the implemented second slice: seed editing,
  boot/registry editing, context/hook, offline planner evaluation, claim check
  and parity are implemented; the live agent-workflow/model-task evaluation is
  not run and stays approval-gated.
- Check `docs/ocelot_workspace.md` against the actual CLI and MCP surface. Add
  any missing command, state or limit; do not promise a UI panel.
- Confirm `docs/task_library.md` and
  `src/ozagia/task_specs/coding/ocelot_workspace.toml` describe the same task
  class, commands and evidence requirements.
- Reconcile `docs/results/ocelot_workspace_2026-11-26.md`,
  `docs/results/ocelot_workspace_eval_2026-11-26.md` and the eval case file:
  dates, roots, counts, pass/fail language and limits.
- Run `pytest -q tests/test_ocelot_workspace*.py tests/test_task_library.py`
  and Ruff on the Ocelot modules.
- Do not touch federation/core relationship files, Bigville docs, `docs/roadmap.md`,
  `docs/reference/` or `REPO_SHAPE.md`.

### W2 — Repository relationships and API completion (agent-owned; federation files only)

- Review the relationship/API implementation for edge cases and add focused
  tests where coverage is thin, especially:
  - mutual `uses`/`plugin_for` integration edges do not become dependency
    cycles;
  - API edges stay out of `dependencies`, order/levels and reverse build
    dependencies;
  - partial operation metadata and duplicate mappings merge without false
    ambiguity.
- Add a worked Bigville ↔ Ocelot federation example (mutual integration plus a
  family) to the federation docs/plan; make clear it is declared evidence,
  not inferred from dynamic imports.
- Reconcile the plan status and add a short "verification" record with the
  commands and results. Keep follow-ons explicit.
- Run the federation/API/Dependency/OpenAPI tests and Ruff.
- Do not touch Ocelot files, Bigville docs, `docs/roadmap.md`,
  `docs/reference/` or `REPO_SHAPE.md`.

### W3 — Bigville integration (agent-owned; Bigville docs only)

- Update `docs/plans/2026-11-26_bigville_support.md`: the cross-repository
  Ocelot item is now served by declared repository relationships/API
  references, not inferred links.
- Add a concrete Bigville/Ocelot federation manifest example to
  `docs/bigville_adapter.md`, with member ids, mutual `relationships` and a
  `families` entry; mention `ApiMapping`/OpenAPI references only where the
  repositories actually expose them, and label absent evidence.
- Keep the test-coverage audit's `advisory_only` conclusion unchanged.
- Run the Bigville adapter/project-invariant tests and Ruff.
- Do not touch core code, Ocelot files, federation docs, `docs/roadmap.md`,
  `docs/reference/` or `REPO_SHAPE.md`.

### W4 — Coordinator integration and verification

- Review W1–W3 diffs for overlapping edits and unsupported claims.
- Update `docs/roadmap.md` only if the plan/status changes require it.
- Run the full suite, Ruff, the docs generator check and the repo-shape check.
- Regenerate `docs/reference/` and `REPO_SHAPE.md` once, after all workstreams.
- Final report: changed files, checks, deferred items and the exact approval
  gate for live evaluation.

## Phase 2 — follow-ons requested after Phase 1

Phase 2 starts only after W4 is green. These are not Phase 1 completion
criteria, and each has its own acceptance gate.

### W5 — Adapter-declared dynamic bridges

**Goal:** let an adapter record a dynamic bridge it can see in project source
(for example Bigville's `import_module("worlds.bigville_ocelot_adapter")`)
without importing the target, executing it or guessing a relationship.

**Design:**

- Add an explicit `RepoBridge` snapshot node type emitted by adapters. Required
  attributes: `source_repo`, `target_repo`, a bounded `relation` (for example
  `dynamic_import`, `protocol`, `plugin`, `subprocess`), non-empty
  `provenance`, optional bounded `evidence` path/line entries, optional
  `symbol`, and `explicit: true`.
- Provide a small adapter helper that validates and emits the node; it performs
  no filesystem reads itself. The adapter owns discovery and must cite the
  source path/line.
- `repo_relations` reads `RepoBridge` nodes from member snapshots and adds
  `origin: "adapter_bridge"` integration edges. Source and target must resolve
  to selected members; unselected or external targets are reported as
  `unpaired`/`external`, never guessed. Bridges never enter the dependency
  view, so a dynamic cycle cannot create a build-order cycle.
- Exercise it in the Bigville adapter: emit the Bigville → Ocelot dynamic
  bridge with `bigville/backends/ocelot.py` evidence. Do not modify the Ocelot
  checkout.
- Tests: accepted explicit bridge; missing provenance / `explicit: false`
  rejected; unknown target reported; duplicates deduped; bridge edges stay out
  of cycles/order/reverse build dependencies; CLI/MCP report includes them.
- Acceptance: targeted tests and Ruff pass; no target import or write.

### W6 — Persisted relationship supergraph

**Goal:** persist the cross-repository graph so repeated queries are stable,
inspectable and usable without recomputing every manifest, without copying
every member node.

**Design:**

- Add a versioned `.ozagia/supergraph.json` artifact built by
  `ozagia federation build` (and optionally written by an explicit
  `relations --write`). It stores member metadata (id, root, head, snapshot
  hash, state), declared/package/API/bridge edges, families, API references,
  mappings and validation errors, plus a deterministic content hash. It does
  **not** persist every member node; member snapshots remain the source of
  graph detail. Call it the relationship supergraph, not a full node union.
- Freshness/refresh: key each member contribution by member id + snapshot hash
  + federation-manifest hash. An unchanged artifact is a cache hit; a changed
  source rebuilds the whole relationship report. Per-member fragment caching
  remains a follow-on. A missing, stale or corrupt artifact reports
  `absent`/`invalid`/`stale` with the rebuild command. Writes are atomic
  (temp + replace) and never touch a member snapshot.
- Query: `ozagia federation graph --root ...` reads a fresh artifact;
  `--refresh` rebuilds. `relations --write` persists and returns the graph with
  `persisted` metadata; `relations --cached` reads a fresh artifact and fails
  non-zero when absent/stale/invalid. `relations`/`check` remain functional
  without it. Avoid a new MCP tool unless the existing `federation_check`
  section is insufficient.
- Bounds: member count, edge/reference counts, artifact byte cap, and bounded
  errors. Tests cover deterministic build, fresh/stale/corrupt reads,
  source-hash freshness, atomic failure, explicit-output safety and no member
  writes.
- Acceptance: the on-demand `relations` report and the persisted report agree;
  targeted tests and Ruff pass. A full node-union supergraph is explicitly out
  of scope unless requested separately.

### W7 — Family-aware retrieval, gated by a paired evaluation

**Goal:** use repository families to rank related-repository hits and context
higher, but only if a paired offline evaluation shows a real gain.

**Design:**

- Add an opt-in family preference to federation search/context ranking
  (default off). Hits expose `family_match`; non-family hits remain visible and
  are only re-ranked. No repository is filtered out.
- Build a small multi-repo evaluation fixture with an anchor, a family member
  containing the answer, and an unrelated distractor repo. Compare baseline vs
  family-aware ranking at k=1/3/5 and MRR over the same frozen queries; record
  per-query deltas, sample size and limits.
- Gate: the default stays off unless the paired evaluation shows no regression
  and a meaningful gain. If the evidence is neutral or mixed, keep the arm
  available but off and document why. No live model calls.
- Tests: explicit flag only; family resolution; deterministic tie-breaking; no
  effect when no families exist. Evaluation report goes under
  `evaluation/` or `docs/results/`.
- Acceptance: opt-in code, paired offline report, unchanged default behavior.

### Phase 2 execution order

- W5 first: it adds the bridge node/evidence path and the federation report
  changes.
- W6 and W7 can then run in parallel if their file sets stay disjoint; W6 owns
  the persisted-supergraph module/CLI and federation docs, W7 owns search and
  the evaluation fixture/report. The coordinator runs the combined verification
  and only then decides whether the family arm stays enabled.

## Audit remediation

A read-only audit reviewed the combined batch after W7. These are the
coordinator-owned fixes; no item changes the declared product boundary.

| Item | Owner | Status |
| --- | --- | --- |
| Docs generator width/path reproducibility; then regenerate reference docs and `REPO_SHAPE.md` | coordinator/R4 | complete; 47 pages and REPO_SHAPE current |
| Plan statuses for W5–W7; relations-vs-check member-state wording | W2 | complete |
| API report `absent` vs `partial` when only rejected mappings exist | W2 | complete |
| Supergraph explicit `--out` inside a member checkout | W6 | complete |
| Bridge merged provenance/evidence bounds | W5 | complete |
| Ocelot eval command paths, failure exit code, recall caveat, cycle/context tests | W1 | complete; 132 Ocelot/task-library tests |
| Ocelot session-hint concurrency and no-session behavior | R3 | complete |
| Bigville example anchor membership | W3 | complete |
| Capability catalogue entries for supergraph/bridges/family ranking | coordinator | complete |
| Real-checkout seed/boot applies | approval-gated | not exercised |

## Non-goals and gates

- **Live Ocelot agent-workflow or model-task evaluation**: requires the
  existing `--live` / `OZAGIA_EVAL_LIVE=1` gates and a person's approval. This
  batch records the offline replay only.
- **Ocelot UI status panel**: not built; keep it a named follow-on.
- **Docs-derived API-reference source**: deferred; prose mentions are not
  promoted to API references.
- **Target repository writes**: none. Ocelot/Bigville checkouts are read-only;
  seed/boot edits remain preview-first and are not exercised against the real
  checkouts in this batch.
- **Network**: no network except the existing opt-in reference sync; tests use
  fixtures.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/python scripts/docs/generate_docs.py --check
.venv/bin/python -m ozagia overview --check REPO_SHAPE.md
```

Final run, 2026-11-27:

| Check | Result |
| --- | --- |
| Full suite | 7,318 passed, 27 skipped, 0 failed |
| Ruff | clean across `src`, `tests`, `addins`, evaluation and docs generator |
| Reference docs | 47 pages, up to date; deterministic at width 80 and 140 |
| `REPO_SHAPE.md` | current (`sha256:0e30fb9c...c746`) |
| Task library | 15 built-in classes, valid |
| Target writes / live calls | none; real Ocelot applies and live evaluation remain approval-gated |

## Risks

- Duplicate or contradictory status edits across plans. File ownership and the
  coordinator review prevent this.
- Generated reference docs becoming stale after doc changes. Regenerate once at
  the end, not per workstream.
- Overstating Ocelot/Bigville coverage or API compatibility. Keep the existing
  "advisory", "declared" and "unknown" language.
- Full-suite runtime. Run targeted tests per agent and the full suite once at
  the end; do not run live/model evaluations.
