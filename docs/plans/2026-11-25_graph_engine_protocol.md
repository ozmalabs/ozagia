# Graph engine protocol (v2 first slice)

Status: implemented 2026-11-25 in `src/ozagia/core/graph_engine.py`, with the
MCP surface in `src/ozagia/integrations/mcp/server.py`, the optional loopback
HTTP transport in `src/ozagia/engine_http.py`, and tests in
`tests/test_graph_engine.py` and `tests/test_engine_http.py`.

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
  does not run tests, edit files, refresh the snapshot, start a persistent
  daemon or import a target module.
- **Process isolation is fork-only, not a daemon service.** When fork is
  available, one supervised child process per engine computes speculation
  payloads and the parent applies validated messages. The child inherits the
  graph snapshot at fork, has no parent-session access and applies per-child
  `RLIMIT_AS`/`RLIMIT_CPU` when the platform supports it. A daemon thread is the
  fallback for unavailable/failed fork and cannot preempt. A hard per-job
  watchdog preempts a long call at process granularity by killing and replacing
  the child.
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
- Optional HTTP transport: `ozagia engine serve` (see below)
- The tool's input schema has `additionalProperties: false`.
- JSON-RPC framing, errors and `tools/list` are the existing MCP server's.
  A bad tool argument is a JSON-RPC `-32602`; a bad engine request is a normal
  JSON result with an `e` message.

The optional HTTP transport (`src/ozagia/engine_http.py`,
`ozagia.graph_engine.http/1`) carries the same engine objects, not a different
protocol:

| Endpoint | Method | Body / result |
| --- | --- | --- |
| `/v1/graph` | `POST` | engine request object / engine JSON |
| `/v1/ozma/context` | `POST` | `{task,budget_tokens,max_items,allow_stale}` / versioned `task_context` result |
| `/healthz` | `GET` | no body / liveness only, no graph content |

It binds `127.0.0.1` by default, requires a bearer token from
`OZAGIA_ENGINE_TOKEN` or `<state>/engine/token` (constant-time compare, no
serving without one), caps bodies at 64 KiB and concurrent calls at four, and
refuses a non-loopback bind without `--allow-remote` (HTTP has no TLS). Stable
JSON errors cover `400`, `401`, `404`, `405`, `503` and `500`; there is no CORS,
OPTIONS preflight, static route or body echo. SIGINT/SIGTERM stop the server and
close the engine. The HTTP endpoint is a local transport for the engine and the
existing Ozma `task_context` provider; it is not full Ozma protocol
conformance.

v2 is a wire-incompatible simplification of v1: success responses no longer
carry `ok`/`op`/`fresh`/`budget`, top-level metadata uses short keys, columns are
included on every compact tabular response by default (`cols:false` or an
explicit `compact:true` restores per-session reuse), and the per-response
speculation ledger moved behind `stats: true`.

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
| `c` | columns; included on every compact tabular response by default, omitted on repeats only with `cols:false` / explicit `compact:true` |
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

All row operations accept `cols`. By default `c` is present on every compact
tabular response. `cols:false` opts into the old per-session column reuse (first
response teaches the columns, later reuse requests with the same columns omit
them); an explicit `compact:true` is an alias for that column-reuse mode.
`cols:true` forces the columns back.

## Compact rows

Rows are arrays in the order of the op's fixed columns. Columns are stable per
op. Every compact tabular response includes `c` unless the caller opts into
reuse with `cols:false` or an explicit `compact:true`; the first reuse response
includes `c`, and later reuse requests with the same columns omit it.

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
- the fixed columns seen for each op (used by the explicit column-reuse mode);
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

4. The computation and application are separate. A worker prepares a
   `PreparedSpec` containing `(handle, payload)` results. The parent then
   validates the job again and applies the results to the session. The worker
   captures the job id, session id, snapshot token, freshness, scope,
   generation, budget, deadline and entry expiry.
5. The preferred worker is a **supervised forked child process** per engine. The
   child inherits the graph snapshot, builds its own speculation-disabled engine,
   applies per-child `RLIMIT_AS`/`RLIMIT_CPU` where supported, and computes
   payloads; it never touches parent sessions. A small parent listener thread
   receives IPC messages and applies validated results. A supervisor thread
   polls liveness and an idle heartbeat, restarts an unexpectedly dead child
   with bounded exponential backoff, and enforces a hard per-job watchdog: a
   candidate that overruns the worker budget plus grace kills the child,
   cancels that child's outstanding handles and starts a fresh child for later
   work. A bounded pipe/queue, 300 ms worker budget and `time.sleep(0)` yields
   keep it from spinning. Child-epoch tagging plus revalidation means a late
   result from a replaced child is ignored rather than applied twice.
