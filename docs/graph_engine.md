# The `graph` engine surface

The `engine` MCP surface is a compact, stateful, read-only graph session for
LLM clients. It exposes **one** tool, `graph`, instead of the `dev_*` family or
the eight separate graph verbs.

```bash
OZAGIA_SURFACE=engine python -m ozagia.integrations.mcp.server
python -m ozagia.integrations.mcp.server --surface engine   # overrides env
```

The full and graph surfaces are unchanged. The engine surface lists only
`graph`; `dev_*` calls are unknown tools (JSON-RPC `-32602`).

Design and wire details:
[`docs/plans/2026-11-25_graph_engine_protocol.md`](plans/2026-11-25_graph_engine_protocol.md).
Implementation: `src/ozagia/core/graph_engine.py`.

## Session

Start once:

```json
{"op": "hello"}
```

The default reply is deliberately small:

```json
{"p": "ozagia.graph_engine/2", "v": "2.0", "sid": "s1",
 "s": "3f2a…", "f": "fresh", "b": [1200, 8000], "c": 511, "t": 33}
```

- `p`/`v`: protocol and version.
- `sid`: session handle; pass it on later requests.
- `s`/`f`: snapshot identity and freshness.
- `b`: default and maximum estimated-token budget.
- `c`: capability bit mask (`find=1`, `outline=2`, `expand=4`, `impact=8`,
  `tests=16`, `context=32`, `shape=64`, `fetch=128`, `bye=256`).
- `t`: estimated response tokens.

Ask for the full handshake only when needed:

```json
{"op": "hello", "caps": true}
```

`caps: true` (alias `detail: true`) adds the full op names, features, snapshot
counts, current budget and the speculation ledger. The default handshake omits
them.

Sessions are isolated and bounded to 16 per server process (LRU); an evicted
handle must call `hello` again.

## Operations

| Op | Purpose |
| --- | --- |
| `hello` | handshake and session |
| `find` | ranked key/name/file/span hits |
| `outline` | one key's outline, plus a file's defined symbols |
| `expand` | graph walk along edges, including callers/callees |
| `impact` | transitive callers, dependent files and covering tests |
| `tests` | tests covering a key (discovery only; never runs pytest) |
| `context` | cited task context from the existing context pack |
| `shape` | compact repository identity, counts, languages, modules, commands |
| `fetch` | a prepared speculative payload, pending result or cancellation |
| `bye` | close the session, cancel pending work and clear caches |

Example:

```json
{"op": "find", "session": "s1", "q": "retry backoff", "budget": 800}
```

Rows are compact arrays in fixed columns. Columns are sent on the first
response for an op in a session and then assumed until `cols: true` asks for
them again. `r` is rows, `c` columns, `k` key, `f` file, `sp` source span,
`sig` signature, `sum` summary, `why` reason, `txt` text, `u` unchanged keys,
`x` truncated, `o` omitted, `n` next request and `q` speculative handles.
`verbose: true` expands rows to named objects for debugging.

## Budgets and paging

Every result reports `t`, an estimated response size. `budget` is a hard
estimated-token limit (default 1,200, maximum 8,000, minimum 64). When the
result does not fit:

- long text is shortened first, then rows are cut;
- `x` and `o` are set;
- `n` (when useful) is a complete follow-up request with the next `offset`.

```json
{"op": "find", "session": "s1", "q": "pkg", "budget": 100}
```

## `known` deltas

If you already have an outline or context item, send its `{key, hash}`:

```json
{"op": "outline", "session": "s1", "key": "sym:pkg/core.py::add",
 "known": [{"key": "sym:pkg/core.py::add", "hash": "1a2b3c4d5e6f7a8b"}]}
```

A matching item comes back only as a key handle:

```json
{"u": ["sym:pkg/core.py::add"], "t": 10}
```

Changed or missing hashes are reserialized with a new `h`.

## Speculation and stats

Set `spec: true` to let the engine suggest next work from the current result:

```json
{"op": "find", "session": "s1", "q": "add", "spec": true}
```

It returns compact handles and counts immediately:

```json
{"q": {"h": ["q1", "q2"], "r": 0, "p": 4}, "t": 130}
```

`q.h` are planned handles, `q.r` is how many are already ready at response
time, and `q.p` is the number of further suggestions. A single worker prepares
at most two payloads **after** the response, while the model is thinking. The
handler never waits for it.

When fork is available, the worker is a forked child process per engine: the
child computes `(handle, payload)` results against its inherited graph snapshot
and a small parent listener thread applies them after revalidating the session
snapshot/scope/generation/expiry. If fork is unavailable or the child fails,
the engine falls back to the in-process daemon thread. `bye` cancels that
session's work; engine shutdown joins/terminates the child and listener.

Fetch one or more:

```json
{"op": "fetch", "session": "s1", "fetch": ["q1"], "wait_ms": 50}
```

- ready: returns the prepared payload without graph work;
- still pending: waits up to `wait_ms` (0–250) and then returns
  `{"h": "q1", "fs": "pending"}`;
- cancelled/expired: returns `{"h": "q1", "fs": "cancelled"}`.

The full accounting is opt-in with `stats: true` on any op (including `hello`
and `fetch`):

```json
{"st": {"prepared": 2, "fetched": 2, "wasted": 0, "evicted": 0,
        "cancelled": 0, "worker_ms": 1.0, "hits": 2, "misses": 0,
        "hit_rate": 1.0}}
```

The worker captures the session's snapshot, scope, generation, budget and
deadline. It discards results if the snapshot/scope/generation changes, the
session is evicted or closed, or the entry expires. `OZAGIA_ENGINE_SPEC=0`
disables speculation; `OZAGIA_ENGINE_SPEC_PROCESS=0` forces the thread worker;
`OZAGIA_ENGINE_SPEC_MS` bounds the worker time (default 300 ms). One long graph
call is still not preempted, so on larger repositories a candidate can overrun
the think window; the larger-repository result records this.

## Freshness and safety

The engine refuses structural data when the snapshot is `stale` or `unknown`:

```json
{"e": "snapshot is not verifiably fresh; structural data is withheld",
 "f": "stale", "s": "3f2a…", "t": 28}
```

A successful response omits repeated freshness/snapshot labels; it implies the
snapshot was fresh at the time of the call. The labels are repeated only in
handshake/error responses.

It is read-only: no edit, create, test execution, refresh, shell, target import,
network or model call. Sessions and speculation live in memory and are not
written to the checkout.

## Current limits

- Process-local sessions; a restart loses them.
- Speculation is one fork child or one fallback daemon thread. Fork gives
  isolation from parent sessions, but the child has no separate CPU/memory
  budget and inherits a fork-time graph snapshot. One in-flight graph call can
  overrun the worker deadline because it is not preempted.
- Hit-rate/waste is measured on a few local corpus checkouts with simulated
  think time, not real model latency or a broad repository sample.
- A compact 16-hex content hash for `known`, not a cryptographic identity.
- One snapshot per session; federation is not part of this surface.
- Prototype wire/readiness measurements, not model-task gains:
  [`docs/results/graph_engine_prototype_2026-11-25.md`](results/graph_engine_prototype_2026-11-25.md)
  and [`docs/results/graph_engine_speculation_2026-11-25.md`](results/graph_engine_speculation_2026-11-25.md).
