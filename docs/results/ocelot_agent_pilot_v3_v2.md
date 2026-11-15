# Agent end-to-end evaluation

- Driver: `opencode` (live backend; see docs/features/AGENT_E2E_EVAL.md)
- Backend: `opencode`, model `vllm/qwen3.8-27b (opencode config default)`
- Total reported cost: $0.0
- Base: `54e5eb5fae4592444136b9d70fde08e94cb8a914`
- Arms: baseline, dev_memory, graph_intent
- Repeats per task and arm: 3
- Tasks: 8

## Per arm

| arm | runs | success | success rate | mean wall s | mean prep s | prep failures | driver errors | mean tokens in | mean tokens out | mean cost USD | mean tool calls | mean MCP calls | localization rate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 24 | 23 | 0.9583 | 183.007 | 0.0 | 0 | 0 | 87033.7 | 3351.0 | 0.0 | 25.3 | 0.0 | 0.9583 |
| dev_memory | 24 | 24 | 1.0 | 141.418 | 76.0 | 0 | 0 | 67789.0 | 3146.3 | 0.0 | 21.8 | 0.0 | 1.0 |
| graph_intent | 24 | 24 | 1.0 | 236.05 | 71.2 | 0 | 0 | 81793.4 | 3854.7 | 0.0 | 27.2 | 0.0 | 1.0 |

Token, cost and tool columns are blank where the driver reports no value. MCP calls count calls to the ozagia MCP server. Prep (dev_memory snapshot and tag cache) runs before the driver timer; mean prep s is 0 for baseline.

## Graph daemon and plugin

| arm | runs | mean captured calls | mean passthrough calls | total fallbacks | plugin-load failures | daemon failures |
|---|---|---|---|---|---|---|
| graph_intent | 24 | 11.8 | 15.8 | 0 | 0 | 0 |

Captured calls are tool calls the graph layer handled. Passthrough calls are calls it left to the built-in by design (not mapped). Fallbacks are calls where the graph layer failed: for the daemon, the plugin's fallback reports (daemon error, timeout, outcome unknown), one per call; for the native loop, intent-layer exceptions. A plugin-load failure is a run whose daemon recorded no `hello` (or gave no stats).

## Per task (paired)

| task | baseline | dev_memory | graph_intent |
|---|---|---|---|
| admissible-moves-weight | 3/3 | 3/3 | 3/3 |
| bound-audit-quantifier-scope | 3/3 | 3/3 | 3/3 |
| checkpoint-instance-index | 3/3 | 3/3 | 3/3 |
| learner-board-pairs | 3/3 | 3/3 | 3/3 |
| life-step-cache | 2/3 | 3/3 | 3/3 |
| r2-cycle-arms | 3/3 | 3/3 | 3/3 |
| remote-causal-posit-noop | 3/3 | 3/3 | 3/3 |
| seed-dependency-closure | 3/3 | 3/3 | 3/3 |
