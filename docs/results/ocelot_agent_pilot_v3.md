# Ocelot agent pilot, v3 (graph intent layer)

Two live pilots on the Ocelot repo, run on 15–16 Nov 2026 with
`ozagia.eval.agent run --live --driver opencode`. Each one has three arms and
3 repeats per task and arm, a 900 s timeout per session, and the local model
`vllm/qwen3.8-27b` through OpenCode v2.3.0 (cost $0).

- `baseline`: plain OpenCode.
- `dev_memory`: OpenCode plus the ozagia MCP server and a prepared snapshot.
- `graph_intent`: OpenCode with the ozagia plugin, which replaces the
  built-in tools' `execute` and carries out each call's intent through the
  graph daemon.

Raw output: `ocelot_agent_pilot_v3_v1.{json,md}` (7 tasks, run on the code
before the review fixes) and `ocelot_agent_pilot_v3_v2.{json,md}` (8 harder
tasks from Ocelot `evaluation/agent_e2e/tasks_v2.json`, run on `7f294ae`).

## Results

| pilot | arm | success | mean wall s | median wall s | median tokens in | median tool calls | timeouts |
|---|---|---|---|---|---|---|---|
| v1 | baseline | 21/21 | 58.8 | 47.9 | 30.8k | 8 | 0 |
| v1 | dev_memory | 21/21 | 35.4 | 30.6 | 31.1k | 7 | 0 |
| v1 | graph_intent | 21/21 | 34.3 | 28.9 | 26.6k | 7 | 0 |
| v2 | baseline | 23/24 | 183.0 | 109.7 | 63.7k | 24.5 | 2 |
| v2 | dev_memory | 24/24 | 141.4 | 85.9 | 58.1k | 21 | 0 |
| v2 | graph_intent | 24/24 | 236.1 | 126.7 | 63.4k | 24 | 1 |

Wall time does not include prep (snapshot and tag cache, about 75 s). A
timed-out session still counts as a success when the post-run check passes:
baseline `life-step-cache` r2 and graph_intent `remote-causal-posit-noop` r3
both did. The one failure is baseline `life-step-cache` r3 (timeout, check
failed).

In v2 the plugin and daemon had 0 fallbacks, 0 plugin-load failures and
0 daemon failures over 24 sessions. The daemon handled 11.8 calls per session
(43% of calls), and the other 15.8 were left to the built-in by design.

## Findings

- All three arms solve these tasks. The harder v2 set separates them on cost,
  not on success.
- `dev_memory` was the fastest arm in v2 (median 86 s against 110 s for
  baseline) and used the fewest tokens.
- `graph_intent` was fastest in v1 but slowest in v2. Most of that gap comes
  from captured pytest runs. When the model ran the whole suite without a
  timeout (`pytest tests/ -q`), the captured run used a 300 s default, while
  OpenCode's built-in shell stops at 120 s. Two sessions lost 300 s each this
  way (`admissible-moves-weight` r2, `life-step-cache` r1). A third ran 590 s
  because the model asked for a 600 s timeout. After the pilot the captured
  default was changed to 120 s to match the built-in.
- Captured calls are 43% of the total. Most passthroughs are compound shell
  commands: 245 used an operator (`cd X && …`, `;`), 104 were multi-line,
  18 were `ls` and 8 were git reads. The intent layer maps only simple
  commands, so these go to the built-in.

## Correction: MCP calls

The reports' `mean MCP calls` column reads 0.0 for dev_memory in both
pilots. The counter only knew the `tools["ozagia"].x(...)` form of a Code
Mode call, and the model wrote `tools.ozagia.x(...)`. Recounted with the
fixed counter (`eval.agent._MCP_CODE_CALL_RE`, v4): v1 made 0 MCP calls
in 21 dev_memory runs, and v2 made 8 in 24 (0.33 per run).

## Not measured

- Claude Code hooks (Claude Code was logged out).
- The native loop (`--driver native`) beyond one smoke run on
  `admissible-moves-weight` (same model, after the pilot): baseline solved it
  in 192 s with 28 tool calls, and graph_intent in 139 s with 18 calls
  (13 captured, 5 passthrough, 0 fallbacks). Its token counts (982k and
  549k in) add up the whole context resent on every turn, so they are not
  comparable with OpenCode's.
- Variance: 3 repeats per cell is too few for significance tests, and
  single 300–900 s outliers move the means a lot.
