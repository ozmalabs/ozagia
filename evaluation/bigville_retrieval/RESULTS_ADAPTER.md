# Repo-map retrieval evaluation

- Root: `/applications/data/projects/bigville`
- Tasks: 8 (`/home/matt/ozmadev/ozagia/evaluation/bigville_retrieval/tasks.json`)
- Source-file universe: 501
- Budgets: 300, 700, 1300 tokens
- Arms: dev_context, dev_repo_map, both
- Dev-memory snapshot: stale=False (snapshot head `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`, HEAD `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`, root mismatch False, allow_stale=False)
- Deterministic fingerprint: `e4679e95dc4ccb505c5d51c2c5c62860ee596432b8099fd8f596af204c928e9e`
- Wall time: 26.183 s

## Aggregate metrics

| Budget | Arm | Tasks | Mean recall@budget | Mean recall (content tier) | Mean precision | Mean evidence coverage | Mean evidence symbols | Mean evidence/1k tokens | Mean files | Mean content files | Mean tokens | Mean s | Cache parsed | Cache reused |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 300 | dev_context | 8 | 0.000 | 0.000 | 0.000 | 0.069 | 0.0 | 11.58 | 0.5 | 0.5 | 64 | 0.49 | 0 | 0 |
| 300 | dev_repo_map | 8 | 0.292 | 0.292 | 0.202 | 0.130 | 1.8 | 22.50 | 4.4 | 2.6 | 270 | 0.62 | 0 | 3984 |
| 300 | both | 8 | 0.292 | 0.292 | 0.181 | 0.199 | 1.8 | 28.03 | 4.9 | 3.1 | 334 | 1.11 | 0 | 3984 |
| 700 | dev_context | 8 | 0.062 | 0.062 | 0.021 | 0.226 | 0.0 | 19.79 | 1.6 | 1.6 | 493 | 0.53 | 0 | 0 |
| 700 | dev_repo_map | 8 | 0.292 | 0.292 | 0.058 | 0.422 | 7.4 | 35.07 | 11.2 | 9.2 | 685 | 0.54 | 0 | 3984 |
| 700 | both | 8 | 0.354 | 0.354 | 0.066 | 0.510 | 7.4 | 28.91 | 12.4 | 10.4 | 1178 | 1.07 | 0 | 3984 |
| 1300 | dev_context | 8 | 0.062 | 0.062 | 0.016 | 0.378 | 0.0 | 21.68 | 4.0 | 4.0 | 999 | 0.49 | 0 | 0 |
| 1300 | dev_repo_map | 8 | 0.333 | 0.333 | 0.038 | 0.615 | 15.4 | 34.54 | 19.6 | 18.6 | 1226 | 0.54 | 0 | 3984 |
| 1300 | both | 8 | 0.396 | 0.396 | 0.039 | 0.665 | 15.4 | 24.58 | 22.2 | 21.2 | 2225 | 1.03 | 0 | 3984 |

## Recall by task

Each cell is `recall (recall_content)`.

### 300 tokens

| Task | dev_context | dev_repo_map | both |
|---|---:|---:|---:|
| game-checkpoint-roundtrip | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| deterministic-population-report | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| lazy-affordance-mode | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| tick-graph-resource-conflicts | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| seed-manifest-recovery | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| backend-action-boundary | 0.00 (0.00) | 0.33 (0.33) | 0.33 (0.33) |
| scenario-input-validation | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| public-game-construction | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |

### 700 tokens

| Task | dev_context | dev_repo_map | both |
|---|---:|---:|---:|
| game-checkpoint-roundtrip | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| deterministic-population-report | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| lazy-affordance-mode | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| tick-graph-resource-conflicts | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| seed-manifest-recovery | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| backend-action-boundary | 0.00 (0.00) | 0.33 (0.33) | 0.33 (0.33) |
| scenario-input-validation | 0.50 (0.50) | 0.00 (0.00) | 0.50 (0.50) |
| public-game-construction | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |

### 1300 tokens

| Task | dev_context | dev_repo_map | both |
|---|---:|---:|---:|
| game-checkpoint-roundtrip | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| deterministic-population-report | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| lazy-affordance-mode | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| tick-graph-resource-conflicts | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| seed-manifest-recovery | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) |
| backend-action-boundary | 0.00 (0.00) | 0.67 (0.67) | 0.67 (0.67) |
| scenario-input-validation | 0.50 (0.50) | 0.00 (0.00) | 0.50 (0.50) |
| public-game-construction | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |

## Task set

| Task | Style | Area | Expected files | Notes |
|---|---|---|---|---|
| game-checkpoint-roundtrip | symptom | worlds_or_tools | `bigville/game_persistence.py`<br>`tests/test_bigville_game_persistence.py` | Session checkpoint implementation and focused roundtrip tests. |
| deterministic-population-report | symptom | worlds_or_tools | `bigville/reporting.py`<br>`tests/test_phase091_food_reporting.py` | Reporting computes exact values and seeded finite population sample estimates. |
| lazy-affordance-mode | symptom | worlds_or_tools | `bigville/game.py`<br>`tests/test_lazy_evaluation.py` | Mode resolution lives in Game; lazy evaluation behavior has dedicated tests. |
| tick-graph-resource-conflicts | symptom | worlds_or_tools | `worlds/tick_graph.py`<br>`tests/test_tick_graph_executor.py` | Pure claim arbitration and its executor tests are the relevant implementation and validation sites. |
| seed-manifest-recovery | symptom | seeds_boot | `bigville/runtime.py`<br>`tests/test_node_origins.py` | Graph checkpoint encoding and seed manifest validation are implemented in runtime; node provenance tests cover loaded origins. |
| backend-action-boundary | symptom | worlds_or_tools | `bigville/backends/protocol.py`<br>`bigville/game.py`<br>`tests/test_bigville_backends.py` | Backend interface, game decision loop, and backend contract tests. |
| scenario-input-validation | symptom | worlds_or_tools | `bigville/scenarios.py`<br>`tests/test_bigville_scenarios.py` | Scenario loader and scenario validation suite. |
| public-game-construction | symptom | worlds_or_tools | `bigville/api.py`<br>`tests/test_engine_defaults.py` | Public API construction and defaults tests. |

## Reading these numbers

- This is a **retrieval instrument**, not an end-to-end coding benchmark: it measures whether known-relevant files are selected, not whether an agent completes a change.
- `baseline` is a sorted-path file-listing control, not a semantic retriever. `grep` is a lexical control: it counts distinct task words found in each file's text.
- `recall` counts a hit at any tier. `recall_content` counts only hits whose file the arm showed as code (code_search snippet, dev_context item with snippet text, or repo-map `full`/`outline` tier). A listing-tier hit tells the reader the path exists, not what it contains.
- Token estimates are each module's own ~4-chars/token heuristic. Cache counts apply only to repo-map arms.
- `both` is the literal union of the two retrievers run at the same budget; its token cost and latency are the sum of the components, so its budget is per component (up to 2x the nominal budget). Compare it as a cost/recall trade-off, not as an equal-token arm.
- `code_search` runs once per task; its elapsed time is the same call's time at every budget.
- Ground truth is frozen in the task JSON. Re-run the scoped tests after moving or renaming an expected file.
