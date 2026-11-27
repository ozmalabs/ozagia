# Family-aware federation retrieval on real snapshots — 2026-11-27

> **Scope:** real repository snapshots, constructed frozen queries. This is not a general retrieval benchmark and does not authorize a default change. The default remains `family_boost=False`.

## Setup

The script rebuilds read-only graph snapshots from three real checkouts into private temporary member directories. Bigville is the anchor; Ocelot is a declared family member; Ozagia is an unrelated distractor. The anchor's temporary manifest declares the `simulation-stack` family with Bigville and Ocelot. Adapter indexers are not loaded and no target project is imported.

| Member | Checkout | HEAD | Snapshot nodes | Snapshot docs |
| --- | --- | --- | ---: | ---: |
| `bigville` | `/applications/data/projects/bigville` | `e243ea70eee2` | 9385 | 230 |
| `ocelot` | `/applications/data/projects/ocelot` | `b7fda1c9dcc7` | 87857 | 4710 |
| `ozagia` | `/home/matt/ozmadev/ozagia` | `dc3bc2367dcc` | 20470 | 324 |

- Sample: 5 queries (4 constructed family queries, 1 control).
- Arms: baseline (`family_boost=False`) and family-aware (`family_boost=True`, additive rank boost 0.15, capped at 1.0).
- Retrieval depth: top 20 lexical hits; metrics at k=1, 3 and 5 plus MRR.
- Snapshot state is expected to read `stale` because the temporary member roots differ from the recorded checkout roots; search still ranks the loaded snapshots.

## Queries

| Query | Kind | Expected answer (Bigville unless control) | Ozagia distractor |
| --- | --- | --- | --- |
| `bigville-scenario-coverage-near-tie` | family | `docs/SIMPLE_SCENARIO_COVERAGE_AUDIT.md` | `docs/results/bigville_test_coverage_audit_2026-11-27.md` |
| `bigville-boundary-audit-near-tie` | family | `docs/SIMPLE_BOUNDARY_AUDIT.md` | `docs/results/bigville_test_coverage_audit_2026-11-27.md` |
| `bigville-test-plan-near-tie` | family | `docs/SIMPLE_VILLAGE_TEST_PLAN.md` | `docs/results/bigville_test_coverage_audit_2026-11-27.md` |
| `bigville-ocelot-integration-near-tie` | family | `docs/ocelot-integration-baseline.md` | `docs/plans/2026-11-26_ocelot_workspace.md` |
| `ozagia-federation-control` | control | `docs/federation.md` | — |

## Aggregate results

| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |
| --- | --- | ---: | ---: | ---: | ---: |
| all queries | `baseline` | 20.0% | 80.0% | 80.0% | 0.483 |
|  | `family_boost` | 100.0% | 100.0% | 100.0% | 1.000 |
| family queries | `baseline` | 0.0% | 75.0% | 75.0% | 0.354 |
|  | `family_boost` | 100.0% | 100.0% | 100.0% | 1.000 |
| control query | `baseline` | 100.0% | 100.0% | 100.0% | 1.000 |
|  | `family_boost` | 100.0% | 100.0% | 100.0% | 1.000 |

## Per-query results

| Query | Baseline rank | Boosted rank | Δ rank | Distractor baseline → boosted | Baseline RR | Boosted RR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `bigville-scenario-coverage-near-tie` | 2 | 1 | -1 | 1 → 3 | 0.500 | 1.000 |
| `bigville-boundary-audit-near-tie` | 2 | 1 | -1 | 1 → 4 | 0.500 | 1.000 |
| `bigville-test-plan-near-tie` | 3 | 1 | -2 | 1 → 4 | 0.333 | 1.000 |
| `bigville-ocelot-integration-near-tie` | 12 | 1 | -11 | 1 → 11 | 0.083 | 1.000 |
| `ozagia-federation-control` | 1 | 1 | 0 | — | 1.000 | 1.000 |

## Findings

- The family arm moved 4 of 4 family answers to rank 1. The four expected answers moved from ranks 2, 2, 3, 12 to rank 1; the lexical distractor ranks were preserved and reordered, not filtered.
- The control answer is rank 1 in both arms and its metrics are unchanged: `True`.
- Checks passed: `True`.
- The gain is confined to a constructed near-tie regime: each family distractor is within one query term of the answer. This confirms the ranking mechanism on real snapshots, but it does not estimate how often real queries fall in that regime.

## Decision

The arm stays opt-in and the default remains `family_boost=False`. This run is evidence about the mechanism, not a default-change authorization: the query set is small and was selected after inspecting real file titles. A representative paired evaluation on real repositories and queries, with no regression and a meaningful gain, is still required.

## Limits

- One anchor, one declared family member and one unrelated distractor; four constructed family queries plus one control.
- The family queries were selected after reading real document titles and paths as near-ties inside the fixed +0.15 boost. They measure the mechanism in this regime, not the rate at which real queries are near-ties.
- Lexical search only: it matches node type, key and string attrs such as name/path/title. It does not read document bodies, use embeddings, call a model or fetch the network.
- Snapshots are built with --docs features --no-git. Adapter indexers are not loaded, so adapter-produced nodes are absent. The Ocelot and Bigville story is still real source/document data.
- Snapshots live in temporary member roots while their Repo attrs record the real checkouts. Federation therefore reports the snapshots as stale with 'snapshot root differs from checkout path'; search still loads and ranks them.
- Expected answers are Doc nodes. The matching File node can also appear separately and is not counted as the answer.
- The temporary build configurations are evaluation choices (for example, skipped generated run trees); they are recorded for reproduction but are not target-project configuration changes.
- The script's own JSON/Markdown reports are excluded from the temporary Ozagia snapshot when they lie under that checkout, so a repeated run does not index its previous report as corpus.
- No target checkout is imported or written; no graph file is committed; no commit, network or model call is made.

## Reproduce

```sh
.venv/bin/python evaluation/family_retrieval/real_repos.py
```

The script rebuilds the three snapshots under `/tmp/opencode/family-real` and writes this Markdown file plus the sibling JSON report. It does not write into the target checkouts.
