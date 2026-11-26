# OpenCode intent-oriented graph retrieval — 2026-11-17

Status: partial implementation, verified 2026-11-24. Stages A, B and C are
implemented: grep definition enrichment in `intent/structural.py`; bounded
prompt routing in `core/structural_intent.py` and `core/proactive_context.py`
with the permission boundary described below; and the native
`ozagia_structure` tool plus daemon `structural` request. Stage D is partial:
BRE definition search is scoped to matched files, but caller/test/module shell
intent linking and `sed` correlation remain. Stage E is an initial paired
offline classifier check, not an end-to-end usefulness result. Tests:
`tests/test_intent.py`, `tests/test_structural_intent_routing.py`,
`tests/test_intent_live_hints.py` and `tests/test_opencode_context_hook.py`.
The OpenCode plugin does not invoke a model itself. It may append bounded,
untrusted graph context to a tool result or prompt through the existing daemon.

## Goal

Help OpenCode answer structural repository questions from Ozagia's existing
graph services when the user's intent is clear: where a definition lives, who
calls it, which tests cover it, and what a module exposes. Keep `grep`, `sed`,
and shell command behavior predictable. Source output and host permission
requests remain authoritative; graph context is additive and visibly labeled.

## Existing path and constraint

- The normal OpenCode plugin replaces `read`, `grep`, `glob`, `edit`, `write`,
  and `shell`, and registers a read-only structural tool when OpenCode provides
  its native permission assertion API.
- OpenCode MCP configuration is optional in the harness (`mcp_mode=None` by
  default). `dev_symbol_graph_*` tools are therefore unavailable in a normal
  launch. The `graph` MCP surface is selected separately and needs an existing
  graph snapshot; instructions must not tell the model to invoke those tools
  unless launch configuration confirms they are exposed.
- The daemon's prompt hook already calls `core.proactive_context`, whose code
  retrieval refuses stale or unverifiable snapshots. It is a general semantic
  search, not an intent router.
- Captured grep preserves actual option/path behavior and permission handling.
  Shell intent classification is conservative; uncertain commands go to the
  original OpenCode tool.
- `DevMemory.symbol_graph`, `tests_for`, `repo_map`, `architecture`, and
  `staleness` already provide structural services. Symbol graph links are
  syntactic and their tag-cache coverage can be partial.

## Implementation stages

### A. Additive, high-confidence grep enrichment (implemented)

Recognize explicit Python definition patterns, including BRE alternation such
as `^def \|^class `, and derive names only from matching source lines. After
the normal captured grep result, check graph
freshness and ask the existing symbol resolver for definitions. Append bounded
`sym:` keys, index coverage and guidance to use the existing `read` tool with
the key. Limit graph definitions to files that actually matched grep, to avoid
disclosing symbols from outside the requested path. Preserve every grep record
and its permission request. For stale or unknown freshness, append a short
unavailable reason; do not query the graph.

Acceptance: integration tests exercise a fresh definition search, stale graph
fallback, unmodified grep source lines, unchanged permission resources, key
read guidance, and coverage disclosure.

### B. Prompt-level intent routing (implemented with a deliberate boundary)

`core.structural_intent` recognizes explicit definitions, callers, callees,
tests, and module/API outline questions. It does not query or reveal graph data
in the prompt hook: that hook cannot request OpenCode read permission. On a
fresh snapshot and only when the OpenCode tool registry confirms
`ozagia_structure` is available, `core.proactive_context` adds a bounded
instruction with the operation/target and tells the model that the native tool
will check freshness/coverage and ask read permission for cited files. Stale
snapshots, unavailable tools, and ordinary prompts do not get this instruction.
The actual query then goes through stage C. This boundary prevents prompt
context from disclosing graph paths without a permission request.

### C. Expose direct read-only graph operations in normal OpenCode launch

Implemented as the native `ozagia_structure` OpenCode tool plus a bounded
`structural` daemon request. It supports resolve, callers, callees, tests and
ranked outline queries using existing graph services. Results report
freshness, tag/snapshot coverage, and truncation. The tool is registered only
when OpenCode exposes `permission.assert`; every cited file path is asserted
as a `read` resource before returning output. It is `codemode: false` and the
daemon operation has no edit/check/execute verb. If the graph is stale or
unverifiable, it returns no structural data. No project MCP configuration is
changed.

### D. Semantic shell intent beyond definition search

BRE definition searches are implemented, scoped to the exact grep-matched
files. Caller/test/module questions now get direct tool guidance at prompt
time. Automatic enrichment of those shell searches and `sed` intent linking
remain: a captured shell call carries command text and paths but not a reliable
link to the user question, and inferring relationship intent from a
symbol-like regex alone creates false positives. A later implementation must
correlate the active prompt intent and still restrict citations to paths the
command actually matched or permissioned. Preserve command output, exit code,
options, path scope, ordering and permissions; uncertain calls remain with
the original tool.

### E. Evaluation (initial paired offline check implemented)

Offline tests cover fresh definition/caller/test/outline queries, stale graph
fallback, path scoping, permission assertion and denial. The new paired
classifier fixture has 9 explicit structural prompts and 8 ordinary/ambiguous
prompts; all 9 route and none of the 8 are false positives. Add remaining graph
fixtures for overloaded/unresolved and dynamic calls, partial parse coverage,
hidden/excluded paths and shell flags. This is classifier evaluation, not an
end-to-end model usefulness result. Live calls remain gated by `--live`,
`OZAGIA_EVAL_LIVE=1`, and human approval.

## Guardrails

- No project import or live model invocation.
- No graph answer from stale/unknown freshness; no unsupported claim that
  syntactic references are complete or type-resolved.
- Report tag coverage and truncation; partial coverage is not “no callers”.
- Preserve exact shell execution semantics, source output, and permission
  resources. Never bypass the OpenCode permission assertion.
- Use only existing read-only services; all code writes continue through
  `edit/txn.py` and remain outside this retrieval scope.

## Current slice

`intent.structural.definition_hint` implements stage A, including the observed
BRE definition search style and exact matched-file scope. The OpenCode plugin
registers `ozagia_structure` by default when permission assertions are
available; it exposes only existing read-only graph queries and asks read
permission for all cited files. The plugin also verifies it in OpenCode's
`ctx.tool.list()` effective catalog and passes that availability into the
prompt hook. Fresh, exact prompt patterns get a tool-use instruction only;
graph content stays behind the permissioned tool. Remaining work is broader
shell command/task intent linking and richer paired end-to-end usefulness
evaluation. The model should not be directed to MCP graph tool names, which
remain optional.