6. If fork is unavailable, the child cannot be restarted, or restarts are
   exhausted, the engine uses the thread worker fallback; outstanding child jobs
   are cancelled cleanly. Thread mode can check its deadline between candidates
   but cannot preempt one running call. Before and after each candidate the
   parent checks that the session still exists, is not
   closed, and has the same snapshot, freshness, scope and generation. A
   changed/evicted/closed session, an expired entry or an exhausted worker
   budget cancels the remaining handles.
7. The full ledger is **not** sent by default. `stats: true` adds it:

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

### Model-facing interface follow-up (2026-11-25)

The live A/B exposed three model-facing problems that the wire protocol alone did
not fix: `fetch` was advertised with no handles, ready payloads still required a
round-trip, and `shape` module names were ambiguous. The protocol now adds:

- **Compact `no_handles`.** `fetch` with no handles returns
  `{"e": "no_handles", "why": "fetch only q handles returned by find; do not
  retry", "s": ..., "t": ...}` rather than a generic error. A driver may also
  omit `fetch` from the model-facing schema until the session holds handles.
- **Inline ready payloads.** When `spec` is requested and a prepared payload is
  ready at response time, it is returned under `q.i`, removed from `q.h` and
  counted as fetched/hit. It is only inlined while the response fits the budget;
  `inline: false` keeps fetch-only delivery.
- **Shape module paths.** `shape` includes a flat `mods` list of
  repository-relative module file paths alongside the compact rows. The live
  task prompt now asks for those paths explicitly; the acceptance check was not
  relaxed.

The follow-up result is
[`../results/graph_engine_live_2026-11-25_optimized.md`](../results/graph_engine_live_2026-11-25_optimized.md):
the optimized arm accepted 6/6 at 18,118 tokens versus 17,762 for the control
and 35,752 for the previous engine interface, with 20 tool calls in both the
optimized and control arms. On a pinned `pallets/flask` checkout the same three
arms accepted 6/6, but the optimized arm used 122,405 tokens versus 114,899 for
the control (+6.5%, same 34 tool calls) and 108,750 for the previous interface
(−5.4% vs control); the tiny-fixture gap did not generalize. See
[`../results/graph_engine_live_broader_2026-11-25.md`](../results/graph_engine_live_broader_2026-11-25.md).

### Worker limits and lifecycle

- One worker per engine: a supervised forked child when available, otherwise one
  daemon thread. Bounded job queue (8) and one job at a time.
- At most two candidates per job; up to six suggestions are queued in the job.
- `OZAGIA_ENGINE_SPEC_PROCESS=auto` (default) uses fork when available;
  `0` forces the thread worker; `1` requests process mode but still falls back
  if fork is unavailable, the child cannot be restarted, or restarts are
  exhausted.
- Total worker deadline default 300 ms (`OZAGIA_ENGINE_SPEC_MS`, clamped
  50..2000 ms), checked between candidates.
- Hard per-job watchdog default `worker budget + 250 ms`
  (`OZAGIA_ENGINE_SPEC_GRACE_MS`, clamped 0..5000 ms). If one graph call
  overruns, the supervisor kills the child, cancels that child's outstanding
  handles and restarts for later work. This is preemption at process
  granularity; the parent is never blocked.
- Per-child resource limits, applied inside the child where supported:
  address space 2 GiB (`OZAGIA_ENGINE_SPEC_MEM_MB`, 0 disables) and cumulative
  CPU 300 s (`OZAGIA_ENGINE_SPEC_CPU_S`, 0 disables). These are per child, not
  per job: `RLIMIT_CPU` accumulates for the child's lifetime, and replacing the
  child resets it. Windows has no `resource` module; the report says
  `unsupported` and the job still runs.
- Supervisor restart policy: bounded exponential backoff from 50 ms, doubling
  to at most 2 s (`OZAGIA_ENGINE_SPEC_BACKOFF_MS` sets the base), with at most
  three consecutive restarts (`OZAGIA_ENGINE_SPEC_RESTARTS`, 0 disables
  restart). The streak resets after a successful job. Exhaustion moves later
  jobs to the thread fallback.
- The child sends an idle heartbeat every 500 ms; a child with no current job
  and no heartbeat for 3 s is treated as wedged and replaced.
- Entry expiry default 30 s.
- `OZAGIA_ENGINE_SPEC=0` disables speculation entirely (no worker, no handles).
- `bye` cancels one session; engine shutdown stops the supervisor, cancels
  outstanding jobs, sends a sentinel, joins/terminates the child, joins the IPC
  listener and clears the queue. No processes, listener or supervisor threads
  should remain.
- The parent listener only does IPC and application, not graph computation. The
  fork path is closer to true parallelism than the GIL-bound thread fallback,
  but the graph snapshot is a fork-time copy and thread mode cannot preempt a
  running call.

