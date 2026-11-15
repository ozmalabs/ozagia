# Agent end-to-end evaluation

- Driver: `opencode` (live backend; see docs/features/AGENT_E2E_EVAL.md)
- Backend: `opencode`, model `vllm/qwen3.8-27b (opencode config default)`
- Total reported cost: $0.0
- Base: `aef842ba6bc06acb1bab6ee74e879b9bbc81dc53`
- Arms: baseline, dev_memory, graph_intent
- Repeats per task and arm: 3
- Tasks: 7

## Per arm

| arm | runs | success | success rate | mean wall s | mean prep s | prep failures | driver errors | mean tokens in | mean tokens out | mean cost USD | mean tool calls | mean MCP calls | localization rate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 21 | 21 | 1.0 | 58.77 | 0.0 | 0 | 0 | 31822.9 | 971.3 | 0.0 | 9.1 | 0.0 | 1.0 |
| dev_memory | 21 | 21 | 1.0 | 35.394 | 73.5 | 0 | 0 | 32000.3 | 835.0 | 0.0 | 8.0 | 0.0 | 1.0 |
| graph_intent | 21 | 21 | 1.0 | 34.316 | 75.3 | 0 | 0 | 28414.5 | 745.9 | 0.0 | 7.6 | 0.0 | 1.0 |

Token, cost and tool columns are blank where the driver reports no value. MCP calls count calls to the ozagia MCP server. Prep (dev_memory snapshot and tag cache) runs before the driver timer; mean prep s is 0 for baseline.

## Graph daemon and plugin

| arm | runs | mean captured calls | total fallbacks | plugin-load failures | daemon failures |
|---|---|---|---|---|---|
| graph_intent | 21 | 5.8 | 38 | 0 | 0 |

Captured calls are tool calls the daemon handled. A plugin-load failure is a run whose daemon recorded no `hello` (or gave no stats). Fallbacks are calls the plugin sent to the built-in because the daemon failed or timed out.

## Per task (paired)

| task | baseline | dev_memory | graph_intent |
|---|---|---|---|
| architecture-entrypoint-exclusion | 3/3 | 3/3 | 3/3 |
| architecture-hub-order | 3/3 | 3/3 | 3/3 |
| dev-memory-strict-bool | 3/3 | 3/3 | 3/3 |
| py-cst-token-last-char | 3/3 | 3/3 | 3/3 |
| repo-map-baseline-budget | 3/3 | 3/3 | 3/3 |
| repo-map-context-truncated | 3/3 | 3/3 | 3/3 |
| seed-loader-engine-rules | 3/3 | 3/3 | 3/3 |
