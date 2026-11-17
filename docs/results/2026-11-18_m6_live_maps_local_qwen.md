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
- The maps arm's graph setup cost a median 9.56 s per session and added MCP
  tool traffic; the baseline checkouts needed no graph.
- Under this design, adding the full read-only MCP surface did not improve the
  deterministic success count, and the measured times were slower.

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
