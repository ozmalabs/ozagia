# Agent end-to-end evaluation

- Driver: `opencode` (live backend; see docs/features/AGENT_E2E_EVAL.md)
- Backend: `opencode`, model `vllm/qwen3.8-27b (opencode config default)`
- Total reported cost: $0.0
- Base: `54e5eb5fae4592444136b9d70fde08e94cb8a914`
- Arms: graph_intent
- Repeats per task and arm: 2
- Tasks: 8

## Per arm

| arm | runs | success | success rate | mean wall s | mean prep s | prep failures | driver errors | mean tokens in | mean tokens out | mean cost USD | mean tool calls | mean MCP calls | localization rate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| graph_intent | 16 | 16 | 1.0 | 132.392 | 55.0 | 0 | 0 | 79633.9 | 3737.6 | 0.0 | 26.6 | 0.0 | 1.0 |

Token, cost and tool columns are blank where the driver reports no value. MCP calls count calls to the ozagia MCP server. Prep (dev_memory snapshot and tag cache) runs before the driver timer; mean prep s is 0 for baseline.

## Graph daemon and plugin

| arm | runs | mean captured calls | mean passthrough calls | total fallbacks | plugin-load failures | daemon failures |
|---|---|---|---|---|---|---|
| graph_intent | 16 | 19.2 | 7.0 | 0 | 0 | 0 |

Captured calls are tool calls the graph layer handled. Passthrough calls are calls it left to the built-in by design (not mapped). Fallbacks are calls where the graph layer failed: for the daemon, the plugin's fallback reports (daemon error, timeout, outcome unknown), one per call; for the native loop, intent-layer exceptions. A plugin-load failure is a run whose daemon recorded no `hello` (or gave no stats).

## Per task (paired)

| task | graph_intent |
|---|---|
| admissible-moves-weight | 2/2 |
| bound-audit-quantifier-scope | 2/2 |
| checkpoint-instance-index | 2/2 |
| learner-board-pairs | 2/2 |
| life-step-cache | 2/2 |
| r2-cycle-arms | 2/2 |
| remote-causal-posit-noop | 2/2 |
| seed-dependency-closure | 2/2 |
