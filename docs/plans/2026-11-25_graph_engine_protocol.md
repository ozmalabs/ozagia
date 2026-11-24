# Graph engine protocol (v2 first slice)

Status: implemented 2026-11-25 in `src/ozagia/core/graph_engine.py`, with the
MCP surface in `src/ozagia/integrations/mcp/server.py` and tests in
`tests/test_graph_engine.py`.

This is the compact, stateful, token-efficient graph interface for LLMs. It
replaces a family of independent `dev_*` calls with one MCP tool (`graph`), one
versioned session protocol, hard token budgets, `known` deltas and bounded
speculation. It reuses the existing services rather than re-implementing graph
logic:

| Engine op | Existing implementation |
| --- | --- |
| `find`, `outline`, `expand`, `impact` | `ozagia.integrations.mcp.graph_surface.GraphSurface` |
| `tests`, `context` | `DevMemory.tests_for`, `DevMemory.context` |
| `shape` | `ozagia.core.repo_shape.build_shape` |
| freshness and snapshot identity | `DevMemory.staleness` and the loaded, keyed graph |
| speculation ledger concepts | the bounded prefetch/ledger vocabulary in `ozagia.core.prefetch` |

## Non-goals of this slice

- **No model calls, network, target imports or target execution.** The engine
  does not run tests, edit files, refresh the snapshot, start a daemon or
  import a target module.
- **No process isolation.** Speculation runs in one in-process daemon thread.
  It can overlap model thinking time at the interpreter's scheduling granularity
  but is GIL-bounded and has no separate CPU/memory budget. There is no new
  daemon process yet.
- **No speculative writes.** A prepared payload is a read-only graph answer.
- **No edit/create/check/refresh/shell ops.** Those remain on the graph
  surface; the engine deliberately cannot reach them.
- **No multi-repository federation.** A session belongs to one loaded snapshot.
- **No persistent session store.** Sessions live in the server process and are
  bounded; restarting the server discards them.
- **No production hit-rate evidence.** The prototype measurement in
  `docs/results/graph_engine_prototype_2026-11-25.md` counts tokens,
  round-trips, readiness and hit/waste on one tiny fixture; it is not a
  model-task gain or a larger-repository hit-rate study.

## Versioning and transport

- Protocol string: `ozagia.graph_engine/2`
- Protocol version: `2.0`
- MCP tool: `graph`
- MCP surfaces: `OZAGIA_SURFACE=engine` or `--surface engine`
- The tool's input schema has `additionalProperties: false`.
- JSON-RPC framing, errors and `tools/list` are the existing MCP server's.
  A bad tool argument is a JSON-RPC `-32602`; a bad engine request is a normal
  JSON result with an `e` message.

v2 is a wire-incompatible simplification of v1: success responses no longer
carry `ok`/`op`/`fresh`/`budget`, top-level metadata uses short keys, columns are
sent once per session, and the per-response speculation ledger moved behind
`stats: true`.

## Handshake

Default handshake:

```json
{"op": "hello"}
```

```json
{"p": "ozagia.graph_engine/2", "v": "2.0", "sid": "s1",
 "s": "3f2a1c9d8e7b6a50", "f": "fresh", "b": [1200, 8000], "c": 511,
 "t": 33}
```

- `p`/`v`: protocol and version.
- `sid`: session handle.
- `s`: snapshot token.
- `f`: `fresh`, `stale` or `unknown`.
- `b`: default and maximum estimated-token budget.
- `c`: capability bit mask. Bits: `find=1`, `outline=2`, `expand=4`,
  `impact=8`, `tests=16`, `context=32`, `shape=64`, `fetch=128`, `bye=256`;
  `511` means all are present.
- `t`: estimated response tokens.

Full handshake only when requested:

```json
{"op": "hello", "caps": true}
```

`caps: true` (alias `detail: true`) adds `ops` (full op names), `ft`
(features), `bd` (budget default/max/current), `sn`, `compact` and the full
speculation ledger `st`. The default response omits all of those.

Calling `hello` again without `session` creates a new isolated session. With a
live `session` it refreshes freshness and the budget rather than creating a
second handle. `hello` contains no structural rows, so it is safe to answer
even when the snapshot is not usable.

## Success envelope

Success responses omit redundant request/session metadata. There is no `ok`,
`op`, `fresh`, `snap` or `budget` on a successful op; a successful response is
the payload plus `t` (and `c`/`x`/`o`/`n`/`u`/`q` when they apply). `fresh` and
`snap` appear only in the handshake and in withheld/error responses.

