# Bounded graph tag-cache prefetch proposal

Status: **not adopted for MCP integration in 0.3 (decision 2026-11-25)**.
The offline planner and accounting primitives remain implemented, verified
2026-11-24. The measured ceiling is small (about 1.26 s of cache work on one
synthetic 868-file tree) while private-cache publication, freshness tokens,
process cancellation and suspend/resume cross the MCP lifecycle, graph cache
and invalidation paths. Revisit only after larger-repository measurements and
the acceptance tests below. Code and tests:
`src/ozagia/core/prefetch.py`, `tests/test_prefetch.py` and
[`docs/prefetch.md`](../prefetch.md). They select bounded read-only
candidates, enforce authorization/trust/freshness/expiry, cancel and keep a
prepare/use ledger; they do not read a file, graph or network, start a model or
test, write a cache, or inject anything into a prompt. Not implemented: the
MCP prefetch child process, private-cache publication, suspend/resume around
requests, invalidation wiring and the on/off measurement described below. The
plan's decision not to integrate until those acceptance tests pass still
stands.

## Motivation and measured ceiling

The offline profile in
[`graph_intent_prep_profile_2026-11-18.md`](../results/graph_intent_prep_profile_2026-11-18.md)
measured the lazy `GraphSurface._tag_graphs` path on a frozen 868-file tree.
The first call took a median 2.960 s inside the builder. A new process with
the persisted tag cache took 1.702 s, so the observed parsing/cache portion
that a prefetch might overlap is about 1.26 s. The 6.226 s cold snapshot build
and 0.311 s daemon readiness are separate work and are not eliminated by tag
prefetch. The pilot's first structural request took 2.925 s in one cold run;
later queries were 0.237 s or less in the warm task.

The current graph path is synchronous and lazy. `GraphSurface._tag_graphs`
calls `update_tag_cache` on the shared state cache, then fills process-global
symbol/adjacency/reference graph memos. `_invalidate` clears only the
`GraphSurface` instance's references after its own writes. A worker sharing
this method with request handlers would race those writes/memos. A Python
thread cannot be forcibly cancelled during a parse, and it could contend for
the GIL while a normal handler is trying to build the same graph. For those
reasons this proposal does not add a thread around `_tag_graphs`.

## Proposed opt-in lifecycle

The prototype should be a low-priority child process, limited to graph mode
and disabled unless `OZAGIA_GRAPH_PREFETCH=1` is set. Its only work is building
a private tag-cache candidate; it must not write `tags.json`, mutate global
memo tables, query a model, or modify source files.

1. Start only after the snapshot loader has a known, fresh root/HEAD/tree
identity. Capture a token containing resolved root, HEAD, tree digest, cache
schema version and extractor fingerprints.
2. Give the child a private cache path under Ozagia's state directory, a
   single worker, a short wall deadline (initial proposal: 5 seconds), a
   bounded output size, and reduced OS scheduling priority where supported.
   Keep the process handle so cancellation terminates its process group.
3. The child builds its candidate using the same `update_tag_cache` code and
   returns only completion metadata and the candidate path. It does not make
   its candidate visible to handlers.
4. Before publication, the parent rechecks the root/HEAD/tree identity and
   validates the candidate version and root hash. Any unknown state, mismatch,
   timeout, parse failure or oversized candidate discards the candidate.
5. Run prefetch only while the MCP server is idle. On receipt of any request,
   suspend the child before dispatching the normal handler; resume only after
   the response if no invalidation occurred. The structural handler never
   waits for the prefetch child or its future. If a validated completed
   candidate is already available, it can atomically seed the normal tag
   cache and use the existing graph builder. If the child is still running or
   validation fails, the handler follows the existing synchronous
   `_tag_graphs` path. The current freshness check and full snapshot rebuild
   fallback remain authoritative. Platforms without a reliable suspend/resume
   mechanism should leave the feature disabled rather than claim that
   concurrent CPU work cannot affect handler latency.
6. Publish a candidate with the tag-cache module's atomic-write convention,
   then invalidate that path's `_DOC_MEMO` entry before normal graph building.
   Never let the child and handler write the same cache path.
7. Cancel and discard on shutdown, root/snapshot change, edit/reingest or
   extractor-version change. Do not expose partially built state. Do not reuse
   an artifact across a root path, HEAD, tree digest or schema mismatch.

This design only overlaps cache preparation; it does not claim that the
handler always gets faster. If the model calls a structural tool before the
child finishes, the handler runs the existing synchronous path. If local CPU
contention makes that path slower, the feature must remain disabled by
default or be removed.

## Offline acceptance tests before integration

Use a disposable, same-filesystem checkout and a fake/injected worker. No
model endpoint or inference is required.

- Default-off creates no child, cache file or extra tool delay.
- Opt-in starts at most one child, enforces the deadline/size cap and does not
  hold the MCP request loop or a graph-handler lock.
- On receipt of a request while the child is running, suspend it before the
  handler starts. The structural handler returns by the ordinary synchronous
  path without joining or waiting for that child; resume it only after
  response when the root remains valid.
- A finished candidate is used only when root, HEAD, tree digest, schema and
  extractor fingerprints still match; verify an `impact` or `expand` result
  against the existing non-prefetch result. Verify atomic publication also
  invalidates any already-loaded cache document memo.
- Mutate an indexed source file, change HEAD, change root, force an unknown
  freshness result, corrupt the candidate and trigger a timeout. In every
  case the candidate is discarded and the ordinary current/fallback behavior
  is used; stale prefetch output is never returned.
- Cancel at shutdown and on graph invalidation; assert the child is reaped,
  private temporary files are removed, and the shared tag cache is not
  corrupted when the normal handler runs concurrently.
- Compare cold, warm and refresh wall time with prefetch off/on, recording
  handler latency and wasted work if the candidate is unused. Enable for
  broader evaluation only if it reduces handler wait without changing
  freshness or response content.

## Decision

Do not integrate until the private-cache publication, freshness token and
cancellation behavior pass these tests. The current measured benefit is small
(about 1.26 s of cache work on one synthetic tree), while the required race
and freshness handling crosses the MCP lifecycle, graph cache and invalidation
paths. Reusing the existing same-root disk cache remains the lower-risk
optimization for now.
