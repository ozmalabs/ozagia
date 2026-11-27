# Repo-map retrieval evaluation

- Root: `/applications/data/projects/bigville`
- Tasks: 6 (`/home/matt/ozmadev/ozagia/evaluation/bigville_retrieval/adapter_tasks.json`)
- Source-file universe: 501
- Budgets: 300, 700, 1300 tokens
- Arms: baseline, dev_context, dev_repo_map, both, grep, code_search
- Dev-memory snapshot: stale=False (snapshot head `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`, HEAD `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`, root mismatch False, allow_stale=False)
- Deterministic fingerprint: `b183c58acf3bfe809de4c5df7da63d82f98632c664cb7070dca0e54906a5ee7b`
- Wall time: 19.958 s
- Warning: expected files outside the indexed source universe: `bigville/seeds/bigville_bell_schedule.json`, `bigville/seeds/bigville_bells.json`, `bigville/seeds/bigville_terrain_movement.json`, `bigville/seeds/conversation.json`, `bigville/seeds/conversation_reply.json`, `bigville/seeds/gen_charter_bigville_council_charter.json`, `packs/bigville/frames/charters.json`, `packs/bigville/pack.json`, `packs/bigville/world/terrain_movement.json`, `scenarios/house_argument.json`, `scenarios/monopoly_four_players.json`, `seeds/bigville_bells.json`, `seeds/conversation.json`, `seeds/conversation_reply.json`

## Aggregate metrics

| Budget | Arm | Tasks | Mean recall@budget | Mean recall (content tier) | Mean precision | Mean evidence coverage | Mean evidence symbols | Mean evidence/1k tokens | Mean files | Mean content files | Mean tokens | Mean s | Cache parsed | Cache reused |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 300 | baseline | 6 | 0.000 | 0.000 | 0.000 | 0.108 | 0.0 | 15.62 | 43.0 | 0.0 | 297 | 0.00 | 0 | 0 |
| 300 | dev_context | 6 | 0.083 | 0.083 | 0.028 | 0.247 | 0.0 | 34.96 | 2.8 | 2.8 | 268 | 0.38 | 0 | 0 |
| 300 | dev_repo_map | 6 | 0.083 | 0.083 | 0.042 | 0.171 | 1.5 | 26.15 | 5.0 | 2.8 | 283 | 0.53 | 0 | 2988 |
| 300 | both | 6 | 0.083 | 0.083 | 0.021 | 0.290 | 1.5 | 24.46 | 7.2 | 5.2 | 551 | 0.91 | 0 | 2988 |
| 300 | grep | 6 | 0.167 | 0.000 | 0.009 | 0.219 | 0.0 | 34.07 | 39.0 | 0.0 | 295 | 0.04 | 0 | 0 |
| 300 | code_search | 6 | 0.083 | 0.083 | 0.015 | 0.614 | 33.0 | 176.20 | 10.7 | 10.7 | 281 | 0.49 | 0 | 0 |
| 700 | baseline | 6 | 0.167 | 0.000 | 0.004 | 0.226 | 0.0 | 17.68 | 94.0 | 0.0 | 693 | 0.00 | 0 | 0 |
| 700 | dev_context | 6 | 0.083 | 0.083 | 0.056 | 0.301 | 0.0 | 20.44 | 2.7 | 2.7 | 695 | 0.38 | 0 | 0 |
| 700 | dev_repo_map | 6 | 0.083 | 0.083 | 0.009 | 0.512 | 6.3 | 39.24 | 16.3 | 11.3 | 696 | 0.53 | 0 | 2988 |
| 700 | both | 6 | 0.083 | 0.083 | 0.008 | 0.580 | 6.3 | 25.91 | 17.3 | 12.8 | 1390 | 0.91 | 0 | 2988 |
| 700 | grep | 6 | 0.167 | 0.000 | 0.004 | 0.313 | 0.0 | 24.18 | 90.2 | 0.0 | 698 | 0.04 | 0 | 0 |
| 700 | code_search | 6 | 0.083 | 0.083 | 0.006 | 0.755 | 67.0 | 135.42 | 25.2 | 25.2 | 680 | 0.49 | 0 | 0 |
| 1300 | baseline | 6 | 0.167 | 0.000 | 0.002 | 0.266 | 0.0 | 12.32 | 153.0 | 0.0 | 1293 | 0.00 | 0 | 0 |
| 1300 | dev_context | 6 | 0.083 | 0.083 | 0.042 | 0.312 | 0.0 | 12.49 | 2.8 | 2.8 | 1168 | 0.38 | 0 | 0 |
| 1300 | dev_repo_map | 6 | 0.083 | 0.083 | 0.008 | 0.768 | 14.7 | 42.32 | 21.2 | 20.7 | 1130 | 0.53 | 0 | 2988 |
| 1300 | both | 6 | 0.083 | 0.083 | 0.008 | 0.807 | 14.7 | 26.03 | 21.8 | 21.5 | 2299 | 0.92 | 0 | 2988 |
| 1300 | grep | 6 | 0.167 | 0.000 | 0.002 | 0.352 | 0.0 | 16.31 | 163.5 | 0.0 | 1297 | 0.04 | 0 | 0 |
| 1300 | code_search | 6 | 0.083 | 0.083 | 0.003 | 0.858 | 106.7 | 108.44 | 46.8 | 46.8 | 1286 | 0.49 | 0 | 0 |