```json
{"c": ["k", "n", "f", "sp", "sig", "sum", "sc"],
 "r": [["sym:pkg/core.py::add", "add", "pkg/core.py", [4, 4],
        "def add(a, b):", "Add two numbers.", 0.03]],
 "t": 130}
```

Top-level keys:

| Key | Meaning |
| --- | --- |
| `r` | rows (arrays in `c` order) |
| `c` | columns; only the first response for an op in a session, or `cols:true` |
| `u` | unchanged key handles from a `known` request |
| `x` | `true` when the payload was cut |
| `o` | number of rows/items omitted |
| `n` | a complete continuation request (includes `op`, `session`, `offset`) |
| `q` | compact speculation handles/counts |
| `ed` | expand edge triples |
| `ct` | impact group counts |
| `it` | multiple fetched payloads |
| `h` | fetched speculative handle |
| `qo` | op that produced a fetched payload |
| `fs` | fetch state: `pending` or `cancelled` |
| `st` | full speculation ledger, only with `stats:true` |
| `t` | estimated response tokens |

Errors use short keys too:

```json
{"e": "snapshot is not verifiably fresh; structural data is withheld",
 "f": "stale", "s": "3f2a…", "t": 28}
```

`e` marks an error/withheld result. A stale or unknown result never contains
`r`, `c`, `u` or a prepared payload.

## Operations

| Op | Inputs | Result |
| --- | --- | --- |
| `hello` | `session?`, `budget?`, `compact?`, `caps?`, `detail?` | handshake |
| `find` | `q`, `kinds?`, `limit?`, `offset?` | ranked keys, names, files, spans, signatures, summaries, scores |
| `outline` | `key`, `limit?`, `known?` | a root row and (for a file) its defined symbols |
| `expand` | `keys`, `edges?`, `depth?`, `limit?`, `offset?` | nodes plus `ed` edge triples |
| `impact` | `keys`, `depth?`, `limit?` | symbols/files/tests with depth and one `why` path |
| `tests` | `key` or `keys`, `limit?` | covering tests and why each was linked |
| `context` | `task`, `limit?`, `offset?`, `mode?`, `known?` | cited task context items |
| `shape` | `limit?` | bounded repository identity/counts/languages/modules/commands |
| `fetch` | `fetch` (1–4 handles) or `h`, `wait_ms?` | prepared payloads, a compact pending result, or cancelled |
| `bye` | `session` | close the session and clear its caches |

`tests` discovers test keys; it never runs pytest. `shape` is a compact
projection of the deterministic `repo_shape` report. `wait_ms` is clamped to
`0..250`; a pending fetch never blocks indefinitely.

## Compact rows

Rows are arrays in the order of the op's fixed columns. Columns are stable per
op; after the first response that includes `c` in a session, the same columns
are assumed unless `cols:true` asks for them again.

| Short | Long | Notes |
| --- | --- | --- |
| `k` | key | stable node handle (`ozagia.core.keys`) |
| `h` | hash | stable 16-hex content hash for `known` |
| `n` | name | display name |
| `f` | file | repo-relative path |
| `sp` | span | `[start_line, end_line]`, 1-based |
| `sig` | signature | definition header or equivalent |
| `sum` | summary | first docstring/description/title line |
| `sc` | score | fused retrieval score |
| `e` | edges | neighbour counts by type (outline column) |
| `r` | role | `root` or `child` |
| `g` | group | impact group (`s`, `f`, `t`) |
| `d` | depth | impact depth |
| `why` | why | one edge path or link explanation |
| `txt` | text | bounded source/outline text |
| `sec` | section | `repo`, `count`, `lang`, `module`, `cmd`, `conv` |
| `v` | value | shape value |

`verbose: true` expands rows into named objects for debugging. It is not part
of the compact path and is still budgeted.

Node handles are the existing stable keys: `file:<path>`,
`sym:<path>::<qualname>`, `test:<path>::<qualname>`, `doc:<path>`,
`text:<path>#L<a>-L<b>`, `repo:` and adapter keys. They are not per-response
integer ids.

## Sessions

One server process holds at most **16** live sessions (LRU). Evicting a
session cancels its pending work, counts unfetched prepared payloads as wasted,
and clears its cache. A request naming an evicted or unknown handle returns
`{"e": "unknown or expired session; call hello"}`. `bye` closes one session
immediately; engine shutdown closes all sessions and joins the daemon worker.

A session stores:

