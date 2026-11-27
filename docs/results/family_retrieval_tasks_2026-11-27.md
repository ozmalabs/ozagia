# Family-aware federation search on curated Bigville tasks — 2026-11-27

> **Scope:** real repository snapshots plus the existing curated Bigville retrieval tasks. This is a robustness check, not a representative family benchmark. The default remains `family_boost=False`.

## Setup

The temporary federation from `real_repos.py` is reused: Bigville is the anchor, Ocelot is in the `simulation-stack` family, and Ozagia is an unrelated distractor. The task text and expected files come from `evaluation/bigville_retrieval/tasks.json`; they were not rewritten for this check.

## Aggregate results

| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |
| --- | --- | ---: | ---: | ---: | ---: |
| family | `baseline` | 0.0% | 0.0% | 0.0% | 0.019 |
| family | `family_boost` | 0.0% | 0.0% | 0.0% | 0.020 |
| controls | `baseline` | 50.0% | 100.0% | 100.0% | 0.667 |
| controls | `family_boost` | 50.0% | 100.0% | 100.0% | 0.667 |

## Family tasks

| Task | Baseline rank | Boosted rank | Delta | Expected |
| --- | ---: | ---: | ---: | --- |
| `game-checkpoint-roundtrip` | None | None | None | `bigville/game_persistence.py`, `tests/test_bigville_game_persistence.py` |
| `deterministic-population-report` | None | None | None | `bigville/reporting.py`, `tests/test_phase091_food_reporting.py` |
| `lazy-affordance-mode` | None | None | None | `bigville/game.py`, `tests/test_lazy_evaluation.py` |
| `tick-graph-resource-conflicts` | None | None | None | `worlds/tick_graph.py`, `tests/test_tick_graph_executor.py` |
| `seed-manifest-recovery` | 19 | 19 | 0 | `bigville/runtime.py`, `tests/test_node_origins.py` |
| `backend-action-boundary` | None | None | None | `bigville/backends/protocol.py`, `bigville/game.py`, `tests/test_bigville_backends.py` |
| `scenario-input-validation` | 10 | 9 | -1 | `bigville/scenarios.py`, `tests/test_bigville_scenarios.py` |
| `public-game-construction` | None | None | None | `bigville/api.py`, `tests/test_engine_defaults.py` |

## Controls

| Control | Baseline rank | Boosted rank | Delta | Expected |
| --- | ---: | ---: | ---: | --- |
| `ozagia-federation-control` | 1 | 1 | 0 | `docs/federation.md` |
| `ozagia-supergraph-control` | 3 | 3 | 0 | `src/ozagia/core/supergraph.py` |

## Decision

The default remains `family_boost=False`. This check does not show a family-ranking gain: most curated task answers fall outside the federation lexical top 20, and the visible ranks barely move. It also shows that the curated task set, designed for richer single-repository retrieval, is not a suitable family benchmark. It is not authorization to change the default; a representative paired evaluation on real family-relevant queries with adequate retrieval is still required.

## Limits

- One anchor, one family member and one distractor; the task set is small and Bigville-only.
- The tasks were curated for source localisation, not for family-aware ranking; they cannot estimate the rate of family-relevant queries.
- Answers are matched by File/Doc path; a matching symbol or snippet node is not counted separately.
- The temporary member roots differ from the recorded checkout roots, so federation reports the snapshots stale; search still ranks them.
- Lexical search only; no document bodies, embeddings, model calls, network access or target writes.

## Reproduce

```sh
python evaluation/family_retrieval/real_repos.py
python evaluation/family_retrieval/real_tasks.py
```

The first command rebuilds the temporary federation; the second reads it and writes the JSON and Markdown reports.
