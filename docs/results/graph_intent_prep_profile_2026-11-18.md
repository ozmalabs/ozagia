# Graph-intent preparation profile (2026-11-18)

This offline profile traces the 13.80 seconds of `prep_s` in the three-task
general graph-intent pilot. It makes no model calls and does not change the
pilot result.

## Pilot trace

The pilot's valid graph rows were captured in two separate run directories:
`git-ref-moving-callers` came from `seeded-validation2`; the other two rows
came from `seeded-main-final`. The first two measured preparations were cold
(6.531 s and 6.692 s); the third reused a warm snapshot (0.577 s). Therefore
the 13.80 s total includes two cold preparations from separate run setups,
not three cold builds within one continuing graph session.

The graph-intent path is synchronous:

1. `GraphSession.__enter__` runs `prepare_snapshot` in a child process.
   `build=auto` checks snapshot freshness; it refreshes changes where possible
   and falls back to a full build if freshness cannot be re-established.
2. It starts a separate daemon with `--no-build`, which loads the current
   snapshot and reports readiness.
3. The first structural request lazily calls `GraphSurface._tag_graphs`:
   update the disk tag cache, construct the symbol graph and adjacency, and
   prepare the reference graph. This cost is inside the first structural tool
   call, not `prep_s`.

There is no background graph prefetch on this path. The daemon's stdout pump
uses a thread to collect readiness/log output; it does not build the graph.
The OpenCode context hook awaits a task-hint request, which is also not
structural-graph prefetch.

## Offline fixture measurements

The benchmark used three fresh clones of frozen pilot commit
`623ade26ec62ab1e7074c76b7d1fbc080764f5f8` (868 tracked files) under
`/tmp/ozagia-graph-prep-bench-20261118/repeats/`, on the same filesystem.
The Python import was pinned to the isolated runner worktree's `src/ozagia`.
Each clone ran a full cold build, an unchanged warm check, a one-file source
edit followed by automatic refresh, and another warm check. The tag-graph
measurement called the actual `GraphSurface._tag_graphs` path in a fresh
Python process, first without a tag cache and then with the persisted cache.
Every graph snapshot reported `fresh` before use, including after the source
edit. The full snapshot contained 14,419 nodes and 46,837 edges; the tag cache
covered 522 files and 13,801 symbol nodes.

| Phase | Median elapsed | Median snapshot work | Median daemon readiness | Result |
| --- | ---: | ---: | ---: | --- |
| Full cold snapshot build | 6.818 s | 6.226 s | 0.311 s | Built, fresh |
| Unchanged warm start | 0.868 s | 0.214 s | 0.309 s | Fresh snapshot reused |
| One indexed-file change | 1.164 s | 0.525 s | 0.312 s | Incrementally refreshed, fresh |
| First structural tag-graph construction | 3.399 s process wall | 2.960 s in builder | — | 522 files cached |
| Tag-graph construction with disk cache, new process | 2.173 s process wall | 1.702 s in builder | — | Same files/nodes |

The three-repetition ranges were 6.789–6.841 s for cold entry, 0.855–0.884 s
for unchanged warm entry, and 1.160–1.218 s for one-file refresh. Tag-graph
builder times were 2.942–2.975 s cold and 1.694–1.828 s with the persisted
cache. These are local fixture observations, not general performance targets.

The pilot's recorded structural tool durations were 2.925 s for the first
task's first query, 0.237 s for the second task's query, and 1.266 s for the
third task's first query (the remaining task-three queries took 0.149–0.186 s).
This confirms lazy tag-graph work can also affect model-visible first-query
latency; the 13.80 s `prep_s` total does not include it.

## Recommendation

The measured safe optimization is reuse of the existing same-root snapshot
and disk tag cache: warm startup was about 5.95 s faster than the cold path,
and a one-file refresh stayed fresh while taking about 1.16 s end to end. Keep
`build=auto` and its full-build fallback. Do not copy a graph snapshot across
different root paths; the snapshot records root identity and the freshness
check must continue to reject mismatches.

For repeated read-only tasks on the same immutable checkout, reuse one
treatment worktree/session where the experiment design permits it. If task
isolation requires separate roots, each root still needs its own freshness
check and initial preparation. A future bounded prefetch could overlap tag
graph construction with independent work, but it must be invalidated or
revalidated against root, HEAD, and tree digest before any result is served.
This profile did not implement asynchronous prefetch because it cannot make
the cold work disappear and there is no current freshness-safe prefetch
contract to reuse.

No graph builder or freshness behavior was modified for this profile.