- the last observed snapshot token and freshness label;
- up to 1,000 `key -> hash` entries remembered from responses;
- the last cursor/page position (today the self-contained `n` object is
  authoritative; the cursor is session bookkeeping);
- the fixed columns already sent for each op;
- the last response token estimate and cumulative estimated tokens;
- the compact flag and current default budget;
- a snapshot/root scope, a speculation generation and a closed flag;
- an ordered speculation cache (at most 8 entries) and a ledger.

## `known` deltas

Callers send outlines/context they already have:

```json
{"op": "outline", "session": "s1", "key": "sym:pkg/core.py::add",
 "known": [{"key": "sym:pkg/core.py::add", "hash": "1a2b3c4d5e6f7a8b"}]}
```

The engine hashes each item it would serialize. If the key and hash match, the
item is represented only by its key in `u`:

```json
{"u": ["sym:pkg/core.py::add"], "t": 10}
```

Changed, missing or unknown hashes are reserialized with a fresh `h`. A
16-hex SHA-256 prefix is used to keep the wire small; a collision would cause
the client to keep an old payload, so this is a prototype trade-off, not a
cryptographic identity guarantee. A known-only response is dramatically smaller
than reserializing the outline (10 vs 41 estimated tokens on the fixture).

## Token budgets

- Default: **1,200** estimated tokens.
- Maximum: **8,000**.
- Minimum accepted: **64**; lower requests are clamped up.
- Per-session default from `hello`; a per-call `budget` overrides it.
- The engine trims the *payload*, never silently overflows. Long text cells are
  shortened first; then lists are cut from the end.
- A cut list sets `x: true`, an `o` count and, when a continuation is
  meaningful, a complete `n` request with an `offset`.
- Optional metadata (`n`, `total`) is dropped before rows; `t` is computed after
  the final trim and is the value callers should trust for accounting.
- Prepared speculative payloads have their own bound (`SPEC_BUDGET = 400`
  estimated tokens, or the request budget if smaller).

## Speculation lifecycle

When a response has `spec: true`:

1. The engine derives up to six candidate requests from the returned keys:
   outline, callers and callees (`expand`), and covering tests (`tests`).
2. It skips a candidate that repeats the current request and selects at most
   two to prepare.
3. It creates pending handles for those two, queues one `SpecJob`, and returns
   immediately:

   ```json
   {"q": {"h": ["q1", "q2"], "r": 0, "p": 4}, "t": 130}
   ```

   `q.h` planned handles, `q.r` how many are already ready at response time,
   `q.p` further unaddressed suggestions. The handler does **not** wait for
   preparation.

4. A single in-process daemon thread blocks on a bounded queue, prepares at
   most two results per job, and yields between candidates. It captures the
   session id, snapshot token, scope, generation, budget, deadline and entry
   expiry. Before and after each candidate it checks that the session still
   exists, is not closed, and has the same snapshot, scope and generation. A
   changed/evicted/closed session, an expired entry or an exhausted worker
   budget cancels the remaining handles.
5. The full ledger is **not** sent by default. `stats: true` adds it:

   ```json
   {"st": {"prepared": 2, "fetched": 2, "wasted": 0, "evicted": 0,
           "cancelled": 0, "worker_ms": 1.0, "hits": 2, "misses": 0,
           "hit_rate": 1.0}}
   ```

### Fetch

`fetch` (one handle, or up to four) returns:

- a ready prepared payload without repeating graph work; the entry is removed
  after the first fetch;
- `{"h": ..., "qo": ..., "fs": "pending"}` for a still-pending handle, after an
  optional bounded `wait_ms` (`0..250`); the entry remains fetchable;
- `{"h": ..., "qo": ..., "fs": "cancelled", "reason": ...}` for a handle
  cancelled by budget, expiry, invalidation, eviction or close.

A handle that is unknown or prepared under a different snapshot is refused.
If the snapshot becomes stale or unknown, all prepared entries are invalidated.
`stats: true` on `fetch` returns the ledger.

Ledger counters:

| Counter | Meaning |
| --- | --- |
| `prepared` | payloads successfully prepared after the response |
| `fetched` | prepared payloads actually fetched |
| `wasted` | prepared but never fetched when evicted/invalidated |
| `evicted` | cache entries removed |
| `cancelled` | pending requests cancelled before being prepared |
| `worker_ms` | accumulated worker preparation time |
| `hits` / `misses` | fetch ready vs pending/cancelled outcomes |
| `hit_rate` | `hits / (hits + misses)`, 0 when no attempts |

The cache is bounded at 8 entries per session. The engine also keeps
process-lifetime aggregate counters in `GraphEngine.stats()`.

