# M6 live map-arm comparison (local Qwen) — 2026-11-18

Status: **completed live run; invalid for the map-arm question.** The run made
40 local-model sessions with no timeouts or driver errors, but the `maps` arm
never called `dev_maps` (0 calls in all 20 maps sessions). The measured
difference is therefore **baseline vs the other Ozagia MCP read tools**, not
baseline vs the M6 map path. The preregistered thresholds were not met, and
they cannot be interpreted as evidence that maps help or hurt.

The design, corpus, assertions, schedule and thresholds were frozen in
[`evaluation/m6_live/preregistration.md`](../../evaluation/m6_live/preregistration.md)
before the first session. No threshold or corpus was changed after the run.

## Design and gates

- 20 read-only tasks, 10 about the self-graph/maps/work-evidence/preflight/
  action/evidence-store/prefetch/model-API/Ocelot work and 10 general Ozagia
  navigation/safety tasks.
- Arms: `baseline` (plain OpenCode; read/grep/glob only) and `maps` (the same
  permissions plus the Ozagia full MCP server, one read-only tool per action).
- 20 tasks × 2 arms × 1 repeat = 40 sessions, one at a time, frozen randomized
  order (seed `20261118`).
- Model `vllm/qwen3.8-27b` at `http://127.0.0.1:18020/v1`; source commit
  `1b9a7856c7ed043d9b53faf93399ce81fea25a66`, tree
  `9fe74fde5c4084223d42d6b41045f224a259389c`.
- All 40 sessions completed; **0 timeouts, 0 driver errors, 0 setup errors,
  0 permission denials**.
- Schedule SHA-256 `2204d2c41b0ce6c8b6318ff16cbca08d32710d50988696826d8cd54a16ff4ac8`;
  tasks `3656e215…`; assertions `0f265f15…` (full values in
  `evaluation/m6_live/preregistration.md`).

## Preregistered outcomes

| Outcome | Result |
| --- | --- |
| Primary: maps verified success non-inferior to baseline | **False** — baseline 6/20, maps 5/20 |
| Secondary: maps median setup-inclusive time-to-verified-outcome ≥10% lower | **False** — 4 both-verified pairs only; baseline 16,593.5 ms, maps 26,387.0 ms; −590 per mille (maps slower) |
| Insufficient pairs (fewer than 10 both verified) | **True** |

Aggregates:

| Arm | Verified | Median setup | Median driver | Median setup-inclusive | Tokens in/out/cache-read | Tool calls | MCP calls | `dev_maps` calls |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| baseline | 6/20 | 141 ms | 19,383.5 ms | 19,522.0 ms | 558,145 / 20,532 / 1,454,976 | 165 | 0 | 0 |
| maps | 5/20 | 9,560.5 ms | 25,565.0 ms | 35,073.5 ms | 690,197 / 22,134 / 2,611,872 | 173 | 42 | **0** |

The maps arm did use other MCP read tools: `dev_context` (11), `dev_search`
(10), `dev_code_search` (7), `dev_capabilities` (4), `dev_symbol` (4),
`dev_brief` (2), `dev_file` (2), `dev_node` (1), `goal_list` (1). It did not
use the map projector at all.

## Validity findings

1. **Treatment not exercised.** `dev_maps` was allowed and listed by the
   tested configuration, but the model chose other read tools in every maps
   session. The study therefore does not estimate the map surface. A future
   design needs an adherence gate (for example, a map-specific task
   instruction or a minimum `dev_maps` call on map tasks) before thresholds
   can be interpreted.
2. **Deterministic scoring undercounts correct answers.** The held-out scorer
   is an exact-substring check with no model judge. Several failed answers are
   substantively correct but miss a required wording. Examples:
   - `linked-checkout-readonly-join`: both arms describe caller-declared
     authorization and read scope, but matched only 2/6 required groups.
   - `capability-audit-directions`: the baseline names both mismatch
     directions, but was scored as missing them.
   - `mcp-server-readonly-contract`: the baseline matched 5/6 groups and still
     failed the task.
   The success counts (6 vs 5) are therefore likely understated for both arms
   and are not a clean capability estimate.
3. **Scoring was frozen before output.** The run is reported as-is; the
   scoring rule was not changed after seeing answers. A v2 needs a new
   preregistration and a paraphrase-tolerant or independent scorer.

## What this run does establish

- The local-Qwen harness starts, isolates, times and scores 40 sessions with no
  timeout or execution error.
- The maps arm's graph setup cost a median 9.56 s per checkout, not per task;
  the baseline checkouts needed no graph. In this cold design that one-time
  cost is included in every session's wallclock.
