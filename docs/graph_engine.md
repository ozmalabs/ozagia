# The `graph` engine surface

The `engine` MCP surface is a compact, stateful, read-only graph session for
LLM clients. It exposes **one** tool, `graph`, instead of the `dev_*` family or
the eight separate graph verbs. The same engine can also be reached through an
optional bounded, loopback HTTP/JSON transport for an Ozma context assembler or
another local client (see below).

```bash
OZAGIA_SURFACE=engine python -m ozagia.integrations.mcp.server
python -m ozagia.integrations.mcp.server --surface engine   # overrides env
```

The full and graph surfaces are unchanged. The engine surface lists only
`graph`; `dev_*` calls are unknown tools (JSON-RPC `-32602`).

Design and wire details:
[`docs/plans/2026-11-25_graph_engine_protocol.md`](plans/2026-11-25_graph_engine_protocol.md).
Implementation: `src/ozagia/core/graph_engine.py`.

## HTTP transport (optional)

`src/ozagia/engine_http.py` puts one `GraphEngine` and its loaded `DevMemory`
behind a bounded local HTTP/JSON server. It is a transport, not a second graph
implementation: freshness withholding, session bounds, token budgets and
read-only behaviour are exactly the engine's.

Create a token once (mode `0600`; only the path and a fingerprint are printed):

```bash
ozagia engine token --root /path/to/repo
# token file: /path/to/repo/.ozagia/engine/token
# fingerprint: sha256:0123456789abcdef
```

Serve the existing snapshot (the command never builds or refreshes it):

```bash
ozagia engine serve --root /path/to/repo --port 8765
```

The default bind is `127.0.0.1:8765`. A non-loopback bind is refused unless
`--allow-remote` is passed; HTTP has no TLS and the printed warning makes the
caller responsible for the network/auth perimeter. No network exposure happens
by default.