`stats: true` also adds a bounded worker summary under `st.w`, for example:

```json
{"mode": "process", "isolation": "process", "preemptible": true,
 "restarts": 1, "crashes": 1, "kills": 1, "timeouts": 1,
 "queue": 0, "job": "s1:3", "deadline_ms": 420.0,
 "last_error": "hard job deadline exceeded; child replaced",
 "limits": {"preemptible": true, "per_child": true,
            "hard_timeout_ms": 550.0,
            "applied": {"address_space": 2147483648, "cpu": 300}}}
```

It deliberately carries no rows, keys, paths or graph content. In thread mode
the same summary reports `"preemptible": false`, `"hard_timeout_ms": null` and
`"isolation": "thread"`. `GraphEngine.stats()` returns the same worker detail
under `"worker"`.

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
  starts a persistent daemon service or contacts the network. The supervised
  speculation child is an internal, read-only helper.
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
| background workers per engine | 1 supervised fork child or 1 fallback daemon thread |
| parent IPC listener | 1 daemon thread |
| supervisor | 1 daemon thread (process mode) |
| worker queue | 8 jobs |
| worker total budget | 300 ms default (`OZAGIA_ENGINE_SPEC_MS`) |
| hard per-job watchdog | worker budget + 250 ms default (`OZAGIA_ENGINE_SPEC_GRACE_MS`) |
| consecutive restarts | 3 default (`OZAGIA_ENGINE_SPEC_RESTARTS`) |
| restart backoff | 50 ms base, doubling, cap 2 s |
| child address space | 2 GiB default (`OZAGIA_ENGINE_SPEC_MEM_MB`) |
| child cumulative CPU | 300 s default (`OZAGIA_ENGINE_SPEC_CPU_S`) |
| child heartbeat | 500 ms idle; 3 s stale threshold |
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

Larger-repository measurement
(`scripts/measure_graph_engine_corpus.py`, 150 ms simulated think, 1,200-token
budget):

| Repo | Engine tokens / RT | Graph tokens / RT | Ready before fetch | Hit rate | Worker ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| fd | 1,432 / 5 | 3,952 / 5 | 2 / 2 | 1.0 | 49.5 |
| flask | 2,979 / 5 | 10,311 / 5 | 2 / 2 | 1.0 | 24.3 |
| httpx | 3,214 / 5 | 12,185 / 5 | 2 / 2 | 1.0 | 24.6 |
| ozagia | 2,672 / 5 | 12,055 / 5 | 0 / 2 | 0.0 (2 misses) | 1,171.0 |

The `ozagia` run missed the 150 ms fetch window because one candidate took
about 1.2 s; both entries were ready after the follow-up (`ready_after=2`). With
a 1,500 ms simulated think, the same run hit (`3,131 / 5`, `hit_rate=1.0`,
`worker_ms=1,116.4`). This is the non-preemptible-call limit of that
measurement: the worker checked its 300 ms deadline between candidates, not
inside one call. The supervised worker now has a hard per-job watchdog that
would kill and replace the child at the worker budget plus grace instead of
letting it run to completion; that path is covered by tests but not by this
corpus measurement. A thread-fallback `fd` run produced the same 1,432 tokens
and hit rate. Details: `docs/results/graph_engine_speculation_2026-11-25.md`.

## Live model session

A local OpenAI-compatible endpoint check on `127.0.0.1:8000` returned
`ConnectionRefusedError`; no model session was started and no hosted fallback
was used. A live session remains blocked behind `--live`,
`OZAGIA_EVAL_LIVE=1` and explicit human approval.

## Open work

- A long-lived daemon worker shared across engines, rather than one supervised
  fork child per engine; the current child is fork-only.
- Larger and more varied hit-rate/waste evaluation; tune the two-handle,
  300 ms, 550 ms hard timeout and 400-token defaults against measured fetches
  and think times.
- Split expensive operations or cancel inside a single running graph call; the
  current watchdog preempts at process granularity, and thread mode cannot
  preempt at all.
- Optional Ozma transport now exists as the bounded local HTTP transport above;
  full Ozma protocol conformance, an Ozma shim/server and a network-safe remote
  deployment remain open.
- A persistent, versioned session/cache layer if the process-local bound
  proves too short for long agent sessions.
- Stronger item identity than a 16-hex hash prefix if cross-process caching is
  added.
- Broader live-model evidence: more models, larger real repositories,
  counterbalanced arm order, and live inline-delivery coverage. The current
  result is one local 27B model on a tiny fixture and a pinned `flask` checkout;
  it still has no second model, second real repository or live inline delivery.
- The optimized model-facing interface is not yet exercised in the TUI/Desktop
  or other MCP hosts; only the live driver and unit tests cover it.
