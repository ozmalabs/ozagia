# Federated code search on curated Bigville tasks — 2026-11-27

> **Scope:** opt-in code search over current member source plus the existing curated Bigville retrieval tasks. This is not a family benchmark. Code mode is relevance-ranked only; the shipped code API, CLI and MCP surface have no family boost. The `experimental` row applies the rejected +0.15 rule locally for evidence only. Graph-mode family ranking is unchanged and remains opt-in.

## Setup

The temporary federation from `real_repos.py` is reused: Bigville is the anchor, Ocelot is in the `simulation-stack` family, and Ozagia is an unrelated distractor. Source directories are staged read-only into the temporary member roots so current-source search can read them. The task text and expected files come from `evaluation/bigville_retrieval/tasks.json`; they were not rewritten.

## Shipped status

The experimental code-mode family arm was rejected and removed. `federation.code_search`, `ozagia federation code-search` and MCP `federation_search` with `mode="code"` are relevance-ranked only; MCP rejects `family_boost` in code mode. Graph-mode `federation.search(family_boost=...)` is unchanged.

## Aggregate results

| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |
| --- | --- | ---: | ---: | ---: | ---: |
| family | `baseline` | 62.5% | 62.5% | 62.5% | 0.625 |
| family | `experimental +0.15 (rejected)` | 62.5% | 62.5% | 62.5% | 0.625 |
| controls | `baseline` | 0.0% | 50.0% | 50.0% | 0.167 |
| controls | `experimental +0.15 (rejected)` | 0.0% | 0.0% | 0.0% | 0.000 |

## Family tasks

| Task | Baseline rank | Experimental rank | Delta | Expected |
| --- | ---: | ---: | ---: | --- |
| `game-checkpoint-roundtrip` | — | — | — | `bigville/game_persistence.py`, `tests/test_bigville_game_persistence.py` |
| `deterministic-population-report` | 1 | 1 | 0 | `bigville/reporting.py`, `tests/test_phase091_food_reporting.py` |
| `lazy-affordance-mode` | 1 | 1 | 0 | `bigville/game.py`, `tests/test_lazy_evaluation.py` |
| `tick-graph-resource-conflicts` | 1 | 1 | 0 | `worlds/tick_graph.py`, `tests/test_tick_graph_executor.py` |
| `seed-manifest-recovery` | 1 | 1 | 0 | `bigville/runtime.py`, `tests/test_node_origins.py` |
| `backend-action-boundary` | 1 | 1 | 0 | `bigville/backends/protocol.py`, `bigville/game.py`, `tests/test_bigville_backends.py` |
| `scenario-input-validation` | — | — | — | `bigville/scenarios.py`, `tests/test_bigville_scenarios.py` |
| `public-game-construction` | — | — | — | `bigville/api.py`, `tests/test_engine_defaults.py` |

## Controls

| Control | Source universe | Baseline rank | Experimental rank | Delta | Expected |
| --- | --- | ---: | ---: | ---: | --- |
| `ozagia-federation-control` | outside code-search source universe | — | — | — | `docs/federation.md` |
| `ozagia-supergraph-control` | code | 3 | — | — | `src/ozagia/core/supergraph.py` |

The Markdown control is outside the code arm's source universe, so its missing rank is not a code-search recall miss. The rejected experimental family arm regressed the rankable control(s): `ozagia-supergraph-control`.

## Cache and members

- Cache directory: `/tmp/opencode/family-real/bigville/.ozagia/federation-search` (`anchor-state`, fallback: `False`).
- Cache files: 3; outside non-anchor members: `True`.
- Member snapshot states:
  - `bigville`: state `stale`, source `ok`, snapshot `/tmp/opencode/family-real/bigville/.ozagia/graph.json`.
  - `ocelot`: state `stale`, source `ok`, snapshot `/tmp/opencode/family-real/ocelot/.ozagia/graph.json`.
  - `ozagia`: state `stale`, source `ok`, snapshot `/tmp/opencode/family-real/ozagia/.ozagia/graph.json`.

## Comparison with snapshot-only graph search

Prior graph run: `docs/results/family_retrieval_tasks_2026-11-27.json`. The source universes differ: graph search reads snapshots, code search reads current source code files. Both use the same curated tasks.

| Group | Metric | Graph | Code | Delta |
| --- | --- | ---: | ---: | ---: |
| family | hit@1 | 0.000 | 0.625 | +0.625 |
| family | hit@3 | 0.000 | 0.625 | +0.625 |
| family | hit@5 | 0.000 | 0.625 | +0.625 |
| family | mrr | 0.019 | 0.625 | +0.606 |
| controls | hit@1 | 0.500 | 0.000 | -0.500 |
| controls | hit@3 | 1.000 | 0.500 | -0.500 |
| controls | hit@5 | 1.000 | 0.500 | -0.500 |
| controls | mrr | 0.667 | 0.167 | -0.500 |

## Decision

The shipped code arm is relevance-ranked only. The experimental +0.15 family rule was rejected: it did not improve any curated family task and moved the rankable out-of-family supergraph control out of the top 20. It is not exposed by `federation.code_search`, the federation CLI or MCP code mode. The baseline code arm materially improves the curated-task result over snapshot-only graph search (62.5% versus 0% hit@5). Graph-mode family ranking remains unchanged and opt-in.

## Limits

- The source trees are staged into the temporary members from the real checkouts; generated/run directories and adapters are excluded.
- Snapshot state is reported separately. A hit from current source is not proof that the member snapshot is fresh.
- Code search indexes code-file extensions only. The Markdown control (`docs/federation.md`) is outside its source universe.
- One anchor, one family member and one distractor; the task set is small and Bigville-only.
- Answers are matched by exact member-relative path; a matching symbol or snippet is not counted separately.
- The cache is written under the anchor's temporary state directory. No non-anchor member is written and no index is refreshed.
- The `experimental` row is a local evaluation-only arm, not a shipped API option; it applies the old +0.15 rule over a deeper candidate pool after relevance ranking.
- Lexical search only; no embeddings, model calls, network access or target writes.

## Reproduce

```sh
python evaluation/family_retrieval/real_repos.py
python evaluation/family_retrieval/real_tasks_code.py
```

The first command rebuilds the temporary federation; the second stages source read-only into its member roots and writes the JSON and Markdown reports.