The served engine defaults to **auto-known** (see
[`known` deltas](#known-deltas)): a caller that omits `known` on a repeated
`find`, `outline` or `context` gets `u` key handles for rows that session
already saw. Send `known: []` for a full refresh, or start the server with
`--no-auto-known` to restore the core "omitted means full rows" behaviour.

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/v1/graph` | `POST` | one engine request object; the response is the engine JSON, including `hello` and `bye` |
| `/v1/ozma/context` | `POST` | `{task, budget_tokens, max_items, allow_stale}` mapped to `task_context`; the provider's versioned result |
| `/healthz` | `GET` | liveness only, no graph content and no token |

Every `/v1` request needs a bearer token. The token is read from
`OZAGIA_ENGINE_TOKEN` (wins) or `<state>/engine/token` and compared with
`hmac.compare_digest`. A missing, empty, malformed or group/other-readable
token file refuses to serve. The token is never printed, logged or echoed.

```bash
export OZAGIA_ENGINE_TOKEN="$(cat /path/to/repo/.ozagia/engine/token)"
curl -sS http://127.0.0.1:8765/v1/graph \
  -H "Authorization: Bearer $OZAGIA_ENGINE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"op": "hello"}'
# {"p":"ozagia.graph_engine/2","v":"2.0","sid":"s1",...}
```

```bash
curl -sS http://127.0.0.1:8765/v1/graph \
  -H "Authorization: Bearer $OZAGIA_ENGINE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"op": "find", "session": "s1", "q": "retry backoff", "budget": 800}'

curl -sS http://127.0.0.1:8765/v1/ozma/context \
  -H "Authorization: Bearer $OZAGIA_ENGINE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task": "find the retry loop", "budget_tokens": 1500,
       "max_items": 8, "allow_stale": false}'
```

HTTP errors are stable JSON (`{"ok": false, "protocol": ..., "error":
{"code": ..., "message": ...}}`) and never include rows, the request body or
credential material: `400` malformed/oversized/empty body or invalid context
request, `401` missing/wrong bearer token, `404` unknown path, `405` wrong
method, `503` saturation (with `Retry-After`), `500` contained internal error.
There is no CORS, OPTIONS preflight, directory/static route or body echo.

Limits and lifecycle:

- request body: 64 KiB; `Transfer-Encoding`/chunked bodies are rejected;
- concurrent engine/context calls: 4 by default; excess requests get `503`;
- request socket timeout: 15 s; the server is threaded but engine calls are
  bounded by the protocol budget and speculation worker;
- `--port 0` binds an ephemeral port and prints the chosen port;
- SIGINT/SIGTERM stop `serve_forever`, close the server and close the engine
  (sessions and the speculation worker are reaped);
- no target import, execution, network, model call or write outside the state
  token file.

## Python client (Ozma context assembler)

`ozagia.engine_client.GraphEngineClient` is a stdlib-only client for this
transport. It owns one session and remembers every `{op, key, hash}` it has
shown the caller, so a repeated `find`, `outline` or `context` sends the
matching `known` delta automatically. That explicit delta still works against
an auto-known server (an explicit list disables the server-side fallback). It
never retries a request, never contacts anything except the configured base
URL, and never puts the token in `repr`, counters, exceptions or logs.

```python
from ozagia.engine_client import GraphEngineClient

with GraphEngineClient("http://127.0.0.1:8765",
                       token_file=".ozagia/engine/token",
                       budget=1200) as graph:
    hello = graph.hello()                       # session sid, snapshot, freshness
    hits = graph.find("retry backoff")          # rows carry their h hash
    key = hits["r"][0][hits["c"].index("k")]

    # The assembler already showed these rows. A refresh is a key-only delta.
    refresh = graph.find("retry backoff")       # rows in u, not repeated
    outline = graph.outline(key)
    outline = graph.outline(key)                # again: u contains key

    # Paging is explicit and bounded by the caller.
    page = graph.find("retry backoff", limit=200, budget=600)
    while graph.next_request(page) is not None:
        page = graph.next_page(page, budget=600)

    # Speculation and fetch stay opt-in; do not enable them by default.
    # spec = graph.find("retry backoff", spec=True, inline=False)
    # graph.fetch((spec.get("q") or {}).get("h", [])[:2], wait_ms=50)

    print(graph.stats())                        # requests, bytes, engine tokens
```

The token is resolved from the `token` argument, `OZAGIA_ENGINE_TOKEN`, an
explicit `token_file`, or `<state>/engine/token` (normally
`<root>/.ozagia/engine/token`). A token file that is a symlink or readable by
group/other is refused. Use `root=<repo>` instead of `token_file` to use the
same default path as `ozagia engine serve`.

Useful controls:

- `known=False` on one call sends an explicit empty list (the full-refresh
  opt-out, even against an auto-known server); an explicit iterable sends
  exactly those entries. `clear_known()` and `forget(key, op=...)` drop
  remembered entries. Hashes are keyed by `(op, key)`, so a `find` hash and an
  `outline` hash for the same key coexist; a stale or forgotten hash is
  reserialized with a fresh `h`.
- `next_request(result)` returns the engine's self-contained continuation, and
  `next_page(result)` follows it once. The caller owns the loop bound.
- `EngineBudgetError` carries the compact payload plus `next` and
  `suggested_budget`; `EngineWithheldError` carries `freshness` and `snapshot`;
  `EngineAuthError`, `EngineHTTPStatusError` (including
  `EngineUnavailableError`), `EngineTransportError` and `EngineSessionError`
  are the other typed failures. `raise_on_error=False` on any operation
  returns the engine's `e` payload instead.
- `hello(caps=True)` (or `detail=True`) is the only way to request the full
  handshake; `spec` defaults to off and `fetch` is only called explicitly.

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
counts, current budget, the speculation ledger and `ak` (auto-known on/off).
The default handshake omits them.

Sessions are isolated and bounded to 16 per server process (LRU); an evicted
handle must call `hello` again.

## Operations

| Op | Purpose |
| --- | --- |
| `hello` | handshake and session |
| `find` | ranked key/name/file/span hits; rows carry `h` for `known` |
| `outline` | one key's outline, plus a file's defined symbols |
| `expand` | graph walk along edges, including callers/callees |
| `impact` | transitive callers, dependent files and covering tests |
| `tests` | tests covering a key (discovery only; never runs pytest) |
| `context` | cited task context from the existing context pack |
| `shape` | compact repository identity, counts, languages, module file paths (`mods`), commands |
| `fetch` | a prepared speculative payload, pending result, cancellation, or compact `no_handles` |
| `bye` | close the session, cancel pending work and clear caches |

Example:

```json
{"op": "find", "session": "s1", "q": "retry backoff", "budget": 800}
```

Rows are compact arrays in fixed columns. Every compact tabular response
carries `c` by default when the budget allows it. A client that tracks the
columns itself can send `cols: false` (or an explicit `compact: true`) to opt
into per-session reuse: the first response teaches `c`, and later requests that
also ask for reuse with the same columns omit it. `cols: true` forces `c` even
in reuse mode.
`r` is rows, `c` columns, `k` key, `h` hash, `f` file, `sp` source span,
`sig` signature, `sum` summary, `why` reason, `txt` text, `u` unchanged keys,
`x` truncated, `o` omitted, `n` next request, `q` speculative handles, `q.i`
speculative payloads already delivered inline, and `mods` the shape operation's
flat list of repository-relative module file paths. `find` rows end with the
`h` hash column, so a repeated find can be sent back as a `known` delta;
`outline` and `context` rows also carry `h`. `verbose: true` expands rows to
named objects for debugging.

## Budgets and paging

Every result reports `t`, an estimated response size. `budget` is a hard
estimated-token limit (default 1,200, maximum 8,000, minimum 64). When the
result does not fit:

- long text is shortened first, then rows are cut;
- `x` and `o` are set;
- `n` (when useful) is a complete follow-up request with the next `offset`;
- when a trimmed page also has `u` handles, `n.offset` counts the rows and
  handles actually returned, and the fitter gives up one page item if needed to
  keep `n`; a page never loses its last returned row to metadata.

If not even one row fits, the result is an explicit
`{"e": "budget_too_small", "why": "...", "x": true}` (with a larger-budget
retry in `n` when it fits), not an empty truncated object.

```json
{"op": "find", "session": "s1", "q": "pkg", "budget": 100}
```

## `known` deltas

The engine surface and the HTTP serving surface are **auto-known** by default:
a repeated `find`, `outline` or `context` in the same session comes back with
only `u` key handles for items already serialized to that session, so a model
never has to store or send hashes. The rows are already in the conversation;
reuse them. The first response for an op has nothing to compare, so it is
always full.

To force a full refresh, send `known: []`. Any explicitly supplied `known`
(including the empty list) disables auto-known for that request and is used
exactly as described below. Direct/core callers (`GraphEngine(...)`) default
`auto_known=False`; the MCP engine surface and `ozagia engine serve` turn it
on (`--no-auto-known` disables it there).

If you already have a `find`, `outline` or `context` row, you can also send its
`{op, key, hash}` explicitly:

```json
{"op": "find", "session": "s1", "q": "add",
 "known": [{"op": "find", "key": "sym:pkg/core.py::add",
            "hash": "1a2b3c4d5e6f7a8b"}]}
```

A matching item comes back only as a key handle:

```json
{"u": ["sym:pkg/core.py::add"], "t": 10}
```

The engine remembers hashes per `(op, key)`, so a `find` hash and an `outline`
or `context` hash for the same key coexist. An entry with an `op` matches only
that operation's row shape; an entry for another op is ignored. Store
`{op, key, hash}`, where `op` is the request op and `hash` is the row's `h`.
Each `u` handle belongs to the operation that produced the response.

Legacy `{key, hash}` entries (no `op`) still work with key-only behaviour: the
supplied hash is compared with the requested operation's current row hash.
Since the hash covers the op-specific serialization, the last find hash still
matches a repeated find after an outline of the same key, while an outline hash
never satisfies a find row (and vice versa). A client that keeps only one hash
per key may lose one op's hash when it later sees another; that is a client
storage limit, not a server collision.

Changed or missing hashes are reserialized with a new `h`. A repeated find is
the largest routine saving: on the pinned flask checkout a 20-row
`find get_root_path` is 1,189 estimated tokens and 4,572 bytes, while the
matching `known` delta is 300 tokens and 1,177 bytes. The legacy client request
grew from 78 to 1,887 bytes for the 20 hashes; the op tag adds 12 bytes per
known row (240 bytes for 20), still leaving a large net response saving.
Paging is unaffected: when a page is trimmed, `n.offset` counts both returned
rows and `u` handles, so the cut rows are not skipped.

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

**Recommendation: do not enable speculation by default.** In the live
multi-turn workloads the model did not fetch prepared payloads, and the extra
`q` handles made spec `find` responses more expensive. Keep `spec` opt-in
(`spec: true`) until a workload demonstrates that prepared work is consumed.

When a payload is already ready at response time, the engine delivers it under
`q.i` and consumes the handle instead of making the model issue a separate
`fetch`. Inline delivery happens only while the rendered response stays within
the requested budget and only when `spec` was requested. A delivered payload is
removed from `q.h` and counted as fetched/hit, so it cannot be fetched twice.
Pass `inline: false` to keep fetch-only delivery (the live legacy arm does this).

A model-facing driver can also omit the `fetch` operation from its tool schema
while the session has no speculative handles. The engine still accepts `fetch`
for direct clients, but returns a compact `no_handles` response when there is
nothing to fetch.

When fork is available, the worker is a **supervised forked child process** per
engine: the child computes `(handle, payload)` results against its inherited
graph snapshot and a small parent listener thread applies them after
revalidating the session snapshot/scope/generation/expiry. The supervisor
tracks liveness and an idle heartbeat, restarts an unexpectedly dead child with
bounded exponential backoff, and enforces a hard per-job watchdog. If a single
graph call overruns the worker deadline plus grace, the child is killed, that
child's outstanding handles are cancelled, and a fresh child serves later
work. A late result from a replaced child is discarded by its epoch. If fork is
unavailable, the child cannot be restarted, or restarts are exhausted, the
engine falls back to the in-process daemon thread. `bye` cancels that session's
work; engine shutdown stops the supervisor, child and listener. Thread mode can
check its deadline between candidates but **cannot preempt a running call**.

The child applies per-child resource limits where the platform supports them:
address space 2 GiB (`OZAGIA_ENGINE_SPEC_MEM_MB`, 0 disables) and cumulative CPU
300 s (`OZAGIA_ENGINE_SPEC_CPU_S`, 0 disables). These are per child, not per
job, and are reported as `applied`/`unsupported`/`errors` in worker stats.

Fetch one or more:

```json
{"op": "fetch", "session": "s1", "fetch": ["q1"], "wait_ms": 50}
```

- ready: returns the prepared payload without graph work;
- still pending: waits up to `wait_ms` (0–250) and then returns
  `{"h": "q1", "fs": "pending"}`;
- cancelled/expired: returns `{"h": "q1", "fs": "cancelled"}`.

With no handles, the compact reply is:

```json
{"e": "no_handles", "why": "fetch only q handles returned by find; do not retry",
 "s": "3f2a…", "t": 29}
```

A handle that is unknown (including one already consumed by inline delivery) is
refused with the existing `unknown or expired spec handle` error.

The full accounting is opt-in with `stats: true` on any op (including `hello`
and `fetch`):

```json
{"st": {"prepared": 2, "fetched": 2, "wasted": 0, "evicted": 0,
        "cancelled": 0, "worker_ms": 1.0, "hits": 2, "misses": 0,
        "hit_rate": 1.0,
        "w": {"mode": "process", "preemptible": true, "restarts": 0,
              "crashes": 0, "kills": 0, "timeouts": 0, "queue": 0,
              "job": null, "deadline_ms": null, "last_error": null,
              "limits": {"per_child": true, "hard_timeout_ms": 550.0,
                         "applied": {"address_space": 2147483648,
                                     "cpu": 300}}}}}
```

`st.w` is a bounded supervisor summary with no rows, keys, paths or graph
content. In thread fallback it reports `"preemptible": false`,
`"hard_timeout_ms": null` and `"isolation": "thread"`. `worker_mode` is
`process` while a child serves, `restarting` while a replacement is pending,
and `thread` after the fallback is permanent.

The worker captures the session's snapshot, scope, generation, budget and
deadline. It discards results if the snapshot/scope/generation changes, the
session is evicted or closed, the entry expires, or the result comes from a
replaced child. `OZAGIA_ENGINE_SPEC=0` disables speculation;
`OZAGIA_ENGINE_SPEC_PROCESS=0` forces the thread worker;
`OZAGIA_ENGINE_SPEC_MS` bounds the worker time (default 300 ms);
`OZAGIA_ENGINE_SPEC_GRACE_MS` sets the watchdog grace (default 250 ms);
`OZAGIA_ENGINE_SPEC_RESTARTS` and `OZAGIA_ENGINE_SPEC_BACKOFF_MS` bound
restarts; `OZAGIA_ENGINE_SPEC_MEM_MB` and `OZAGIA_ENGINE_SPEC_CPU_S` bound
per-child resources. The hard watchdog now preempts one long graph call at
process granularity instead of letting it overrun indefinitely; thread mode
still cannot preempt.

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
- The HTTP transport is one process and one snapshot. Sessions are shared by
  all clients of that server, so client isolation is the caller's problem.
  Requests are unencrypted HTTP; `--allow-remote` does not add TLS.
- HTTP bodies and concurrency are bounded, but an authenticated client can
  still occupy all worker slots until the request timeout or a protocol budget
  ends the call.
- Speculation is one supervised fork child (plus listener/supervisor threads)
  or one fallback daemon thread. The child applies per-child address-space and
  cumulative-CPU limits, but these are not per-job quotas and fork still
  inherits a fork-time graph snapshot. Preemption is at process granularity:
  the watchdog kills and replaces the child, so a legitimate very long call is
  cancelled rather than resumed. The thread fallback cannot preempt at all.
- Restarts are bounded (three consecutive by default); after that later
  speculation uses the non-preemptive thread fallback for that engine.
- Restart forks from a process that still has threads (the supervisor, and
  possibly the old listener). The child only builds a read-only engine, but
  Python may warn about fork in a multi-threaded process; a long-lived
  daemon/forkserver would avoid this.
- Hit-rate/waste is measured on a few local corpus checkouts with simulated
  think time, not real model latency or a broad repository sample.
- The live-model comparison is one local 27B model on two repository scales: a
  tiny fixture and a pinned `pallets/flask` checkout. On the tiny fixture the
  optimized model-facing interface (conditional `fetch`, inline ready payloads,
  a smaller schema, explicit shape file paths) accepted 6/6 at 18,118 tokens
  versus 17,762 for the stateless control and 35,752 for the previous engine
  interface; it narrowed, but did not beat, the control on tokens. On flask the
  optimized arm also accepted 6/6 but used 122,405 tokens versus 114,899 for the
  control (+6.5%, same 34 tool calls) and 108,750 for the previous interface
  (−5.4% vs control); the tiny-fixture +2.0% gap did not generalize. See
  [`docs/results/graph_engine_live_2026-11-25_optimized.md`](results/graph_engine_live_2026-11-25_optimized.md)
  and
  [`docs/results/graph_engine_live_broader_2026-11-25.md`](results/graph_engine_live_broader_2026-11-25.md).
  A three-turn session suite on the same flask checkout and model reversed the
  sign: a stateful `known` arm used 81,058 tokens versus 86,589 for the
  stateless control (−6.4%) with 18/18 turn acceptance and 22 tool calls. The
  win came from refreshing an already-seen 18-row file outline (714 → 219
  engine tokens) and persisted into the next turn; single-symbol outlines saved
  only 1.5–2.1%, and speculation was net cost because nothing was fetched.
  `find` now participates too: a repeated 20-row find used 28,088 tokens
  versus 35,900 for the control (−21.8%) with 6/6 turn acceptance on flask, and
  12,612 versus 14,468 (−12.8%) on the tiny fixture. See
  [`docs/results/graph_engine_live_multiturn_2026-11-25.md`](results/graph_engine_live_multiturn_2026-11-25.md).
- Small budgets on large tabular results return a shortened row prefix plus
  `n`, or an explicit `budget_too_small` error; they no longer return an empty
  `truncated` result. The `_fit` no-progress loop was fixed after the live run:
  on the pinned flask checkout, the 20-row `find` that returned no rows at
  800/900/1,000 estimated tokens now returns 15/17/19 rows, while 1,100 and
  1,200 still return 20. See
  [`docs/results/graph_engine_live_broader_2026-11-25.md`](results/graph_engine_live_broader_2026-11-25.md).
- Supervision and hard-timeout behaviour are covered by tests, not by the
  corpus measurement; that measurement predates them.
- A compact 16-hex content hash for `known`, not a cryptographic identity.
  Hashes are keyed by `(op, key)`; a legacy client that stores only one hash
  per key can still overwrite its own find hash with an outline hash.
  `GraphEngineClient` keeps its remembered hashes in memory and owns one
  session, so restarting the client or the server loses its delta cache.
- Speculation is opt-in and was a net cost in the measured live workloads; it
  is not recommended as a default.
- One snapshot per session; federation is not part of this surface.
- Prototype wire/readiness measurements, not model-task gains:
  [`docs/results/graph_engine_prototype_2026-11-25.md`](results/graph_engine_prototype_2026-11-25.md)
  and [`docs/results/graph_engine_speculation_2026-11-25.md`](results/graph_engine_speculation_2026-11-25.md).