- Under this design, adding the full read-only MCP surface did not improve the
  deterministic success count. The cold setup-inclusive wallclock was higher,
  but that is a cold-start cost, not a task failure; over the four pairs where
  both arms verified, driver-only medians were 16.46 s baseline and 16.88 s
  maps (near parity, too few pairs to infer a speed effect).

## What it does not establish

- Any effect of `dev_maps`, the M6 map path, model negotiation or map caching.
- That maps are worse than baseline, or that the MCP surface is worse; the
  scorer and zero `dev_maps` use make those claims unsupported.
- General model capability, other repositories, cold/warm indexes, repeats,
  larger/smaller models, or the prompted/context and existing-Ozagia arms.
  Those remain separate evidence and are not pooled here.

## Artifacts

Owner-readable run artifacts are under `/tmp/ozagia-m6-live/`:
`summary.json`, `report.md`, `runs.jsonl` (contains final answers and bounded
errors), `manifest.json`, `source_manifest.json`, `schedule.json`. The source
manifest SHA-256 is `200f465dbee80dda7253e12145bd82429ced846677adbb9daa9439975ec43b6b`.
No credentials or raw logs are retained. The harness, corpus, assertions and
preregistration are committed under `evaluation/m6_live/`.

## Post-fix follow-up — 2026-11-18

After the output-clarity fixes landed at source commit
`406eb1e4079dca7b653047b8d8f33b81fa1270fa` (tree
`5984682b54bbb8d7f151ab0d9632d51a7b3036ba`), the same frozen 20-task corpus,
assertions, schedule and thresholds were run again on the local model. This is
a **post-fix exploratory follow-up, not a new preregistration**; its results
are not pooled with the frozen v1 run above.

- 40 sessions completed; 0 timeouts, 0 driver errors, 0 setup errors,
  0 permission denials.
- Source manifest SHA-256:
  `5fc02ffba5a77b42fdbd3b5ea7b4b59f6821825e670c6a6f0f1372cb7f90e03b`.

| Outcome | Result |
| --- | --- |
| Primary: maps success non-inferior to baseline | **True** — baseline 7/20, maps 7/20 |
| Secondary (cold setup-inclusive): maps median ≥10% lower | **Not a valid cold-task failure** — the measure includes the one-time graph build; see below |
| Insufficient both-verified pairs (fewer than 10) | **True** — 6 pairs |

| Arm | Verified | Median setup | Median driver | Median setup-inclusive | Tokens in/out/cache-read | Tool calls | MCP calls | `dev_maps` calls |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| baseline | 7/20 | 143 ms | 20,667 ms | 20,807.5 ms | 601,866 / 20,312 / 1,594,944 | 162 | 0 | 0 |
| maps | 7/20 | 9,797 ms | 26,175 ms | 35,908 ms | 676,821 / 17,749 / 1,947,456 | 141 | 50 | 1 |

### Cold setup is not a task failure

The preregistered secondary rule divides setup-inclusive totals. That conflates
a one-time graph build with per-task work and is not a valid failure signal on
a cold checkout. The components are:

- Median cold setup: 143 ms baseline vs 9,797 ms maps. This is a one-time cost
  per checkout, not per task; with one shared snapshot over 20 tasks it would
  amortize to about **0.49 s per task**.
- On the 6 pairs where both arms verified, median **driver-only** time was
  **20.26 s baseline vs 23.96 s maps** (+3.71 s). That is a small sample and is
  inconclusive, not a demonstrated slowdown.
- The v1 cold run's 4 both-verified pairs were 16.46 s vs 16.88 s driver-only,
  effectively parity. Across both runs the cold setup is the visible
  difference; steady-state performance is not established.

The fair steady-state test is a warm/reused graph with the build cost
amortized and reported once, which this cold-per-session design did not run.
The user is correct that the cold wallclock increase by itself is not a
failure. The result is: success parity, a known cold-build cost, and an
unresolved steady-state speed question.

The output fixes changed the observable answers, especially for
`capability-audit-directions` (both arms now verified) and
`mcp-server-readonly-contract` (both arms now verified). `dev_maps` was used
once, on `work-map-join-and-redaction`; that arm still failed only on the
exact phrase "secrets", not on its map output.

Remaining failures are still dominated by exact-substring phrasing, not
missing product facts: for example the maps arm answered
`code-search-signals` with "embeddings, a model, the network" while the scorer
requires "no embeddings", "no model", "no network" (the code-search output now
contains those literal strings); and the action-readiness answer said
"starting/stopping/signaling" while the scorer requires "start/stop/signal".
Those are scorer/answer-format limitations, not Ozagia output defects under
the user's rule that product fixes should precede scorer changes.

Artifacts: `/tmp/ozagia-m6-live-v2/` (`summary.json`, `report.md`,
`runs.jsonl`, `manifest.json`, `source_manifest.json`, `schedule.json`).

