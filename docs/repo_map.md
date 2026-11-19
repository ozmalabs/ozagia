# Repo map and agent delivery

`ozagia.core.repo_map` builds a ranked, token-budgeted map of a repository
from tree-sitter tags, a file reference graph and personalized PageRank. The
map names repository-relative paths and symbol line numbers; it is a reading
aid, never an edit and never a substitute for the source.

When a task is present, the map also uses bounded lexical content candidates
from `ozagia.core.search.code_search`. Candidates are scored with exact
definition-name and path-segment matches above substring matches, and
test/documentation/vendored files are demoted. Graph order and BM25 order are
then fused with confidence-weighted rank fusion: each signal's contribution
decays exponentially with rank, so a file must be near the top of at least one
signal instead of entering through a long tail. Candidates below 18% of the top
fused score are dropped, with a hard cap of 32 files. Below the confidence line
(35% of the top score at small budgets, 30% above) a file is kept only when it
adds task evidence not already covered by the files above it; high-confidence
files are always kept. The task header is capped at 200 characters, and the map
spends one full body, compact outlines, matching-line snippets for lexical hits,
and cheap listings within the same token budget.
`repo_map(..., content_search=False)` keeps the graph/name-only behavior;
`min_confidence=0` disables the evidence gate. Each content-aware file entry
carries `confidence` and the matched `evidence_symbols` it exposed; the result
reports `suppressed_files` and `evidence_terms`. The content path opens no
network and calls no model.

The same map is available in three places:

- the MCP tool `dev_repo_map` and
  `python -m ozagia.core.memory repo-map` (the `repo_map_eval` mode is the
  offline retrieval instrument);
- the daemon's `hint` op with `repo_map_tokens`, used by an OpenCode agent
  session;
- `ozagia agent --repo-map`, which turns that hint mode on for one session.

## Runner delivery

```bash
ozagia agent --repo-map --prompt "fix the retry loop"
ozagia agent --repo-map --repo-map-tokens 2500 --prompt-file task.txt
```

The mode is off by default. `--repo-map-tokens` accepts an integer in
200..8000 and defaults to 1500; it is only meaningful with `--repo-map`, and
the CLI refuses it alone. The help text says the map goes in the first model
request and that the mode is off by default.

`run_agent(..., repo_map_tokens=N)` validates the same bounds, puts
`OZAGIA_REPO_MAP_TOKENS=N` in the OpenCode process's environment only, and
returns `repo_map_tokens` in its result. Without the argument the variable is
removed from the child environment, so an inherited value cannot turn the mode
on. The summary adds:

```text
  repo map: 1500 tokens; delivered 2, skipped 1
```

`delivered` and `skipped` are the daemon's counters for the session. The
runner does not retry or synthesize a map when the answer is skipped; the
daemon's reason is in the hint response and the plugin adds nothing.

## Daemon contract

A `hint` request may carry `repo_map_tokens` (integer 200..8000). Without it,
the ordinary proactive hint is unchanged. With it, the daemon answers:

1. `memory.staleness(force=True)`. This is a fresh observation, not the
   memoized check used by ordinary tools.
2. If `freshness != "fresh"` or the snapshot is stale, an empty context with

   ```json
   {"delivered": false,
    "reason": "snapshot freshness is stale",
    "freshness": "stale"}
   ```

   in `meta.repo_map`. The same shape is used when freshness cannot be
   verified (`unknown`) or the staleness check raises.
3. `memory.repo_map(prompt, budget_tokens=N)`. An empty markdown body, a
   non-OK result or an exception is also a skip, with a bounded
   `repo map is empty` / `repo map unavailable (...)` reason.
4. On success, a bounded context with a short header, the markdown map and a
   one-line instruction to verify with `read` or `ozagia_structure`; and

   ```json
   {"delivered": true,
    "tokens": 1500,
    "estimated_tokens": 1204,
    "files": ["pkg/retry.py", "tests/test_retry.py"],
    "freshness": "fresh",
    "graph_hash": "<tag-cache root hash>",
    "cache": {"root_hash": "...", "cached_files": 12,
              "parsed": 0, "reused": 12},
    "truncated": false}
   ```

   `files` is the map's top paths (at most 10). `graph_hash` is the tag
   cache's content root hash, so two answers with the same hash describe the
   same mapped file set. `estimated_tokens` is the map's estimate capped at
   the requested budget. `truncated` is always false: the map renderer trims
   the tail to the budget rather than cutting a line mid-entry.

The daemon never serves a map after a failed or non-fresh verification. An
invalid `repo_map_tokens` is an error response and changes no counter; an
expired request returns the existing empty expired hint.

`stats` adds:

| counter | meaning |
|---|---|
| `repo_map_delivered` | maps delivered |
| `repo_map_skipped` | repo-map requests answered with an empty context |
| `repo_map_tokens` | estimated tokens of delivered maps, summed |
| `repo_map_files` | top paths in delivered maps, summed |

Prompts, history and the map text are not written to the daemon journal; only
the usual request fields and `meta.keys` are recorded.

## Plugin behavior

The OpenCode plugin reads `OZAGIA_REPO_MAP_TOKENS` once at setup and bounds it
to 200..8000; a missing or invalid value leaves the ordinary mode on. When
set, each `context` hook request sends `repo_map_tokens` on the existing
`hint` op. The returned text goes in the existing `[ozagia task context]`
block with explicit wording that it is a ranked repo map from a verified
snapshot and must be checked with `read` / `ozagia_structure`.

In this mode the plugin does not cache the first answer for the whole prompt:
it asks on each model request, so a new task or a changed working tree is
re-evaluated. An empty context or `meta.repo_map.delivered == false` injects
nothing; the plugin does not fall back to a stale or ordinary hint. The
daemon being unavailable also injects nothing. Without the variable, the
once-per-prompt cache and the ordinary on-demand hint are unchanged.

The mode adds no tool, mutates no file, and does not block ordinary search and
reads.

## Related

- [harness.md](harness.md): the `ozagia agent` CLI and session lifecycle.
- [`docs/memory_mcp.md`](memory_mcp.md): the `dev_repo_map` MCP tool and the
  retrieval eval.
- [`docs/eval_retrieval.md`](eval_retrieval.md): offline retrieval measurements
  for the map and its alternatives.