## Recall by task

Each cell is `recall (recall_content)`.

### 300 tokens

| Task | baseline | dev_context | dev_repo_map | both | grep | code_search |
|---|---:|---:|---:|---:|---:|---:|
| scenario-seed-ambiguity | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| generated-charter-source-pack | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| generated-terrain-movement-source-pack | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| seed-dependency-ambiguous-copy | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| monopoly-scenario-seed-ambiguity | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| backend-profiles-browser-server-route | 0.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) | 0.50 (0.50) | 1.00 (0.00) | 0.50 (0.50) |

### 700 tokens

| Task | baseline | dev_context | dev_repo_map | both | grep | code_search |
|---|---:|---:|---:|---:|---:|---:|
| scenario-seed-ambiguity | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| generated-charter-source-pack | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| generated-terrain-movement-source-pack | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| seed-dependency-ambiguous-copy | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| monopoly-scenario-seed-ambiguity | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| backend-profiles-browser-server-route | 1.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) | 0.50 (0.50) | 1.00 (0.00) | 0.50 (0.50) |

### 1300 tokens

| Task | baseline | dev_context | dev_repo_map | both | grep | code_search |
|---|---:|---:|---:|---:|---:|---:|
| scenario-seed-ambiguity | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| generated-charter-source-pack | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| generated-terrain-movement-source-pack | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| seed-dependency-ambiguous-copy | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| monopoly-scenario-seed-ambiguity | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) | 0.00 (0.00) |
| backend-profiles-browser-server-route | 1.00 (0.00) | 0.50 (0.50) | 0.50 (0.50) | 0.50 (0.50) | 1.00 (0.00) | 0.50 (0.50) |

## Task set

| Task | Style | Area | Expected files | Notes |
|---|---|---|---|---|
| scenario-seed-ambiguity | symptom | seeds_boot | `scenarios/house_argument.json`<br>`seeds/conversation.json`<br>`bigville/seeds/conversation.json` | Exercises scenario-to-seed navigation and path-distinct duplicate ID handling. |
| generated-charter-source-pack | symptom | seeds_boot | `bigville/seeds/gen_charter_bigville_council_charter.json`<br>`packs/bigville/pack.json`<br>`packs/bigville/frames/charters.json` | Exercises generated seed to pack table provenance navigation. |
| generated-terrain-movement-source-pack | symptom | seeds_boot | `bigville/seeds/bigville_terrain_movement.json`<br>`packs/bigville/pack.json`<br>`packs/bigville/world/terrain_movement.json` | Second generated seed/table example from a different pack domain. |
| seed-dependency-ambiguous-copy | symptom | seeds_boot | `bigville/seeds/bigville_bell_schedule.json`<br>`seeds/bigville_bells.json`<br>`bigville/seeds/bigville_bells.json` | Exercises a manifest dependency edge with duplicate path-distinct target IDs. |
| monopoly-scenario-seed-ambiguity | symptom | seeds_boot | `scenarios/monopoly_four_players.json`<br>`seeds/conversation.json`<br>`bigville/seeds/conversation.json`<br>`seeds/conversation_reply.json`<br>`bigville/seeds/conversation_reply.json` | Second scenario exercises ambiguity across two seed IDs rather than one reference. |
| backend-profiles-browser-server-route | symptom | worlds_or_tools | `bigville/management/http.py`<br>`bigville/management/static/management.js` | Exercises explicit browser request to server route joining. |

## Reading these numbers

- This is a **retrieval instrument**, not an end-to-end coding benchmark: it measures whether known-relevant files are selected, not whether an agent completes a change.
- `baseline` is a sorted-path file-listing control, not a semantic retriever. `grep` is a lexical control: it counts distinct task words found in each file's text.
- `recall` counts a hit at any tier. `recall_content` counts only hits whose file the arm showed as code (code_search snippet, dev_context item with snippet text, or repo-map `full`/`outline` tier). A listing-tier hit tells the reader the path exists, not what it contains.
- Token estimates are each module's own ~4-chars/token heuristic. Cache counts apply only to repo-map arms.
- `both` is the literal union of the two retrievers run at the same budget; its token cost and latency are the sum of the components, so its budget is per component (up to 2x the nominal budget). Compare it as a cost/recall trade-off, not as an equal-token arm.
- `code_search` runs once per task; its elapsed time is the same call's time at every budget.
- Ground truth is frozen in the task JSON. Re-run the scoped tests after moving or renaming an expected file.
