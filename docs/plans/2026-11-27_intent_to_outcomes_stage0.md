# Intent-to-outcomes Stage 0 execution

Status: complete 2026-11-27. Governing documents:
[product specification](../intent_to_outcomes_spec.md) and
[staged delivery plan](2026-11-27_intent_to_outcomes_delivery.md). The
[interface inventory](../results/intent_to_outcomes_inventory_2026-11-27.md)
and [baseline decision](../results/intent_to_outcomes_stage0_baseline_2026-11-27.md)
are recorded; [Stage 1](2026-11-27_intent_to_outcomes_stage1.md) is executing.

Stage 0 selects and baselines one real workflow and settles the reuse
architecture before Stage 1 code. This execution slice is read-only outside
scratch directories: no target checkout writes, no live model calls, no service
cutover.

## Stage 0 outputs

1. `docs/results/intent_to_outcomes_inventory_2026-11-27.md` — an interface
   inventory and gap map for existing intent/work, task, goal, session, graph,
   evidence, aperture, action, grant, review, mesh and daemon surfaces.
2. `docs/results/intent_to_outcomes_stage0_baseline_2026-11-27.md` — the
   selected real workflow, pinned revisions, usual-process baseline, journalist
   ownership map, memory/UI projection baseline, and the graph-native versus
   separate-DAG-runner decision with reasons.
3. `docs/plans/2026-11-27_intent_to_outcomes_stage1.md` — the Stage 1 vertical
   slice derived from those findings, with file-level ownership and acceptance
   tests.

## Workstreams

### S0-A — Existing interface inventory

Read the current implementation and map each spec contract to reusable
surfaces, narrow adapters and missing guarantees:

- task, goal, session, evidence, action, aperture, grant, review, mesh and
  daemon modules;
- task profiles, preflight, execution, scheduling, budgets and replay;
- graph persistence and query surfaces;
- MCP, CLI and library entry points;
- existing tests and documented limits.

The report must cite files and symbols, distinguish implemented behavior from
proposal, and recommend what Stage 1 can reuse directly versus wrap.

### S0-B — Baseline workflow and architecture decision

Inspect the available Dev Manager, Ocelot and Ozma checkouts and their cited
documents. Select one small Dev Manager change-and-review workflow with a
clear verification recipe and one meaningful steering point. Record:

- pinned revisions and interface choices;
- the usual preparation, outcome criteria, elapsed time, interruptions and
  context-regain effort;
- an ownership map for intent, graph state, aperture, effect execution,
  review and delivery;
- the graph-native versus separate-DAG-runner comparison against recovery,
  revision binding, revocation, effect replay and migration;
- workspace-to-graph binding evidence and the limits of the existing smoke;
- idle/peak memory and process-count baseline if measurable without changing
  the workflow;
- a small UI projection sketch that supports a list-first view and generic
  extension rendering.

No target checkout is modified and no Dev Manager cutover is attempted.

## State at the end of Stage 0

The coordinator reviews both reports, resolves disagreements, and writes the
Stage 1 plan. Stage 1 is expected to start with a durable local outcome:
typed intent/work/attempt/event/evidence records, a configurable gate, restart
recovery, a versioned status/operations view and a fixture effect adapter, with
the real edit transaction used only through an explicit adapter boundary.

## Verification

- Reports cite concrete files, symbols and revisions.
- Every reused surface is labelled implemented, partial or absent.
- The selected workflow has a reproducible baseline and a named owner for each
  lifecycle decision.
- No target writes, network access, live model calls or commits occur during
  this stage.