### Worker limits

- One daemon thread per engine process, bounded queue (8 jobs).
- One job at a time; at most two candidates per job.
- Total worker deadline default 300 ms (`OZAGIA_ENGINE_SPEC_MS`, clamped
  50..2000 ms), checked between candidates. A single in-flight graph call can
  overrun the deadline because it is not preempted.
- Entry expiry default 30 s.
- `OZAGIA_ENGINE_SPEC=0` disables speculation entirely (no worker, no handles).
- The thread is daemonized; `bye` closes one session, and engine shutdown joins
  the worker. The GIL still serialises CPU work with the handler; this overlaps
  model-side waiting and some I/O, not two CPU-bound Python tasks.

## Freshness and unknowns

Before any structural op, the engine asks `DevMemory.staleness(force=True,
allow_project_adapters=False)`. Project adapters are **not** loaded: freshness
must not execute project code. If the working tree, HEAD or root differs from
the snapshot, freshness is `stale`; if the check cannot be completed (no git,
no recorded digest/root/head, unknown adapter inputs, digest error), it is
`unknown`. Neither state answers with structural rows. A successful response
therefore implies the snapshot was fresh at the time of the call; the label is
not repeated. Stale/unknown responses return `e`, `f`, `s` and no payload. A
session snapshot token change invalidates prepared speculation and updates the
token on the next fresh answer.

`shape` reuses the deterministic `repo_shape` report; it is still gated by the
same freshness check.

## Security and read-only guarantees

- The engine's only verbs are `hello`, `find`, `outline`, `expand`, `impact`,
  `tests`, `context`, `shape`, `fetch`. Edit/create/check/refresh/shell verbs
  are unknown ops.
- It never imports the target project, executes target code, runs pytest,
  starts a daemon or contacts the network.
- It never writes. Sessions, caches and ledgers are in memory. It does not
  write the journal, snapshot, brief or any file under the target checkout.
- Paths resolve through the existing graph surface, which refuses paths outside
  the root.
- Snapshot freshness checks read git and the working tree, but do not execute
  adapters.
- All payloads are untrusted repository content and must be treated as
  reference material by the model.

## Bounds summary

| Bound | Value |
| --- | --- |
| sessions | 16 (LRU) |
| known hashes remembered per session | 1,000 |
| speculation cache per session | 8 |
| suggested candidates per response | 6 |
| prepared per background job | 2 |
| background worker threads per engine | 1 (daemon) |
| worker queue | 8 jobs |
| worker total budget | 300 ms default (`OZAGIA_ENGINE_SPEC_MS`) |
| speculation entry expiry | 30 s |
| fetch wait | 0–250 ms |
| handles per `fetch` | 4 |
| default response budget | 1,200 estimated tokens |
| maximum response budget | 8,000 |
| minimum accepted budget | 64 |
| prepared payload budget | 400 (or the request budget, if smaller) |
| rows requested by an op | 200 |

## Measured wire size

From `scripts/measure_graph_engine.py` on the tiny fixture:

| Call | Estimated tokens |
| --- | ---: |
| `hello` (default) | 33 |
| `find` + spec | 130 |
| `fetch` two prepared handles | 169 |
| `outline` with matching `known` | 10 |

Single-turn task total: 374 tokens / 4 round-trips, versus 637 / 4 for the
stateless graph surface and 1,707 / 5 for the `dev_*` sequence. The background
measurement sleeps 150 ms after `find` to simulate model thinking: two handles
were pending at response time and both ready before `fetch`, with
`prepared=2`, `fetched=2`, `worker_ms≈1`, `hit_rate=1.0`.
The synchronous inline-worker sequence has the same 374 tokens but pays
preparation inside the `find` handler; the background handler does not wait.
Multi-turn (discover, known outline, follow-up impact): 578 / 5 versus 684 / 5
for the stateless graph sequence, with the same two prepared/fetched and
`hit_rate=1.0`. See
`docs/results/graph_engine_prototype_2026-11-25.md`.

## Open work

- Process/daemon worker or executor with real parallelism beyond the GIL.
- Hit-rate and waste evaluation on larger repositories; tune the two-handle,
  300 ms and 400-token defaults against measured fetches and think times.
- Optional Ozma transport (the protocol values are JSON-ready and
  transport-neutral).
- A persistent, versioned session/cache layer if the process-local bound
  proves too short for long agent sessions.
- Stronger item identity than a 16-hex hash prefix if cross-process caching is
  added.
